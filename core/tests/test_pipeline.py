"""
Pipeline board regression tests — written from qa/FINDINGS.md, entries PIPE-1
through PIPE-9.

WHY THIS FILE IS RED ON PURPOSE
Every test below describes the board as it SHOULD behave. None of them pass
today, because each one pins a bug the QA pass logged. They are not broken
tests and they are not aspirational: they are executable specifications. Fix the
bug named in the docstring and the matching test goes green. Do not "fix" them
by weakening the assertion.

THE ONE THING THAT MAKES THEM FAIL CLEANLY
PIPE-4 through PIPE-9 all have a half that lives in JavaScript, CSS, or a
browser viewport. Of those, the half that can be checked from the boundary the
server controls — the markup and the script the server ships — is asserted
here. The half that genuinely needs a DOM is asserted where there is one:
PIPE-9's double-drop lives in core/tests/test_browser.py, which drives the real
board in headless Chromium and skips loudly when Playwright is not installed.
Each of those tests states its contract in the docstring, so whoever fixes it
knows exactly what the test will accept.

TWO DECISIONS WORTH READING BEFORE FIXING ANYTHING
PIPE-6 is not being solved with a position column. Reordering inside a column
is being switched OFF (`sort: false`), because a field that only the board reads
is a field that goes stale the moment anyone filters. See ReorderDisabledTests.
PIPE-9 splits in two: the touch half is Sortable config and is tested as config
(TouchDragDelayTests); the double-drop half needs a real DOM and is tested in
core/tests/test_browser.py.

SCOPE
Pipeline only. The activity-timeline findings (QA-01, QA-02) live in
qa/FINDINGS.md too but belong to a different surface, so they are not here.
"""

import re
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import Stage

from .factories import PASSWORD, make_company, make_deal, make_user


def _static(*parts):
    """Read a file out of static/. The JS/CSS under test is a build artifact."""
    return (Path(settings.BASE_DIR) / "static").joinpath(*parts).read_text(encoding="utf-8")


def app_js():
    return _static("js", "app.js")


def app_css():
    return _static("css", "app.css")


# HTML elements with no closing tag. If one appeared inside a tracked element it
# would push the parser's nesting depth out of sync and swallow the element's
# text, so they are counted as depth zero.
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}


class _IdTextParser(HTMLParser):
    """
    Collects `{element id: (has hx-swap-oob, text content)}` for every element in
    a fragment that carries an id.

    TEACHING NOTE: `assertContains` can only tell you a string is present
    SOMEWHERE. The PIPE-2 contract needs two things it cannot see — which element
    the number came from, and whether that element is marked for out-of-band
    swap. So the fragment gets parsed instead of grepped. Standard library only;
    BeautifulSoup is not a dependency here and does not need to become one.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = {}
        self._open_id = None
        self._depth = 0
        self._text = []

    def handle_starttag(self, tag, attrs):
        if self._open_id is not None:
            if tag not in VOID_TAGS:
                self._depth += 1
            return
        # HTMLParser hands attrs over as a list of (name, value) tuples, so a
        # membership test against the raw list would never match. Dict first.
        attrs = dict(attrs)
        node_id = attrs.get("id")
        if node_id is None:
            return
        self._open_id = node_id
        self._depth = 1
        self._text = []
        self.elements.setdefault(node_id, ("hx-swap-oob" in attrs, ""))

    def handle_data(self, data):
        if self._open_id is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self._open_id is None or tag in VOID_TAGS:
            return
        self._depth -= 1
        if self._depth <= 0:
            has_oob = self.elements[self._open_id][0]
            self.elements[self._open_id] = (has_oob, "".join(self._text))
            self._open_id = None
            self._text = []

    def result(self):
        # Flush an element the fragment left unclosed, so a malformed response
        # shows up as a surprising assertion rather than a silent omission.
        if self._open_id is not None:
            has_oob = self.elements[self._open_id][0]
            self.elements[self._open_id] = (has_oob, "".join(self._text))
        return self.elements


def oob_elements(html):
    """Parse an HTMX response fragment into {id: (has hx-swap-oob, text)}."""
    parser = _IdTextParser()
    parser.feed(html)
    parser.close()
    return parser.result()


class EmptyColumnPlaceholderTests(TestCase):
    """
    PIPE-1 — the "No deals" placeholder goes stale after a drag.

    Steps: drag the only card out of Qualified into the empty Lead column.
    Actual: Lead shows the card AND "No deals"; Qualified is blank.

    THE CONTRACT: the empty state is a function of the DOM, not one server
    render. pipeline.html bakes `<div class="kanban-empty">No deals</div>` into
    the page once (pipeline.html:27-28) and nothing in app.js's onEnd touches
    it, so the string outlives the emptiness that produced it. Fix it the way
    QA-01 recommends for the same bug class: derive the placeholder from CSS
    (`:has()`/`:empty`) so it is correct by construction. A JS fix is equally
    acceptable as long as the string stops being baked into the markup.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.deal = make_deal(
            make_company(owner=self.rep), owner=self.rep, stage=Stage.QUALIFIED
        )

    def test_placeholder_is_not_baked_into_the_server_render(self):
        resp = self.client.get(reverse("pipeline"))
        # Five of six columns are empty, so today the response ships the
        # placeholder five times and it never gets taken back out.
        self.assertNotContains(
            resp, "No deals", msg_prefix="PIPE-1: the placeholder is server-rendered, so it goes stale"
        )

    def test_placeholder_string_lives_in_css_or_js_not_the_template(self):
        # Whichever half gets fixed, the human-readable string has to come from
        # the layer that can see the current DOM state.
        self.assertTrue(
            "No deals" in app_css() or "No deals" in app_js(),
            "PIPE-1: 'No deals' must be generated client-side, not emitted by the template",
        )


class ColumnTotalsAfterMoveTests(TestCase):
    """
    PIPE-2 — the column totals freeze after a move.

    Expected: Lead `$4,250 · 1`, Qualified `$0 · 0`, and both weighted footers
    to follow. Actual: the board contradicts itself until a reload.

    THE CONTRACT: deal_stage_move returns only the card partial (views.py:711),
    so every header and footer stays pinned at its pre-move value. The fix ships
    out-of-band swaps — refreshed `col-head-<stage>` and `col-foot-<stage>`
    elements, each carrying `hx-swap-oob` — for the destination column AND the
    one the card left. Both halves matter; swapping only the destination is how
    you get a board that disagrees with itself in a new way.

    The totals have to be computed under the SAME visibility rules as the board
    itself. PipelineView filters a Rep's board down to their own deals, so a Rep's
    out-of-band totals must do the same. Getting that wrong leaks another rep's
    pipeline value into your board, which is why (b) and (c) are here and not
    just a "does the number change" assertion.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")
        self.admin = make_user("admin", "Admin")
        self.company = make_company(owner=self.rep)
        # rep owns two Proposal deals; rep2 owns a large Negotiation one. The
        # rep2 deal is invisible to `rep` and visible to `admin` — that
        # asymmetry is the whole point of tests (b) and (c).
        self.a = make_deal(
            self.company, owner=self.rep, name="A", stage=Stage.PROPOSAL, value=Decimal("1000")
        )
        self.b = make_deal(
            self.company, owner=self.rep, name="B", stage=Stage.PROPOSAL, value=Decimal("3000")
        )
        self.theirs = make_deal(
            self.company, owner=self.rep2, name="Theirs", stage=Stage.NEGOTIATION, value=Decimal("50000")
        )
        self.a_url = reverse("deal_stage_move", args=[self.a.pk])

    def _move_a_to_negotiation_as(self, username):
        self.client.login(username=username, password=PASSWORD)
        resp = self.client.post(
            self.a_url, {"stage": Stage.NEGOTIATION}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(resp.status_code, 200)
        return oob_elements(resp.content.decode())

    def test_move_returns_oob_head_and_foot_for_both_columns(self):
        els = self._move_a_to_negotiation_as("rep")
        # Proposal keeps B: $3,000, one deal, weighted at the 50% stage default.
        self.assertIn("col-head-proposal", els, "PIPE-2: no OOB head for the column the card left")
        has_oob, text = els["col-head-proposal"]
        self.assertTrue(has_oob, "PIPE-2: col-head-proposal is missing hx-swap-oob")
        self.assertIn("$3,000", text)
        self.assertIn("· 1", text)

        self.assertIn("col-head-negotiation", els, "PIPE-2: no OOB head for the destination column")
        has_oob, text = els["col-head-negotiation"]
        self.assertTrue(has_oob, "PIPE-2: col-head-negotiation is missing hx-swap-oob")
        self.assertIn("$1,000", text)
        self.assertIn("· 1", text)

        self.assertIn("col-foot-proposal", els, "PIPE-2: no OOB foot for the column the card left")
        has_oob, text = els["col-foot-proposal"]
        self.assertTrue(has_oob, "PIPE-2: col-foot-proposal is missing hx-swap-oob")
        self.assertIn("Weighted: $1,500", text)

        self.assertIn("col-foot-negotiation", els, "PIPE-2: no OOB foot for the destination column")
        has_oob, text = els["col-foot-negotiation"]
        self.assertTrue(has_oob, "PIPE-2: col-foot-negotiation is missing hx-swap-oob")
        self.assertIn("Weighted: $750", text)

    def test_oob_totals_respect_rep_visibility(self):
        els = self._move_a_to_negotiation_as("rep")
        _has_oob, text = els.get("col-head-negotiation", (False, ""))
        self.assertIn("$1,000", text, "PIPE-2: the rep's own deal is missing from the total")
        self.assertIn("· 1", text)
        # rep2's 50,000 deal is not on the rep's board, so it cannot be in the
        # number either. Leaking it would be a cross-account disclosure.
        self.assertNotIn("51,000", text, "PIPE-2: the OOB total leaks another rep's deal value")

    def test_admin_oob_totals_include_everyone(self):
        els = self._move_a_to_negotiation_as("admin")
        _has_oob, text = els.get("col-head-negotiation", (False, ""))
        # Admin sees both: rep2's 50,000 plus rep's 1,000 now in Negotiation.
        self.assertIn("$51,000", text, "PIPE-2: the admin's total must include every deal")
        self.assertIn("· 2", text)

    def test_pipeline_page_has_head_and_foot_ids(self):
        # The OOB swaps target these ids, so the board itself has to carry them
        # or the swap lands on nothing.
        self.client.login(username="rep", password=PASSWORD)
        els = oob_elements(self.client.get(reverse("pipeline")).content.decode())
        for value, _label in Stage.choices:
            self.assertIn(f"col-head-{value}", els, f"PIPE-2: board has no col-head-{value}")
            self.assertIn(f"col-foot-{value}", els, f"PIPE-2: board has no col-foot-{value}")


class ClosedStageWeightTests(TestCase):
    """
    PIPE-3 — weighted value ignores closed stages.

    A deal with a manual probability of 25% dropped into Won still weighs 25%,
    and in Lost it still adds weight to the forecast. STAGE_PROBABILITY already
    says Won is 100 and Lost is 0; a per-deal override shouldn't be able to
    contradict the one stage where the outcome is already known.

    This is the one finding in the set that is purely a model bug, so it gets
    the plainest possible assertion.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)

    def test_won_is_always_worth_full_value(self):
        deal = make_deal(
            self.company, owner=self.rep, stage=Stage.WON,
            value=Decimal("1000"), probability=25,
        )
        self.assertEqual(deal.effective_probability, 100, "PIPE-3: a Won deal is not 25% likely")
        self.assertEqual(deal.weighted_value, Decimal("1000"))

    def test_lost_is_always_worth_nothing(self):
        deal = make_deal(
            self.company, owner=self.rep, stage=Stage.LOST,
            value=Decimal("1000"), probability=25, lost_reason="Went with ADT",
        )
        self.assertEqual(deal.effective_probability, 0, "PIPE-3: a Lost deal still adds weight")
        self.assertEqual(deal.weighted_value, Decimal("0"))

    def test_open_stage_still_honours_the_override(self):
        # Guard rail: the fix must clamp the CLOSED stages only. A manual 25%
        # on an open deal is legitimate and has to keep working.
        deal = make_deal(
            self.company, owner=self.rep, stage=Stage.PROPOSAL,
            value=Decimal("1000"), probability=25,
        )
        self.assertEqual(deal.effective_probability, 25)
        self.assertEqual(deal.weighted_value, Decimal("250"))


class NetworkFailureRecoveryTests(TestCase):
    """
    PIPE-4 — a dropped connection leaves the card in the wrong column.

    app.js listens for `htmx:responseError` (4xx/5xx) and reloads the board.
    `htmx:sendError` — the request never completed, server down, connection cut
    mid-flight — has no listener, so the card stays exactly where the user
    dropped it while the database still has it in the old stage. The board is
    lying and nothing says so.

    THE CONTRACT: app.js handles htmx:sendError for the move form and recovers
    the same way the responseError path does — put the card back where it came
    from, or reload and let the truth come back from the server.
    """

    def test_script_handles_send_error_for_the_move_form(self):
        js = app_js()
        self.assertIn("htmx:sendError", js, "PIPE-4: htmx:sendError is unhandled")

    def test_send_error_handler_actually_recovers_the_board(self):
        js = app_js()
        idx = js.find("htmx:sendError")
        self.assertNotEqual(idx, -1, "PIPE-4: htmx:sendError is unhandled")
        handler = js[idx:idx + 500]
        self.assertTrue(
            any(token in handler for token in ("reload", "insertBefore", "putBack")),
            "PIPE-4: the sendError handler must put the card back or reload the board",
        )


class ViewerCannotDragTests(TestCase):
    """
    PIPE-5 — a Viewer can drag cards.

    Cross-column drops snap back (the server 403s) but reorders inside a column
    stick on screen, so a read-only user gets a board that lies to them.
    deal_card.html correctly omits the move form for non-editors, but SortableJS
    ignores the HTML5 `draggable` attribute, so the JS initialises a draggable
    column either way.

    THE CONTRACT: the board markup has to carry the view's existing `can_edit`
    decision in a form the script can read (data-can-edit on .kanban), and app.js
    has to consult it before creating any Sortable instance. Both halves are
    asserted — a flag nobody reads is as broken as no flag at all, and a flag
    that is hardcoded "false" is the same bug wearing a disguise. So both
    values get tested: a Viewer must read false, a Rep must read true.
    """

    def setUp(self):
        self.viewer = make_user("viewer", "Viewer")
        self.rep = make_user("rep", "Rep")
        self.client.login(username="viewer", password=PASSWORD)
        make_deal(make_company(owner=self.viewer), owner=self.viewer, stage=Stage.LEAD)

    def test_board_markup_tells_the_script_dragging_is_off(self):
        resp = self.client.get(reverse("pipeline"))
        self.assertContains(
            resp, 'data-can-edit="false"', msg_prefix="PIPE-5: the script has no way to know the role"
        )

    def test_board_markup_tells_the_script_dragging_is_on_for_a_rep(self):
        # The other half. Without this, `data-can-edit="false"` forever is a
        # pass — a board where nobody can drag anything satisfies it perfectly.
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        self.assertContains(
            resp, 'data-can-edit="true"',
            msg_prefix="PIPE-5: a Rep can move deals, so the script has to be told so",
        )

    def test_script_consults_the_permission_before_enabling_drag(self):
        self.assertIn("can-edit", app_js(), "PIPE-5: Sortable is initialised regardless of role")


class ReorderDisabledTests(TestCase):
    """
    PIPE-6 — reordering within a column is cosmetic.

    app.js's onEnd returns early when `evt.from === evt.to`, Deal.Meta.ordering
    is `-updated_at`, and nothing in the schema records a card's place in its
    column. So a reorder is never sent, never stored, and reverts on the next
    reload.

    THE DECISION: no position field. Persisting order means every code path that
    creates, imports, or bulk-moves a deal has to keep that field honest, and a
    board column sorted by -updated_at means "most recently touched first",
    which is a defensible order on its own. Reordering inside a column is
    switched OFF instead: `sort: false` tells SortableJS that the only legal
    drag is across columns, where a stage change actually gets saved.
    """

    def test_reordering_within_a_column_is_switched_off(self):
        js = app_js()
        self.assertRegex(
            js, r"sort\s*:\s*false",
            msg="PIPE-6: in-column reordering must be disabled, not left cosmetic",
        )


class DragHighlightTests(TestCase):
    """
    PIPE-7 — the `.drag-over` highlight is dead CSS, and it's blue.

    app.css:240 styles `.kanban-col.drag-over` with `--brand-blue` and a `#eef6ff`
    wash, but no JS ever adds the class. Two problems: the affordance doesn't
    fire, and when it eventually does it violates the no-blue brand rule.

    THE CONTRACT: some drag-state class is actually toggled by the script, and
    none of its styling reaches for blue. Selector naming is deliberately loose
    (drag OR drop) so a rename counts as a fix.
    """

    def test_script_actually_toggles_the_drag_highlight(self):
        # Not just "the word drag appears somewhere" — app.js talks about drops
        # in its comments today. What has to exist is a class toggle, because a
        # rule nobody can switch on is the whole bug.
        js = app_js()
        toggles = re.search(r"classList\.(?:add|toggle|remove)\([^)]*(?:drag|drop)", js, re.IGNORECASE)
        self.assertIsNotNone(
            toggles,
            "PIPE-7: nothing in app.js ever adds a drag-state class, so the rule is dead CSS",
        )

    def test_drag_highlight_uses_no_blue(self):
        css = app_css()
        rules = re.findall(r"[^{}]*(?:drag|drop)[^{}]*\{[^}]*\}", css, re.IGNORECASE)
        self.assertTrue(rules, "PIPE-7: expected the drag-state rule to still exist")
        blue = [rule for rule in rules if "--brand-blue" in rule or "#eef6ff" in rule]
        self.assertEqual(blue, [], f"PIPE-7: blue in the drag highlight: {blue}")


class ReopenClearsLostReasonTests(TestCase):
    """
    PIPE-8 — moving a Lost deal back to an open stage keeps the old lost_reason.

    views.py:690 only writes lost_reason when moving INTO Lost. Reopen the deal
    and it walks the pipeline still carrying "went with ADT", which then shows up
    on the deal detail page and poisons the reopen/relose history.

    THE CONTRACT: leaving a closed stage clears the reason, because a deal that
    is back in the pipeline has no reason for having been lost.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.deal = make_deal(
            make_company(owner=self.rep),
            owner=self.rep,
            stage=Stage.LOST,
            lost_reason="Went with ADT",
        )
        self.url = reverse("deal_stage_move", args=[self.deal.pk])

    def test_reopening_clears_the_stale_reason(self):
        resp = self.client.post(self.url, {"stage": Stage.NEGOTIATION}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.NEGOTIATION)
        self.assertEqual(
            self.deal.lost_reason, "", "PIPE-8: the reopen deal still carries its old lost_reason"
        )

    def test_losing_again_still_captures_a_fresh_reason(self):
        # Guard rail: clearing on reopen must not break the reason being written
        # when the deal is lost a second time.
        self.client.post(
            self.url, {"stage": Stage.LOST, "lost_reason": "Budget froze"}, HTTP_HX_REQUEST="true"
        )
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.LOST)
        self.assertEqual(self.deal.lost_reason, "Budget froze")

    def test_history_keeps_the_old_reason(self):
        # Clearing lost_reason on reopen must not erase WHY the deal died.
        # History is newest-first, so [0] is the reopen and [1] is the Lost state.
        self.client.post(self.url, {"stage": Stage.NEGOTIATION}, HTTP_HX_REQUEST="true")
        self.assertEqual(
            self.deal.history.all()[1].lost_reason,
            "Went with ADT",
            "PIPE-8: reopening a deal must not rewrite what happened when it was lost",
        )


class LostStillNeedsAReasonTests(TestCase):
    """
    Guard rail, not a bug. PIPE-8's fix touches the lost_reason write in
    deal_stage_move, which is the same line that enforces "a Lost deal needs a
    reason". This test exists so that fix cannot quietly delete the rule while
    it is in there editing the reopen case.

    BoardLostReasonTests in test_phase2.py covers the same ground; this copy
    lives with the PIPE-8 work so the guard rail is read by whoever reads it.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.deal = make_deal(
            make_company(owner=self.rep), owner=self.rep, stage=Stage.PROPOSAL
        )
        self.url = reverse("deal_stage_move", args=[self.deal.pk])

    def test_lost_with_a_blank_reason_is_refused(self):
        resp = self.client.post(self.url, {"stage": Stage.LOST, "lost_reason": "  "}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 422)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.PROPOSAL)
        self.assertEqual(self.deal.lost_reason, "")


class TouchDragDelayTests(TestCase):
    """
    PIPE-9 (touch half) — dragging on a phone starts on scroll, not on touch.

    SortableJS fires a drag as soon as a finger lands. At 375px that means a rep
    trying to scroll the board sideways reorders or moves cards by accident,
    which on this board means a real stage change saved to the database.

    THE CONTRACT: the fix is Sortable configuration, not a schema change — delay
    the drag long enough that a scroll wins over a drag, and apply the delay to
    touch ONLY so mouse dragging still feels instant.
    """

    def test_touch_drag_is_delayed(self):
        js = app_js()
        self.assertRegex(
            js, r"delayOnTouchOnly\s*:\s*true",
            msg="PIPE-9: the drag delay must apply to touch only, or desktop drag feels broken",
        )

    def test_the_delay_is_long_enough_to_outlast_a_scroll(self):
        match = re.search(r"(?<![A-Za-z])delay\s*:\s*(\d+)", app_js())
        self.assertIsNotNone(match, "PIPE-9: no `delay:` value configured on Sortable")
        self.assertGreaterEqual(
            int(match.group(1)), 150,
            msg="PIPE-9: under 150ms the browser still reads the gesture as a scroll",
        )


# PIPE-9's double-drop half is not here: it needs a real DOM, and this file never
# builds one. It lives in core/tests/test_browser.py, which drives the actual
# board in headless Chromium and skips loudly when Playwright isn't installed.
