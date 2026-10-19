"""
Phase 6, workstream 4 — quotes, line items, and what a quote is allowed to become.

WHAT THIS SPECIFIES
-------------------
A deal keeps its single `value`. A quote breaks that value into `QuoteLine` rows,
sums them on the server, and moves through a status whose legal transitions live in
`core/quotes.py` rather than in a view. Three URLs, all keyed on the DEAL's pk:

  quote_detail  /deals/<deal_pk>/quote/          read-only; the total
  quote_edit    /deals/<deal_pk>/quote/edit/     the line editor
  quote_status  /deals/<deal_pk>/quote/status/   POST; move the status

WHY THE ARITHMETIC TESTS ARE SHAPED THE WAY THEY ARE
---------------------------------------------------
Tests 1–4 exist because of one distinction: no quote (`None`), a quote with no
lines (`Decimal("0")`), and a quote with lines (a number) are three different
answers. A total that collapsed any two of them would pass a test that only checked
one, so each is pinned separately. Test 2 pins a total that is NOT the sum of round
numbers (7350.00 from 7200.00 + 150.00) and test 3 pins a FRACTIONAL quantity,
because both are the ways a float or an `int()` sneaks in.

Test 5 pins `unit_price` being required. It is the one field in this workstream
that could have been made convenient with `default=0`, and the convenience is the
bug: a line typed with no price would save as $0.00 and the quote would go out
looking finished.

Test 20 is the money sanity check the whole workstream rests on. 99999999.99 is
the largest value `max_digits=12, decimal_places=2` admits, and it goes through
the FORM, not just the model. If anything on the way coerced the Decimal to a
float, 99999999.99 would come back as 99999999.99000001 or similar, and the total
would be wrong in cents on a big quote — the one nobody spots, because a wrong
number on a large deal looks like a large deal.

ON THE `{{ }}` TEMPLATE ARITHMETIC TRAP
--------------------------------------
These tests assert on what the page RENDERS, not on what a template computes.
Django's `multiply` filter opens with `int(value) * int(arg)`, and
`int(Decimal("0.25"))` is 0 — so `{{ line.quantity|multiply:line.unit_price }}`
prints $0.00 for a quarter hour of labour and never raises. The amounts are
therefore computed in `core.quotes.line_amount` and the tests check the rendered
figures against Python's answer.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_quotes`.
"""

from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.dates import local_today
from core.forms_quotes import QuoteLineFormSet
from core.models import Quote, QuoteLine
from core.quotes import (
    ALLOWED_TRANSITIONS,
    LOCKED_MESSAGE,
    allowed_next_statuses,
    expired,
    line_amount,
    locked,
    quote_total,
    quote_total_for_deal,
    set_status,
    subtotal,
)

from .factories import PASSWORD, make_company, make_deal, make_user


# ------------------------------------------------------------------ helpers


def make_quote(deal, **kw):
    """A saved Quote for a deal. Every field here has a usable default."""
    return Quote.objects.create(deal=deal, **kw)


def make_line(quote, description="Panel", quantity=1, unit_price=100, position=0, **kw):
    """A saved QuoteLine. Quantity and price accept ints; the field coerces."""
    return QuoteLine.objects.create(
        quote=quote,
        description=description,
        quantity=Decimal(str(quantity)),
        unit_price=Decimal(str(unit_price)),
        position=position,
        **kw,
    )


def lines_post(*lines, initial=0):
    """
    A POST body for the line editor's formset.

    TEACHING NOTE — the `<prefix>-*` keys are the formset's MANAGEMENT FORM, and
    they are not optional decoration: Django reads TOTAL_FORMS to know how many
    rows came back and INITIAL_FORMS to know how many of them already existed. A
    test that posts only the field values gets an empty formset that validates
    cleanly and saves nothing — which reads like the app is broken when nothing is.

    The prefix is read from `get_default_prefix()` rather than typed in, because
    `inlineformset_factory` derives it from the reverse accessor (`lines`) and not
    from the form's name. A hand-written "form-" here would produce a POST that
    looks complete, validates, and silently saves zero rows.

    `initial` is the count of lines ALREADY on the quote. `lines` is the rows
    being submitted, so the same helper builds "add a line to a quote that has
    none" (initial=0, one row) and "delete the one line it has" (initial=1, one
    row with delete=True).
    """
    prefix = QuoteLineFormSet.get_default_prefix()
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(lines)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
        "valid_until": "",
        "notes": "",
    }
    for index, line in enumerate(lines):
        data[f"{prefix}-{index}-description"] = line.get("description", "")
        data[f"{prefix}-{index}-quantity"] = line.get("quantity", "1")
        data[f"{prefix}-{index}-unit_price"] = line.get("unit_price", "")
        data[f"{prefix}-{index}-id"] = str(line["id"]) if line.get("id") else ""
        if line.get("delete"):
            data[f"{prefix}-{index}-DELETE"] = "on"
    return data


class _BrowserFormParser(HTMLParser):
    """
    Reads a rendered page and records, per POST `<form>`, what it would submit.

    TEACHING NOTE — stdlib `html.parser` rather than a dependency, and it is worth
    being explicit about what this class is NOT: it is not a DOM, and it does not
    care about styles, ids, labels or focus. It implements the one rule that
    decides a submission — "a named control that is not a submit button goes in the
    body, and its value is its `value` attribute" — plus the four wrinkles below,
    each of which is a rule a real browser already follows:

      - `<template>` content is inert. This page carries `{{ formset.empty_form }}`
        inside one (the blueprint for the "Add line" button), so a parser that did
        not skip it would submit a second set of `lines-__prefix__-*` names.
      - a checkbox or radio is submitted only if it carries `checked`, and its
        value defaults to "on". `lines_post` above hardcodes `"on"` for the DELETE
        box; this reads it off the markup instead.
      - `submit`, `reset` and `button` controls are not submitted.
      - `<button>` is not handled at all. A `<button>` reaches the server only when
        it is the control that was clicked, and neither button on this page has a
        `name`, so there is nothing to lose.

    Every POST form on the page is collected rather than just the first, because
    base.html puts a sign-out form in the topbar. `browser_post` picks between them.
    """

    SKIPPED_INPUT_TYPES = {"submit", "reset", "button", "image"}

    def __init__(self):
        # convert_charrefs=True matters for attribute values: Django escapes `'` as
        # &#x27;, and a description of "Callahan's install" has to come back as the
        # same string the page renders rather than as an escaped one.
        super().__init__(convert_charrefs=True)
        self.post_forms = []
        self._form = None  # the POST form currently being filled, or None
        self._template_depth = 0
        self._select = None
        self._text_kind = None
        self._text_name = None
        self._text_value = None
        self._text_checked = False
        self._text_buf = []

    # ------------------------------------------------------------- start tags

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "template":
            self._template_depth += 1
            return
        if self._template_depth:
            return
        if tag == "form":
            # A GET form is a link with extra steps; nothing in it is ever posted.
            self._form = {} if attrs.get("method", "get").lower() == "post" else None
            if self._form is not None:
                self.post_forms.append(self._form)
        elif tag == "input":
            self._input(attrs)
        elif tag == "textarea":
            self._open_text("textarea", attrs.get("name"), None)
        elif tag == "select":
            name = attrs.get("name")
            self._select = {"name": name, "options": [], "chosen": None} if name else None
        elif tag == "option" and self._select is not None:
            self._open_text("option", None, attrs.get("value"), checked="selected" in attrs)

    def _input(self, attrs):
        name = attrs.get("name")
        if self._form is None or not name:
            return
        kind = attrs.get("type", "text").lower()
        if kind in self.SKIPPED_INPUT_TYPES:
            return
        if kind in ("checkbox", "radio"):
            if "checked" in attrs:
                self._form[name] = attrs.get("value", "on")
            return
        self._form[name] = attrs.get("value", "")

    def _open_text(self, kind, name, value, checked=False):
        self._text_kind = kind
        self._text_name = name
        self._text_value = value
        self._text_checked = checked
        self._text_buf = []

    # ------------------------------------------------------------ character data

    def handle_data(self, data):
        if self._template_depth or self._text_kind is None:
            return
        self._text_buf.append(data)

    # --------------------------------------------------------------- end tags

    def handle_endtag(self, tag):
        if tag == "template":
            self._template_depth = max(0, self._template_depth - 1)
            return
        if self._template_depth:
            return
        if tag == "form":
            self._form = None
        elif tag == "textarea":
            if self._text_kind == "textarea" and self._form is not None and self._text_name:
                self._form[self._text_name] = "".join(self._text_buf)
            self._text_kind = None
        elif tag == "option" and self._select is not None:
            # An <option> with no `value` submits its own text, which is the
            # browser's rule and the reason the fallback is not just "".
            text = "".join(self._text_buf).strip()
            index = len(self._select["options"])
            self._select["options"].append(self._text_value if self._text_value is not None else text)
            if self._text_checked and self._select["chosen"] is None:
                self._select["chosen"] = index
            self._text_kind = None
        elif tag == "select":
            select, self._select = self._select, None
            # Nothing selected means the first option is what the browser sends.
            chosen = 0 if select["chosen"] is None else select["chosen"]
            if select["name"] and select["options"] and self._form is not None:
                self._form[select["name"]] = select["options"][chosen]


def browser_post(response, **overrides):
    """
    The POST body a browser would send for a page it has just rendered.

    TEACHING NOTE — this exists because `lines_post()` sends what a test author
    IMAGINES the page contains and this sends what the page RENDERS, and for this
    editor those are different bodies. `lines_post` defaults an unsubmitted quantity
    to "1", which happens to be right — but it is right by the author's memory of
    `QuoteLine.quantity`, not by anything the template did, so a change to the
    widget would leave the suite green and the bug back. Every assertion in this
    module about the editor therefore had a hole in it the same shape: a POST
    assembled from the model rather than from the markup, and REVIEW-33's
    reproduction was one of them (see the class below).

    `**overrides` are full field names — `"lines-0-description"` — and are applied
    last, so a test types into the page instead of re-describing it. The management
    form is NOT special-cased and never overridden by hand: TOTAL_FORMS is harvested
    off the rendered page, which is what makes "three blank rows really were
    submitted" an assertion rather than an assumption.
    """
    parser = _BrowserFormParser()
    parser.feed(response.content.decode(response.charset))
    if not parser.post_forms:
        raise AssertionError(f"no <form method=post> in {response.request['PATH_INFO']}")
    # The sign-out form in base.html's topbar is also a POST, and it carries a CSRF
    # token and nothing else. The editor is the POST form with controls on it, and
    # the tiebreak is written down here instead of living in a sort key nobody sees.
    data = dict(max(parser.post_forms, key=len))
    data.update(overrides)
    return data


# --------------------------------------------------------- models & arithmetic


class QuoteArithmeticTests(TestCase):
    """Specs 1–4: what a quote costs, and the three answers that must not merge."""

    def setUp(self):
        self.company = make_company()
        self.deal = make_deal(self.company)
        self.quote = make_quote(self.deal)

    def test_an_empty_quote_totals_zero_not_none(self):
        """
        Spec 1. `Decimal("0")` is the answer, and the assertion is on the VALUE,
        not just on "not None" — `Decimal("0") == 0` is True in Python, so a test
        that only checked falsiness would pass for a function returning the int 0,
        and the next multiplication by a Decimal would raise TypeError.
        """
        self.assertEqual(quote_total(self.quote), Decimal("0.00"))
        self.assertIsInstance(quote_total(self.quote), Decimal)

    def test_two_lines_total_correctly(self):
        """
        Spec 2. 3 × 2400.00 + 1.5 × 100.00 = 7350.00.

        The numbers are chosen so the answer is not the sum of round amounts
        (7200.00 + 150.00), which is where a float shows: 0.1 + 0.2 being 0.3 is
        the famous example, and this is the same bug in a field where nobody would
        ever notice it if it were a cent out on a $7,350 quote. Exact Decimal
        arithmetic gets it right.
        """
        make_line(self.quote, "Laptop", quantity=3, unit_price="2400.00")
        make_line(self.quote, "Training", quantity="1.5", unit_price="100.00")
        self.assertEqual(quote_total(self.quote), Decimal("7350.00"))

    def test_fractional_quantity_works(self):
        """
        Spec 3. 0.25 hours at $800.00 is $200.00, and it is a DecimalField with
        two decimal places rather than an integer because services are sold in
        fractions of an hour. `quantity=0.25` through an integer field would have
        rounded to 0 and made the line free.
        """
        line = make_line(self.quote, "Setup labour", quantity="0.25", unit_price="800.00")
        self.assertEqual(line.quantity, Decimal("0.25"))
        self.assertEqual(line_amount(line), Decimal("200.00"))
        self.assertEqual(quote_total(self.quote), Decimal("200.00"))

    def test_no_quote_and_an_empty_quote_are_different_answers(self):
        """
        Spec 4, and the most load-bearing assertion in this file.

        `quote_total_for_deal` returns None for a deal with no quote and
        Decimal("0.00") for a quote that has no lines. Those are not the same
        statement: "nobody has quoted this" and "this was quoted at nothing" are
        different facts about the world, and a client that renders both as $0.00
        is telling a rep that a job nobody has priced is a free job.

        `hasattr(deal, "quote")` is False for a deal with no quote because the
        reverse side of a OneToOneField raises RelatedObjectDoesNotExist, which
        subclasses AttributeError precisely so that question can be asked without
        a try block. Two SEPARATE deals, because that is the only way to put both
        states on screen at once and compare them.
        """
        unquoted = make_deal(self.company, name="Never quoted")

        self.assertFalse(hasattr(unquoted, "quote"))
        self.assertIs(quote_total_for_deal(unquoted), None)

        self.assertTrue(hasattr(self.deal, "quote"))
        self.assertEqual(quote_total_for_deal(self.deal), Decimal("0.00"))

    def test_subtotal_equals_the_total_because_there_is_no_tax(self):
        """
        Spec — the scaffold has no tax rate, so `subtotal` is the sum. It is
        tested rather than assumed, because a tax step lands here later and the
        test that says "these were meant to be the same number" is what stops
        somebody redefining subtotal as "the amount before discount" without
        noticing.
        """
        make_line(self.quote, "Panel", quantity=2, unit_price="1500.00")
        self.assertEqual(subtotal(self.quote), quote_total(self.quote))
        self.assertEqual(subtotal(self.quote), Decimal("3000.00"))


class QuoteModelTests(TestCase):
    """Specs 5 and 6: the schema decisions that are expensive to undo."""

    def setUp(self):
        self.company = make_company()
        self.deal = make_deal(self.company)
        self.quote = make_quote(self.deal)

    def test_unit_price_cannot_be_null(self):
        """
        Spec 5. Two levels, because there are two doors.

        `full_clean()` refuses it, which is what a ModelForm calls, so the person
        filling in the form gets a message next to the input. The DATABASE also
        refuses it, which matters because `save()` on its own skips validation:
        `QuoteLine(...).save()` skips clean(), so without a NOT NULL column a
        shell script or an import could write a priceless line and the total would
        quietly be wrong. `null=False` with no default is what makes both true —
        a `default=0` would satisfy the schema and still be a lie.
        """
        with self.assertRaises(ValidationError):
            QuoteLine(quote=self.quote, description="Panel", quantity=1).full_clean()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QuoteLine.objects.create(
                    quote=self.quote,
                    description="Panel",
                    quantity=Decimal("1"),
                    unit_price=None,
                )

    def test_a_deal_can_have_at_most_one_quote(self):
        """
        Spec — why `deal` is a OneToOneField. A second quote for the same deal is
        a data-entry error, not a second version: versioning needs its own table
        with its own rules, and two rows on this table means there is no way to say
        which revision anybody is looking at.
        """
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Quote.objects.create(deal=self.deal)

    def test_lines_sort_by_position_then_pk(self):
        """
        Spec 6. Created OUT of order on purpose — the test is about the sort, so
        if the rows were created in the expected order a broken ordering would
        still pass.

        The two rows sharing position 0 are the tie case the `-pk`... no, the `pk`
        ASCENDING half of the ordering exists for: a line added through the admin
        gets the model default of 0, and until the editor renumbers everything the
        order is genuinely tied. Without the tiebreak SQLite is free to return
        those two in either order on a different query, and a quote whose lines
        reshuffle on refresh is a quote nobody trusts.
        """
        third = make_line(self.quote, "Third", unit_price="1", position=2)
        first = make_line(self.quote, "First", unit_price="1", position=1)
        tied_low = make_line(self.quote, "Tie, created first", unit_price="1", position=0)
        tied_high = make_line(self.quote, "Tie, created second", unit_price="1", position=0)

        self.assertEqual(QuoteLine._meta.ordering, ["position", "pk"])
        self.assertEqual(
            list(self.quote.lines.all()),
            [tied_low, tied_high, first, third],
            msg="the order is defined once, on the model, and every consumer gets it",
        )


# ---------------------------------------------------------------- statuses


class QuoteStatusTests(TestCase):
    """Specs 7 and 8: the transition table, and the one move that is refused."""

    def setUp(self):
        self.company = make_company()
        self.deal = make_deal(self.company)

    def test_every_allowed_transition_succeeds(self):
        """
        Spec 7, and it iterates the TABLE rather than spelling the moves out again
        in the test. A test that listed the six moves by hand would still pass the
        day somebody deleted one from ALLOWED_TRANSITIONS — the test would be
        checking its own copy. Reading the table means the test fails when a move
        is REMOVED, which is the change somebody needs to notice.

        A fresh deal per move, because a deal has at most one quote and six of them
        on one deal would be a uniqueness error rather than six answers.
        """
        for from_status, to_status in ALLOWED_TRANSITIONS:
            with self.subTest(move=f"{from_status}->{to_status}"):
                deal = make_deal(self.company, name=f"Deal {from_status} {to_status}")
                quote = make_quote(deal, status=from_status)
                self.assertEqual(set_status(quote, to_status).status, to_status)
                quote.refresh_from_db()
                self.assertEqual(quote.status, to_status)

    def test_draft_to_accepted_is_refused(self):
        """
        Spec 8. A quote that was never sent cannot be accepted, because nobody has
        seen the terms. Allowing it produces a deal marked Won against a price the
        customer was never shown, and it happens exactly when somebody is in a
        hurry and the button is right there. The refusal is a ValueError, not a
        False, so a caller who forgets to check gets a loud failure rather than a
        silent no-op.
        """
        quote = make_quote(self.deal, status=Quote.Status.DRAFT)
        with self.assertRaises(ValueError):
            set_status(quote, Quote.Status.ACCEPTED)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.DRAFT)

    def test_an_unknown_status_is_refused_like_a_forbidden_move(self):
        """
        Spec — a browser POSTs whatever it likes. `"banana"` is in no pair in the
        table, so it raises through the same one comparison; there is no separate
        "is this a real status" branch to keep in step with the enum, and a status
        invented next year cannot slip through a check written today.
        """
        quote = make_quote(self.deal, status=Quote.Status.SENT)
        with self.assertRaises(ValueError):
            set_status(quote, "banana")
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)

    def test_the_next_statuses_offered_are_exactly_the_legal_ones(self):
        """
        Spec — a SENT quote can become Accepted, Declined or Draft, and cannot
        become Sent again (recall is a two-step, and "re-send without recalling"
        would skip the state where the terms are editable). The page renders these
        and only these, which is the cheapest possible documentation of the state
        machine: you cannot see that draft -> accepted is forbidden except by
        noticing its absence.

        In `Quote.Status` DECLARATION order, because that is the order a quote
        moves through and the buttons read as a life story rather than as
        alphabetical.
        """
        quote = make_quote(self.deal, status=Quote.Status.SENT)
        self.assertEqual(
            allowed_next_statuses(quote),
            [
                (Quote.Status.DRAFT, "Draft"),
                (Quote.Status.ACCEPTED, "Accepted"),
                (Quote.Status.DECLINED, "Declined"),
            ],
        )

    def test_expired_is_read_against_the_local_date(self):
        """
        Spec — `local_today`, not UTC. In Chicago, UTC's date is already tomorrow
        from 7 pm, so a UTC-based `expired()` would lapse every quote a day early
        every evening and behave perfectly for the other eighteen hours. That is a
        bug nobody catches by looking at it.

        `valid_until == today` is NOT expired: "good until the 30th" means through
        the end of the 30th, and `<=` would quietly take a day off every quote's
        life. Both sides are pinned, including the one-day-later case, so the
        comparison cannot be edited from `<` to `<=` without this going red.
        """
        # Four deals, four quotes: one quote per deal, so a single setUp quote
        # cannot carry four different dates.
        deals = [make_deal(self.company, name=f"Validity {n}") for n in range(4)]
        yesterday, today, tomorrow, never = [
            make_quote(deals[0], valid_until=local_today() - timedelta(days=1)),
            make_quote(deals[1], valid_until=local_today()),
            make_quote(deals[2], valid_until=local_today() + timedelta(days=1)),
            make_quote(deals[3], valid_until=None),
        ]

        self.assertTrue(expired(yesterday))
        self.assertFalse(expired(today))
        self.assertFalse(expired(tomorrow))
        self.assertFalse(expired(never))

    def test_only_an_accepted_quote_is_locked(self):
        """
        Spec — the freeze is on ACCEPTED and nowhere else. Sending is not the
        problem: recall-and-resend (sent -> draft -> sent) is the normal way this
        work gets done, so "read-only after sent" would block the most common
        correction there is. The way OUT of accepted is set_status — declined, then
        draft — which is a visible event in the history rather than a quiet edit.

        Four deals again: one quote each, because the question is about four
        different statuses.
        """
        for index, (status, is_locked) in enumerate(
            (
                (Quote.Status.DRAFT, False),
                (Quote.Status.SENT, False),
                (Quote.Status.ACCEPTED, True),
                (Quote.Status.DECLINED, False),
            )
        ):
            with self.subTest(status=status):
                deal = make_deal(self.company, name=f"Lock {index}")
                self.assertIs(locked(make_quote(deal, status=status)), is_locked)
        self.assertIn("Declined", LOCKED_MESSAGE)


# --------------------------------------------------- ownership and access


class QuoteAccessTests(TestCase):
    """
    Specs 10–14.

    SECURITY NOTE — five tests for one lock, because the ways to get it wrong are
    different from one another and a single test with subTest would report one
    failure and hide which shape broke. The rule under test is `can_edit(user,
    deal)`, asked of the DEAL and not of the quote: a quote is a quote OF a deal,
    and permissions.py has no branch for a Quote at all, so asking it about the
    quote would make every quote Admin-only and look exactly like correct
    behaviour in an Admin-only test run.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.other_rep = make_user("other", "Rep")
        self.viewer = make_user("viewer", "Viewer")
        self.admin = make_user("boss", "Admin")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        make_line(self.quote, "Panel", quantity=2, unit_price="1500.00")

    def log_in(self, user):
        self.client.force_login(user)

    def test_a_rep_can_edit_a_quote_on_a_deal_they_own(self):
        """Spec 10."""
        self.log_in(self.rep)
        response = self.client.get(reverse("quote_edit", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Line items")

    def test_a_rep_gets_403_on_a_deal_they_do_not_own(self):
        """
        Spec 11. The check is in `dispatch`, so it covers the POST as well as the
        GET — and this test POSTs, because a 403 on the form and no 403 on the
        save is the bug this file exists to prevent.
        """
        self.log_in(self.other_rep)
        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post({"description": "Sneaky", "quantity": "1", "unit_price": "1.00"}, initial=1),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.quote.lines.count(), 1)

    def test_a_viewer_gets_403_on_the_editor(self):
        """Spec 12. A Viewer reads everything and edits nothing, quotes included."""
        self.log_in(self.viewer)
        self.assertEqual(self.client.get(reverse("quote_edit", args=[self.deal.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("quote_detail", args=[self.deal.pk])).status_code, 200)

    def test_anonymous_is_redirected_to_login(self):
        """
        Spec 13, and 302 rather than 403: "who are you?" is the honest answer for
        somebody with no session, and it tells them there is something to sign in
        to. The middleware is global, so this needs no decorator to be right.
        """
        for name in ("quote_detail", "quote_edit"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=[self.deal.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])

    def test_status_change_is_post_only(self):
        """
        Spec 14. A GET is a 405 and changes nothing — a status must not move on a
        link click, a prefetch, or a browser reusing a connection.

        `assertNotEqual(405, 404)` and not `assertIn(status_code, {404, 405})`: the
        route exists, so a 404 here would mean the URL is wrong rather than the
        verb being refused, and a test that accepts either cannot tell those apart.
        """
        self.log_in(self.rep)
        url = reverse("quote_status", args=[self.deal.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

        self.assertEqual(self.client.post(url, {"status": Quote.Status.SENT}).status_code, 302)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_deleting_every_line_of_an_accepted_quote_is_refused(self):
        """
        Spec 15. A quote somebody has agreed to cannot be emptied underneath them:
        every line removed means the agreed terms are now "nothing was agreed".

        The rule is `locked()` in core/quotes.py, checked once in the editor's
        dispatch — so the delete-everything POST is refused by the same code that
        refuses any other edit, and there is no second guard that could one day
        disagree with the first.

        Followed, so the refusal is asserted the way a person meets it: a redirect
        to the quote with the reason on it. And asserted on STATE as well, because
        a refusal that re-rendered the form at 200 would still be a refusal, and
        what actually matters is that the lines are still there.
        """
        self.quote.status = Quote.Status.ACCEPTED
        self.quote.save(update_fields=["status"])
        self.log_in(self.rep)
        line = self.quote.lines.get()

        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post(
                {"id": line.pk, "description": line.description, "quantity": "2", "unit_price": "1500.00", "delete": True},
                initial=1,
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, LOCKED_MESSAGE)
        self.assertEqual(self.quote.lines.count(), 1)
        self.assertEqual(quote_total(self.quote), Decimal("3000.00"))


class QuoteStatusChangeViewTests(TestCase):
    """Specs 9 and 15 from the endpoint's side: a refusal is a message, not a 500."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)

    def test_a_refused_transition_shows_a_message_and_changes_nothing(self):
        """
        Spec 9. draft -> accepted is refused, and refused LOUDLY: the person who
        clicked gets the reason, not a traceback. What matters underneath is that
        `set_status` validates before it writes, so the except branch has no save
        in it and the stored status is untouched — asserted on the row, because a
        redirect that happens to show the old status in a flash message would look
        identical from the browser.
        """
        response = self.client.post(
            reverse("quote_status", args=[self.deal.pk]),
            {"status": Quote.Status.ACCEPTED},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot go from Draft to Accepted")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_an_allowed_transition_saves_and_reports_itself(self):
        """The other half of the same endpoint: a legal move goes through."""
        response = self.client.post(
            reverse("quote_status", args=[self.deal.pk]),
            {"status": Quote.Status.SENT},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)
        self.assertContains(response, "now Sent")

    def test_another_rep_cannot_move_somebody_elses_quote_status(self):
        """
        SECURITY NOTE — the endpoint is POST-only, which is a statement about the
        verb, not about who may use it. Without the ownership check this is a
        one-field form a Rep posts about a colleague's deal, and it is the same IDOR
        shape as the editor's.
        """
        other = make_user("other", "Rep")
        self.client.force_login(other)
        response = self.client.post(
            reverse("quote_status", args=[self.deal.pk]),
            {"status": Quote.Status.SENT},
        )
        self.assertEqual(response.status_code, 403)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)


# ------------------------------------------------------------- live totals


class QuoteRenderTests(TestCase):
    """
    Specs 16–19: the numbers on the page, and what the editor refuses to touch.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)

    def test_the_detail_page_shows_the_total_recomputed_from_the_lines(self):
        """
        Spec 16. Asserted twice over: the figure is in the response, AND it is
        equal to what `quote_total()` says. The first catches a page that stopped
        printing a total; the second catches a page printing a total computed some
        other way, which is the failure a stored column would have produced.

        $7,350.00 is the same figure test 2 asserts in Python. Both tests read the
        same two lines, so a change to how the total is worked out has to be
        changed in two places or this goes red.
        """
        make_line(self.quote, "Laptop", quantity=3, unit_price="2400.00")
        make_line(self.quote, "Training", quantity="1.5", unit_price="100.00")

        response = self.client.get(reverse("quote_detail", args=[self.deal.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], Decimal("7350.00"))
        self.assertContains(response, "$7,350.00")
        # The per-row amount too, because a page whose rows disagree with its total
        # is worse than a page with no rows.
        self.assertContains(response, "$7,200.00")
        self.assertContains(response, "$150.00")

    def test_the_editor_saves_a_new_line_and_the_total_moves(self):
        """
        Spec 17. The POST carries one row against a quote with no lines. What is
        asserted afterwards is the TOTAL, not just the row count: a save that
        creates the line but does not change the arithmetic has saved nothing a
        customer can read.
        """
        self.assertEqual(quote_total(self.quote), Decimal("0.00"))

        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post({"description": "Setup labour", "quantity": "0.25", "unit_price": "800.00"}),
        )

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.lines.count(), 1)
        self.assertEqual(quote_total(self.quote), Decimal("200.00"))
        self.assertContains(self.client.get(response["Location"]), "$200.00")

    def test_positions_are_written_from_the_formset_order(self):
        """
        Spec — the view renumbers by formset index so ordering is explicit rather
        than incidental. Two rows POSTed in one order and one row POSTed in the
        other must renumber: positions 1 and 2, not 0 and 0 (the model default) or
        whatever the pk tiebreak happened to produce.
        """
        self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post(
                {"description": "First", "quantity": "1", "unit_price": "100.00"},
                {"description": "Second", "quantity": "2", "unit_price": "50.00"},
            ),
        )
        self.assertEqual(
            [(l.description, l.position) for l in self.quote.lines.all()],
            [("First", 1), ("Second", 2)],
        )

        second = self.quote.lines.get(description="Second")
        self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post(
                {"id": second.pk, "description": "Second", "quantity": "2", "unit_price": "50.00"},
                {"id": self.quote.lines.get(description="First").pk, "description": "First", "quantity": "1", "unit_price": "100.00"},
                initial=2,
            ),
        )
        self.assertEqual(
            [(l.description, l.position) for l in self.quote.lines.all()],
            [("Second", 1), ("First", 2)],
            msg="swapping the rows in the form swaps the stored order",
        )

    def test_an_expired_quote_is_marked_and_its_number_unchanged(self):
        """
        Spec 18. The marker says so; the total does NOT move. Nobody re-priced
        those laptops because the calendar moved, and a page that zeroes or inflates
        the figure of a lapsed quote is reporting a number that never existed. The
        assertion on `Decimal("7350.00")` after marking it expired is the whole
        test — the marker alone is easy.
        """
        self.quote.valid_until = local_today() - timedelta(days=1)
        self.quote.save(update_fields=["valid_until"])
        make_line(self.quote, "Laptop", quantity=3, unit_price="2400.00")

        response = self.client.get(reverse("quote_detail", args=[self.deal.pk]))

        self.assertTrue(response.context["expired"])
        self.assertContains(response, "Expired")
        self.assertEqual(response.context["total"], Decimal("7200.00"))
        self.assertContains(response, "$7,200.00")

    def test_editing_an_accepted_quote_is_refused(self):
        """
        Spec 19. The editor redirects to the quote with an explanation instead of
        rendering a form that would refuse to save. Asserted on what came back and
        on the line that was NOT changed, because an edit that redirects and still
        saves would pass a status-code-only assertion.
        """
        self.quote.status = Quote.Status.ACCEPTED
        self.quote.save(update_fields=["status"])
        line = make_line(self.quote, "Laptop", quantity=3, unit_price="2400.00")

        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post(
                {"id": line.pk, "description": "Laptop", "quantity": "3", "unit_price": "1.00"},
                initial=1,
            ),
        )

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        line.refresh_from_db()
        self.assertEqual(line.unit_price, Decimal("2400.00"))
        self.assertEqual(quote_total(self.quote), Decimal("7200.00"))

    def test_a_deal_with_no_quote_redirects_to_the_editor(self):
        """
        Spec — `quote_detail` on a deal that has never been quoted redirects rather
        than 404s. The quote is not missing, it has not been written yet, and the
        editor is the page that writes it. A 404 would be a statement about a fact
        that does not exist, and would leave the rep with nowhere obvious to go.
        """
        self.quote.delete()
        response = self.client.get(reverse("quote_detail", args=[self.deal.pk]))
        self.assertRedirects(response, reverse("quote_edit", args=[self.deal.pk]))


# ------------------------------------------------------------ money sanity


class QuoteMoneyTests(TestCase):
    """Spec 20: the biggest price the schema admits, round-tripped through the FORM."""

    def test_a_large_unit_price_round_trips_without_loss(self):
        """
        Spec 20. 99999999.99 is the largest value `max_digits=12,
        decimal_places=2` accepts, and it goes through the FORM — not the model,
        which would prove nothing about the widget, the field, or the coercion in
        between.

        A float round-trip returns 99999999.99 as something that formats to
        99999999.99000001, and the total inherits it. That is a wrong number in
        the cents on a big quote, which is the kind of wrong nobody spots, because
        a big number on a big deal looks exactly right.
        """
        self.company = make_company()
        self.deal = make_deal(self.company, owner=make_user("rep", "Rep"))
        quote = make_quote(self.deal)
        self.client.force_login(self.deal.owner)

        self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post({"description": "Rack", "quantity": "1", "unit_price": "99999999.99"}),
        )

        line = quote.lines.get()
        self.assertEqual(line.unit_price, Decimal("99999999.99"))
        self.assertEqual(quote_total(quote), Decimal("99999999.99"))

    def test_a_line_with_no_price_is_a_form_error_not_a_zero(self):
        """
        Spec — the reason `unit_price` has no default. An extra formset row that
        somebody half-filled in — a description typed, no price yet — must not
        save as $0.00 and land in the total as though it were free. The row is
        rejected and the quote is left exactly as it was.

        `initial=0` and one row with a description and a blank price: the row is
        NOT empty, so `empty_permitted` does not save it, and `unit_price` is
        required, so the whole submission is refused. Which is the point: a quote
        that goes out looking finished while a line is free is worse than one that
        will not save.
        """
        owner = make_user("rep", "Rep")
        company = make_company()
        deal = make_deal(company, owner=owner)
        quote = make_quote(deal)
        make_line(quote, "Existing", unit_price="100.00")
        self.client.force_login(owner)

        response = self.client.post(
            reverse("quote_edit", args=[deal.pk]),
            lines_post(
                {"id": quote.lines.get().pk, "description": "Existing", "quantity": "1", "unit_price": "100.00"},
                {"description": "Half typed", "quantity": "1", "unit_price": ""},
                initial=1,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["formset"].forms[1].errors)
        self.assertEqual(quote.lines.count(), 1)
        self.assertEqual(quote_total(quote), Decimal("100.00"))


class QuoteDeleteLineTests(TestCase):
    """
    The `can_delete=True` half of the editor: a line nobody wanted comes off, and
    the rows around it keep their order.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)

    def test_deleting_one_line_leaves_the_others_renumbered_in_order(self):
        """
        Three lines, the middle one deleted. What is asserted is not "two rows
        remain" but the full `(description, position)` list — because the failure
        mode of a formset delete is not the wrong rows surviving, it is the right
        rows surviving at positions 1 and 3 with a hole where the second was.

        The deleted row carries its own values back, and that is not incidental:
        a row with an `id` is an INITIAL form, so `empty_permitted` does not apply
        to it and `unit_price` is validated as required. Ticking DELETE does not
        excuse a blank field, and in the real template the box sits next to the
        values that were already there. A test that posted an empty row would be
        testing a form the app never renders.
        """
        make_line(self.quote, "One", unit_price="100.00", position=1)
        middle = make_line(self.quote, "Two", unit_price="200.00", position=2)
        make_line(self.quote, "Three", unit_price="300.00", position=3)

        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post(
                {"id": self.quote.lines.get(description="One").pk, "description": "One", "quantity": "1", "unit_price": "100.00"},
                {"id": middle.pk, "description": "Two", "quantity": "1", "unit_price": "200.00", "delete": True},
                {"id": self.quote.lines.get(description="Three").pk, "description": "Three", "quantity": "1", "unit_price": "300.00"},
                initial=3,
            ),
        )

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        self.assertEqual(
            [(l.description, l.position) for l in self.quote.lines.all()],
            [("One", 1), ("Three", 2)],
        )
        self.assertEqual(quote_total(self.quote), Decimal("400.00"))


class NonNegativeLineTests(TestCase):
    """
    REVIEW-30. A quantity or a unit price below zero is refused, on the way in.

    TEACHING NOTE — this class exists because `QuoteLine.quantity` and
    `unit_price` were plain `DecimalField`s, and a plain `DecimalField` has no
    minimum. The widgets carried `min="0"`, which is an affordance for a phone
    keypad and not a rule: a hand-made POST never sees a widget, so probe 6.9
    posted `quantity=-5` and `unit_price=-250.00` at the real editor and the app
    saved both, and `quote_total()` then reported a negative total for a quote a
    customer is supposed to agree to.

    The two tests below are one behaviour seen twice, and the split matters. The
    form test proves the EDGE the attacker came in through, because that is what
    was broken. The model test proves WHERE the rule lives — on the field, so the
    admin, an importer and a management command get it too, which a `forms.py`
    clean could not deliver. A `forms.py`-only fix would turn the first green and
    leave the second failing, which is the shape of a fix that has only moved the
    hole.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)

    def _post(self, **line):
        return self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post({"description": "Probe", **line}),
        )

    def test_the_editor_refuses_a_negative_quantity(self):
        response = self._post(quantity="-5", unit_price="100.00")

        self.assertEqual(response.status_code, 200, "a refusal re-renders; it must not redirect")
        self.assertIn("quantity", response.context["formset"].forms[0].errors)
        self.assertEqual(self.quote.lines.count(), 0)
        self.assertEqual(quote_total(self.quote), Decimal("0.00"))

    def test_the_editor_refuses_a_negative_unit_price(self):
        response = self._post(quantity="1", unit_price="-250.00")

        self.assertEqual(response.status_code, 200)
        self.assertIn("unit_price", response.context["formset"].forms[0].errors)
        self.assertEqual(self.quote.lines.count(), 0)
        self.assertEqual(quote_total(self.quote), Decimal("0.00"))

    def test_a_legal_line_still_saves(self):
        """
        The control the two refusals need. A formset that refused every line would
        pass both of them while the feature did nothing — which is the shape of
        probe 1.4 in REVIEW-14, where a check passed for a reason that had nothing
        to do with the defence.
        """
        response = self._post(quantity="2.5", unit_price="80.00")

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        self.assertEqual(quote_total(self.quote), Decimal("200.00"))

    def test_the_model_itself_refuses_a_negative_amount(self):
        """
        The rule is on the FIELD, so it holds outside the editor — the Django
        admin, a CSV importer and a management command all go through `full_clean`
        rather than through this form. Zero is allowed on purpose: a line of
        nothing is a real thing to type while a price is being worked out, and
        the "a missing price is an error, not a zero" argument in the QuoteLine
        docstring is about `unit_price` being required, not about zero being
        banned.

        `full_clean()` rather than `objects.create()`, because a validator is only
        consulted by `full_clean()` — `create()` writes whatever it is handed,
        which is precisely why this assertion cannot be made through `make_line`.
        """
        make_line(self.quote, "Zero is allowed", quantity=0, unit_price="10.00")

        for bad in (
            {"description": "Negative quantity", "quantity": "-1", "unit_price": "10.00"},
            {"description": "Negative price", "quantity": "1", "unit_price": "-0.01"},
        ):
            with self.assertRaises(ValidationError, msg=f"{bad['description']} was accepted"):
                QuoteLine(quote=self.quote, **bad).full_clean()

        self.assertEqual(quote_total(self.quote), Decimal("0.00"))


class QuoteEditorBrowserSubmitTests(TestCase):
    """
    REVIEW-33, which did not reproduce — and the tests that prove it did not.

    REVIEW-33 claimed the quote editor cannot be saved from a browser, because the
    three blank rows `QuoteLineFormSet(extra=3)` always renders fail validation. It
    did not reproduce, and the reason is a single attribute. The repro POSTed
    `quantity=""` on the blank rows; a real browser POSTs `quantity="1"`, because
    `QuoteLine.quantity` has `default=1` and the widget renders that default into
    the blank row's `value`. Django compares "1" to Decimal("1"), `has_changed()`
    is False, and `empty_permitted` skips the row entirely — the three rows come
    back in the body and none of them is a line.

    So the claim was wrong and the SUITE was wrong with it. Every test above built
    its POST by hand through `lines_post`, which means nothing in this module had
    ever submitted what the page renders — a bug that only appears when the browser
    is the client is invisible to a suite where the author is. The three tests below
    close that gap by reading the body off the rendered HTML.

    ACCEPTED BEHAVIOUR, not a bug — the third test. Blanking the "1" on a row
    somebody is NOT using ("") turns that row into a changed row, which brings it
    back under validation, which refuses it for the one thing that is now missing.
    That is the whole contract working as written: a row is skipped only while it
    still holds exactly what it was rendered with. A rep who clears the quantity box
    on an empty row has said "I meant to type something here", and the page says so
    instead of quietly dropping it.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company()
        self.deal = make_deal(self.company, owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)
        # Read off the formset, never typed in. `lines_post` documents why: the
        # prefix comes from the reverse accessor ("lines"), not from the form's
        # name, and a hand-written prefix produces a POST that looks complete,
        # validates, and saves nothing.
        self.prefix = QuoteLineFormSet.get_default_prefix()
        self.url = reverse("quote_edit", args=[self.deal.pk])

    def test_new_quote_saves_with_blank_extra_rows(self):
        """
        REVIEW-33's exact claim, submitted the way REVIEW-33 did not submit it.

        No quote, so the editor's GET creates one with zero lines and renders three
        blank rows. Only row 0 is typed into. If the blank rows were a problem this
        is where it shows up, because they are three submitted rows that are not
        lines.

        The `TOTAL_FORMS` assertion is what makes the rest of the test mean
        anything: without it, a save could be explained by the rows never having
        been sent at all, which is a different failure and an invisible one.
        """
        self.quote.delete()

        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)

        data = browser_post(
            page,
            **{
                f"{self.prefix}-0-description": "Three laptops",
                f"{self.prefix}-0-unit_price": "2400.00",
            },
        )
        self.assertEqual(data[f"{self.prefix}-TOTAL_FORMS"], "3")

        response = self.client.post(self.url, data)

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        quote = Quote.objects.get(deal=self.deal)
        self.assertEqual(quote.lines.count(), 1)
        line = quote.lines.get()
        self.assertEqual(line.position, 1, msg="the typed row is line 1")
        self.assertEqual(line.quantity, Decimal("1"), msg="the rendered default round-tripped")

    def test_existing_quote_saves_with_blank_extra_rows(self):
        """
        The same editor with two lines already on it, so the page renders five rows:
        two existing and three blank. `INITIAL_FORMS` is the other half of the
        management form and it is asserted for the same reason as TOTAL_FORMS — two
        rows that exist and three that do not, and the view has to be told which is
        which before it can renumber anything.

        Nothing is deleted, and that is worth pinning: the DELETE checkbox renders
        unchecked, an unchecked checkbox is not submitted, and a formset that lost a
        line to a checkbox nobody ticked would still redirect and still look fine.
        """
        make_line(self.quote, "Four laptops", quantity=4, unit_price="1900.00", position=1)
        make_line(self.quote, "Setup labour", quantity="2.5", unit_price="800.00", position=2)

        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)

        data = browser_post(page, **{f"{self.prefix}-0-description": "Five laptops"})

        self.assertEqual(data[f"{self.prefix}-TOTAL_FORMS"], "5")
        self.assertEqual(data[f"{self.prefix}-INITIAL_FORMS"], "2")
        self.assertNotIn(f"{self.prefix}-0-DELETE", data, msg="an unticked checkbox is not submitted")

        response = self.client.post(self.url, data)

        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        self.assertEqual(self.quote.lines.count(), 2)
        first = self.quote.lines.get(position=1)
        self.assertEqual(first.description, "Five laptops")

    def test_clearing_the_default_quantity_on_an_unused_row_is_refused(self):
        """
        The one edge, and it is the class docstring's ACCEPTED BEHAVIOUR: a row that
        still holds exactly what it rendered is skipped, so a cleared quantity box on
        an unused row counts as an edit, and an edit to a row with no price and no
        description is refused rather than saved.

        200 and zero lines, not a redirect — a refusal re-renders the form so the rep
        can see which row is complaining, which is the whole difference between a
        form that argues with you and one that throws the work away.
        """
        self.quote.delete()

        page = self.client.get(self.url)
        data = browser_post(
            page,
            **{
                f"{self.prefix}-0-description": "Three laptops",
                f"{self.prefix}-0-unit_price": "2400.00",
                f"{self.prefix}-2-quantity": "",
            },
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200, msg="a refusal re-renders; it must not redirect")
        self.assertContains(response, "This field is required.")
        self.assertEqual(Quote.objects.get(deal=self.deal).lines.count(), 0)