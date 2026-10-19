"""
Batch B — tasks: failing tests for QA-05, QA-06, QA-08 and QA-15.

Batch B is the tasks half of the QA sweep, and all four findings turned out to
point at the same object:

  QA-05  A task is write-once. No edit, no delete, and a tick on My Day is
         permanent — My Day lists `status=OPEN` only, so the reopen toggle in
         task_list.html can never render.
  QA-06  Tasks are invisible on the record pages they hang off. Creating a task
         from a deal bounces you back to the deal page, which shows no sign of
         it, and an Admin cannot see anyone else's tasks anywhere.
  QA-08  TaskForm has no clean(), so one task can point at a company and a deal
         that belongs to a different company at the same time.
  QA-15  The complete checkbox is a 22x22px target sitting next to a mistake
         that currently cannot be undone. The guideline is 44x44.

THESE TESTS WERE WRITTEN RED FIRST. Every assertion below describes behaviour
the app does not have yet, on purpose: the tests are the spec and the fix is a
later, separate job that turns them green. Nothing in this file changes the
app. A test that passes before the fix is not automatically suspicious — the
reopen toggle and the open-redirect guard are two of them, and both are load-
bearing — but each one should be a deliberate claim about what already works.

Tests that use `task_update` or `task_delete` currently ERROR with
NoReverseMatch, because those two URL names don't exist yet. That is the
expected red. The URLs are spelled with reverse() rather than hardcoded paths
so the fix can't quietly land somewhere else.

`db.sqlite3` is a live demo database and is never touched here: Django builds
an empty test database at the start of every run and drops it at the end. Run
this file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_tasks_qa`.
"""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Task

from .factories import PASSWORD, make_company, make_contact, make_deal, make_task, make_user


# ------------------------------------------------------------------ QA-06
# Tasks are invisible on the records they hang off.
#
# One fixture serves the whole class, because the four tasks differ only in
# which record they point at — and that link is the entire finding. T1 is on the
# deal, T2 on the contact, T3 on the company alone, T4 on the deal again but
# finished. If the panel filtered by status as well as by record, T4 is what
# catches it.


class TaskRecordPageTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.contact = make_contact(owner=self.rep, company=self.company)
        self.deal = make_deal(self.company, owner=self.rep)

        self.deal_task = make_task(assignee=self.rep, title="Send the signed order", deal=self.deal)
        self.contact_task = make_task(assignee=self.rep, title="Confirm the site address", contact=self.contact)
        self.company_task = make_task(assignee=self.rep, title="Review the renewal quote", company=self.company)
        self.done_task = make_task(
            assignee=self.rep,
            title="Chase the invoice",
            deal=self.deal,
            status=Task.Status.DONE,
        )
        self.client.login(username=self.rep.username, password=PASSWORD)

    def test_deal_page_lists_only_that_deals_open_tasks(self):
        resp = self.client.get(self.deal.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "open_tasks",
            resp.context.keys(),
            "QA-06: the deal page puts no task list in the template context",
        )
        self.assertEqual(list(resp.context["open_tasks"]), [self.deal_task])
        self.assertNotIn(self.done_task, resp.context["open_tasks"])
        self.assertContains(resp, self.deal_task.title)

    def test_contact_page_lists_only_that_contacts_open_tasks(self):
        resp = self.client.get(self.contact.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "open_tasks",
            resp.context.keys(),
            "QA-06: the contact page puts no task list in the template context",
        )
        self.assertEqual(list(resp.context["open_tasks"]), [self.contact_task])
        self.assertContains(resp, self.contact_task.title)

    def test_company_page_collects_tasks_from_its_deals_and_contacts(self):
        # A task on one of the company's deals, or one of its contacts, belongs
        # on the company page too — that is the page people actually land on
        # after creating a task, and it is where the task disappears today.
        resp = self.client.get(self.company.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "open_tasks",
            resp.context.keys(),
            "QA-06: the company page puts no task list in the template context",
        )
        open_tasks = list(resp.context["open_tasks"])
        self.assertIn(self.deal_task, open_tasks)
        self.assertIn(self.contact_task, open_tasks)
        self.assertIn(self.company_task, open_tasks)
        self.assertNotIn(self.done_task, open_tasks)

    def test_deal_page_offers_an_add_task_link_carrying_the_deal(self):
        expected = f"{reverse('task_create')}?deal={self.deal.pk}"
        resp = self.client.get(self.deal.get_absolute_url())
        self.assertContains(
            resp,
            expected,
            msg_prefix="QA-06: no 'add a task' link that pre-fills the deal",
        )

    def test_completing_from_a_record_page_returns_to_that_record(self):
        deal_url = self.deal.get_absolute_url()
        url = reverse("task_complete", args=[self.deal_task.pk])
        resp = self.client.post(url, {"next": deal_url})
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, deal_url, fetch_redirect_response=False)

    def test_next_may_not_redirect_off_site(self):
        # A hand-POSTed `next` is user input. Without a check it is an open
        # redirect: a link that leaves through your own domain, signed by you.
        other = make_task(assignee=self.rep, title="Ring the installer", deal=self.deal)
        resp = self.client.post(
            reverse("task_complete", args=[other.pk]),
            {"next": "https://evil.example/"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertRedirects(resp, reverse("task_list"), fetch_redirect_response=False)

    def test_viewer_sees_the_task_panel_with_no_controls(self):
        viewer = make_user("viewer", "Viewer")
        self.client.login(username=viewer.username, password=PASSWORD)
        resp = self.client.get(self.deal.get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.deal_task.title)
        self.assertNotContains(resp, reverse("task_complete", args=[self.deal_task.pk]))
        self.assertNotContains(resp, reverse("task_update", args=[self.deal_task.pk]))


# ------------------------------------------------------------------ QA-05
# A task is write-once: no edit, no delete, nothing to reopen it from, and a
# typo in the title is permanent.


class TaskEditDeleteReopenTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")
        self.viewer = make_user("viewer", "Viewer")
        self.task = make_task(assignee=self.rep, title="Follow up", due_at=timezone.now())

    def login(self, user):
        self.client.login(username=user.username, password=PASSWORD)

    def payload(self, title):
        return {
            "title": title,
            "due_at": "",
            "priority": Task.Priority.NORMAL,
            "company": "",
            "contact": "",
            "deal": "",
        }

    def test_assignee_can_edit_their_task(self):
        self.login(self.rep)
        url = reverse("task_update", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, self.payload("Renamed"))
        self.assertEqual(resp.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Renamed")

    def test_another_rep_cannot_edit_someone_elses_task(self):
        self.login(self.rep2)
        url = reverse("task_update", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.payload("Hijacked")).status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Follow up")

    def test_viewer_cannot_edit_a_task(self):
        self.login(self.viewer)
        url = reverse("task_update", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.payload("Hijacked")).status_code, 403)

    def test_admin_can_edit_anyones_task(self):
        self.login(self.admin)
        url = reverse("task_update", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, self.payload("Renamed by admin"))
        self.assertEqual(resp.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Renamed by admin")

    def test_assignee_can_delete_their_task_and_nobody_else_can(self):
        self.login(self.rep)
        url = reverse("task_delete", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 200, "GET is the confirm page, not the delete")
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Task.objects.filter(pk=self.task.pk).exists())

        kept = make_task(assignee=self.rep, title="Second task")
        self.login(self.rep2)
        self.assertEqual(self.client.post(reverse("task_delete", args=[kept.pk])).status_code, 403)
        self.login(self.viewer)
        self.assertEqual(self.client.post(reverse("task_delete", args=[kept.pk])).status_code, 403)
        self.assertTrue(Task.objects.filter(pk=kept.pk).exists())

    def test_my_day_lists_what_was_completed_in_the_last_seven_days(self):
        # Without this, a task ticked by mistake is gone from the only screen
        # that showed it, with no way to see it, let alone reopen it.
        today_done = make_task(assignee=self.rep, title="Finished today", status=Task.Status.DONE)
        old_done = make_task(assignee=self.rep, title="Finished long ago", status=Task.Status.DONE)
        Task.objects.filter(pk=old_done.pk).update(completed_at=timezone.now() - timedelta(days=10))

        self.login(self.rep)
        resp = self.client.get(reverse("task_list"))
        self.assertIn(
            "done_recent",
            resp.context.keys(),
            "QA-05: My Day has no recently-completed section",
        )
        done_recent = list(resp.context["done_recent"])
        self.assertIn(today_done, done_recent)
        self.assertNotIn(old_done, done_recent)

    def test_reopening_returns_the_task_to_the_open_buckets(self):
        done = make_task(assignee=self.rep, title="Mistaken tick", status=Task.Status.DONE)
        self.login(self.rep)
        resp = self.client.post(reverse("task_complete", args=[done.pk]))
        self.assertEqual(resp.status_code, 302)
        done.refresh_from_db()
        self.assertEqual(done.status, Task.Status.OPEN)
        self.assertIsNone(done.completed_at)

        buckets = self.client.get(reverse("task_list")).context["buckets"]
        open_tasks = [t for bucket in buckets.values() for t in bucket]
        self.assertIn(done, open_tasks)

    def test_only_an_admin_my_day_carries_the_team_bucket(self):
        # An Admin assigns work to people and then has no way to see it: My Day
        # is filtered to `assignee=user` and nothing else exists.
        self.login(self.rep)
        rep_ctx = self.client.get(reverse("task_list")).context
        self.assertFalse(
            rep_ctx.get("team_open"),
            "QA-05: a Rep must not see other people's tasks on My Day",
        )

        self.login(self.admin)
        admin_ctx = self.client.get(reverse("task_list")).context
        self.assertIn(
            "team_open",
            admin_ctx.keys(),
            "QA-05: an Admin's My Day cannot show anyone else's open tasks",
        )
        self.assertIn(self.task, list(admin_ctx["team_open"]))


# ------------------------------------------------------------------ QA-08
# A task can point at a company and a deal that belongs to a different one.
# DealForm.clean already refuses that mismatch on a deal's primary contact;
# TaskForm has no clean() at all.


class TaskFormConsistencyTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.other_company = make_company(owner=self.rep, name="Unrelated Holdings")
        self.contact = make_contact(owner=self.rep, company=self.company)
        self.deal = make_deal(self.company, owner=self.rep)
        self.client.login(username=self.rep.username, password=PASSWORD)

    def payload(self, **overrides):
        data = {
            "title": "New task",
            "due_at": "",
            "priority": Task.Priority.NORMAL,
            "company": "",
            "contact": "",
            "deal": "",
        }
        data.update(overrides)
        return data

    def assert_rejected(self, **overrides):
        resp = self.client.post(reverse("task_create"), self.payload(**overrides))
        self.assertEqual(resp.status_code, 200, "QA-08: the form saved instead of complaining")
        self.assertTrue(
            resp.context["form"].errors,
            msg="QA-08: mismatched company/deal/contact saved without a form error",
        )
        self.assertEqual(Task.objects.count(), 0, "QA-08: a task was created anyway")

    def test_deal_from_another_company_is_rejected(self):
        self.assert_rejected(company=self.other_company.pk, deal=self.deal.pk)

    def test_contact_from_another_company_is_rejected(self):
        self.assert_rejected(company=self.other_company.pk, contact=self.contact.pk)

    def test_a_lone_deal_fills_in_its_own_company(self):
        resp = self.client.post(reverse("task_create"), self.payload(deal=self.deal.pk))
        self.assertEqual(resp.status_code, 302)
        task = Task.objects.get()
        self.assertEqual(task.company_id, self.company.pk)

    def test_a_lone_contact_fills_in_its_own_company(self):
        resp = self.client.post(reverse("task_create"), self.payload(contact=self.contact.pk))
        self.assertEqual(resp.status_code, 302)
        task = Task.objects.get()
        self.assertEqual(task.company_id, self.company.pk)

    def test_a_filled_in_company_is_still_checked_against_the_contact(self):
        # Found in review, not by the first draft of this file: with the company
        # left blank, the fill from the deal ran AFTER the contact check, so a
        # deal at Acme plus a contact at Unrelated Holdings saved silently.
        stranger = make_contact(owner=self.rep, company=self.other_company)
        resp = self.client.post(reverse("task_create"), self.payload(deal=self.deal.pk, contact=stranger.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("contact", resp.context["form"].errors)
        self.assertFalse(Task.objects.exists())


# ------------------------------------------------------------------ QA-15
# Touch targets. 44x44 is the guideline; the complete checkbox is 22x22 and
# sits next to a mistake that QA-05 makes permanent.


class TaskTouchTargetTests(SimpleTestCase):
    def test_task_checkbox_meets_the_44px_touch_target(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
        rule = re.search(r"\.task-check-btn\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "QA-15: no .task-check-btn rule in static/css/app.css")
        body = rule.group(1)
        for prop in ("min-width", "min-height"):
            found = re.search(rf"{prop}\s*:\s*([\d.]+)px", body)
            self.assertIsNotNone(
                found,
                msg=f"QA-15: .task-check-btn sets no {prop}, so the hit area is the glyph",
            )
            self.assertGreaterEqual(
                float(found.group(1)),
                44,
                msg=f"QA-15: {prop} is {found.group(1)}px, the guideline is 44px",
            )