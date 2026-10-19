"""
Quote arithmetic and quote status — the rules, kept out of the views.

TEACHING NOTE — same shape as `core/leads.py`, and for the same reason. A view's
job is to decide WHEN something happens; what is allowed and what a number means
is a rule, and a rule written inside a view can only be enforced by that view.
`create_lead` was extracted because two views called it; `set_status` is
extracted because a rule with a table in it should be readable in one screen, and
because a POST endpoint and a future email reminder will both need to move a
quote and must not disagree about when they may.

**WHY THE TOTAL IS COMPUTED AND NEVER STORED.** There is no `total` column on
Quote, and that is the most load-bearing omission in this workstream. A stored
total is denormalised state: a second answer to a question the database can
answer exactly, and every one of those copies is a chance to be wrong. It goes
stale the moment a line is edited anywhere the quote editor isn't: an import, an
admin fix, a fix in the Django shell, a second browser tab. Nothing in this app
would notice — there is no trigger that recomputes it, because triggers are for
side effects and "the total" is not one, it is an arithmetic identity that was
duplicated by accident.

And SQLite cannot help. There is no decimal type in SQL, so summing Decimals in
the database means either storing money as an integer number of cents (a schema
decision this app has already made against, deliberately — `Deal.value` is a
DecimalField) or letting SQLite hand back a float and having the arithmetic
happen in Python anyway. Since the addition has to be in Python regardless, the
database can never be the thing that computes the total, and a `total` column
buys nothing except a place to keep the wrong answer.

The honest version is a function: always right, never wrong, and the cost is one
query on a page that renders a handful of rows.
"""

from decimal import Decimal

from .dates import local_today
from .models import Quote

# Where a sum starts. `Decimal("0")` and not `0` and not `None`, for three
# reasons that each show up as a bug later: adding a Decimal to an int raises
# TypeError in Python, so the starting value decides whether an EMPTY quote can
# be totalled at all; a float would quietly reintroduce the inexactness Decimal
# exists to avoid; and `None` is the answer for "this deal has no quote", which is
# a genuinely different statement from "this quote is worth nothing".
ZERO = Decimal("0")

# The legal moves, as the set of (from, to) pairs. Read it as the grammar of a
# quote's life; `Quote.Status` is its vocabulary.
#
#   draft    -> sent      the quote goes out
#   sent     -> accepted  the customer says yes
#   sent     -> declined  the customer says no
#   sent     -> draft     recall: pulled back before it was agreed
#   accepted -> declined  a deal falls through after signing (lost hardware,
#                         a scope change, a cancellation). Allowed, because the
#                         alternative is an "accepted" quote on a deal that is
#                         dead, which is a lie nobody ever corrects.
#   declined -> draft     reopen it to edit and send again
#
# What is NOT here is the interesting part: draft -> accepted. A quote that was
# never sent cannot be accepted, because nobody has seen the terms. Allowing it
# produces a deal marked Won against a price the customer was never shown, and it
# happens exactly when somebody is in a hurry and the button is right there. The
# cost of refusing it is two clicks. The cost of allowing it is a number nobody
# can defend.
ALLOWED_TRANSITIONS = {
    (Quote.Status.DRAFT, Quote.Status.SENT),
    (Quote.Status.SENT, Quote.Status.ACCEPTED),
    (Quote.Status.SENT, Quote.Status.DECLINED),
    (Quote.Status.SENT, Quote.Status.DRAFT),
    (Quote.Status.ACCEPTED, Quote.Status.DECLINED),
    (Quote.Status.DECLINED, Quote.Status.DRAFT),
}

# The one status whose LINES may not be changed. Accepted means the terms were
# agreed, and the terms are the lines; editing them afterwards edits a thing two
# parties already signed off on without either of them being asked. The way out
# is `set_status` — an accepted quote moves to declined and then back to draft,
# which is a visible, deliberate act in the history rather than a quiet edit. One
# status rather than a general "read-only after sent" rule, because sending is not
# the problem: recalled and re-sent drafts are the normal way this work gets done.
LOCKED_STATUSES = (Quote.Status.ACCEPTED,)

# Shown wherever a locked quote is met — the editor's refusal and the deal page's
# explanation of why the Edit button is missing. One string, two callers, so the
# two can't word it differently.
LOCKED_MESSAGE = (
    "This quote is accepted, so its line items are the terms that were agreed. "
    "Move it to Declined and back to Draft to edit them."
)


def quote_total(quote):
    """
    What the quote costs: the sum of `quantity * unit_price` over its lines.

    TEACHING NOTE — every operation here is Decimal. `line.quantity *
    line.unit_price` is exact (a product of two exact decimals is exact), and
    summing exact decimals is exact, so $0.10 is $0.10 and not $0.10000000004.
    Doing this in floats gets a total that is wrong in the ninth decimal place and
    visibly wrong once a quote runs to twenty lines.

    `quote.lines.all()` is ordered by `QuoteLine.Meta.ordering` — position, then
    pk — so the ADDITION order is the quote's display order. Decimal addition is
    commutative and associative, so that only matters if anyone ever makes the sum
    depend on the order; it costs nothing to be deterministic and everything to
    wonder about it later.

    An empty quote returns `Decimal("0")`, not `None`. There is a real difference
    and it is load-bearing: `quote_total_for_deal` below returns `None` for "this
    deal has no quote", and a client rendering "—" for None and "$0.00" for this
    is telling the truth about two genuinely different situations.
    """
    total = ZERO
    for line in quote.lines.all():
        total += line.quantity * line.unit_price
    return total


def quote_total_for_deal(deal):
    """
    The same total for a deal that may not have a quote yet. `None` if it has none.

    TEACHING NOTE — the distinction this function exists to preserve. Three
    different things, and a client that collapses any two of them is lying to
    somebody:

        no quote at all     -> None      ("nothing has been quoted")
        quote, no lines     -> Decimal(0) ("quoted, at nothing")
        quote with lines    -> a number

    Collapsing "no quote" into "$0.00" is the one that hurts: a rep scanning the
    pipeline sees a free job sitting at zero, or filters on a quote that does not
    exist, and neither of those is a fact about the deal. `RelatedObjectDoesNotExist`
    subclasses AttributeError, which is why the `hasattr` below is a clean test and
    why a template can ask `{% if deal.quote %}` without a try block.
    """
    if not hasattr(deal, "quote"):
        return None
    return quote_total(deal.quote)


def subtotal(quote):
    """
    The total before tax. Which today is the total.

    TEACHING NOTE — a named function that currently adds nothing, and that is on
    purpose. The first thing anyone adds to a quote is a tax rate, and the honest
    shape for that is: this function stays "the sum of the lines", and a NEW
    `tax()` sits above it, and the template prints both. Retrofitting "total"
    semantics into a function called `subtotal` at the moment tax arrives means
    going back and re-reading every caller to find out which one meant which.

    On expiry: an expired quote still totals what it always totalled. Nobody has
    re-priced the laptops because the calendar moved, and a template that quietly
    zeros or inflates the number of a lapsed quote is reporting a number that
    never existed. `expired()` says so in words on the page instead.
    """
    return quote_total(quote)


def line_amount(line):
    """
    What ONE line costs: `quantity * unit_price`, as a Decimal.

    TEACHING NOTE — this exists because the obvious alternative does not work.
    Django's built-in `multiply` template filter starts with `int(value) *
    int(arg)`, and `int(Decimal("0.25"))` is 0 — so rendering
    `{{ line.quantity|multiply:line.unit_price }}` prints $0.00 for a quarter hour
    of labour, silently, on every fractional line in the app. The filter then
    falls through to `value * arg`, which raises TypeError on Decimal * Decimal
    mixed with a float and returns an empty string. Both failure modes are quiet,
    and both produce a number that looks like a number.

    So the multiplication happens here, in Python, in Decimal, where it is exact
    and where a test can see it. The template prints what this returns.
    """
    return line.quantity * line.unit_price


def allowed_next_statuses(quote):
    """
    `[(value, label)]` for the statuses this quote may legally move to from here.

    TEACHING NOTE — the template offers exactly these, and the POST endpoint
    accepts exactly these, because both read `ALLOWED_TRANSITIONS`. A page that
    rendered four buttons and had one of them refused on submit is a page that
    teaches people the buttons are unreliable; a page that renders one and it
    works teaches them the opposite. Offering the legal moves is also the cheapest
    possible UI documentation of the state machine: you cannot see that
    draft -> accepted is forbidden except by noticing its absence.

    Empty is a real answer, not a gap — a quote can reach a status with nowhere to
    go (declined -> draft is the only way out of declined, but SENT -> DRAFT and
    then DRAFT -> SENT is a cycle, so a quote is never truly stuck). The template
    says so rather than rendering an empty box.
    """
    return [
        (value, Quote.Status(value).label)
        for value in Quote.Status.values
        if (quote.status, value) in ALLOWED_TRANSITIONS
    ]


def _status_label(value):
    """
    "accepted" -> "Accepted", and anything unknown -> itself.

    TEACHING NOTE — used only in the refusal message, where the one case worth
    handling well is a browser POSTing something that is not a status at all.
    Printing the raw string back at whoever sent it ("A quote cannot go from Draft
    to banana") is more use than a generic "invalid transition": it is the only
    thing that tells a person what went wrong with their request, and unlike a
    500 it does not tell an attacker whether a guessed status was close.
    """
    try:
        return Quote.Status(value).label
    except ValueError:
        return str(value)


def set_status(quote, new_status):
    """
    Move a quote to `new_status`, if that move is legal. Saves it and returns it.

    TEACHING NOTE — one function owns the transition table because there is no
    such thing as a second place that is allowed to disagree with it. The table is
    a Python set, so asking "may this happen" is a membership test rather than a
    chain of `if`s that grows a hole every time somebody adds a status. And the
    answer to a refused move is `ValueError` rather than a `False`, because a
    caller that forgets to check a returned bool gets a silent no-op — which is the
    bug class this repo has already been bitten by (see `Deal.clean()` on a lost
    deal with no reason).

    An unknown status string is refused by the same test: `"banana"` is in no
    pair, so it raises exactly like a real-but-forbidden move does. There is
    deliberately no separate "is this even a status" branch to keep in step with
    the enum — the table is the only list, and anything not in it is refused.
    """
    if (quote.status, new_status) not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"A quote cannot go from {quote.get_status_display()} to {_status_label(new_status)}."
        )
    quote.status = new_status
    quote.save(update_fields=["status", "updated_at"])
    return quote


def locked(quote):
    """
    True when this quote's lines must not change — i.e. it has been accepted.

    TEACHING NOTE — the rule, named, so that the editor view, the status-change
    endpoint and the template that hides the Edit button all read the SAME answer
    rather than each testing `status == "accepted"`. The nastiest version of that
    bug is a template that hides Edit and a view that permits it: everything looks
    right until somebody types the URL.

    The delete-all-lines case falls out of this rather than being a second rule.
    Emptying an accepted quote is the worst case of the same violation — every
    line removed, so the agreed terms are now that nothing was agreed — and a
    guard written twice is a guard that one day answers differently in the two
    places.
    """
    return quote.status in LOCKED_STATUSES


def expired(quote):
    """
    Has this quote's `valid_until` passed? True only if there IS a date and it is gone.

    TEACHING NOTE — `local_today()`, not `timezone.now().date()`. A date-only
    comparison phrased in the office's terms has to use the office's calendar, and
    UTC's date is already tomorrow from 7 pm Chicago time. That bug would expire
    quotes early every evening and only in the evening, which is a version of
    wrong that a human never catches.

    `valid_until` is inclusive: a quote valid_until 2026-10-04 is good ON the 4th
    and lapses on the 5th. `<` rather than `<=`, because "good until the 4th"
    means through the end of the 4th, and the alternative quietly takes a day off
    every quote's life.
    """
    return quote.valid_until is not None and quote.valid_until < local_today()