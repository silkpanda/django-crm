"""
Review fixes for the record forms and the contact merge.

TEACHING NOTE: each class below is one bug, written first and seen failing
against the old code. The docstring says what went wrong; the test is the
contract that keeps it fixed.
"""

from django.test import TestCase
from django.urls import reverse

from core.models import BuyingRole, Contact, Stage, Task

from .factories import PASSWORD, make_company, make_contact, make_deal, make_task, make_user


class MixedCaseDuplicateEmailTests(TestCase):
    """
    Contact.save lowercases the email, but the form's unique check ran on the
    email as typed. `Pat@Acme.example` passed it while `pat@acme.example`
    existed, and the save then hit the database constraint: a 500.
    """

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)
        make_contact(first_name="Pat", last_name="Doe", email="pat@acme.example")

    def test_mixed_case_copy_is_a_form_error_not_a_500(self):
        # A different name, so the soft same-name warning doesn't step in first.
        resp = self.client.post(
            reverse("contact_create"),
            {"first_name": "Patricia", "last_name": "Other", "email": " Pat@Acme.example ", "lifecycle": "lead"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already exists", resp.content.decode())
        self.assertEqual(Contact.objects.count(), 1)

    def test_blank_email_still_saves_as_null(self):
        resp = self.client.post(
            reverse("contact_create"), {"first_name": "Sam", "last_name": "Blank", "email": "", "lifecycle": "lead"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(Contact.objects.get(first_name="Sam").email)


class InactiveOwnerKeptOnEditTests(TestCase):
    """
    The owner and assignee dropdowns only listed active users, so a record owned
    by someone deactivated had no matching option. The browser sent a blank, and
    an Admin's unrelated edit quietly cleared the owner (or, on a task, made the
    Admin the assignee).
    """

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.gone = make_user("gone", "Rep", is_active=False)
        self.other_gone = make_user("othergone", "Rep", is_active=False)
        self.client.login(username="admin", password=PASSWORD)
        self.company = make_company(owner=self.gone)
        self.deal = make_deal(self.company, owner=self.gone)

    def _deal_post(self, owner):
        return self.client.post(
            reverse("deal_update", args=[self.deal.pk]),
            {"name": "Renamed", "company": self.company.pk, "stage": Stage.LEAD, "value": "0", "owner": owner.pk},
        )

    def test_deal_edit_keeps_the_inactive_owner(self):
        resp = self._deal_post(self.gone)
        self.assertEqual(resp.status_code, 302)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.name, "Renamed")
        self.assertEqual(self.deal.owner, self.gone)

    def test_a_different_inactive_user_is_still_refused(self):
        resp = self._deal_post(self.other_gone)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("owner", resp.context["form"].errors)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.owner, self.gone)

    def test_task_edit_keeps_the_inactive_assignee(self):
        task = make_task(assignee=self.gone)
        resp = self.client.post(
            reverse("task_update", args=[task.pk]),
            {"title": "Still theirs", "priority": Task.Priority.NORMAL, "assignee": self.gone.pk},
        )
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.title, "Still theirs")
        self.assertEqual(task.assignee, self.gone)


class MergeKeepsBuyingRolesTests(TestCase):
    """
    Merging contacts carried the loser's tags across but not its buying roles,
    so deleting the loser threw away "Champion" with it.
    """

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)

    def test_survivor_gets_the_losers_roles_plus_its_own(self):
        champion = BuyingRole.objects.get(name="Champion")
        blocker = BuyingRole.objects.get(name="Blocker")
        loser = make_contact(first_name="Ray", email="ray@old.example")
        survivor = make_contact(first_name="Ray", email="ray@new.example")
        loser.roles.add(champion)
        survivor.roles.add(blocker)

        resp = self.client.post(reverse("contact_merge", args=[loser.pk]), {"survivor_id": survivor.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contact.objects.filter(pk=loser.pk).exists())
        self.assertEqual(set(survivor.roles.all()), {champion, blocker})

    def test_company_merge_still_works(self):
        loser, survivor = make_company(name="Acme"), make_company(name="Acme Inc")
        resp = self.client.post(reverse("company_merge", args=[loser.pk]), {"survivor_id": survivor.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(type(loser).objects.filter(pk=loser.pk).exists())


class EditFormReopenClearsLostReasonTests(TestCase):
    """
    PIPE-8 again, through the edit form: the board clears lost_reason when a
    Lost deal goes back into the pipeline, DealForm kept it.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep)
        self.deal = make_deal(self.company, owner=self.rep, stage=Stage.LOST, lost_reason="Went with ADT")

    def _post(self, stage, lost_reason):
        return self.client.post(
            reverse("deal_update", args=[self.deal.pk]),
            {"name": self.deal.name, "company": self.company.pk, "stage": stage, "value": "0",
             "lost_reason": lost_reason},
        )

    def test_reopening_clears_the_reason(self):
        resp = self._post(Stage.NEGOTIATION, "Went with ADT")
        self.assertEqual(resp.status_code, 302)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.stage, Stage.NEGOTIATION)
        self.assertEqual(self.deal.lost_reason, "")

    def test_lost_still_needs_a_reason(self):
        resp = self._post(Stage.LOST, "  ")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("lost_reason", resp.context["form"].errors)
