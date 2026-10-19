"""
Phase 6, workstream 2 — notifications: the bell, the list, and the three triggers.

WHAT THIS SPECIFIES
-------------------
A Notification is one thing that happened, addressed to ONE user. Two triggers
write them today — a lead assigned to a Rep, and a task assigned to a Rep — plus
`task_due_tomorrow`, which is a query rather than a trigger and is deliberately
left unwired. A sidebar bell counts the current user's unread rows, the list page
shows them newest first, and two POST endpoints mark them read.

THE THREE URLS
--------------
  notification_list    /notifications/                     login required
  notification_read    /notifications/<pk>/read/           POST, owner only
  notification_read_all /notifications/read-all/           POST, owner only

SECURITY NOTE — the three tests that matter most here are the IDOR ones (8, 9,
10), and they are three tests rather than one because they are three different
shapes of the same bug. A list view with no filter leaks by showing; a mark-read
endpoint that fetches by pk and checks ownership afterwards leaks by existing; a
mark-all-read that forgets the user filter leaks by rewriting. A single test with
subTest would report one failure and hide which of the three broke.

THE SAME SHAPE, THREE TIMES: a notification that is not yours is a 404, never a
403. A 403 confirms the row is real to somebody who should not know that it is.

ON `captureOnCommitCallbacks` — the triggers write through
`transaction.on_commit`, which is the property that stops a rolled-back
assignment leaving a notification claiming it happened. Inside `django.test.TestCase`
the whole test runs in a transaction that never commits, so those writes are held
back until something asks for them. `captureOnCommitCallbacks(execute=True)` is
that ask: it runs the held callbacks when the block closes, and it is what lets a
test assert on a trigger's effect without pretending the deferral isn't there.
Tests 3, 4 and 6 use it deliberately. Test 5 proves the other half — that a
rollback really does discard the pending write.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_notifications`.
"""

from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from core.context_processors import unread_notifications
from core.dates import local_today_start
from core.leads import create_lead
from core.models import Notification, Tag, Task
from core.notifications import lead_assigned, new_lead, task_assigned, task_due_tomorrow

from .factories import PASSWORD, make_company, make_deal, make_task, make_user
from .test_leads import csrf_token, desktop_payload, public_payload


# ------------------------------------------------------------------ helpers


def make_notification(user, text="Something happened", kind=None, url=""):
    """
    A saved Notification, with the fields every test here does not care about
    filled in. Built through the ORM rather than through the triggers because
    these tests are about the READ side — who can see and mark what — and a
    trigger's deferral would only get in the way of saying that.
    """
    return Notification.objects.create(
        user=user, kind=kind or Notification.Kind.TASK_DUE, text=text, url=url
    )


# --------------------------------------------------------------- the model


class NotificationModelTests(TestCase):
    """Spec 1 and 2: what a Notification must carry, and how it sorts."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")

    def test_it_requires_a_user_a_known_kind_and_some_text(self):
        """
        Spec 1. `full_clean()` is what enforces `choices`, so these are
        ValidationErrors from the model rather than database errors — which is
        the point: a rule that only the database catches has already lost the
        value by the time somebody reads the traceback.
        """
        valid = {"user": self.rep, "kind": Notification.Kind.LEAD_ASSIGNED, "text": "A lead"}
        Notification(**valid).full_clean()

        for field, bad in (
            ("user", None),
            ("kind", "invented_later"),
            ("text", ""),
        ):
            with self.subTest(field=field):
                broken = dict(valid, **{field: bad})
                with self.assertRaises(ValidationError, msg=f"{field}={bad!r} should not validate"):
                    Notification(**broken).full_clean()

    def test_newest_first_with_pk_breaking_the_tie(self):
        """
        Spec 2. The three rows are written inside one transaction and their
        `created_at` is then forced to be IDENTICAL — which is the situation the
        `-pk` tiebreak exists for. Left to chance the test would pass on
        `-created_at` alone and the tiebreak could be deleted without anything
        going red, which is the whole failure mode an ordering test is supposed
        to prevent.
        """
        self.assertEqual(
            Notification._meta.ordering,
            ["-created_at", "-pk"],
            msg="the list page's order is defined once, on the model",
        )

        with transaction.atomic():
            first, second, third = [
                make_notification(self.rep, text=f"number {n}") for n in (1, 2, 3)
            ]
        Notification.objects.update(created_at=first.created_at)

        self.assertEqual(
            list(Notification.objects.values_list("pk", flat=True)),
            [third.pk, second.pk, first.pk],
            msg="rows sharing a created_at come back newest-inserted first, every time",
        )


# -------------------------------------------------------------- the triggers


class LeadAssignedTriggerTests(TestCase):
    """Spec 3 and 4: what fires when a lead is handed to a Rep, and what must not."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(name="Lakeside Dental")
        self.deal = make_deal(self.company, owner=None, name="Lakeside Dental - new office")

    def test_it_tells_the_assignee_and_points_at_the_deal(self):
        """Spec 3: one notification, for the assignee, naming the company."""
        with self.captureOnCommitCallbacks(execute=True):
            returned = lead_assigned(self.deal, self.rep)

        self.assertEqual(
            Notification.objects.count(),
            1,
            msg="assigning a lead is one notification, not one per interested party",
        )
        note = Notification.objects.get()
        self.assertEqual(note.user, self.rep, msg="the notification belongs to the Rep it was given to")
        self.assertEqual(note.kind, Notification.Kind.LEAD_ASSIGNED)
        self.assertIn(
            self.company.name,
            note.text,
            msg="the Rep has to ring the company, so the text names the company, not the deal",
        )
        self.assertEqual(
            note.url,
            self.deal.get_absolute_url(),
            msg="the link goes to the deal, which is where the work is recorded",
        )
        self.assertIsNone(note.read_at, msg="a fresh notification is unread by definition")
        # The write was deferred, so there was nothing to hand back at call time.
        # The trigger promises "the notification or None", and this is the None.
        self.assertIsNone(returned, msg="inside a transaction the write is still pending, so None is the honest answer")

    def test_an_unowned_lead_notifies_nobody(self):
        """
        Spec 4. An unowned lead is not an edge case in this app, it is what the
        new-leads queue is MADE of (see the module docstring in core/leads.py),
        so "nobody to tell" is the common case and it has to be quiet.
        """
        with self.captureOnCommitCallbacks(execute=True):
            returned = lead_assigned(self.deal, None)

        self.assertIsNone(returned)
        self.assertEqual(
            Notification.objects.count(),
            0,
            msg="no recipient means no row — not a row with a null user, and not a guess at an Admin",
        )

    def test_a_rolled_back_assignment_leaves_no_notification(self):
        """
        Spec 5. This is the whole reason the trigger writes through
        `transaction.on_commit`, so it is proven the only way that means
        anything: a transaction that raises, and the notification must be gone.

        The IntegrityError comes from a second Tag with the same name (Tag.name
        is unique) because that is a genuine database refusal rather than a
        hand-thrown exception — the rollback path under test is the one a real
        failure takes. The assertion has to wrap the `atomic()` block and not sit
        inside it, or the savepoint is never released and everything after this
        fails for a different reason.
        """
        Tag.objects.create(name="forced-dup")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                lead_assigned(self.deal, self.rep)
                Tag.objects.create(name="forced-dup")

        self.assertEqual(
            Notification.objects.count(),
            0,
            msg="a rolled-back assignment must not leave a Rep with a notification claiming it happened",
        )

        # ...and the same call inside a transaction that DOES commit writes the
        # row, so the test above is not passing merely because the trigger is
        # silent. The trigger has to be CALLED inside the capture block — a
        # callback registered before it opened was already handed to a
        # transaction that is never going to commit, and nothing can rescue it.
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                lead_assigned(self.deal, self.rep)

        self.assertEqual(Notification.objects.count(), 1, msg="and it lands once the transaction commits")


class NewLeadTriggerTests(TestCase):
    """
    A lead ARRIVING tells the salespeople who can pick it up.

    Not in the spec's numbered list and here anyway, for the reason
    `TriggerWiringTests` below gives: `create_lead` has always left a deal
    unowned, and until now the only thing that noticed a lead arriving was a human
    remembering to look. These are the tests that pin who hears about it — everyone
    who could act, and nobody who could not.

    The recipients are the same queryset `LeadAssignForm` offers in a dropdown,
    which is the point of the comparison: a Rep who is on that list is somebody an
    Admin may hand a lead to, and a Rep who is NOT on it is somebody this trigger
    must not ring. The two would otherwise be free to drift.

    The payloads come from `.test_leads` rather than being written out again here.
    The public body carries a blank honeypot field, and that blank is not
    incidental — a submission that omits the key passes against a form that never
    rendered the field at all, which is not what "the honeypot stays empty" means.
    One copy of that rule, in the file that specifies it.
    """

    def setUp(self):
        cache.clear()
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep", "Rep")
        self.viewer = make_user("watcher", "Viewer")
        self.root = make_user("root", is_superuser=True)
        self.gone = make_user("gone", "Rep", is_active=False)
        # Two role groups on purpose: `groups__name__in` is a JOIN, so without
        # `distinct()` this user is notified twice and their bell badge reads 2
        # for a single lead. Nobody has ever been added to both by accident, which
        # is exactly why it needs a test rather than a code review.
        self.both = make_user("both", "Admin")
        self.both.groups.add(self.rep.groups.get(name="Rep"))

    def file_lead(self, company_name="Lakeside Dental", submitted_by=None):
        """
        One submission through `create_lead` — the function both forms call, so
        this covers the desk form and the public form without going near either.
        """
        return create_lead(
            company_name=company_name,
            first_name="Pat",
            last_name="Doe",
            email="pat@lakeside.example",
            phone="555-0100",
            message="Wants a quote",
            team_name="Support",
            submitted_by=submitted_by,
        )

    def new_lead_notes(self):
        """Every NEW_LEAD notification, however many rows that turns out to be."""
        return Notification.objects.filter(kind=Notification.Kind.NEW_LEAD)

    def test_it_tells_every_active_salesperson_and_nobody_else(self):
        """Who hears about a lead arriving: everyone who could pick it up, once each."""
        with self.captureOnCommitCallbacks(execute=True):
            deal = self.file_lead()

        notified = set(self.new_lead_notes().values_list("user_id", flat=True))
        self.assertEqual(
            notified,
            {self.admin.pk, self.rep.pk, self.root.pk, self.both.pk},
            msg=(
                "an active Admin, Rep and superuser all hear about it — a superuser "
                "is an Admin as far as permissions.role_of is concerned — and a Viewer "
                "and a deactivated Rep do not, because neither could ever claim it"
            ),
        )
        self.assertEqual(
            self.new_lead_notes().count(),
            4,
            msg="one row per person, not one per row of the join: a user in two groups is still one user",
        )

        for note in self.new_lead_notes():
            with self.subTest(user=note.user_id):
                self.assertEqual(note.text, f"New lead: {deal.company}")
                self.assertEqual(
                    note.url,
                    reverse("lead_queue"),
                    msg="the action is 'pick this up', and Pick up is a button on the queue",
                )
                self.assertIsNone(note.read_at, msg="a fresh notification is unread")

        self.assertFalse(
            self.new_lead_notes().filter(user=self.viewer).exists(),
            msg="a Viewer cannot open the queue, so a notification would be a dead link and a reason to ignore the bell",
        )
        self.assertFalse(
            self.new_lead_notes().filter(user=self.gone).exists(),
            msg="an inactive user cannot sign in to read it",
        )

    def test_nobody_is_told_twice_and_nobody_extra_is_told(self):
        """
        Two of the same assertion as above and for two different reasons, kept as
        separate failures so a regression says which rule it broke.

        `distinct()` protects the two-group user from a JOIN duplicate. The `count`
        against the whole table is the one that catches a RecipIENT LIST that grew
        a role nobody asked for — a Viewer added "just in case" passes every
        per-user check below it and fails here.
        """
        with self.captureOnCommitCallbacks(execute=True):
            self.file_lead()

        self.assertEqual(
            self.new_lead_notes().filter(user=self.both).count(), 1, msg="a JOIN duplicate, not a second lead"
        )
        self.assertEqual(Notification.objects.count(), self.new_lead_notes().count(), msg="no other kind of notification")

    def test_the_person_who_filed_it_is_not_told_about_it(self):
        """
        A Rep who took the call does not need a bell to tell them what they typed.

        The exclusion is on `submitted_by`, so it only bites when somebody was
        signed in. On the public form there is no submitter to exclude and the
        second assertion below is the proof: the same lead, the same recipients,
        one more row.
        """
        with self.captureOnCommitCallbacks(execute=True):
            self.file_lead(submitted_by=self.rep)

        self.assertEqual(
            set(self.new_lead_notes().values_list("user_id", flat=True)),
            {self.admin.pk, self.root.pk, self.both.pk},
            msg="the submitter is skipped and nobody else is",
        )
        self.assertFalse(
            self.new_lead_notes().filter(user=self.rep).exists(),
            msg="you do not notify yourself that you filed the lead yourself",
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.file_lead(company_name="Harbour Vet", submitted_by=None)

        self.assertEqual(
            self.new_lead_notes().filter(user=self.rep).count(),
            1,
            msg="nobody signed in means nobody to skip, so the Rep hears about it",
        )

    def test_a_company_name_that_fills_the_whole_column_is_truncated(self):
        """
        `Company.name` is allowed to be exactly as long as `Notification.text`, so
        the company name alone fills the column and the prefix overflows it.

        The failure this pins is not cosmetic. An over-length value is refused by
        the column, not clipped, and the write happens inside an `on_commit`
        callback — by which point the lead has been saved and committed. Without
        truncation the visitor's lead would be on the board, in the queue, with
        nobody told, and the exception would surface to a view that has already
        redirected.
        """
        limit = Notification._meta.get_field("text").max_length
        with self.captureOnCommitCallbacks(execute=True):
            deal = self.file_lead(company_name="L" * limit)

        note = self.new_lead_notes().filter(user=self.rep).get()
        self.assertLessEqual(
            len(note.text),
            limit,
            msg=(
                "the limit is read off the model rather than assumed, so widening the "
                "column later cannot leave this trigger truncating at the old number"
            ),
        )
        self.assertTrue(
            note.text.endswith("…"),
            msg="a cut-off name says it was cut off; a name that just stops reads like the company's actual name",
        )
        self.assertTrue(note.text.startswith("New lead: L"), msg="the prefix survives, or nobody knows what arrived")
        self.assertEqual(
            deal.company.name,
            "L" * limit,
            msg="and the company keeps all 200 characters — only the notification is shortened",
        )

    def test_a_rolled_back_lead_notifies_nobody(self):
        """
        The half of the on-commit rule the other direction: a transaction that
        raises must not leave a bell ringing about a lead that does not exist.

        The IntegrityError is a genuine database refusal (Tag.name is unique), for
        the same reason as `LeadAssignedTriggerTests`: the rollback path under test
        is the one a real failure takes, not a hand-thrown exception that only
        happens to unwind the stack. The assertion has to WRAP the `atomic()`
        block — sitting inside it, the savepoint is never released and everything
        after this fails for a different reason.
        """
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.file_lead()
                Tag.objects.create(name="forced-dup")
                Tag.objects.create(name="forced-dup")

        self.assertEqual(
            self.new_lead_notes().count(),
            0,
            msg="a rolled-back lead must not leave salespeople with a notification claiming it happened",
        )

        # ...and the same call inside a transaction that DOES commit does notify,
        # so the test above is not passing merely because the trigger is silent.
        with self.captureOnCommitCallbacks(execute=True):
            self.file_lead(company_name="Harbour Vet")

        self.assertEqual(
            self.new_lead_notes().count(),
            4,
            msg="four salespeople, four notifications — and the one that rolled back added none",
        )

    def test_the_public_form_notifies_a_rep(self):
        """
        End to end over HTTP: no login, a CSRF token, and a bell afterwards.

        This is the path the feature actually exists for. A stranger on the website
        at 2am is the case where nobody was in the office to text anybody, and it
        is the one where "the function works" and "someone finds out" are the same
        claim. `cache.clear()` in setUp matters here because the rate limiter is
        per IP in the cache and a neighbour's POSTs would otherwise turn this into
        a 429 test.
        """
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("lead_submit"), public_payload(csrfmiddlewaretoken=token)
            )

        self.assertRedirects(response, reverse("lead_thanks"), fetch_redirect_response=False)
        self.assertTrue(
            self.new_lead_notes().filter(user=self.rep).exists(),
            msg="a lead from the website with nobody watching is exactly when the bell matters",
        )
        self.assertEqual(self.new_lead_notes().filter(user=self.rep).count(), 1, msg="one lead, one bell")

    def test_the_desk_form_notifies_a_rep_but_not_the_viewer_who_filed_it(self):
        """
        The other door onto the same room, and the two actors that matter on it.

        The desk form is open to a Viewer — the phone rings for whoever is nearest
        (see views_leads.lead_create) — so this is the ONE path in the app where
        the submitter is a Viewer and not a Rep. Nothing here changes for them: a
        Viewer's exclusion is about not receiving notifications, never about not
        being able to file a lead.
        """
        self.client.force_login(self.viewer)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("lead_create"), desktop_payload())

        self.assertEqual(response.status_code, 302, msg="the lead was taken, not refused")
        self.assertTrue(
            self.new_lead_notes().filter(user=self.rep).exists(),
            msg="the Rep who will find this lead in the queue is told it is there",
        )
        self.assertFalse(
            self.new_lead_notes().filter(user=self.viewer).exists(),
            msg="the Viewer filed it and cannot claim it anyway — two independent reasons for silence",
        )


class TaskAssignedTriggerTests(TestCase):
    """Spec 6: the task trigger, and its link to My Day rather than a record."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.task = make_task(assignee=self.rep, title="Ring the installer")

    def test_it_points_at_the_task_list(self):
        with self.captureOnCommitCallbacks(execute=True):
            task_assigned(self.task, self.rep)

        note = Notification.objects.get()
        self.assertEqual(note.kind, Notification.Kind.TASK_ASSIGNED)
        self.assertEqual(note.user, self.rep)
        self.assertIn(self.task.title, note.text)
        self.assertEqual(
            note.url,
            reverse("task_list"),
            msg="a task's subject is the task; the one page holding every task assigned to somebody is My Day",
        )

    def test_an_unassigned_task_notifies_nobody(self):
        orphan = make_task(assignee=None, title="File this away")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertIsNone(task_assigned(orphan, None))
        self.assertEqual(Notification.objects.count(), 0)


class TaskDueTomorrowTests(TestCase):
    """Spec 7: the query helper, which is NOT a trigger and writes nothing."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.other = make_user("rep2", "Rep")
        tomorrow = local_today_start() + timedelta(days=1)

        self.early = make_task(assignee=self.rep, title="Morning", due_at=tomorrow + timedelta(hours=9))
        self.late = make_task(assignee=self.rep, title="Evening", due_at=tomorrow + timedelta(hours=21))
        self.due_today = make_task(assignee=self.rep, title="Tonight", due_at=local_today_start() + timedelta(hours=22))
        self.due_in_three = make_task(
            assignee=self.rep, title="Later", due_at=tomorrow + timedelta(days=2, hours=9)
        )
        self.no_due_date = make_task(assignee=self.rep, title="Someday")
        self.someone_elses = make_task(assignee=self.other, title="Theirs", due_at=tomorrow + timedelta(hours=9))

    def test_it_returns_only_this_users_tasks_due_tomorrow(self):
        self.assertEqual(
            list(task_due_tomorrow(self.rep)),
            [self.early, self.late],
            msg=(
                "tomorrow only, this user only, soonest first — a task due tonight, one due in "
                "three days, one with no date at all and another Rep's task are all out"
            ),
        )

    def test_it_writes_no_notifications(self):
        """It is a query. Nothing calls it yet, and nothing should appear because it exists."""
        before = Notification.objects.count()
        task_due_tomorrow(self.rep)
        self.assertEqual(Notification.objects.count(), before, msg="a helper that queries cannot notify")


# ------------------------------------------------------------------- the views


class NotificationListTests(TestCase):
    """Spec 8 and 11: the list page is private, and it is behind the login."""

    def setUp(self):
        self.alice = make_user("alice", "Rep")
        self.bob = make_user("bob", "Rep")
        self.mine = make_notification(self.alice, text="ALICE ONLY: call Lakeside Dental")
        self.also_mine = make_notification(self.alice, text="ALICE ONLY: send the quote")
        self.theirs = make_notification(self.bob, text="BOB ONLY: ring the installer")

    def test_a_rep_never_sees_another_users_notifications(self):
        """Spec 8, the IDOR this repo's whole security gate exists to catch."""
        self.client.login(username="bob", password=PASSWORD)
        response = self.client.get(reverse("notification_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.context["notifications"]),
            [self.theirs],
            msg="the queryset itself must be filtered — a template that merely failed to print them is not a lock",
        )
        self.assertContains(response, "BOB ONLY")
        self.assertNotContains(response, "ALICE ONLY", msg_prefix="another Rep's mail is on the page")
        self.assertEqual(
            Notification.objects.filter(user=self.alice).count(),
            2,
            msg="Alice's rows are filtered out of the page, not deleted by it — hiding and erasing are different bugs",
        )

    def test_the_newest_comes_first(self):
        make_notification(self.bob, text="NEWEST ONE")
        self.client.login(username="bob", password=PASSWORD)
        response = self.client.get(reverse("notification_list"))

        self.assertEqual(list(response.context["notifications"])[0].text, "NEWEST ONE")

    def test_anonymous_is_sent_to_the_login_page(self):
        response = self.client.get(reverse("notification_list"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertIn("next=", response["Location"])


class NotificationReadTests(TestCase):
    """Spec 9, 12 and 15: marking read is POST-only, and only ever your own."""

    def setUp(self):
        self.alice = make_user("alice", "Rep")
        self.bob = make_user("rep_bob", "Rep")
        self.note = make_notification(self.alice, text="A lead is yours")
        self.url = reverse("notification_read", args=[self.note.pk])

    def test_marking_your_own_read_stamps_it_and_follows_the_link(self):
        self.client.login(username="alice", password=PASSWORD)
        self.note.url = reverse("task_list")
        self.note.save(update_fields=["url"])

        response = self.client.post(self.url)

        self.note.refresh_from_db()
        self.assertIsNotNone(self.note.read_at, msg="the whole feature is this column moving")
        self.assertRedirects(response, reverse("task_list"), fetch_redirect_response=False)

    def test_a_notification_with_no_link_returns_to_the_list(self):
        self.client.login(username="alice", password=PASSWORD)
        self.assertEqual(self.note.url, "")

        response = self.client.post(self.url)

        self.assertRedirects(response, reverse("notification_list"), fetch_redirect_response=False)

    def test_another_user_gets_a_404_and_it_stays_unread(self):
        """Spec 9. 404 rather than 403: a 403 confirms the row exists."""
        self.client.login(username="rep_bob", password=PASSWORD)

        response = self.client.post(self.url)

        self.assertEqual(
            response.status_code,
            404,
            msg="not yours and not found are the same answer, on purpose",
        )
        self.note.refresh_from_db()
        self.assertIsNone(
            self.note.read_at,
            msg="a refused request must not have marked it anyway",
        )

    def test_marking_twice_keeps_the_first_seen_time(self):
        self.client.login(username="alice", password=PASSWORD)
        self.client.post(self.url)
        self.note.refresh_from_db()
        first_seen = self.note.read_at

        self.client.post(self.url)

        self.note.refresh_from_db()
        self.assertEqual(self.note.read_at, first_seen, msg="two tabs is not two different times of seeing it")

    def test_it_is_post_only(self):
        """Spec 12. A GET must not be able to change anything."""
        self.client.login(username="alice", password=PASSWORD)

        self.assertEqual(self.client.get(self.url).status_code, 405)

        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)

    def test_mark_all_read_leaves_another_users_alone(self):
        """Spec 10. The endpoint has no pk to get wrong, so its only guard is the user filter."""
        alice_second = make_notification(self.alice, text="Alice, second")
        bob_note = make_notification(self.bob, text="Bob's own")
        self.client.login(username="rep_bob", password=PASSWORD)

        response = self.client.post(reverse("notification_read_all"))

        self.assertEqual(response.status_code, 302)
        self.note.refresh_from_db()
        alice_second.refresh_from_db()
        bob_note.refresh_from_db()
        self.assertIsNone(self.note.read_at, msg="mark-all-read is not a global sweep")
        self.assertIsNone(alice_second.read_at, msg="one of hers was enough to prove it")
        self.assertIsNotNone(bob_note.read_at, msg="the caller's own unread rows do get marked")

    def test_mark_all_read_is_post_only(self):
        """Spec 15."""
        self.client.login(username="alice", password=PASSWORD)

        self.assertEqual(self.client.get(reverse("notification_read_all")).status_code, 405)

        self.note.refresh_from_db()
        self.assertIsNone(self.note.read_at)


# --------------------------------------------------- the bell and the badge


class UnreadCountTests(TestCase):
    """Spec 13 and 14: the context processor and the sidebar bell."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.url = reverse("notification_list")

    def request_for(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_anonymous_gets_zero_without_asking_the_database(self):
        """
        Spec 13. The assertion is about the QUERY, not the answer.

        The public lead form renders through registration/auth_base.html, which
        overrides base.html's body block — but context processors run on the way
        to every render, so this function is on the most attacked page in the app
        whether or not the bell is drawn there. A COUNT() nobody needs is a free
        query for a scraper to make us run.
        """
        with mock.patch("core.context_processors.Notification") as fake:
            result = unread_notifications(self.request_for(AnonymousUser()))

        self.assertEqual(result["unread_notifications"], 0)
        fake.objects.filter.assert_not_called()
        fake.objects.count.assert_not_called()

    def test_it_counts_only_this_users_unread_rows(self):
        make_notification(self.rep, text="one")
        make_notification(self.rep, text="two")
        read = make_notification(self.rep, text="already read")
        Notification.objects.filter(pk=read.pk).update(read_at=self.rep.date_joined)
        someone_else = make_user("rep2", "Rep")
        make_notification(someone_else, text="theirs")

        result = unread_notifications(self.request_for(self.rep))

        self.assertEqual(result["unread_notifications"], 2, msg="read rows and other people's rows both stay out")

    def test_the_bell_carries_the_count_and_is_escaped(self):
        """Spec 14: the badge shows the number, and nothing on it is marked safe."""
        for n in range(3):
            make_notification(self.rep, text=f"notification number {n}")

        self.client.login(username="rep", password=PASSWORD)
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unread_notifications"], 3)
        self.assertContains(response, self.url)
        self.assertContains(response, '<span class="badge">3</span>')

    def test_no_bell_at_all_when_everything_is_read(self):
        """
        A bell reading zero is a thing that has to be looked at to learn is empty.
        The count is 0, so nothing is drawn — which also means nothing for a
        stranger's eye on a shared screen.
        """
        note = make_notification(self.rep, text="the only one")
        Notification.objects.filter(pk=note.pk).update(read_at=self.rep.date_joined)

        self.client.login(username="rep", password=PASSWORD)
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.context["unread_notifications"], 0)
        self.assertNotContains(response, self.url, msg_prefix="a zero-count bell is still a bell")

    def test_the_base_template_marks_nothing_safe(self):
        """
        Spec 14's escaping half.

        The count is an integer the app computed, so it cannot carry markup — but
        `notification.text` can, and a `|safe` anywhere in the sidebar chrome is
        how a company name somebody typed ends up executing in every Rep's
        browser. Asserted against the file rather than against one rendered
        page, because the failure being guarded against is somebody adding the
        filter tomorrow to some other variable on some other line.
        """
        base = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertNotIn("|safe", base, msg="base.html's chrome must escape everything it prints")

    def test_a_hostile_notification_text_is_printed_as_text(self):
        make_notification(self.rep, text="<script>alert(1)</script> was a company name")

        self.client.login(username="rep", password=PASSWORD)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>", msg_prefix="the raw tag is live on the page")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


# ------------------------------------------------ the triggers, wired to views


class TriggerWiringTests(TestCase):
    """
    The two places that actually fire a trigger, proven end to end.

    Not in the spec's numbered list, and here anyway: a trigger nothing calls is
    a function that passes every test above while the feature does nothing at
    all. These two go through the real views, the real forms and the real
    redirect, because that is the only level at which "the wiring is missing"
    is visible.
    """

    def setUp(self):
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep", "Rep")
        self.company = make_company(name="Lakeside Dental")

    def test_assigning_a_lead_notifies_the_rep(self):
        from django.core.cache import cache

        cache.clear()
        deal = make_deal(self.company, owner=None, name="Lakeside Dental - new office")
        self.client.login(username="boss", password=PASSWORD)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("lead_assign", args=[deal.pk]), {"owner": self.rep.pk})

        self.assertEqual(response.status_code, 302)
        note = Notification.objects.get()
        self.assertEqual(note.user, self.rep)
        self.assertEqual(note.kind, Notification.Kind.LEAD_ASSIGNED)
        self.assertEqual(
            Notification.objects.count(),
            1,
            msg="one act, one notification — the lead notification replaces the task notification here",
        )

    def test_creating_a_task_notifies_its_assignee(self):
        self.client.login(username="rep", password=PASSWORD)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("task_create"),
                {"title": "Send the signed order", "due_at": "", "priority": Task.Priority.NORMAL,
                 "company": "", "contact": "", "deal": ""},
            )

        self.assertEqual(response.status_code, 302)
        note = Notification.objects.get()
        self.assertEqual(note.user, self.rep)
        self.assertEqual(note.kind, Notification.Kind.TASK_ASSIGNED)
        self.assertEqual(note.url, reverse("task_list"))