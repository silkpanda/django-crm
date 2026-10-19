"""
Phase 2 tests: pipeline (kanban), tasks (My Day), dashboard metrics, global search,
duplicate warnings, and stage-move authorization.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.dates import local_today_start
from core.models import Activity, Company, Contact, Deal, Stage, Task

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user


class PipelineViewTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")
        self.admin = make_user("admin", "Admin")
        self.company = make_company(owner=self.rep)
        self.deal = make_deal(self.company, owner=self.rep, stage=Stage.PROPOSAL, value=Decimal("1000"))

    def test_pipeline_shows_deals_grouped_by_stage(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Proposal")
        self.assertContains(resp, self.deal.name)

    def test_rep_sees_only_own_deals(self):
        other_deal = make_deal(self.company, owner=self.rep2, name="Other Deal")
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        self.assertContains(resp, self.deal.name)
        self.assertNotContains(resp, other_deal.name)

    def test_admin_sees_all_deals(self):
        other_deal = make_deal(self.company, owner=self.rep2, name="Other Deal")
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        self.assertContains(resp, self.deal.name)
        self.assertContains(resp, other_deal.name)

    def test_stage_move_saves_and_returns_card(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(
            reverse("deal_stage_move", args=[self.deal.pk]),
            {"stage": Stage.NEGOTIATION},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.NEGOTIATION)

    def test_rep_cannot_move_deal_they_dont_own(self):
        other_deal = make_deal(self.company, owner=self.rep2, name="Other Deal")
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(
            reverse("deal_stage_move", args=[other_deal.pk]),
            {"stage": Stage.WON},
        )
        self.assertEqual(resp.status_code, 403)
        other_deal.refresh_from_db()
        self.assertNotEqual(other_deal.stage, Stage.WON)

    def test_stage_move_rejects_invalid_stage(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(
            reverse("deal_stage_move", args=[self.deal.pk]),
            {"stage": "invalid_stage"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_stage_move_requires_post(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("deal_stage_move", args=[self.deal.pk]))
        self.assertEqual(resp.status_code, 405)


class TaskListViewTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep)
        now = timezone.now()
        # Built with the SAME helper the view uses. This line used to copy the
        # view's UTC formula, so fixture and view agreed on the wrong midnight
        # and the test could never fail (REVIEW-25). The clock-pinned tests in
        # LocalDayBoundaryTests are what actually prove the boundary is local.
        today_start = local_today_start(now)
        self.overdue = Task.objects.create(
            title="Overdue task", assignee=self.rep, due_at=today_start - timedelta(days=1)
        )
        self.today_task = Task.objects.create(
            title="Today task", assignee=self.rep, due_at=now
        )
        self.next7 = Task.objects.create(
            title="Next 7 task", assignee=self.rep, due_at=today_start + timedelta(days=3)
        )
        self.no_due = Task.objects.create(
            title="No due date", assignee=self.rep
        )

    def test_buckets_are_correct(self):
        resp = self.client.get(reverse("task_list"))
        self.assertEqual(resp.status_code, 200)
        buckets = resp.context["buckets"]
        self.assertIn(self.overdue, buckets["overdue"])
        self.assertIn(self.today_task, buckets["today"])
        self.assertIn(self.next7, buckets["next7"])
        self.assertIn(self.no_due, buckets["no_due"])

    def test_completed_tasks_excluded(self):
        done = Task.objects.create(
            title="Done task", assignee=self.rep, status=Task.Status.DONE
        )
        resp = self.client.get(reverse("task_list"))
        buckets = resp.context["buckets"]
        all_tasks = list(buckets["overdue"]) + list(buckets["today"]) + list(buckets["next7"]) + list(buckets["no_due"])
        self.assertNotIn(done, all_tasks)

    def test_task_complete_toggles_status(self):
        resp = self.client.post(reverse("task_complete", args=[self.today_task.pk]))
        self.assertEqual(resp.status_code, 302)
        self.today_task.refresh_from_db()
        self.assertEqual(self.today_task.status, Task.Status.DONE)
        # Toggle back
        self.client.post(reverse("task_complete", args=[self.today_task.pk]))
        self.today_task.refresh_from_db()
        self.assertEqual(self.today_task.status, Task.Status.OPEN)

    def test_rep_cannot_complete_others_task(self):
        rep2 = make_user("rep2", "Rep")
        other_task = Task.objects.create(title="Other", assignee=rep2)
        resp = self.client.post(reverse("task_complete", args=[other_task.pk]))
        self.assertEqual(resp.status_code, 403)


CHICAGO = ZoneInfo("America/Chicago")


@override_settings(TIME_ZONE="America/Chicago")
class LocalDayBoundaryTests(TestCase):
    """
    REVIEW-25: "today" must be the office's today, not UTC's.

    TEACHING NOTE — why the clock is pinned. A test that uses the real clock only
    catches this bug if it happens to run during the hours when Chicago and UTC
    disagree about the date (7 pm to midnight), which is how it slipped through.
    `mock.patch` swaps `timezone.now` for a function that returns a fixed moment,
    so every run lands in that window on purpose. Each moment below was picked so
    the old UTC-midnight code puts the task in the WRONG bucket; these tests
    failed against it before the fix.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)

    def at(self, *wall_clock):
        """Pretend it's this Chicago wall-clock time for the rest of the test.

        The fake returns the moment IN UTC, because that's what the real
        `timezone.now()` returns with USE_TZ on. The first version of this
        helper returned it in Chicago time, and the old buggy code passed every
        test: `.replace(hour=0)` on a Chicago datetime happens to give Chicago
        midnight. A fake clock that's friendlier than the real one hides the bug.
        """
        moment = datetime(*wall_clock, tzinfo=CHICAGO).astimezone(dt_timezone.utc)
        patcher = mock.patch("django.utils.timezone.now", return_value=moment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def buckets(self):
        return self.client.get(reverse("task_list")).context["buckets"]

    def test_evening_task_due_tonight_is_today_not_next_week(self):
        # 8 am Chicago on Sep 30. A task due 9 pm tonight is 02:00 UTC on Oct 1,
        # past UTC's "end of today", so the old code filed it under Next 7 days.
        self.at(2026, 9, 30, 8, 0)
        task = Task.objects.create(title="Evening call", assignee=self.rep,
                                   due_at=datetime(2026, 9, 30, 21, 0, tzinfo=CHICAGO))
        buckets = self.buckets()
        self.assertIn(task, buckets["today"])
        self.assertNotIn(task, buckets["next7"])

    def test_this_afternoons_task_is_not_overdue_at_night(self):
        # 11:30 pm Chicago on Sep 30 is already Oct 1 in UTC. A task due at 6 pm
        # today was due TODAY — the old code called it overdue, and the sidebar
        # badge counted it.
        self.at(2026, 9, 30, 23, 30)
        task = Task.objects.create(title="Afternoon follow-up", assignee=self.rep,
                                   due_at=datetime(2026, 9, 30, 18, 0, tzinfo=CHICAGO))
        buckets = self.buckets()
        self.assertIn(task, buckets["today"])
        self.assertNotIn(task, buckets["overdue"])
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["overdue_task_count"], 0)

    def test_closing_this_month_uses_the_local_month(self):
        # 8 pm Chicago on Sep 30 is October in UTC. The dashboard must still
        # show September's deals, not October's.
        self.at(2026, 9, 30, 20, 0)
        company = make_company(owner=self.rep)
        september = make_deal(company, owner=self.rep, stage=Stage.PROPOSAL,
                              expected_close=datetime(2026, 9, 30).date())
        october = make_deal(company, owner=self.rep, stage=Stage.PROPOSAL, name="October deal",
                            expected_close=datetime(2026, 10, 15).date())
        closing = list(self.client.get(reverse("dashboard")).context["closing_this_month"])
        self.assertIn(september, closing)
        self.assertNotIn(october, closing)


class DashboardMetricsTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep)
        now = timezone.now()
        # Built with the SAME helper the view uses. This line used to copy the
        # view's UTC formula, so fixture and view agreed on the wrong midnight
        # and the test could never fail (REVIEW-25). The clock-pinned tests in
        # LocalDayBoundaryTests are what actually prove the boundary is local.
        today_start = local_today_start(now)

        # Deals in various stages
        make_deal(self.company, owner=self.rep, stage=Stage.LEAD, value=Decimal("5000"))
        make_deal(self.company, owner=self.rep, stage=Stage.PROPOSAL, value=Decimal("10000"))
        make_deal(self.company, owner=self.rep, stage=Stage.WON, value=Decimal("8000"), closed_at=now - timedelta(days=30))
        make_deal(self.company, owner=self.rep, stage=Stage.LOST, value=Decimal("3000"), closed_at=now - timedelta(days=60))

        # Deal closing this month (use day 1 to stay within the month)
        make_deal(self.company, owner=self.rep, stage=Stage.NEGOTIATION, value=Decimal("12000"),
                  expected_close=today_start.replace(day=1) + timedelta(days=1))

        # Overdue task
        Task.objects.create(title="Overdue", assignee=self.rep, due_at=today_start - timedelta(days=2))

        # Recent activity
        Activity.objects.create(body="Called the client", company=self.company, author=self.rep, occurred_at=now - timedelta(days=1))

    def test_pipeline_by_stage(self):
        resp = self.client.get(reverse("dashboard"))
        pipeline = resp.context["pipeline_by_stage"]
        self.assertEqual(pipeline[Stage.LEAD]["count"], 1)
        self.assertEqual(pipeline[Stage.PROPOSAL]["count"], 1)
        self.assertEqual(pipeline[Stage.LEAD]["value"], Decimal("5000"))

    def test_closing_this_month(self):
        resp = self.client.get(reverse("dashboard"))
        closing = resp.context["closing_this_month"]
        self.assertEqual(len(closing), 1)
        self.assertEqual(closing[0].value, Decimal("12000"))

    def test_win_rate(self):
        resp = self.client.get(reverse("dashboard"))
        # 1 won out of 2 closed in last 90 days = 50%
        self.assertEqual(resp.context["win_rate"], 50)
        self.assertEqual(resp.context["won_count"], 1)
        self.assertEqual(resp.context["closed_count"], 2)

    def test_overdue_tasks(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["overdue_count"], 1)
        self.assertIn("Overdue", [t.title for t in resp.context["my_overdue_tasks"]])


class GlobalSearchTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep, name="Acme Supply", domain="acme.com")
        self.contact = make_contact(company=self.company, first_name="Pat", last_name="Doe", email="pat@acme.com")
        self.deal = make_deal(self.company, owner=self.rep, name="Plan upgrade")

    def test_search_companies(self):
        resp = self.client.get(reverse("global_search"), {"q": "Acme"})
        self.assertEqual(resp.status_code, 200)
        results = resp.context["results"]
        self.assertTrue(any(r["type"] == "Company" and r["name"] == "Acme Supply" for r in results))

    def test_search_contacts(self):
        resp = self.client.get(reverse("global_search"), {"q": "Pat"})
        results = resp.context["results"]
        self.assertTrue(any(r["type"] == "Contact" for r in results))

    def test_search_deals(self):
        resp = self.client.get(reverse("global_search"), {"q": "Plan"})
        results = resp.context["results"]
        self.assertTrue(any(r["type"] == "Deal" for r in results))

    def test_empty_search(self):
        resp = self.client.get(reverse("global_search"), {"q": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["results"]), 0)


class DuplicateWarningTests(TestCase):
    """
    The duplicate check runs on the real form POST.

    TEACHING NOTE: the old tests here sent the form's fields as a GET query
    string, which is the one request a browser never sends when you click Save,
    so they passed while the feature couldn't fire. These POST, like the page.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep, name="Acme", domain="acme.com")
        self.contact = make_contact(company=self.company, first_name="Pat", last_name="Doe", email="pat@acme.com")

    def _company(self, **extra):
        return self.client.post(reverse("company_create"), {"name": "Acme 2", "domain": "ACME.com ", "lifecycle": "lead", **extra})

    def test_same_domain_warns_and_creates_nothing(self):
        resp = self._company()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertContains(resp, self.company.get_absolute_url())
        self.assertContains(resp, 'value="Acme 2"')  # the user's input survives the warning
        self.assertEqual(Company.objects.count(), 1)

    def test_create_anyway_creates_it(self):
        resp = self._company(confirm_duplicate="1")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Company.objects.filter(domain="acme.com").count(), 2)

    def test_no_domain_falls_back_to_name(self):
        self.client.post(reverse("company_create"), {"name": "acme", "lifecycle": "lead"})
        self.assertEqual(Company.objects.count(), 1)

    def test_unique_company_saves_without_a_warning(self):
        resp = self.client.post(reverse("company_create"), {"name": "Unique", "domain": "unique.com", "lifecycle": "lead"})
        self.assertEqual(resp.status_code, 302)

    def test_contact_same_name_warns(self):
        resp = self.client.post(reverse("contact_create"), {"first_name": "pat", "last_name": "DOE", "lifecycle": "lead"})
        self.assertContains(resp, "already exists")
        self.assertEqual(Contact.objects.count(), 1)

    def test_contact_same_email_is_a_hard_error_not_a_warning(self):
        resp = self.client.post(reverse("contact_create"),
                                {"first_name": "Other", "email": "pat@acme.com", "lifecycle": "lead", "confirm_duplicate": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Contact.objects.count(), 1)


class TaskCreateViewTests(TestCase):
    """
    POST to the task form, the way the browser does.

    TEACHING NOTE: every task test above builds its tasks with the ORM, so none
    of them ever ran the form's save() — which crashed on every call (REVIEW-10).
    Creating records through the ORM tests the model; only a POST tests the page.
    """

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")

    def test_rep_task_is_assigned_to_the_rep(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(reverse("task_create"), {"title": "Call back", "priority": "normal"})
        self.assertRedirects(resp, reverse("task_list"))
        self.assertEqual(Task.objects.get(title="Call back").assignee, self.rep)

    def test_rep_cannot_smuggle_an_assignee(self):
        self.client.login(username="rep", password=PASSWORD)
        self.client.post(reverse("task_create"), {"title": "Smuggled", "priority": "normal", "assignee": self.rep2.pk})
        self.assertEqual(Task.objects.get(title="Smuggled").assignee, self.rep)

    def test_admin_can_assign_to_someone_else(self):
        self.client.login(username="admin", password=PASSWORD)
        self.client.post(reverse("task_create"), {"title": "For rep2", "priority": "normal", "assignee": self.rep2.pk})
        self.assertEqual(Task.objects.get(title="For rep2").assignee, self.rep2)

    def test_admin_blank_assignee_defaults_to_admin(self):
        self.client.login(username="admin", password=PASSWORD)
        self.client.post(reverse("task_create"), {"title": "Mine", "priority": "normal"})
        self.assertEqual(Task.objects.get(title="Mine").assignee, self.admin)

    def test_task_from_a_company_page_returns_there(self):
        company = make_company(owner=self.rep)
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(reverse("task_create"), {"title": "Site visit", "priority": "normal", "company": company.pk})
        self.assertRedirects(resp, company.get_absolute_url())

    def test_new_task_lands_on_my_day(self):
        self.client.login(username="rep", password=PASSWORD)
        self.client.post(reverse("task_create"), {"title": "Shows up", "priority": "normal"})
        self.assertContains(self.client.get(reverse("task_list")), "Shows up")


class BoardLostReasonTests(TestCase):
    """
    REVIEW-07: the board enforces the same rules as the edit form.

    TEACHING NOTE: `deal.save()` skips `clean()`, so the board used to mark
    deals Lost with no reason. These POST the way the board's JavaScript does
    (with the HX-Request header HTMX adds) and check the database.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.deal = make_deal(make_company(owner=self.rep), owner=self.rep, stage=Stage.PROPOSAL)
        self.url = reverse("deal_stage_move", args=[self.deal.pk])

    def test_lost_without_a_reason_is_refused(self):
        resp = self.client.post(self.url, {"stage": "lost"}, HTTP_HX_REQUEST="true")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("reason", resp.content.decode())
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.PROPOSAL)

    def test_lost_with_a_reason_saves_both(self):
        self.client.post(self.url, {"stage": "lost", "lost_reason": "Went with ADT"}, HTTP_HX_REQUEST="true")
        self.deal.refresh_from_db()
        self.assertEqual((self.deal.stage, self.deal.lost_reason), (Stage.LOST, "Went with ADT"))
        self.assertIsNotNone(self.deal.closed_at)

    def test_without_htmx_the_refusal_is_a_message_not_a_422(self):
        resp = self.client.post(self.url, {"stage": "lost"})
        self.assertRedirects(resp, reverse("pipeline"), fetch_redirect_response=False)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.PROPOSAL)

    def test_returned_card_can_be_dragged_again(self):
        """The swapped-in card must still carry its move form."""
        resp = self.client.post(self.url, {"stage": "negotiation"}, HTTP_HX_REQUEST="true")
        self.assertContains(resp, 'class="deal-move-form"')
        self.assertContains(resp, 'name="lost_reason"')
