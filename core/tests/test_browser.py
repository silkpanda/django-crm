"""
PIPE-9's other half, driven in a real browser.

qa/FINDINGS.md split PIPE-9 in two. The touch half — a finger landing on a card
starting a drag instead of a scroll — is Sortable configuration, and is asserted
as configuration by `core/tests/test_pipeline.py::TouchDragDelayTests`. This file
is the DOUBLE-DROP half, and that one is not a configuration question. It asks
what the board looks like after `onEnd` runs twice for one card before the first
POST comes back: one `target` element, `swap: "outerHTML"`, plus the out-of-band
`col-head-*` / `col-foot-*` fragments aimed at ids that only exist once. The only
honest way to answer that is to run the shipped `app.js`, the shipped SortableJS
and the shipped htmx against a real server and then read the database. The finding
said it needed a harness this repo didn't have. This is the harness: Django's own
`StaticLiveServerTestCase`, which serves the app AND its static files on a real
port, plus Playwright's Chromium at 375x667 with touch.

THE ANSWER, MEASURED (the "no bug found" outcome the spec names): the second
POST never leaves the browser. `onEnd` does call `htmx.ajax` twice —
probe-verified — but the shipped htmx refuses the second call while the first is
still in flight: requests sourced from the same element are serialized (the same
per-element machinery the `hx-sync` attribute exists to configure), and the
parked request is then dropped outright because its source element is no longer
in the document — which the first response's `outerHTML` swap guarantees, since
the swap target (the card) contains the source (the move form). One XHR, one
POST, one save, one history row, one card: the board survives a double-drop with
no guard in `app.js` at all, so no fix was made there. The test still exists
because "refused client-side" is a claim about the future. The proven-red run
(temporarily nulling htmx's in-flight marker so both POSTs land) fails assertion
6 exactly as PIPE-9 feared — two POSTs for one drag — and the run is green again
once the marker is left alone.

WHY THE FIXTURE PRECISELY THIS SHAPE
- 375x667, `has_touch`, `is_mobile`. PIPE-9 is a 375px finding and the bug is a
  fast double gesture at phone width. A desktop-sized window would be testing a
  board nobody uses.
- A **Rep**, signing in through the real login form. Rep is the role that owns
  deals and therefore the role whose cards carry a `.deal-move-form`, so
  `data-can-edit` is "true" and the board is genuinely draggable. A Viewer gets
  a board you cannot move at all, and a board you cannot move proves nothing
  about what happens when you move one twice.
- Non-Lost target stages, so no `window.prompt` lands in the middle of the
  double-drop. A dialog handler is still registered for everything else: app.js
  ALERTS on a refused move and then reloads, which would reset the board
  mid-assertion instead of letting the test say what went wrong.
- Two deals on the board, not one. "No card was lost" is meaningless on a board
  that only ever held the card being moved.

HOW THE DOUBLE-DROP IS TRIGGERED
Not by intercepting `htmx.ajax`. A test that stubs the POST proves only that the
stub was called twice; it cannot show a duplicated card or a doubled audit row,
because neither of those exists inside a stub. The test reaches into the LIVE
Sortable instance with `Sortable.get(col)` and calls its own `onEnd` twice, back
to back, inside one `page.evaluate` — so the second call happens before the first
response could possibly have landed. That is the shipped closure running against
the shipped board with the network left in the loop, which is the only version of
this test that can fail.

WHY THE SKIP IS LOUD
`playwright` is an optional extra (requirements-browser.txt) and the Chromium
download is a second step on top of it, so this file has to survive being run by
someone who has neither. The import is guarded, and so is the RUNTIME:
`sync_playwright().start()` succeeds with the package installed and raises when
`playwright install chromium` was never run, which is a perfectly normal
half-finished state. Both have to produce a skip, because `manage.py test core`
— the command the README documents and the security gate runs — has to stay
green either way. The skip message says exactly what to install. Skipped still
shows up in the test list, which is the whole reason PIPE-9's placeholder was
worth keeping in the first place, and that property is preserved here rather than
deleted.
"""

import os
import unittest
from decimal import Decimal

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from core.models import Deal, Stage

from .factories import PASSWORD, make_company, make_deal, make_user

try:  # optional extra: pip install -r requirements-browser.txt
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - a property of the machine, not the code
    HAS_PLAYWRIGHT = False
    PlaywrightError = PlaywrightTimeout = Exception


# TEACHING NOTE: Playwright's sync API drives Chromium by running an asyncio
# event loop in THIS thread, inside a greenlet, and swapping between the two.
# That means `asyncio.get_running_loop()` succeeds for the rest of the process,
# which is exactly the signal Django's `async_unsafe` guard reads as "you are
# inside an async context" — so it refuses every database call, starting with the
# ones in setUp and ending with the flush in teardown. Nothing here is actually
# async: the browser client is synchronous, the Django test is synchronous, and
# the live server has a thread of its own. This is asgiref's documented escape
# hatch for a synchronous caller that merely has a loop lying around, and it is
# set only when Playwright is actually installed so a normal run keeps the guard.
if HAS_PLAYWRIGHT:
    os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")


def _chromium_is_installed():
    """
    Can this machine actually launch Chromium right now?

    A separate probe from the import guard, because the two states are different
    problems with different fixes. `playwright install chromium` is a download
    nobody's checkout has until somebody runs it, so "playwright imports fine" is
    not a promise that a browser exists. Probed at import time so the skip
    decorator can see it, and probed in a real `launch()` rather than by looking
    for a directory — a partially-downloaded browser is exactly the case a
    filesystem check would wave through.
    """
    if not HAS_PLAYWRIGHT:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except (PlaywrightError, PlaywrightTimeout):  # pragma: no cover
        return False


HAS_CHROMIUM = _chromium_is_installed()

SKIP_REASON = (
    "PIPE-9's double-drop half needs a real browser, which is an optional extra:\n"
    "    pip install -r requirements-browser.txt\n"
    "    python -m playwright install chromium\n"
    "The Chromium download is ~150MB, which is why it is not in "
    "requirements-dev.txt; core/tests/test_pipeline.py still covers PIPE-9's "
    "touch half as Sortable configuration without any of this."
)

# 375x667 with touch is not decoration: PIPE-9 is a phone-width finding, and
# `delayOnTouchOnly` means the drag delay that half of it fixes exists only on
# the touch path. `is_mobile` also flips the board's own CSS — under 720px the
# kanban becomes a vertical stack, so "drag between two columns" is a vertical
# gesture here, not a horizontal one across a scroll strip.
VIEWPORT = {"width": 375, "height": 667}

# The two columns these tests move between. Neither is Lost, so app.js's
# window.prompt never runs. Qualified -> Proposal is an ordinary forward move.
FROM_STAGE = Stage.QUALIFIED
TO_STAGE = Stage.PROPOSAL


class PipelineBoardBrowserCase(StaticLiveServerTestCase):
    """
    Board fixture and browser lifecycle, shared by the tests below.

    TEACHING NOTE: `StaticLiveServerTestCase`, not the plain test client, because
    there is no point driving a browser against markup. This runs the real WSGI
    stack in a background thread AND serves `static/` over the same port, so
    `vendor/sortable.min.js`, `vendor/htmx.min.js` and `js/app.js` all load.
    Without them there is no Sortable instance to call and no board to drag, and
    the test would pass happily against a blank page — which is the exact failure
    mode this file was written to remove.

    TEACHING NOTE: the "Rep" group is re-created in setUp rather than
    trusted to survive. Every LiveServer test is a TransactionTestCase
    and flushes the database between tests, and flush does not re-run
    data migrations — so the groups core/0002_roles.py creates are gone
    after the first test. `serialized_rollback = True` would restore
    them, but it cannot be trusted in THIS file: Playwright's sync API
    keeps an event loop running in the test thread, and while a loop is
    running Django's thread-critical connection handler swaps to a
    contextvar storage (asgiref's Local._lock_storage checks
    asyncio.get_running_loop()). The serialized contents are set on the
    per-thread storage at database-setup time, so every per-test restore
    silently finds nothing — the second test method dies on
    Group.DoesNotExist, and the next class's restore collides with the
    content types that flush's post_migrate has just recreated. Two
    lines of get_or_create have no moving parts.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page_errors = []
        cls._playwright = sync_playwright().start()
        cls.browser = cls._playwright.chromium.launch()
        cls.context = cls.browser.new_context(
            viewport=VIEWPORT,
            has_touch=True,
            is_mobile=True,
        )
        cls.context.set_default_timeout(15_000)
        # Registered on the context, not on the test's page: app.js alerts
        # before it reloads on a refused or dropped move, and a dialog nobody is
        # listening for blocks every subsequent Playwright call.
        cls.context.on("page", lambda page: page.on("dialog", lambda d: d.dismiss()))

    @classmethod
    def tearDownClass(cls):
        cls.context.close()
        cls.browser.close()
        cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        from django.contrib.auth.models import Group

        self.page_errors = []
        self.page = self.context.new_page()
        self.page.on("pageerror", self._remember_page_error)
        # id(request) -> the request. The dict holds the object alive, so the id
        # cannot be recycled by the garbage collector while it is still in
        # flight — a recycled id would silently drop a pending request and let
        # `settle()` return early.
        self._inflight = {}
        self._moves_posted = 0
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._request_done)
        self.page.on("requestfailed", self._request_done)

        # 0002_roles created this group, the flush between tests deleted
        # it, and nothing brings it back — see the class docstring for
        # why serialized_rollback can't do that job here.
        Group.objects.get_or_create(name="Rep")
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)
        self.moved = make_deal(
            self.company,
            owner=self.rep,
            name="Tower upgrade",
            stage=FROM_STAGE,
            value=Decimal("4000"),
        )
        self.bystander = make_deal(
            self.company,
            owner=self.rep,
            name="Hardware refresh",
            stage=Stage.LEAD,
            value=Decimal("1500"),
        )
        self._sign_in()
        self._open_board()

    def tearDown(self):
        self.page.close()

    def _remember_page_error(self, error):
        # Captured, not raised. An htmx swap aimed at an element the first
        # response already replaced throws inside the browser, and whether that
        # throw mattered is what the assertions below are for — not the throw.
        self.page_errors.append(str(error))

    # ------------------------------------------------------------------ fixture

    def _sign_in(self):
        """Log in through the real form — the same page and POST a person uses."""
        self.page.goto(self.live_server_url + reverse("login"))
        self.page.fill('input[name="username"]', self.rep.username)
        self.page.fill('input[name="password"]', PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_url(lambda url: "/accounts/login/" not in str(url))

    def _open_board(self):
        """
        Load the board, then wait for app.js to have built its Sortable instances.

        app.js runs on DOMContentLoaded and returns early unless `.kanban` is
        there and `data-can-edit` reads "true", so "the cards are on screen" is
        not the same as "dragging is wired up". A double-drop fired before
        Sortable existed would find no handler to call and pass for free.
        """
        self.page.goto(self.live_server_url + reverse("pipeline"))
        self.page.wait_for_selector(f'.kanban-col[data-stage="{FROM_STAGE}"]')
        self.page.wait_for_function(
            """() => {
                const col = document.querySelector('.kanban-col-body');
                return !!col && !!window.Sortable && !!Sortable.get(col);
            }"""
        )

    # ------------------------------------------------------------------ network

    def _on_request(self, request):
        if request.method == "POST":
            self._inflight[id(request)] = request
            # Count the stage-move POSTs specifically. Counting every POST would
            # also count the sign-in, and a regression that doubled all traffic
            # is not the bug this test is about.
            if request.url.endswith(reverse("deal_stage_move", args=[self.moved.pk])):
                self._moves_posted += 1

    def _request_done(self, request):
        self._inflight.pop(id(request), None)

    def settle(self, timeout=20_000):
        """
        Wait until nothing is in flight, then wait again to be sure.

        Asserting the instant after firing the second onEnd is the mistake this
        method exists to prevent: the board is mid-swap, and reading it then
        measures a race rather than a result. Two consecutive quiet polls stop
        "the second request hasn't been sent yet" from reading as "the board
        settled", which is the same thing.
        """
        quiet = 0
        step = 100
        remaining = timeout
        while remaining > 0:
            if self._inflight:
                quiet = 0
            else:
                quiet += 1
                if quiet >= 2:
                    return
            self.page.wait_for_timeout(step)
            remaining -= step

    # --------------------------------------------------------------- board reads

    def card_count(self, deal_id):
        return self.page.evaluate(
            """id => document.querySelectorAll(
                '.deal-card[data-deal-id="' + id + '"]').length""",
            deal_id,
        )

    def card_column(self, deal_id):
        """The stage of the column the deal's card is actually sitting in."""
        return self.page.evaluate(
            """id => {
                const card = document.querySelector(
                    '.deal-card[data-deal-id="' + id + '"]');
                if (!card) return null;
                const col = card.closest('.kanban-col');
                return col ? col.dataset.stage : null;
            }""",
            deal_id,
        )

    def column_head_text(self, stage):
        return " ".join(self.page.inner_text(f"#col-head-{stage}").split())

    def column_foot_text(self, stage):
        return " ".join(self.page.inner_text(f"#col-foot-{stage}").split())

    def board_counts(self):
        """Every deal rendered as a card, counted per deal id."""
        return self.page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('.deal-card[data-deal-id]').forEach(card => {
                    out[card.dataset.dealId] = (out[card.dataset.dealId] || 0) + 1;
                });
                return out;
            }"""
        )

    def _centre_both(self, *selectors):
        """Scroll so every one of these elements is comfortably inside the window."""
        self.page.evaluate(
            """selectors => {
                const rects = selectors.map(s => document.querySelector(s)
                    .getBoundingClientRect());
                const top = Math.min(...rects.map(r => r.top));
                const bottom = Math.max(...rects.map(r => r.bottom));
                window.scrollBy(0, (top + bottom) / 2 - window.innerHeight / 2);
            }""",
            list(selectors),
        )

    # -------------------------------------------------------------- db helpers

    def stage_of(self, deal):
        return Deal.objects.get(pk=deal.pk).stage

    def history_count(self, deal):
        """simple_history's audit rows for this deal."""
        return Deal.history.filter(id=deal.pk).count()


def fire_double_on_end(page, deal_id, from_stage, to_stage):
    """
    Call the LIVE Sortable instance's `onEnd` twice for one card, no await between.

    Worth being precise about what this does and does not fake:

    - `Sortable.get(col)` returns the instance app.js created, so
      `inst.options.onEnd` IS the shipped closure — the one carrying `putBack`,
      the `lost` prompt and the `htmx.ajax` call.
    - Nothing is stubbed. `htmx.ajax` runs twice — the shipped closure has no
      guard of its own — and the second call is refused by the vendored htmx
      itself: an element with a request in flight does not issue another (the
      behavior `hx-sync` exists to configure), and the parked request is then
      dropped because the first response's swap detached its source form. One
      XHR goes out, the server saves once, and the test reads the database to
      prove all of that stayed true.
    - Both calls live in one `evaluate`, so they run in one task. A network round
      trip cannot complete between them, which is precisely the window PIPE-9 is
      about.

    Returns a dict with `ok: False` and a reason if the page was not in the shape
    the test needed, so an unreachable target reads as a named failure instead of
    a pass.
    """
    return page.evaluate(
        """({ dealId, fromStage, toStage }) => {
            const card = document.querySelector(
                '.deal-card[data-deal-id="' + dealId + '"]');
            if (!card) return { ok: false, why: 'the card is not on the board' };
            const from = document.querySelector(
                '.kanban-col[data-stage="' + fromStage + '"] .kanban-col-body');
            const to = document.querySelector(
                '.kanban-col[data-stage="' + toStage + '"] .kanban-col-body');
            if (!from || !to) return { ok: false, why: 'a column is missing' };
            const inst = Sortable.get(from);
            if (!inst || !inst.options.onEnd) {
                return { ok: false, why: 'app.js never built a Sortable instance' };
            }
            const oldIndex = Array.prototype.indexOf.call(from.children, card);
            // SortableJS has already moved the element into the destination
            // column by the time onEnd runs. Reproduce that, or the handler's
            // swap target and putBack() are reasoning about a board shape that
            // cannot occur in the browser and the test measures nothing real.
            to.appendChild(card);
            const evt = { item: card, from, to, oldIndex, newIndex: 0 };
            inst.options.onEnd(evt);
            inst.options.onEnd(evt);
            return { ok: true };
        }""",
        {"dealId": deal_id, "fromStage": from_stage, "toStage": to_stage},
    )


@unittest.skipUnless(HAS_PLAYWRIGHT and HAS_CHROMIUM, SKIP_REASON)
class DealDoubleDropTests(PipelineBoardBrowserCase):
    """
    PIPE-9's double-drop half. One drag, one POST, one board — and the test
    proves the second POST never leaves the browser.

    WHAT EACH ASSERTION IS BUYING, IN ORDER OF HOW MUCH IT HURTS WHEN BROKEN
    1. One card for one deal. Two is the bug a rep sees: the same opportunity
       listed twice in a column.
    2. The surviving card's column equals the stage the DATABASE now holds. A
       dropped stage change is the other half of the finding — the card sits
       somewhere the data never agreed to, and the next person to read the board
       is reading a lie.
    3. No card lost and none cloned. Every deal that was on the board is still on
       it, exactly once.
    4. The column heads and feet match the database's real per-stage count, total
       and weighted total. A duplicated out-of-band fragment landing on
       `col-head-*` / `col-foot-*` is the quiet version of this bug: the right
       cards under the wrong numbers.
    5. Exactly ONE new history row. `Deal.history` is simple_history's audit
       trail. A second POST that saves again writes a second row, so the board can
       look flawless while the audit log claims the deal moved twice — and nobody
       finds out until somebody asks why.
    6. Exactly one POST went out at all. The client-side contract, stated as
       the thing it actually is: the second onEnd is supposed to be refused —
       by htmx, as it happens, not by app.js — not sent and then politely
       reconciled.

    "DOM alone is not enough" is this repo's own rule (README, Security gate) —
    did it land is answered by reading the database — so every assertion that the
    database can answer is.
    """

    def test_a_double_drop_leaves_one_card_one_stage_one_history_row(self):
        self.settle()
        history_before = self.history_count(self.moved)
        posts_before = self._moves_posted
        bystander_stage_before = self.stage_of(self.bystander)

        result = fire_double_on_end(
            self.page, self.moved.pk, str(FROM_STAGE), str(TO_STAGE)
        )
        self.assertTrue(result["ok"], f"could not reach the handler: {result}")
        self.settle()

        # 1. Exactly one card for the deal. The headline.
        self.assertEqual(
            self.card_count(self.moved.pk),
            1,
            "PIPE-9: the double-drop left two cards for one deal on the board",
        )

        # 2. The card's column and the database agree.
        self.moved.refresh_from_db()
        self.assertEqual(
            self.moved.stage,
            TO_STAGE,
            "PIPE-9: the stage change did not land at all",
        )
        self.assertEqual(
            self.card_column(self.moved.pk),
            self.moved.stage,
            "PIPE-9: the card is sitting in a column the database never agreed to",
        )

        # 3. Nothing lost, nothing cloned, nobody else touched.
        board = self.board_counts()
        for deal_id, count in board.items():
            self.assertEqual(
                count, 1, f"PIPE-9: deal {deal_id} is on the board {count} times"
            )
        self.assertEqual(
            board.get(str(self.bystander.pk)),
            1,
            "PIPE-9: a deal nobody touched went missing from the board",
        )
        self.assertEqual(
            self.stage_of(self.bystander),
            bystander_stage_before,
            "PIPE-9: a deal nobody touched changed stage",
        )

        # 4. The column arithmetic still matches the database. Recomputed here
        # rather than read off the page, because the page is what is under test.
        for stage in (FROM_STAGE, TO_STAGE):
            in_stage = Deal.objects.filter(stage=stage)
            total = sum(d.value for d in in_stage)
            weighted = sum(d.weighted_value for d in in_stage)
            head = self.column_head_text(stage)
            foot = self.column_foot_text(stage)
            self.assertIn(f"${total:,.0f}", head, f"PIPE-9: {stage} header total is stale")
            self.assertIn(
                f"· {in_stage.count()}", head, f"PIPE-9: {stage} header count is stale"
            )
            self.assertIn(
                f"Weighted: ${weighted:,.0f}", foot,
                f"PIPE-9: {stage} weighted footer is stale",
            )

        # 5. One move, one audit row.
        self.assertEqual(
            self.history_count(self.moved) - history_before,
            1,
            "PIPE-9: one drag wrote more than one history row — the second POST "
            "saved again, so the audit log now claims the deal moved twice",
        )

        # 6. And really only one request was made.
        self.assertEqual(
            self._moves_posted - posts_before,
            1,
            "PIPE-9: onEnd issued two POSTs for one drag; the second one has to "
            "be refused client-side, not sent and then reconciled",
        )

    def test_a_card_moved_again_after_the_first_landed_still_moves(self):
        """
        The other half of the contract, and the one the fix can break.

        A guard that never clears turns a single move into a permanently dead
        card, which is a worse bug than the one it fixes. So: drop once, let the
        board settle, then drop the SAME deal again and prove it saved. The
        second drop is also a double invocation on purpose — after a settled
        board the two calls are as legitimate as the first pair were, and a guard
        that only ever lets one move through per page load would pass the first
        test and fail here.
        """
        first = fire_double_on_end(
            self.page, self.moved.pk, str(FROM_STAGE), str(TO_STAGE)
        )
        self.assertTrue(first["ok"], f"could not reach the handler: {first}")
        self.settle()
        self.moved.refresh_from_db()
        self.assertEqual(self.moved.stage, TO_STAGE, "the first move did not land")

        history_before = self.history_count(self.moved)
        second = fire_double_on_end(
            self.page, self.moved.pk, str(TO_STAGE), str(FROM_STAGE)
        )
        self.assertTrue(second["ok"], f"could not reach the handler again: {second}")
        self.settle()

        self.assertEqual(
            self.history_count(self.moved) - history_before,
            1,
            "PIPE-9: a settled card took a second deliberate move and it did not "
            "save exactly once — the in-flight guard is never clearing, which "
            "turns one move into a dead card",
        )
        self.moved.refresh_from_db()
        self.assertEqual(self.moved.stage, FROM_STAGE)
        self.assertEqual(self.card_column(self.moved.pk), FROM_STAGE)
        self.assertEqual(self.card_count(self.moved.pk), 1)


@unittest.skipUnless(HAS_PLAYWRIGHT and HAS_CHROMIUM, SKIP_REASON)
class DealDragGestureTests(PipelineBoardBrowserCase):
    """
    Harness check: a real pointer drag still moves a real deal at 375px.

    Not a PIPE-9 assertion. Its job is to prove this harness can drive the
    genuine gesture path at all, so a green double-drop result can never be
    explained by the harness having never dragged anything. If this one fails,
    the other file's results mean nothing, which is exactly why it is here.

    TWO GESTURE TECHNIQUES, ON PURPOSE. SortableJS on desktop Chromium drives
    the drag through the browser's native HTML5 drag-and-drop, and a synthetic
    pointer sequence is not the same thing as a native drag session: Chromium
    hands the input to its drag controller the moment a draggable element is
    pressed and moved, and the rest of the movement stops arriving as mousemove.
    Playwright's `locator.drag_to` handles that interception explicitly;
    `page.mouse` does not. So `drag_to` is tried first, and the plain mouse path
    is kept as a fallback for a build where the interception is unavailable.
    Whichever one works, the assertion is the same — did the deal move, in the
    database — because a gesture that "ran" without moving anything is precisely
    the outcome this test exists to reject.
    """

    def test_a_real_pointer_drag_moves_the_deal_and_saves_it(self):
        card = f'.deal-card[data-deal-id="{self.moved.pk}"]'
        column = f'.kanban-col[data-stage="{TO_STAGE}"] .kanban-col-body'
        history_before = self.history_count(self.moved)
        failures = []

        for label, gesture in (
            ("locator.drag_to", self._drag_to),
            ("page.mouse down/move/up", self._drag_with_mouse),
        ):
            self._open_board()  # a half-finished drag leaves the board mid-gesture
            self._centre_both(card, column)
            try:
                gesture(card, column)
            except (PlaywrightTimeout, PlaywrightError) as exc:
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            self.settle()
            self.moved.refresh_from_db()
            if self.moved.stage != TO_STAGE:
                failures.append(f"{label}: the gesture finished but the deal is still {self.moved.stage}")
                continue
            # Landed. Everything below is about the board agreeing with it.
            self.assertEqual(
                self.card_count(self.moved.pk), 1, "the gesture left two cards for one deal"
            )
            self.assertEqual(
                self.card_column(self.moved.pk), TO_STAGE,
                "the gesture saved to the database but the card is in another column",
            )
            self.assertEqual(
                self.history_count(self.moved) - history_before, 1,
                "one real drag wrote more than one history row",
            )
            return

        self.fail(
            "no synthetic pointer gesture could start a SortableJS drag in this "
            "Chromium build, so the harness cannot vouch for the gesture path "
            "and the PIPE-9 results above are not trustworthy:\n  "
            + "\n  ".join(failures)
        )

    def _drag_to(self, card, column):
        self.page.locator(card).drag_to(self.page.locator(column))

    def _drag_with_mouse(self, card, column):
        source = self.page.locator(card).bounding_box()
        destination = self.page.locator(column).bounding_box()
        self.assertIsNotNone(source, "the card has no box, so there is nothing to drag")
        self.assertIsNotNone(destination, "the destination column has no box")
        start_x = source["x"] + source["width"] / 2
        start_y = source["y"] + source["height"] / 2
        end_x = destination["x"] + destination["width"] / 2
        end_y = destination["y"] + destination["height"] / 2
        self.page.mouse.move(start_x, start_y)
        self.page.mouse.down()
        # Intermediate moves, not one jump: a drag is a gesture, and a single
        # teleport from A to B is a click as far as the browser is concerned.
        for step in range(1, 13):
            self.page.mouse.move(
                start_x + (end_x - start_x) * step / 12,
                start_y + (end_y - start_y) * step / 12,
            )
            self.page.wait_for_timeout(20)
        self.page.mouse.up()