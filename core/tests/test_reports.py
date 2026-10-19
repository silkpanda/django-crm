"""
Reports — the numbers, and the arguments they settle.

TEACHING NOTE — these tests exist because a report is only as trustworthy as the
definition behind it, and "of the deals that ever reached stage A, how many moved
on" is the kind of sentence two people can implement two different ways and both
believe. Each test here names the definition it is pinning.

THE ONE THAT MATTERS MOST is `test_conversion_uses_history_not_current_stage`.
A conversion rate computed from `Deal.stage` — the obvious implementation — is
wrong in a specific, flattering way: its denominator is only the deals you still
have, so the rate climbs as the pipeline empties, and it reads as a real
improvement when nothing about anybody's performance changed. The test builds the
exact dataset where the two implementations disagree and asserts the honest
number.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from core.models import Company, Deal, Stage
from core.reports import (
    forecast,
    pipeline_by_stage,
    report_for,
    rep_performance,
    stage_conversion,
)

from .factories import PASSWORD, make_deal, make_user


class PipelineByStageTests(TestCase):
    def test_every_stage_appears_even_at_zero(self):
        """A row that vanishes when a column empties is a row that gets screenshotted."""
        rows = pipeline_by_stage([])
        for stage in Stage:
            self.assertIn(stage, rows)
            self.assertEqual(rows[stage]["count"], 0)
            self.assertEqual(rows[stage]["value"], Decimal("0"))

    def test_counts_value_and_weighted_per_stage(self):
        company = Company.objects.create(name="Acme", domain="acme.example")
        make_deal(company=company, name="One", stage=Stage.PROPOSAL, value=Decimal("1000.00"), probability=50)
        make_deal(company=company, name="Two", stage=Stage.PROPOSAL, value=Decimal("3000.00"), probability=50)
        rows = pipeline_by_stage(Deal.objects.all())
        self.assertEqual(rows[Stage.PROPOSAL]["count"], 2)
        self.assertEqual(rows[Stage.PROPOSAL]["value"], Decimal("4000.00"))
        # 1000 and 3000 both at 50% is 500 + 1500.
        self.assertEqual(rows[Stage.PROPOSAL]["weighted_value"], Decimal("2000.00"))

    def test_weighted_uses_the_deals_own_probability_when_it_has_one(self):
        company = Company.objects.create(name="Acme")
        make_deal(company=company, stage=Stage.LEAD, value=Decimal("1000.00"), probability=90)
        rows = pipeline_by_stage(Deal.objects.all())
        self.assertEqual(rows[Stage.LEAD]["weighted_value"], Decimal("900.00"))


class StageConversionTests(TestCase):
    """
    The report where the definition matters, so the fixtures are built to make the
    wrong implementation give a visibly different answer.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.deals = []

    def _deal(self, name, **kwargs):
        deal = make_deal(company=self.company, name=name, **kwargs)
        self.deals.append(deal)
        return deal

    def _advance(self, deal, stage):
        """Move a deal and save, which is what writes the history row."""
        deal.stage = stage
        deal.save()

    def test_a_brand_new_deal_has_reached_its_starting_stage(self):
        self._deal("One", stage=Stage.LEAD)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.LEAD]["reached"], 1)
        self.assertEqual(rows[Stage.LEAD]["advanced"], 0)
        self.assertEqual(rows[Stage.LEAD]["rate"], 0.0)

    def test_conversion_counts_deals_that_advanced(self):
        advanced = self._deal("Advanced", stage=Stage.LEAD)
        self._deal("Stuck", stage=Stage.LEAD)
        self._advance(advanced, Stage.QUALIFIED)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.LEAD]["reached"], 2)
        self.assertEqual(rows[Stage.LEAD]["advanced"], 1)
        self.assertEqual(rows[Stage.LEAD]["rate"], 0.5)

    def test_a_deal_that_skipped_a_stage_did_not_advance_from_it(self):
        """
        A deal that went Lead -> Negotiation never stood in Proposal. Counting it
        as having advanced from Proposal inflates that row with deals that
        skipped it, and the funnel stops being a funnel.
        """
        jumper = self._deal("Jumper", stage=Stage.LEAD)
        self._advance(jumper, Stage.NEGOTIATION)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.LEAD]["advanced"], 1)
        self.assertEqual(rows[Stage.PROPOSAL]["reached"], 0)

    def test_conversion_uses_history_not_current_stage(self):
        """
        THE TEST. Two deals reached Proposal. One is sitting in Won; the other was
        closed out and DELETED, and so no longer appears in `Deal.objects` at all.

        Built from current stages, the rate would be 1 of 1 — a perfect 100% —
        because the deal that went nowhere is no longer in the table to be
        counted. Built from history, both are in the denominator and the rate is
        50%. The second number is the honest one, and it is the one this repo
        ships.
        """
        survivor = self._deal("Survivor", stage=Stage.PROPOSAL)
        self._advance(survivor, Stage.WON)
        doomed = self._deal("Doomed", stage=Stage.PROPOSAL)
        doomed.delete()

        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.PROPOSAL]["reached"], 2, "both deals reached Proposal, history says so")
        self.assertEqual(rows[Stage.PROPOSAL]["advanced"], 1)
        self.assertEqual(rows[Stage.PROPOSAL]["rate"], 0.5)

        # And the naive answer is demonstrably different, so this test would fail
        # if someone swapped the implementation back.
        naive_denominator = Deal.objects.filter(stage__in=[Stage.PROPOSAL, Stage.WON]).count()
        self.assertEqual(naive_denominator, 1)

    def test_a_deal_that_went_backwards_counts_once(self):
        """Bounced Qualified -> Proposal -> Qualified is still one deal that reached Qualified."""
        deal = self._deal("Bouncer", stage=Stage.QUALIFIED)
        self._advance(deal, Stage.PROPOSAL)
        self._advance(deal, Stage.QUALIFIED)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.QUALIFIED]["reached"], 1)
        # It did reach Proposal, so it HAS advanced from Qualified.
        self.assertEqual(rows[Stage.QUALIFIED]["advanced"], 1)

    def test_the_last_stage_reports_zero_advanced_because_nothing_follows_it(self):
        """
        Won is the last stage in the default list, so nothing can advance past it.

        Its rate is 0.0 rather than a dash: the numerator is a real, empty
        measurement, not a missing one. A dash here would imply we cannot say,
        when what we mean is "nothing moved on, because there is nowhere to move
        to" — which is worth stating plainly instead of hiding behind a blank.
        """
        self._deal("Won", stage=Stage.WON)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertEqual(rows[Stage.WON]["reached"], 1)
        self.assertEqual(rows[Stage.WON]["advanced"], 0)
        self.assertEqual(rows[Stage.WON]["rate"], 0.0)

    def test_a_stage_with_nobody_in_it_has_no_rate_at_all(self):
        """The dash: a denominator of zero is unanswerable, not zero."""
        self._deal("Just a lead", stage=Stage.LEAD)
        rows = {row["stage"]: row for row in stage_conversion()}
        self.assertIsNone(rows[Stage.NEGOTIATION]["rate"])

    def test_an_empty_history_gives_zero_denominators_and_no_rates(self):
        """A fresh database must render a report, not a ZeroDivisionError."""
        rows = stage_conversion()
        for row in rows:
            self.assertEqual(row["reached"], 0)
            self.assertIsNone(row["rate"])

    def test_since_filters_the_report_to_a_window(self):
        self._deal("Recent", stage=Stage.LEAD)
        self._advance(self.deals[0], Stage.QUALIFIED)
        far_future = Deal.history.latest("history_date").history_date + timedelta(days=30)
        rows = {row["stage"]: row for row in stage_conversion(since=far_future)}
        self.assertEqual(rows[Stage.LEAD]["reached"], 0)


class RepPerformanceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.rep = make_user("rep", "Rep")
        self.other = make_user("rep2", "Rep")

    def test_counts_open_won_and_lost_separately(self):
        make_deal(company=self.company, owner=self.rep, name="Open", stage=Stage.LEAD)
        make_deal(company=self.company, owner=self.rep, name="Won", stage=Stage.WON, value=Decimal("1000.00"))
        make_deal(company=self.company, owner=self.rep, name="Lost", stage=Stage.LOST, value=Decimal("500.00"))
        rows = {row["owner"]: row for row in rep_performance(Deal.objects.select_related("owner"))}
        rep = rows["rep"]
        self.assertEqual(rep["open_deals"], 1)
        self.assertEqual(rep["won"], 1)
        self.assertEqual(rep["lost"], 1)
        self.assertEqual(rep["won_value"], Decimal("1000.00"))
        self.assertEqual(rep["closed"], 2)
        self.assertEqual(rep["win_rate"], 0.5)

    def test_win_rate_is_none_with_nothing_closed_not_zero(self):
        """"—" and "0%" are different answers and the template shows them differently."""
        make_deal(company=self.company, owner=self.rep, name="Open", stage=Stage.LEAD)
        rows = {row["owner"]: row for row in rep_performance(Deal.objects.select_related("owner"))}
        self.assertIsNone(rows["rep"]["win_rate"])

    def test_unowned_deals_are_reported_not_dropped(self):
        """
        A lead from the public form is deliberately left unowned, so there are
        always open deals belonging to nobody and sitting in the queue. Hiding
        them makes every rep look better than the office's actual position.
        """
        make_deal(company=self.company, owner=None, name="Unowned", stage=Stage.LEAD)
        rows = {row["owner"]: row for row in rep_performance(Deal.objects.select_related("owner"))}
        self.assertIn("unassigned", rows)
        self.assertEqual(rows["unassigned"]["open_deals"], 1)


class ForecastTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.rep = make_user("rep", "Rep")

    def test_counts_only_deals_closing_inside_the_horizon(self):
        from django.utils import timezone
        from core.dates import local_today

        today = local_today()
        make_deal(company=self.company, owner=self.rep, name="Soon", stage=Stage.LEAD,
                  expected_close=today + timedelta(days=10))
        make_deal(company=self.company, owner=self.rep, name="Later", stage=Stage.LEAD,
                  expected_close=today + timedelta(days=200))
        rows = forecast(Deal.objects.all())
        self.assertEqual(rows["open_count"], 1)

    def test_undated_deals_are_counted_and_reported_separately(self):
        from core.dates import local_today

        today = local_today()
        make_deal(company=self.company, owner=self.rep, name="Undated", stage=Stage.LEAD, expected_close=None)
        make_deal(company=self.company, owner=self.rep, name="Dated", stage=Stage.LEAD,
                  expected_close=today + timedelta(days=5))
        rows = forecast(Deal.objects.all())
        self.assertEqual(rows["undated"], 1)
        self.assertEqual(rows["open_count"], 2, "the undated deal is in the total, not dropped from it")

    def test_won_value_is_added_to_the_forecast(self):
        make_deal(company=self.company, owner=self.rep, name="Won", stage=Stage.WON, value=Decimal("7000.00"))
        rows = forecast(Deal.objects.all())
        self.assertEqual(rows["won_value"], Decimal("7000.00"))
        self.assertEqual(rows["open_count"], 0)

    def test_lost_value_is_not_a_forecast(self):
        make_deal(company=self.company, owner=self.rep, name="Lost", stage=Stage.LOST, value=Decimal("7000.00"))
        rows = forecast(Deal.objects.all())
        self.assertEqual(rows["won_value"], Decimal("0"))


class ReportForTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.rep = make_user("rep", "Rep")
        self.other = make_user("rep2", "Rep")
        self.admin = make_user("admin", "Admin")

    def test_a_rep_only_sees_their_own_deals_in_their_own_report(self):
        """A report that ignored scope would be a way to read a colleague's win rate."""
        make_deal(company=self.company, owner=self.rep, name="Mine", stage=Stage.LEAD)
        make_deal(company=self.company, owner=self.other, name="Theirs", stage=Stage.LEAD)
        report = report_for(self.rep)
        owners = {row["owner"] for row in report["reps"]}
        self.assertIn("rep", owners)
        self.assertNotIn("rep2", owners)

    def test_an_admin_sees_the_whole_office(self):
        make_deal(company=self.company, owner=self.rep, name="Mine", stage=Stage.LEAD)
        make_deal(company=self.company, owner=self.other, name="Theirs", stage=Stage.LEAD)
        report = report_for(self.admin)
        owners = {row["owner"] for row in report["reps"]}
        self.assertEqual(owners, {"rep", "rep2"})

    def test_the_report_contains_all_four_sections(self):
        make_deal(company=self.company, owner=self.rep, name="One", stage=Stage.LEAD)
        report = report_for(self.rep)
        self.assertEqual(
            set(report), {"pipeline", "conversion", "reps", "forecast"}
        )

    def test_the_conversion_funnel_is_scoped_too(self):
        """
        REVIEW-31. `report_for()` scoped three of its four sections with one
        `scoped_deals()` call and then called `stage_conversion()` with no
        arguments at all, so the funnel was built from `Deal.history.all()` — every
        deal the office has ever had — on a page whose other three tables were
        correctly scoped.

        `ReportForTests.test_a_rep_only_sees_their_own_deals_in_their_own_report`
        did not catch it because it only looks at the `reps` section. This one
        walks both reps' deals all the way to Won and asserts the whole funnel.
        Each walk is a real `save()`, never `.update()` — `.update()` writes the
        row without writing history, so a funnel built from history would never
        hear about it, which is the entire subject of this report.
        """
        for deal in (make_deal(company=self.company, owner=self.rep, name="Mine", stage=Stage.LEAD),
                     make_deal(company=self.company, owner=self.other, name="Theirs", stage=Stage.LEAD)):
            deal.stage = Stage.WON
            deal.save()

        mine = {row["stage"]: row for row in report_for(self.rep)["conversion"]}
        theirs = {row["stage"]: row for row in report_for(self.other)["conversion"]}
        admin = {row["stage"]: row for row in report_for(self.admin)["conversion"]}

        self.assertEqual((mine[Stage.LEAD]["reached"], mine[Stage.WON]["reached"]), (1, 1))
        self.assertEqual(mine[Stage.LEAD]["advanced"], 1)
        self.assertEqual(theirs[Stage.LEAD]["advanced"], 1)
        self.assertEqual(
            (theirs[Stage.WON]["reached"], theirs[Stage.WON]["advanced"]),
            (1, 0),
            "nothing follows Won, so nothing advances out of it",
        )
        self.assertEqual(
            (admin[Stage.WON]["reached"], admin[Stage.LEAD]["reached"]),
            (2, 2),
            "the Admin still sees the whole office",
        )

    def test_a_deleted_deal_leaves_the_funnel(self):
        """
        The second, quieter half of REVIEW-31. `Deal.history` keeps every row it
        ever wrote, and a hard delete writes a `history_type='-'` row without
        removing the `+` and `~` rows before it. Filtering the history by the
        caller's deal ids drops those orphans as a side effect, so a deal that no
        longer exists stops inflating every conversion number in the app.

        Asserted with the deal still present and then gone, so the test cannot
        pass by the funnel simply being empty.
        """
        doomed = make_deal(company=self.company, owner=self.rep, name="Doomed", stage=Stage.WON)
        before = {row["stage"]: row["reached"] for row in report_for(self.rep)["conversion"]}
        self.assertEqual(before[Stage.WON], 1)

        Deal.objects.filter(pk=doomed.pk).delete()

        after = {row["stage"]: row["reached"] for row in report_for(self.rep)["conversion"]}
        self.assertEqual(after[Stage.WON], 0, "a deleted deal's stages stayed in the funnel")
        self.assertEqual(after[Stage.LEAD], 0)

class ReportPageTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.company = Company.objects.create(name="Acme")
        make_deal(company=self.company, owner=self.admin, name="One", stage=Stage.PROPOSAL, value=Decimal("1000.00"))

    def test_an_admin_can_see_the_page(self):
        self.client.login(username="admin", password=PASSWORD)
        response = self.client.get("/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pipeline by stage")
        self.assertContains(response, "Stage conversion")

    def test_a_rep_can_see_their_own_report(self):
        rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.assertEqual(self.client.get("/reports/").status_code, 200)

    def test_the_page_does_not_print_a_colleagues_conversion_numbers(self):
        """
        REVIEW-31, at the level the user sees. `ReportForTests` pins the function;
        this pins the rendered page, because a report is a thing people read off a
        screen rather than a dict they call.

        The Rep owns nothing, so every funnel cell on their page must be zero. The
        Admin's page is read in the same test, so the assertion cannot pass because
        the funnel is broken in both directions at once.
        """
        other = make_user("rep2", "Rep")
        make_user("rep", "Rep")
        make_deal(company=self.company, owner=other, name="Theirs", stage=Stage.WON)
        self.assertTrue(
            any(row["reached"] for row in report_for(self.admin)["conversion"]),
            "the fixture must produce a non-empty funnel or this test proves nothing",
        )

        self.client.login(username="rep", password=PASSWORD)
        rows = self._conversion_cells(self.client.get("/reports/").content.decode())
        self.assertTrue(rows, "the page printed no conversion table at all")
        self.assertEqual(
            [cells for label, cells in rows if label in ("Lead", "Won")],
            [["0", "0"], ["0", "0"]],
            "a Rep with no deals must read an empty funnel, not the office's",
        )

        self.client.logout()
        self.client.login(username="admin", password=PASSWORD)
        admin_rows = dict(self._conversion_cells(self.client.get("/reports/").content.decode()))
        self.assertEqual(admin_rows["Won"][0], "1", "the Admin still sees the whole office")

    @staticmethod
    def _conversion_cells(html):
        """[(stage label, [reached, advanced])] out of the rendered conversion table."""
        start = html.find("Stage conversion")
        table = html[start:html.find("</table>", start)]
        found = []
        for label, rest in re.findall(r"<tr>\s*<td>([^<]*)</td>(.*?)</tr>", table, re.S):
            cells = [value.strip() for value in re.findall(r">([^<>]*)</td>", rest)]
            if cells:
                found.append((re.sub(r"\s+", " ", label).strip(), cells[:2]))
        return found

    def test_an_anonymous_visitor_is_sent_to_login(self):
        response = self.client.get("/reports/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_the_page_renders_on_an_empty_database(self):
        """A brand new install must show a report, not a crash."""
        Deal.objects.all().delete()
        self.client.login(username="admin", password=PASSWORD)
        self.assertEqual(self.client.get("/reports/").status_code, 200)

    def test_a_perfect_win_rate_renders_as_one_hundred_percent(self):
        """
        REGRESSION. `rate` and `win_rate` are fractions (1.0 is 100%), so
        `{{ row.rate|floatformat:0 }}%` renders 1.0 as "1%" — a rep who has won
        everything looks like they have won 1% of everything. The arithmetic is
        right in `core/reports.py` and was wrong on screen, which is exactly the
        kind of defect no unit test on the arithmetic would catch.
        """
        won_only = make_deal(company=self.company, owner=self.admin, name="Clean sweep",
                            stage=Stage.WON, value=Decimal("100.00"))
        Deal.objects.filter(pk=won_only.pk).update(stage=Stage.WON)
        self.client.login(username="admin", password=PASSWORD)
        html = self.client.get("/reports/").content.decode()
        self.assertNotIn("1%", html, "a 1.0 fraction must render as 100%, not 1%")
        self.assertIn("100%", html)

    def test_no_won_and_no_lost_renders_a_dash_not_zero_percent(self):
        make_deal(company=self.company, owner=self.admin, name="Open", stage=Stage.LEAD)
        self.client.login(username="admin", password=PASSWORD)
        html = self.client.get("/reports/").content.decode()
        self.assertIn("&mdash;", html)

    def test_the_page_renders_with_no_users_at_all(self):
        Deal.objects.all().delete()
        self.client.logout()
        self.assertEqual(self.client.get("/reports/").status_code, 302)