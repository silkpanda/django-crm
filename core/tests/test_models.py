"""
Phase 1 model tests: the rules that live on the data itself, not on any page.

TEACHING NOTE: a "model" is a Python class that maps to a database table (the
ORM, Object-Relational Mapper, turns attribute reads and writes into SQL). A
rule enforced in the model holds no matter how a record gets written: a form,
an import, the Django admin, or a shell. That's why the stage rules are tested
here and not only through the web pages:

- `stage_changed_at` moves only when the stage actually changes. Saving a deal
  for any other reason must not reset its "days in stage" clock.
- Closing a deal stamps `closed_at`; reopening clears it; Lost needs a reason.
- A company with deals can't be deleted. Its FK (ForeignKey, a column pointing
  at a row in another table) is PROTECT, so the database refuses rather than
  silently deleting the deals along with it. Contacts survive a company delete.
- Emails are stored lowercase, and blank becomes NULL (no value). That matters
  because the email column is unique: two blank strings would collide, but two
  NULLs don't.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from core.models import Activity, Contact, Deal, Stage

from .factories import make_company, make_contact, make_deal


class DealStageTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_stage_changed_at_moves_only_on_stage_change(self):
        deal = make_deal(self.company)
        first = deal.stage_changed_at
        Deal.objects.filter(pk=deal.pk).update(stage_changed_at=first - timedelta(days=3))
        deal.refresh_from_db()
        stale = deal.stage_changed_at

        deal.value = Decimal("500")
        deal.save()
        deal.refresh_from_db()
        self.assertEqual(deal.stage_changed_at, stale, "editing value must not touch stage_changed_at")

        deal.stage = Stage.PROPOSAL
        deal.save()
        deal.refresh_from_db()
        self.assertGreater(deal.stage_changed_at, stale)

    def test_closing_stamps_closed_at_and_reopening_clears_it(self):
        deal = make_deal(self.company)
        self.assertIsNone(deal.closed_at)
        deal.stage = Stage.WON
        deal.save()
        self.assertIsNotNone(deal.closed_at)
        deal.stage = Stage.NEGOTIATION
        deal.save()
        self.assertIsNone(deal.closed_at)

    def test_lost_requires_reason(self):
        deal = Deal(name="x", company=self.company, stage=Stage.LOST)
        with self.assertRaises(ValidationError):
            deal.full_clean()
        deal.lost_reason = "Went with ADT"
        deal.full_clean()

    def test_probability_defaults_by_stage_and_can_be_overridden(self):
        deal = make_deal(self.company, stage=Stage.PROPOSAL, value=Decimal("1000"))
        self.assertEqual(deal.effective_probability, 50)
        self.assertEqual(deal.weighted_value, Decimal("500"))
        deal.probability = 80
        self.assertEqual(deal.weighted_value, Decimal("800"))

    def test_probability_over_100_rejected(self):
        deal = Deal(name="x", company=self.company, probability=150)
        with self.assertRaises(ValidationError):
            deal.full_clean()

    def test_history_records_stage_move(self):
        deal = make_deal(self.company)
        deal.stage = Stage.QUALIFIED
        deal.save()
        self.assertEqual(deal.history.count(), 2)
        self.assertEqual(deal.history.first().stage, Stage.QUALIFIED)


class CompanyContactTests(TestCase):
    def test_contact_without_company(self):
        contact = make_contact()
        self.assertIsNone(contact.company)

    def test_company_with_deals_cannot_be_deleted(self):
        company = make_company()
        make_deal(company)
        with self.assertRaises(ProtectedError):
            company.delete()

    def test_company_delete_keeps_contacts(self):
        company = make_company()
        contact = make_contact(company=company)
        company.delete()
        contact.refresh_from_db()
        self.assertIsNone(contact.company)

    def test_email_normalised_and_blank_is_null(self):
        a = make_contact(email="  Pat@Example.COM ")
        self.assertEqual(a.email, "pat@example.com")
        b = make_contact(email="")
        c = make_contact(email="")
        self.assertIsNone(b.email)
        self.assertIsNone(c.email)

    def test_domain_lowercased(self):
        self.assertEqual(make_company(domain=" Acme.COM ").domain, "acme.com")


class TimelineOrderTests(TestCase):
    def test_newest_first(self):
        company = make_company()
        now = timezone.now()
        old = Activity.objects.create(body="old", company=company, occurred_at=now - timedelta(days=2))
        new = Activity.objects.create(body="new", company=company, occurred_at=now)
        mid = Activity.objects.create(body="mid", company=company, occurred_at=now - timedelta(days=1))
        self.assertEqual(list(company.activities.all()), [new, mid, old])

    def test_activity_needs_a_parent(self):
        with self.assertRaises(ValidationError):
            Activity(body="orphan").full_clean()


class ContactUniqueEmailTests(TestCase):
    def test_duplicate_email_rejected(self):
        make_contact(email="a@b.com")
        with self.assertRaises(Exception):
            Contact.objects.create(first_name="X", email="A@B.com")
