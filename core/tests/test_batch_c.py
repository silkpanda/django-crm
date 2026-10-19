"""
Batch C — the timeline and the small UI: failing tests for QA-01, QA-02, QA-10,
QA-11 and QA-15.

Batch B took the Tasks half of the QA sweep. Batch C is the rest of it that hangs
off a record page rather than off a list:

  QA-01  "Nothing logged yet" is a DOM node, so it can be removed but never
         brought back. timeline_panel.html renders the placeholder once, app.js
         deletes it on the first add, and activity_delete returns an empty body
         — so deleting the last entry leaves a blank box with no explanation.
         The empty state has to live in CSS (`:has()`), where it survives every
         swap without any JS being told to put it back.
  QA-02  hx-swap="afterbegin" drops every new entry at the TOP, so a call logged
         today with occurred_at last week lands above today's note. A reload
         re-sorts it into place, which is what makes it read as a bug: the same
         two entries, two different orders, thirty seconds apart.
  QA-10  Global search caps each type at 20 and then reports the length of what
         it kept, so 25 matches read as "20 results" and nothing on the page says
         anything was dropped.
  QA-11  The CSV export leads with owner, lifecycle, source, notes and
         external_id — the inherited OwnedRecord fields — with name further
         right, no id column and no tags at all. And ?page=999 is a bare 404
         instead of the last page.
  QA-15  The timeline's "Remove" button is 47x20px. It sits next to a delete
         that has no undo, and the guideline is 44x44.

THESE TESTS WERE WRITTEN RED FIRST. Every assertion below describes behaviour
the app does not have yet, on purpose: the tests are the spec and the fix is a
later, separate job that turns them green. Nothing in this file changes the app.
Twelve of the thirteen fail right now. The one that passes is deliberate: the
`class="new"` highlight on the entry you just logged is the whole list-swap's
load-bearing detail, and a fix for QA-02 that repaints every row would break it.

Against the app as it stands, the exact red is: ?page=999 AND ?page=abc both
404 (not just the first, as the finding recorded); the companies export leads
with ['owner', 'lifecycle', 'source', 'notes', 'external_id'] and has no tags
column at all; and the HTMX response to a logged entry carries only the new
entry, so a back-dated call never even appears next to the note it should sit
under.

The CSS and JS assertions read the files off disk rather than driving a browser,
so they run in the plain test suite with no Playwright and no headless setup.
They check the SOURCE, not the computed box: the point of QA-01 is that the
empty state is declared in the stylesheet, and the point of QA-15 is that the
hit area is padded rather than the glyph being drawn larger.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_batch_c`.
"""

import csv
import io
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Activity, Tag

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user


# ------------------------------------------------------------------ helpers


def read_static(*parts):
    """Read a file out of the repo's static/ directory as text."""
    return (Path(settings.BASE_DIR) / "static").joinpath(*parts).read_text(encoding="utf-8")


def css_rules(css):
    """
    Every leaf rule in a stylesheet as (selector, body) pairs.

    TEACHING NOTE — the at-rule preludes are dropped first. `re.findall` walks
    the braces from the left, so left in place, `@media (max-width: 720px) {`
    swallows the first rule nested inside it: the media query parses as the
    "selector" and that first rule's selector parses as its "body". Both halves
    are then wrong, and a rule the fix puts inside a media query would be
    invisible to every assertion below. Removing each wrapper's prelude and its
    opening brace leaves the leftover closing braces as separators, which is
    exactly what the selector pattern already treats as one.
    """
    css = re.sub(r"@(?:media|supports|container)[^{]*\{", "", css)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def csv_rows(response):
    """
    The export's rows, as a list of lists of strings.

    `utf-8-sig` because the export writes a byte-order mark for Excel: decoded as
    plain utf-8 the first header cell comes back as "\\ufeffid" and every column
    comparison in QA-11 fails for a reason that has nothing to do with QA-11.
    """
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


# ------------------------------------------------------------------ QA-01
# The empty state is a DOM node, so it can be removed but never restored.


class TimelineEmptyStateMarkupTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.deal = make_deal(self.company, owner=self.rep)
        self.client.login(username=self.rep.username, password=PASSWORD)

    def test_an_empty_timeline_carries_no_placeholder_in_the_markup(self):
        self.assertEqual(Activity.objects.filter(deal=self.deal).count(), 0)
        resp = self.client.get(self.deal.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        # The deal page carries the timeline panel, so both of these are the
        # timeline's own markup and neither may be there: the placeholder is
        # coming from CSS now, where it survives being swapped away.
        self.assertNotContains(
            resp,
            'id="timeline-empty"',
            msg_prefix="QA-01: the placeholder is still a DOM node",
        )
        self.assertNotContains(
            resp,
            "Nothing logged yet",
            msg_prefix="QA-01: the empty state is still server-rendered",
        )


class TimelineEmptyStateAssetTests(SimpleTestCase):
    def test_app_css_draws_the_empty_state_with_a_has_selector(self):
        rules = css_rules(read_static("css", "app.css"))
        hits = [
            body
            for sel, body in rules
            if "#timeline" in sel
            and ":has(" in sel
            and re.search(r"content\s*:\s*[\"'][^\"']*Nothing logged yet", body)
        ]
        self.assertTrue(
            hits,
            msg=(
                "QA-01: no rule in app.css matches `#timeline` with `:has()` and a "
                "`content:` of \"Nothing logged yet\". The empty state has to be CSS: "
                "the server sends the same markup whether the timeline has 0 or 40 "
                "entries, and only CSS can notice the difference between them."
            ),
        )

    def test_app_js_no_longer_knows_about_the_placeholder(self):
        js = read_static("js", "app.js")
        self.assertNotIn(
            "timeline-empty",
            js,
            msg=(
                "QA-01: app.js still reaches for the placeholder element. That is the "
                "finding itself: JS that removes a node on the first add can never put "
                "it back after a delete."
            ),
        )


# ------------------------------------------------------------------ QA-02
# A back-dated entry logged over HTMX has to come back in date order.


class TimelineBackdatedEntryTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.deal = make_deal(self.company, owner=self.rep)
        self.today = timezone.now()
        Activity.objects.create(
            body="today note",
            deal=self.deal,
            author=self.rep,
            occurred_at=self.today,
        )
        self.client.login(username=self.rep.username, password=PASSWORD)

    def post_backdated_call(self):
        """
        POST the call exactly the way the timeline form does.

        The timestamp is the string a `<input type="datetime-local">` submits:
        local wall-clock time, `%Y-%m-%dT%H:%M`. `HTTP_HX_REQUEST` is how the
        test client sends the HX-Request header HTMX would send, which is the
        header activity_create branches on.
        """
        occurred = (timezone.localtime(timezone.now()) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
        return self.client.post(
            reverse("activity_create", args=["deal", self.deal.pk]),
            {"kind": "call", "body": "last week call", "occurred_at": occurred},
            HTTP_HX_REQUEST="true",
        )

    def test_a_back_dated_entry_comes_back_below_the_older_one(self):
        # Odd-sounding name, deliberate: the entry logged FIRST is the OLDER one.
        # "below the older one" is the bug. It should come back below the newer.
        resp = self.post_backdated_call()
        self.assertEqual(Activity.objects.filter(deal=self.deal).count(), 2, "the call did not save")
        html = resp.content.decode()
        self.assertIn("last week call", html)
        self.assertIn(
            "today note",
            html,
            msg=(
                "QA-02: the response carries only the new entry. The timeline is sorted by "
                "occurred_at server-side, so the swap can only put a back-dated entry in "
                "the right place if the whole sorted list comes back with it."
            ),
        )
        self.assertLess(
            html.index("today note"),
            html.index("last week call"),
            msg="QA-02: the back-dated call is rendered above today's note",
        )

    def test_the_timeline_form_replaces_the_whole_list(self):
        resp = self.client.get(self.deal.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        form = re.search(r"<form\b[^>]*class=\"[^\"]*activity-form[^\"]*\"[^>]*>", html)
        self.assertIsNotNone(form, "no activity form on the deal page")
        tag = form.group(0)
        self.assertIn('hx-target="#timeline"', tag, f"form is: {tag}")
        self.assertIn(
            'hx-swap="innerHTML"',
            tag,
            msg=(
                f"QA-02: the form is {tag} — with `afterbegin` the browser, not the "
                "server, decides where the entry lands, and it always picks the top."
            ),
        )

    def test_only_the_new_entry_is_highlighted_in_the_response(self):
        resp = self.post_backdated_call()
        html = resp.content.decode()
        self.assertEqual(
            html.count('class="new"'),
            1,
            msg=(
                "QA-02: exactly one entry may carry the `new` highlight — the one just "
                "logged. Counting it also catches a swap that would mark every entry in "
                "the list, which would repaint the whole timeline on every keystroke."
            ),
        )


# ------------------------------------------------------------------ QA-10
# Search reports the length of what it kept, not how many there were.


class SearchCapHonestyTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username=self.rep.username, password=PASSWORD)

    def search(self, q):
        resp = self.client.get(reverse("global_search"), {"q": q})
        self.assertEqual(resp.status_code, 200)
        return resp

    def assert_reports(self, resp, q, total, truncated):
        self.assertIn(
            "total",
            resp.context.keys(),
            "QA-10: the search view reports no true count, so the page can only quote the cap",
        )
        self.assertIn(
            "truncated",
            resp.context.keys(),
            "QA-10: the search view reports no truncation flag, so the page cannot say why it stopped",
        )
        self.assertEqual(resp.context["total"], total, msg=f"QA-10: wrong total for {q!r}")
        self.assertIs(resp.context["truncated"], truncated)

    def test_more_matches_than_the_cap_reports_both_numbers(self):
        for i in range(25):
            make_company(owner=self.rep, name=f"Bulk {i:02d}")

        resp = self.search("Bulk")
        self.assert_reports(resp, "Bulk", 25, True)
        self.assertContains(resp, "25 results")
        self.assertContains(
            resp,
            "first 20",
            msg_prefix="QA-10: the page never admits it stopped at 20",
        )

    def test_a_short_list_reports_no_cap_at_all(self):
        for i in range(3):
            make_company(owner=self.rep, name=f"Solo {i:02d}")

        resp = self.search("Solo")
        self.assert_reports(resp, "Solo", 3, False)
        self.assertContains(resp, "3 results")
        self.assertNotContains(
            resp,
            "first 20",
            msg_prefix="QA-10: nothing was dropped here, so nothing should claim it was",
        )


# ------------------------------------------------------------------ QA-11
# The export leads with the wrong columns, and an out-of-range page 404s.


class CsvExportColumnOrderTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.client.login(username=self.rep.username, password=PASSWORD)

    def export(self, name):
        resp = self.client.get(reverse(name), {"export": "1"})
        self.assertEqual(resp.status_code, 200)
        return csv_rows(resp)

    def test_company_export_starts_id_name_and_carries_tags(self):
        # Set in reverse alphabetical order on purpose: the export joins them,
        # and it has to join them in a stable order or the same company exports
        # two different ways depending on which row the query hit first.
        alpha = Tag.objects.create(name="Alpha")
        beta = Tag.objects.create(name="Beta")
        tagged = make_company(owner=self.rep, name="Tagged Co")
        tagged.tags.set([beta, alpha])
        make_company(owner=self.rep, name="Untagged Co")

        rows = self.export("company_list")
        header = rows[0]
        self.assertEqual(
            header[:2],
            ["id", "name"],
            msg=(
                f"QA-11: the companies export leads with {header[:5]} — those are the "
                "inherited OwnedRecord fields, so the first thing you cannot identify a "
                "row by is the row."
            ),
        )
        self.assertIn(
            "tags",
            header,
            msg=f"QA-11: no tags column in {header} — the list screen shows tags, the export drops them",
        )
        body = [r for r in rows[1:] if len(r) == len(header) and r[header.index("name")] == "Tagged Co"]
        self.assertEqual(len(body), 1, "QA-11: the tagged company is missing from its own export")
        self.assertEqual(body[0][header.index("tags")], "Alpha; Beta")

    def test_contact_export_starts_id_first_name_last_name_email(self):
        make_contact(owner=self.rep, company=self.company, first_name="Pat", last_name="Doe")

        header = self.export("contact_list")[0]
        self.assertEqual(
            header[:4],
            ["id", "first_name", "last_name", "email"],
            msg=f"QA-11: the contacts export leads with {header[:5]}",
        )

    def test_deal_export_starts_id_name_company(self):
        make_deal(self.company, owner=self.rep, name="Plan upgrade")

        header = self.export("deal_list")[0]
        self.assertEqual(
            header[:3],
            ["id", "name", "company"],
            msg=f"QA-11: the deals export leads with {header[:5]}",
        )


class ListPaginationFallbackTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username=self.rep.username, password=PASSWORD)

    def test_a_bad_page_number_falls_back_instead_of_404ing(self):
        # Two ways to be wrong, one behaviour. `?page=999` is a bookmark somebody
        # kept after rows were deleted; `?page=abc` is a fat-fingered URL. Django
        # raises PageNotAnInteger for the second and lets the Paginator raise
        # EmptyPage for the first, so both arrive as a bare 404 — answering a
        # question the user never asked, on a screen that has rows to show.
        self.assertEqual(
            settings.PAGE_SIZE,
            25,
            "QA-11: this test counts 30 rows against a 25-row page to get a second page",
        )
        for i in range(30):
            make_company(owner=self.rep, name=f"Paged {i:02d}")

        resp = self.client.get(reverse("company_list"), {"page": "999"})
        self.assertEqual(
            resp.status_code,
            200,
            msg="QA-11: a stale ?page= is a bare 404 instead of the last page",
        )
        self.assertEqual(
            resp.context["page_obj"].number,
            2,
            msg="QA-11: past the end should land on the LAST page, not an arbitrary one",
        )

        resp = self.client.get(reverse("company_list"), {"page": "abc"})
        self.assertEqual(resp.status_code, 200, "QA-11: ?page=abc is a 404, not page 1")
        self.assertEqual(resp.context["page_obj"].number, 1)


# ------------------------------------------------------------------ QA-15
# Touch targets. 44x44 is the guideline; the timeline's Remove button is 47x20
# and the delete it fires has no undo.


class TimelineTouchTargetTests(SimpleTestCase):
    def test_the_timeline_remove_button_meets_the_44px_touch_target(self):
        rules = [
            body
            for sel, body in css_rules(read_static("css", "app.css"))
            if ".timeline" in sel and ".btn-link" in sel
        ]
        self.assertTrue(
            rules,
            msg=(
                "QA-15: no rule in app.css targets the timeline's Remove button. The "
                "button is `class=\"btn-link\"` inside `.timeline`, and padding the hit "
                "area (not the glyph) is the fix."
            ),
        )
        heights = []
        for body in rules:
            heights += [float(px) for px in re.findall(r"min-height\s*:\s*([\d.]+)px", body)]
        self.assertTrue(
            heights,
            msg="QA-15: the rule sets no min-height, so the hit area is the glyph",
        )
        self.assertGreaterEqual(
            max(heights),
            44,
            msg=f"QA-15: min-height is {max(heights)}px, the guideline is 44px",
        )

class HeaderTouchTargetTests(SimpleTestCase):
    """QA-15's last third: the header's Password link and Sign out button were
    51x21 and 62x21. Added in review after batch C left them out of scope."""

    def test_header_user_links_meet_the_44px_touch_target(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
        rules = re.findall(r"([^{}]*\.usermenu[^{}]*)\{([^}]*)\}", css)
        hit = [body for sel, body in rules if ".btn-link" in sel and re.search(r"(?<![-\w])a\b", sel)]
        self.assertTrue(hit, "QA-15: no .usermenu rule covering both its links and .btn-link")
        size = re.search(r"min-height\s*:\s*([\d.]+)px", hit[0])
        self.assertIsNotNone(size, "QA-15: the header links set no min-height")
        self.assertGreaterEqual(float(size.group(1)), 44)
