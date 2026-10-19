"""
Sales-lead intake — failing tests for the "somebody asked us for something" door.

WHAT THIS SPECIFIES
-------------------
Staff on any team, and visitors on the company website, can submit a sales
lead. A lead becomes: a Company (matched case-insensitively by name, else
created), a Contact (matched by email, else created, linked to that company),
and a Deal in stage LEAD with NO owner, plus a timeline note on the deal holding
the submitter's message. Company and Contact get `source` = the LeadSource
named after the submitting team. Unowned Lead-stage deals are the "new leads"
queue; Admins and Reps may open it. A Rep takes one for themselves through
`lead_claim`, and an Admin may hand one to somebody else through `lead_assign`.
Either way the deal gets an owner and a Task for that owner ("Contact new lead:
<company>", due tomorrow).

Teams are LeadSource rows seeded by migration: "Sales", "Support",
"Partners", "Front desk", "Website". No email is sent (proof of concept).

THE SEVEN URLS
--------------
  lead_create   /leads/new/           login required, ANY role
  lead_submit   /leads/submit/        PUBLIC
  lead_thanks   /leads/thanks/        PUBLIC
  lead_queue    /leads/               Admin or Rep
  lead_claim    /leads/<deal pk>/claim/    POST, Admin or Rep — take it yourself
  lead_assign   /leads/<deal pk>/assign/   POST, Admin only — give it to someone else
  lead_company_suggestions  /leads/companies/  GET, ANY role — company names for
                              the desk form's Company datalist

THE TWO FORMS
-------------
Both forms take company_name (required, max 200), first_name (required),
last_name, email, phone, message (required, max 2000). The desktop form also
has `team`, a choice of the four staff teams. The public form has a honeypot
field `website` that must stay empty, and its team is always "Website" — a POST
that names any other team is ignored, not obeyed.

THE ONE HARD SHAPE RULE: THIS MODULE MUST LOAD
----------------------------------------------
None of those six URLs exist yet, and `reverse()` raises `NoReverseMatch` the
instant it is called. Every `reverse()` in this file therefore sits INSIDE a
test method. A module-level `LEAD_QUEUE = reverse("lead_queue")` would blow up
while Python is still importing, Django would report one broken module, and you
would get zero test results and no idea which of the seventeen behaviours were
already correct. Same reason `core.models.LeadSource` is imported at the top:
that one is safe, it has been in models.py since 0001_initial. What is missing
is the migration that seeds its rows, which is why `lead_source("Support")`
raises `LeadSource.DoesNotExist` today.

Seventeen tests for sixteen numbered behaviours: the spec's item 14 covers two
actors going down the same view — an Admin assigning a lead, and a Rep trying it
and getting 403 — and that is two tests here. A broken Admin path must not stop
the Rep path from being reported.

THESE TESTS WERE WRITTEN RED FIRST. Nothing in this file changes the app; the
assertions ARE the spec, and turning them green is a separate job.

Notes for whoever turns this green — the traps these tests are aimed at:

  - `lead_create` must NOT be gated on `permissions.can_create()`. Every other
    create page in this app is, and that function returns False for a Viewer.
    A lead form is the one place a read-only person has to be able to write,
    because the phone rings for whoever picks up. Test 1 posts as a Viewer on
    purpose; routing this through `RecordCreateView` fails it.
  - The deal's `owner` must be left null and `stage` must be `Stage.LEAD`, or
    the "new leads" queue is permanently empty and every new lead looks like
    somebody already worked it. That is what tests 1, 6 and 9 pin, and why 9
    smuggles `owner`, `stage` and `value` in the POST: a lead form built as a
    plain ModelForm over Deal will happily take all three.
  - `lead_submit` and `lead_thanks` are PUBLIC, and `LoginRequiredMiddleware` is
    on globally. It defers to a `login_required = False` attribute on the view,
    so the two public views have to carry it; setting `login_url` is not enough
    and neither is decorating with `login_required`.
  - Matching is case-insensitive on BOTH halves. `iexact` on the company name,
    and `iexact` on the contact email too — `Contact.save()` lowercases what it
    stores, so the row in the database is `pat@acme.example` while the POST body
    says `PAT@ACME.EXAMPLE`, and an exact `email=` match silently makes a
    duplicate.
  - The honeypot is checked BEFORE anything is written, and it still redirects
    to `lead_thanks`. A bot that gets a 400 learns the field is watched.
  - The rate limit is per IP and lives in the cache. `setUp` calls
    `cache.clear()` on every test precisely so that state cannot leak from one
    test into the next and make this one pass or fail on a neighbour's POSTs.
  - `Task.due_at` is a DateTimeField, not a date. "Due tomorrow" is a datetime
    some time in the next 24 hours, and test 14 asserts against `timezone.now()`
    rather than against midnight.
  - The desk form offers the four STAFF teams, not all five. "Website" on that
    dropdown would mean a rep could file a lead that claims it came from the
    public form, which then never shows up in the website-attributed numbers.
  - `lead_claim` has to be ONE conditional UPDATE (`filter(...).update(...)`),
    not a read-then-check-then-save. Read-then-save is a race two Reps clicking
    the same row both pass, and the loser never finds out. Tests 18 and 19 are
    the trap: 18 pins that the SECOND claim changes nothing at all — owner, task
    count and message — and a view that loads the deal first, sees it unowned,
    and saves over the top passes every other test in this file.
  - Claiming sends NO notification. `lead_assigned` exists to tell somebody ELSE
    their diary changed, and test 17 wraps the POST in
    `captureOnCommitCallbacks(execute=True)` so a stray `on_commit` write would
    actually be written rather than quietly discarded when the test's transaction
    rolls back — which is exactly what an un-run callback looks like.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv/Scripts/python.exe manage.py test core.tests.test_leads`.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.leads import create_lead
from core.models import Activity, Company, Contact, Deal, LeadSource, Notification, Stage, Task

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user


# ------------------------------------------------------------------- helpers


def lead_source(name):
    """
    The seeded LeadSource called `name` — "Support", "Website", ...

    TEACHING NOTE: `LeadSource` the MODEL has existed since 0001_initial; it is
    the five ROWS a data migration puts in it that do not exist yet. So this
    helper raising `DoesNotExist` is the honest red for "the seed migration is
    missing", and it fails the same way in every test that needs a team.
    """
    return LeadSource.objects.get(name=name)


def desktop_payload(**over):
    """
    A desktop (signed-in) lead submission, spelling out only the required fields.

    `last_name` and `phone` are deliberately absent: the spec calls them optional,
    and every POST made from this helper is therefore also a check that the form
    does not demand them. `website` is absent on purpose too — the honeypot
    belongs to the PUBLIC form only.
    """
    data = {
        "team": "Support",
        "company_name": "Lakeside Dental",
        "first_name": "Pat",
        "message": "Wants a quote",
    }
    data.update(over)
    return data


def public_payload(**over):
    """
    A public (website) lead submission.

    The honeypot is included and blank, because that is the state every honest
    browser sends. Leaving the key out entirely would pass a form that never
    rendered the field at all, which is not what "the honeypot stays empty" means.
    """
    data = {
        "company_name": "Lakeside Dental",
        "first_name": "Pat",
        "message": "Wants a quote",
        "website": "",
    }
    data.update(over)
    return data


def csrf_token(client, url):
    """
    Load `url` and return its csrftoken cookie — what the page's form posts back.

    TEACHING NOTE: the GET is not optional. `{% csrf_token %}` is what makes
    Django set the cookie, so a client that has never loaded the form has no
    token to send and every one of its POSTs is rejected for a reason that has
    nothing to do with what is under test. Pulling the token out of the response
    this way is also what makes these tests honest about the PUBLIC page: a real
    visitor's browser does exactly this.
    """
    return client.get(url).cookies["csrftoken"].value


def assertNothingCreated(test, why):
    """No Deal, Company or Contact exists. `why` says which attempt this was."""
    test.assertEqual(Deal.objects.count(), 0, msg=f"{why}: no Deal may exist.")
    test.assertEqual(Company.objects.count(), 0, msg=f"{why}: no Company may exist.")
    test.assertEqual(Contact.objects.count(), 0, msg=f"{why}: no Contact may exist.")


def bound_form(response):
    """The form the view re-rendered, or a loud failure saying it re-rendered none."""
    form = response.context.get("form") if response.context is not None else None
    if form is None:
        raise AssertionError(
            "the response carries no `form` in its context, so there is nothing to "
            "assert on. A view that re-renders a bound form must hand it to the "
            "template under the name `form` (the Django convention) — otherwise "
            "the page has no way to show the field errors."
        )
    return form


# --------------------------------------------------- the desktop form (staff)


class DesktopLeadTests(TestCase):
    """Spec items 1-4: lead_create, and the Company/Contact/Deal a POST creates."""

    def setUp(self):
        cache.clear()
        # Deliberately a Viewer. "Any signed-in role may submit a lead" is the
        # whole point of having a desk form: the phone rings for whoever picks up.
        self.viewer = make_user("watcher", "Viewer")
        self.client.force_login(self.viewer)

    def test_viewer_can_get_and_post_a_lead(self):
        """Spec 1: one POST, three records, and the message kept on the deal."""
        response = self.client.get(reverse("lead_create"))
        self.assertEqual(response.status_code, 200, msg="a signed-in Viewer must be able to open the desk form")

        response = self.client.post(
            reverse("lead_create"),
            {
                "team": "Support",
                "company_name": "Lakeside Dental",
                "first_name": "Pat",
                "email": "pat@lakeside.example",
                "message": "Wants a quote",
            },
        )
        # A 200 here is the form refusing the POST, and `team` is the field most
        # likely to be missing from the form entirely — a form without it still
        # validates every other field, so only this status code catches it.
        self.assertEqual(response.status_code, 302)

        deal = Deal.objects.get()
        self.assertIn("Lakeside Dental", deal.name, msg="the deal is named after the company it is about")
        self.assertEqual(deal.stage, "lead", msg="a new lead starts at the top of the pipeline")
        self.assertIsNone(deal.owner, msg="an unowned lead is what makes the new-leads queue work")

        company = Company.objects.get()
        self.assertEqual(company.name, "Lakeside Dental")
        self.assertEqual(company.source, lead_source("Support"), msg="the company is attributed to the submitting team")

        contact = Contact.objects.get()
        self.assertEqual(contact.email, "pat@lakeside.example")
        self.assertEqual(contact.company, company, msg="the contact is linked to the company the deal is on")
        self.assertEqual(contact.source, lead_source("Support"), msg="the contact carries the same source as the company")

        note = Activity.objects.get(deal=deal)
        self.assertIn("Wants a quote", note.body, msg="the submitter's message is kept as a note on the deal")
        self.assertEqual(note.author, self.viewer, msg="a note filed by staff is attributed to whoever filed it")

        self.assertRedirects(response, deal.get_absolute_url(), fetch_redirect_response=False)

    def test_existing_company_and_contact_are_reused(self):
        """Spec 2: "acme supply" and "PAT@ACME.EXAMPLE" must both match what is already there."""
        company = make_company(owner=self.viewer, name="Acme Supply")
        make_contact(owner=self.viewer, company=company, first_name="Pat", email="pat@acme.example")
        companies_before = Company.objects.count()
        contacts_before = Contact.objects.count()

        response = self.client.post(
            reverse("lead_create"),
            desktop_payload(
                team="Sales",
                company_name="acme supply",
                email="PAT@ACME.EXAMPLE",
            ),
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(Company.objects.count(), companies_before, msg="the company name matches case-insensitively")
        self.assertEqual(Contact.objects.count(), contacts_before, msg="the contact email matches case-insensitively too")

        deal = Deal.objects.get()
        self.assertEqual(deal.company, company, msg="the new deal lands on the company that already existed")
        self.assertIsNone(deal.owner)
        self.assertEqual(deal.stage, "lead")

    def test_anonymous_get_redirects_to_login(self):
        """Spec 3: the desk form is for staff. `next=` is how it remembers where to come back to."""
        # setUp signs a Viewer in for the other three, because a lead has to be
        # postable by a Viewer. This one needs nobody at all, so sign them out
        # first — a logged-in GET of a login-protected page is a 200, and this
        # test would pass against a view with no protection whatsoever.
        self.client.logout()

        response = self.client.get(reverse("lead_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertIn("next=", response["Location"])

    def test_a_lead_with_no_message_is_refused(self):
        """Spec 4: `message` is required, and a refused POST writes nothing at all."""
        data = desktop_payload()
        del data["message"]

        response = self.client.post(reverse("lead_create"), data)

        self.assertEqual(response.status_code, 200, msg="a refused POST re-renders the form with its errors")
        self.assertIn("message", bound_form(response).errors)
        assertNothingCreated(self, "a lead with no message")


# -------------------------------------------------- the public form (website)


class PublicLeadTests(TestCase):
    """Spec items 5-11: lead_submit, open to anyone, and hardened accordingly."""

    def setUp(self):
        cache.clear()

    def test_the_page_is_public_and_carries_a_token_and_a_honeypot(self):
        """Spec 5: no login, a CSRF token (it is still a form), and a field nobody fills in."""
        response = self.client.get(reverse("lead_submit"))

        self.assertEqual(response.status_code, 200, msg="the public form must not ask a visitor to log in")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, 'name="website"', msg_prefix="the honeypot input has to be in the markup to trap anything")

    def test_anonymous_post_creates_an_unowned_lead(self):
        """Spec 6: the whole happy path, token and all."""
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))

        response = client.post(reverse("lead_submit"), public_payload(csrfmiddlewaretoken=token))

        self.assertRedirects(response, reverse("lead_thanks"), fetch_redirect_response=False)

        deal = Deal.objects.get()
        self.assertEqual(deal.stage, "lead")
        self.assertIsNone(deal.owner, msg="nothing owns a lead until an Admin assigns it")

        company = Company.objects.get()
        self.assertEqual(company.source, lead_source("Website"), msg="a public lead is always attributed to Website")
        self.assertEqual(Contact.objects.get().source, lead_source("Website"))

        self.assertIsNone(Activity.objects.get(deal=deal).author, msg="nobody is logged in, so the note has no author")

    @override_settings(PUBLIC_LEAD_TEAM="Partners")
    def test_the_no_login_form_files_under_the_configured_team(self):
        """
        PUBLIC_LEAD_TEAM: a business whose no-login form is an in-house desk names
        that team, and a `team` key in the POST still has nowhere to land.
        """
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))

        client.post(reverse("lead_submit"), public_payload(csrfmiddlewaretoken=token, team="Front desk"))

        self.assertEqual(Company.objects.get().source, lead_source("Partners"))
        self.assertEqual(Contact.objects.get().source, lead_source("Partners"))

    def test_posting_without_a_csrf_token_is_403(self):
        """Spec 7: the cookie is loaded, the form token is not sent. That is the forgery."""
        client = Client(enforce_csrf_checks=True)
        csrf_token(client, reverse("lead_submit"))  # a real session cookie, no matching form field

        response = client.post(reverse("lead_submit"), public_payload())

        self.assertEqual(response.status_code, 403)
        assertNothingCreated(self, "a POST with no CSRF token")

    def test_a_filled_honeypot_is_told_it_worked_and_creates_nothing(self):
        """Spec 8: 302 to thanks for the bot, an empty database for everyone else."""
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))

        response = client.post(
            reverse("lead_submit"),
            public_payload(csrfmiddlewaretoken=token, website="http://spam.example"),
        )

        self.assertRedirects(response, reverse("lead_thanks"), fetch_redirect_response=False)
        assertNothingCreated(self, "a submission with the honeypot filled in")

    def test_smuggled_deal_fields_are_ignored(self):
        """Spec 9: the form decides the deal's owner, stage and value. Not the browser."""
        admin = make_user("boss", "Admin")
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))

        client.post(
            reverse("lead_submit"),
            public_payload(
                csrfmiddlewaretoken=token,
                owner=admin.pk,
                stage="won",
                value="99999",
            ),
        )

        deal = Deal.objects.get()
        self.assertIsNone(deal.owner, msg="a POST does not get to choose the owner")
        self.assertEqual(deal.stage, "lead", msg="a POST does not get to skip the pipeline")
        self.assertEqual(deal.value, Decimal("0"), msg="nobody typed a price, so there is no price")

    def test_the_sixth_post_from_one_ip_in_an_hour_is_refused(self):
        """Spec 10: five an hour per IP, and the refused one leaves no trace."""
        ip = "203.0.113.10"

        for n in range(5):
            response = self.client.post(
                reverse("lead_submit"),
                public_payload(company_name=f"Bayside Clinic {n}"),
                REMOTE_ADDR=ip,
            )
            self.assertEqual(response.status_code, 302, msg=f"submission {n + 1} of 5 must go through")
        self.assertEqual(Deal.objects.count(), 5)

        response = self.client.post(
            reverse("lead_submit"),
            public_payload(company_name="Bayside Clinic 6"),
            REMOTE_ADDR=ip,
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Deal.objects.count(), 5, msg="the refused submission must not have been half-written")
        self.assertEqual(Company.objects.count(), 5, msg="no company for the refused submission either")

        # The limit is PER IP, so a different visitor is untouched. A single
        # global counter passes everything above and fails here.
        response = self.client.post(
            reverse("lead_submit"),
            public_payload(company_name="Harbour Vet"),
            REMOTE_ADDR="198.51.100.7",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Deal.objects.count(), 6)

    def test_a_message_containing_html_is_stored_and_rendered_as_text(self):
        """Spec 11: the note keeps the message exactly as written; the page shows it inert."""
        client = Client(enforce_csrf_checks=True)
        token = csrf_token(client, reverse("lead_submit"))
        client.post(
            reverse("lead_submit"),
            public_payload(csrfmiddlewaretoken=token, message="Wants a quote. <script>alert(1)</script>"),
        )
        deal = Deal.objects.get()
        self.assertEqual(
            Activity.objects.get(deal=deal).body,
            "Wants a quote. <script>alert(1)</script>",
            msg="what was submitted is what is stored — escaping is the template's job, not the model's",
        )

        self.client.force_login(make_user("boss", "Admin"))
        response = self.client.get(deal.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>", msg_prefix="the raw tag is live on the page")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


# ------------------------------- create_lead's two optional arguments


class CreateLeadOptionsTests(TestCase):
    """
    `create_lead` takes `match_company` and `external_ref`, so a second codebase
    can file a lead through the SAME function instead of growing its own.

    TEACHING NOTE — `match_company=False` exists for a caller whose form has
    no company field at all: there is nothing to match on, so two unrelated people
    who happen to share a name would be fused into one customer and their history
    split in half by a guess. A duplicate an Admin can merge later beats a merge
    nobody made. What makes that safe is the DEFAULT: the desk form and the public
    form do not pass the argument and must go on matching exactly as they always
    have. An argument added for one new caller that quietly changes the behaviour
    of the two old ones is a regression wearing a feature's coat, which is why
    `test_the_defaults_still_match_by_name` is here rather than assumed.
    """

    def setUp(self):
        cache.clear()

    def file_lead(self, **over):
        """
        One submission through `create_lead`, spelled out the way the QA-18 tests
        spell it. Called directly rather than through a view because these two
        arguments belong to the FUNCTION and no form in this app sends them; a view
        in the way would be testing the view.

        `**over` is how the two new arguments get in. A call that passes nothing
        through it is the default-caller shape, and one of these tests is only
        meaningful because that call exists.
        """
        payload = {
            "company_name": "John Smith",
            "first_name": "John",
            "last_name": "Smith",
            "email": "john.smith@example.test",
            "phone": "555-0100",
            "message": "Wants a quote",
            "team_name": "Front desk",
            "submitted_by": None,
        }
        payload.update(over)
        return create_lead(**payload)

    def test_match_company_false_creates_a_new_company_not_a_match(self):
        """
        The argument doing its job, against a company that already has that name.

        The third assertion is the one worth reading twice. A lead arriving for a
        name that already exists must leave the existing company exactly as it was
        — `company.source` is where an account originally came from, and rewriting it
        because the newest phone call said Front desk throws away the trade show
        the account was actually found at. That is history; a lead is not.
        """
        referral = LeadSource.objects.create(name="Referral")
        existing = make_company(name="John Smith", source=referral)

        deal = self.file_lead(match_company=False)

        self.assertEqual(
            Company.objects.filter(name__iexact="john smith").count(),
            2,
            msg="match_company=False has to make its own Company, not adopt the one that already exists",
        )
        self.assertNotEqual(
            deal.company,
            existing,
            msg="the new deal has to hang off the company this call created",
        )
        self.assertEqual(
            Company.objects.get(pk=existing.pk).source,
            referral,
            msg="and the company that was already there keeps its own origin",
        )

    def test_external_ref_lands_on_the_note_and_defaults_to_empty(self):
        """
        The caller's own id rides along, and everybody else's row is unchanged.

        TEACHING NOTE — the default is `""` and not None, because
        `Activity.external_ref` is `blank=True` and NOT null: every lead this app
        filed before the argument existed stores an empty string there. A default
        of None would start writing NULL into a column that has only ever held
        an empty string, and every later `filter(external_ref=...)` would silently
        miss half the table. Asserted against the DATABASE for the same reason as
        everywhere else in this file: a stale in-memory instance cannot tell an
        empty string from a NULL.
        """
        deal = self.file_lead(external_ref="ref-123")

        self.assertEqual(
            Activity.objects.get(deal=deal).external_ref,
            "ref-123",
            msg="the reference has to reach the note, or a resend has nothing to find the deal by",
        )

        plain = self.file_lead(company_name="Harbour Vet")

        self.assertEqual(
            Activity.objects.get(deal=plain).external_ref,
            "",
            msg="a caller that says nothing about a reference leaves that column exactly as it always was",
        )

    def test_the_defaults_still_match_by_name(self):
        """
        The regression guard: a caller passing NEITHER argument gets the old
        behaviour, which is the only reason this was allowed to happen at all.

        No `over` at all, so this is the exact call the desk form and the public
        form make. Written as its own test rather than as an extra assertion
        inside the one above, because it fails for a different reason: the other
        two fail when the new arguments are ignored, this one fails when they
        are wired in too eagerly.
        """
        existing = make_company(name="John Smith")

        deal = self.file_lead()

        self.assertEqual(
            Company.objects.filter(name__iexact="john smith").count(),
            1,
            msg="the default still matches the company that is already there, case and all",
        )
        self.assertEqual(
            deal.company,
            existing,
            msg="and the lead lands on it rather than minting a twin",
        )


# ------------------------------------------------------ the queue and assigning


class LeadQueueTests(TestCase):
    """Spec items 12, 13 and 16: the new-leads queue, and the badge that counts it."""

    def setUp(self):
        cache.clear()
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep")
        self.viewer = make_user("watch1", "Viewer")
        self.company = make_company(name="Lakeside Dental")

    def test_the_queue_lists_unowned_leads_and_nothing_else(self):
        """Spec 12: unowned AND lead-stage. Either test on its own is not the queue."""
        waiting = make_deal(self.company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)
        also_waiting = make_deal(self.company, owner=None, name="Riverbend Vet - quote", stage=Stage.LEAD)
        owned = make_deal(self.company, owner=self.admin, name="Northside Clinic - renewal", stage=Stage.LEAD)
        later = make_deal(self.company, owner=None, name="Westgate - expansion", stage=Stage.PROPOSAL)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("lead_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, waiting.name)
        self.assertContains(response, also_waiting.name)
        self.assertNotContains(response, owned.name, msg_prefix="an assigned deal is somebody's work, not a new lead")
        self.assertNotContains(response, later.name, msg_prefix="a deal already past Lead is not waiting to be picked up")

    def test_the_queue_is_for_admins_and_reps(self):
        """
        Spec 13: a Rep works the queue, a Viewer is still kept out of it.

        TEACHING NOTE — the queue widened but `lead_assign` did not, and this test
        only pins half of that. A Rep can now SEE a lead nobody owns and pick it up
        for themselves (LeadClaimTests); a Rep still cannot hand a lead to a
        colleague, which is `test_a_rep_cannot_assign` in LeadAssignmentTests. A
        Viewer may file leads at /leads/new/ and may not read the queue, so this
        is the one status code here that has to stay 403.
        """
        for who in (self.admin, self.rep):
            with self.subTest(username=who.username):
                self.client.force_login(who)
                response = self.client.get(reverse("lead_queue"))
                self.assertEqual(
                    response.status_code,
                    200,
                    msg=f"{who.username} has to be able to open the queue to work it",
                )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("lead_queue"))
        self.assertEqual(
            response.status_code,
            403,
            msg="a Viewer can file a lead and cannot read one anybody else owns",
        )

    def test_the_dashboard_badge_counts_the_queue_for_admins_and_reps(self):
        """
        Spec 16: the badge is how anybody finds the queue, so it has to agree with it.

        TEACHING NOTE — a Rep now gets the count because a Rep can now ACT on it:
        that is what makes the queue findable. A Viewer gets no number at all, and
        `response.context.get("new_lead_count", 0)` is the assertion for that —
        the key is simply absent, so a hardcoded `== 0` would pass against a view
        that computed the count and then displayed nothing.
        """
        make_deal(self.company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)
        make_deal(self.company, owner=None, name="Riverbend Vet - quote", stage=Stage.LEAD)
        make_deal(self.company, owner=self.admin, name="Northside Clinic - renewal", stage=Stage.LEAD)
        make_deal(self.company, owner=None, name="Westgate - expansion", stage=Stage.PROPOSAL)

        for who in (self.admin, self.rep):
            with self.subTest(username=who.username):
                self.client.force_login(who)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context.get("new_lead_count"),
                    2,
                    msg="the badge is the queue size: two unowned Lead-stage deals, nothing else counted",
                )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(
            response.context.get("new_lead_count", 0),
            0,
            msg="a Viewer cannot open the queue, so a Viewer has nothing to be reminded of",
        )


class LeadAssignmentTests(TestCase):
    """Spec items 14 and 15: assignment sets the owner, leaves a to-do, and refuses
the people who cannot do the job."""

    def setUp(self):
        cache.clear()
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep")
        self.company = make_company(name="Lakeside Dental")

    def test_assignment_sets_the_owner_and_raises_a_task(self):
        """
        Spec 14, first half: an Admin assigns a lead, and the Rep it went to gets a
        task to call the company, due tomorrow.

        TEACHING NOTE on why this is a test of its own rather than a second half of
        the "a Rep cannot" one below: two actors, two different code paths through
        the same view, and one broken path must not stop the other being reported.
        `subTest` would do that, but it also reports every subtest twice, so the
        summary at the end of a run counts more errors than there are tests — which
        is the last thing a red-first suite needs. Separate tests stay countable.
        """
        deal = make_deal(self.company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)
        self.client.force_login(self.admin)

        response = self.client.post(reverse("lead_assign", args=[deal.pk]), {"owner": self.rep.pk})

        # Deliberately not pinned to 302: the spec fixes the EFFECT, not whether
        # the view bounces to the queue or re-renders it.
        self.assertIn(
            response.status_code,
            (200, 302),
            msg=f"a successful assignment answered {response.status_code}",
        )
        deal.refresh_from_db()
        self.assertEqual(deal.owner, self.rep, msg="assigning a lead is what gives it an owner")

        task = Task.objects.get(assignee=self.rep, deal=deal)
        self.assertTrue(
            task.title.startswith("Contact new lead"),
            msg=f'the task is titled "Contact new lead: <company>"; got {task.title!r}',
        )
        self.assertIn(self.company.name, task.title, msg="the title names the company to call")
        now = timezone.now()
        self.assertIsNotNone(task.due_at, msg="a task with no due date is the task nobody does")
        self.assertLessEqual(task.due_at, now + timedelta(days=2), msg="due tomorrow")
        self.assertGreaterEqual(task.due_at, now - timedelta(hours=1), msg="due tomorrow, not last month")

    def test_a_rep_cannot_assign(self):
        """Spec 14, second half: 403, and the deal is still nobody's."""
        deal = make_deal(self.company, owner=None, name="Riverbend Vet - quote", stage=Stage.LEAD)
        self.client.force_login(self.rep)

        response = self.client.post(reverse("lead_assign", args=[deal.pk]), {"owner": self.rep.pk})

        self.assertEqual(response.status_code, 403)
        deal.refresh_from_db()
        self.assertIsNone(deal.owner, msg="a refused assignment must not quietly succeed")
        self.assertEqual(Task.objects.count(), 0, msg="a refused assignment raises no task")

    def test_an_inactive_user_or_a_viewer_cannot_be_given_a_lead(self):
        """
        Spec 15: the dropdown only lists people who can actually do the job.

        TEACHING NOTE: an inactive user fails at the auth layer — a task assigned
        to them can never be completed, so the lead is nobody's again. A Viewer
        fails for a different reason: they may not edit records they don't own,
        and they would be handed one they don't. Either way the refusal has to be
        the SERVER's, because the dropdown is only politeness.
        """
        gone = make_user("gone", "Rep")
        gone.is_active = False
        gone.save()
        # Why an inactive user is not even in the running: they cannot sign in,
        # so a task assigned to them can never be ticked off and the lead is
        # nobody's again. This asserts that premise rather than assuming it.
        self.assertFalse(
            self.client.login(username="gone", password=PASSWORD),
            msg="an inactive Rep must not be able to authenticate at all",
        )

        watcher = make_user("watch1", "Viewer")
        deal = make_deal(self.company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)
        self.client.force_login(self.admin)

        for who in (gone, watcher):
            with self.subTest(username=who.username):
                response = self.client.post(reverse("lead_assign", args=[deal.pk]), {"owner": who.pk})

                # Either a rejection status, or the form re-rendered with errors —
                # the spec allows both, and this accepts both.
                self.assertIn(
                    response.status_code,
                    (200, 400),
                    msg=f"assigning to {who.username} answered {response.status_code}, which is neither a refusal nor a form error",
                )
                if response.status_code == 200:
                    self.assertTrue(
                        bound_form(response).errors,
                        msg=f"assigning to {who.username} re-rendered the page with no error to show",
                    )
                deal.refresh_from_db()
                self.assertIsNone(deal.owner, msg=f"{who.username} must not have become the owner")
                self.assertEqual(Task.objects.count(), 0, msg=f"no task for {who.username}")


# ------------------------------------------------------------ picking one up


class LeadClaimTests(TestCase):
    """
    `lead_claim`: a Rep takes an unowned Lead-stage deal for themselves.

    TEACHING NOTE — this class is separate from LeadAssignmentTests because
    claiming is the OPPOSITE act, and a shared test class would hide which one
    broke. `lead_assign` puts a call on somebody else's diary and tells them;
    `lead_claim` puts it on yours and tells nobody, because you were there. Every
    assertion below is about what the database says afterwards — who owns the row,
    how many tasks exist, what the loser was shown — because that is the only part
    of a race two requests can both pass.

    The user set is two Reps on purpose. One Rep cannot test a race.
    """

    def setUp(self):
        cache.clear()
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep")
        self.other_rep = make_user("rep2", "Rep")
        self.company = make_company(name="Lakeside Dental")
        self.deal = make_deal(self.company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)

    def test_a_rep_picks_a_lead_up_and_gets_a_to_do_and_no_notification(self):
        """
        The whole happy path: owner set, one HIGH task due tomorrow, and silence.

        TEACHING NOTE — `captureOnCommitCallbacks(execute=True)` is the point of the
        notification assertion, not decoration. A notification is written by a
        `transaction.on_commit` callback, and in a `TestCase` that callback never
        runs: the outer transaction is rolled back at the end of the test. So the
        naive `assertFalse(Notification.objects...)` would pass against a view that
        DID notify, because the row was never written. Forcing the callbacks to
        execute is what makes the assertion mean anything.
        """
        self.client.force_login(self.rep)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("lead_claim", args=[self.deal.pk]))

        # Straight to the deal, not back to the queue: the Rep's next move is to
        # read what the customer actually asked for.
        self.assertRedirects(response, self.deal.get_absolute_url(), fetch_redirect_response=False)

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.owner, self.rep, msg="picking a lead up is what gives it an owner")

        self.assertEqual(
            Task.objects.filter(assignee=self.rep).count(),
            1,
            msg="exactly one to-do, and it belongs to the Rep who took the lead",
        )
        task = Task.objects.get()
        self.assertEqual(task.title, f"Contact new lead: {self.company.name}")
        self.assertEqual(task.deal, self.deal)
        self.assertEqual(task.priority, Task.Priority.HIGH, msg="a new lead is the most urgent row in the queue")
        # "Due tomorrow" is a datetime about a day out, not midnight tonight — so
        # this brackets it against `now` rather than against a date boundary. The
        # hour either side is slack for how long the request took.
        now = timezone.now()
        self.assertGreaterEqual(task.due_at, now + timedelta(hours=23), msg="due tomorrow, not right now")
        self.assertLessEqual(task.due_at, now + timedelta(hours=25), msg="due tomorrow, not next week")

        self.assertFalse(
            Notification.objects.filter(user=self.rep).exists(),
            msg="you do not notify yourself that you picked the lead up yourself",
        )

        latest = self.deal.history.first()
        self.assertEqual(latest.owner_id, self.rep.pk, msg="the claim has to reach the deal's change log")
        self.assertEqual(latest.history_user, self.rep, msg="and the log has to say who took it")

    def test_the_second_rep_to_click_the_button_loses_and_is_told_so(self):
        """
        Two Reps, one lead, in order: the database gives it to exactly one of them.

        TEACHING NOTE — this is SEQUENTIAL, and it is worth being honest about what
        that buys. Two ordinary POSTs in a row catch the whole family of bugs where
        a claim ignores the row's current state: the second rep overwrites the first,
        gets a second to-do, or is redirected to the deal as if all went well. It
        does NOT catch the read-then-save race, because a fresh read sees the first
        claim and a stale read needs genuine concurrency that a single-transaction
        `TestCase` cannot produce. (Swap the view's conditional UPDATE for a
        load-then-save and this test still passes.) The atomicity guarantee is pinned
        by `test_the_claim_is_one_conditional_update` below; this one pins the
        behaviour around it.
        """
        self.client.force_login(self.rep)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("lead_claim", args=[self.deal.pk]))

        self.client.force_login(self.other_rep)
        with self.captureOnCommitCallbacks(execute=True):
            # follow=True so the assertion can read the page the loser lands on.
            # `assertRedirects` returns None and leaves `response` as the 302, so
            # asserting the message off it would be asserting against a redirect
            # body that never contained the message at all.
            response = self.client.post(reverse("lead_claim", args=[self.deal.pk]), follow=True)

        self.assertRedirects(response, reverse("lead_queue"))
        self.assertContains(
            response,
            "already picked up",
            msg_prefix="the loser has to be told, or they ring the customer and find out the hard way",
        )

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.owner, self.rep, msg="the first Rep keeps it; the second must not steal it")
        self.assertEqual(Task.objects.count(), 1, msg="the loser gets no to-do for a lead they do not have")
        self.assertEqual(Task.objects.get().assignee, self.rep)

    def test_a_rep_cannot_pick_up_a_deal_that_is_past_the_lead_stage(self):
        """
        Stage is half of the lock, not decoration.

        A deal past Lead has been qualified, quoted or lost by somebody; it is not
        waiting to be picked up, and letting the newest Rep grab it hands them work
        that has already been decided.
        """
        deal = make_deal(self.company, owner=None, name="Westgate - expansion", stage=Stage.PROPOSAL)
        self.client.force_login(self.rep)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("lead_claim", args=[deal.pk]))

        self.assertRedirects(response, reverse("lead_queue"))
        deal.refresh_from_db()
        self.assertIsNone(deal.owner)
        self.assertEqual(Task.objects.count(), 0, msg="a refused claim raises no to-do")

    def test_the_claim_is_one_conditional_update_not_a_read_then_save(self):
        """
        Pins the actual guarantee: the lock lives in the WHERE clause.

        TEACHING NOTE — this is a white-box test and it is here on purpose, because
        the property is a property of the SQL. `TestCase` wraps each test in one
        transaction, so two requests in it can never be genuinely concurrent, and a
        behavioural test cannot tell a conditional UPDATE from a load-then-save that
        happens to be correct when nothing else is writing. Both versions agree on
        every sequential POST; only one of them is safe when two Reps really are
        clicking at once.

        So the test asserts what reaches the database: the FIRST UPDATE against the
        deals table carries BOTH conditions. Rewriting the view as
        `deal = Deal.objects.get(pk=pk)` followed by `deal.save()` makes the first
        write an unconditional UPDATE and fails both assertions — and the
        conditional one cannot silently decay into `filter(pk=pk)` while the suite
        is green. A second, plain UPDATE follows a WON claim on purpose: it is the
        save that writes the simple-history row (`QuerySet.update()` writes none),
        and it runs only after the conditional one has already returned 1.

        Filtering on the table rather than on "UPDATE" at all: the session row is
        also updated during the request, and asserting on every UPDATE would fail
        for a reason that has nothing to do with the view.
        """
        self.client.force_login(self.rep)

        with CaptureQueriesContext(connection) as queries:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("lead_claim", args=[self.deal.pk]))

        table = Deal._meta.db_table
        updates = [q["sql"] for q in queries.captured_queries
                   if q["sql"].lstrip().upper().startswith("UPDATE") and f'"{table}"' in q["sql"]]

        self.assertEqual(len(updates), 2, msg=f"expected the conditional claim then the history save, got {updates}")
        self.assertIn(
            f'"{table}"."owner_id" IS NULL',
            updates[0],
            msg="the unowned check has to be in the WHERE clause, not in Python before the write",
        )
        self.assertIn(
            f'"{table}"."stage"',
            updates[0],
            msg="the Lead-stage check has to be in the WHERE clause too",
        )

    def test_a_viewer_is_refused_and_a_stranger_is_sent_to_the_login_page(self):
        """
        The role check, and the decorator order that leaks less.

        TEACHING NOTE — the anonymous POST answering 302 and not 405 is the assertion
        that `@login_required` sits OUTSIDE `@require_POST`, exactly as on
        `lead_assign`. Swap the two decorators and a stranger learns this endpoint
        exists before learning they are allowed to use it.
        """
        self.client.force_login(make_user("watch1", "Viewer"))
        response = self.client.post(reverse("lead_claim", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 403)

        self.client.logout()
        response = self.client.post(reverse("lead_claim", args=[self.deal.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertIn("next=", response["Location"], msg="the login page has to remember where to come back to")

        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.owner, msg="neither attempt may quietly succeed")
        self.assertEqual(Task.objects.count(), 0)

    def test_a_get_is_refused(self):
        """Claiming is a POST. A GET could be triggered by a link, so it must not act."""
        self.client.force_login(self.rep)

        response = self.client.get(reverse("lead_claim", args=[self.deal.pk]))

        self.assertEqual(response.status_code, 405)
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.owner, msg="a GET must not pick the lead up as a side effect")

    def test_a_deal_that_does_not_exist_is_a_404(self):
        """
        The loser branch distinguishes "gone" from "too late".

        Without the `get_object_or_404`, any unclaimable pk produces the same "somebody
        beat you to it" message — including pks that were never a deal at all, so a
        Rep typing a wrong id is told a story about a colleague.
        """
        self.client.force_login(self.rep)

        response = self.client.post(reverse("lead_claim", args=[self.deal.pk + 1000]))

        self.assertEqual(response.status_code, 404)

    def test_the_queue_offers_a_rep_a_pick_up_and_an_admin_both_buttons(self):
        """
        The template's half of the permission split.

        TEACHING NOTE — this asserts on the ASSIGN URL rather than on the word
        "assign", because a Rep's page must not contain a working way to hand a lead
        to a colleague, and the URL is the thing a hand-written POST would reuse.
        `name="owner"` is the assign form's widget, so its absence is the direct
        check that the dropdown is not merely hidden but gone.
        """
        self.client.force_login(self.rep)
        response = self.client.get(reverse("lead_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("lead_claim", args=[self.deal.pk]))
        self.assertContains(response, "Pick up")
        self.assertNotContains(response, reverse("lead_assign", args=[self.deal.pk]))
        self.assertNotContains(response, 'name="owner"', msg_prefix="a Rep must not get the assign dropdown")

        self.client.force_login(self.admin)
        response = self.client.get(reverse("lead_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("lead_claim", args=[self.deal.pk]))
        self.assertContains(response, reverse("lead_assign", args=[self.deal.pk]))
        self.assertContains(response, 'name="owner"')


class TeamDropdownTests(TestCase):
    """Found in review: LeadSource also holds non-team sources (a "Referral", anything
    the CSV importer creates), and the desk form offered them."""

    def test_the_desk_form_offers_only_the_four_staff_teams(self):
        from core.models import LeadSource
        LeadSource.objects.get_or_create(name="Referral")
        LeadSource.objects.get_or_create(name="Trade show 2025")
        self.client.force_login(make_user("desk", "Viewer"))
        choices = [label for _, label in self.client.get(reverse("lead_create")).context["form"].fields["team"].choices]
        self.assertEqual(sorted(choices), ["Front desk", "Partners", "Sales", "Support"])

    def test_lead_teams_setting_names_the_desks(self):
        """A business with other desks lists them in LEAD_TEAMS. A name with no
        LeadSource row is left off rather than offered and then refused."""
        from core.models import LeadSource
        LeadSource.objects.get_or_create(name="Wholesale")
        self.client.force_login(make_user("desk", "Viewer"))
        with self.settings(LEAD_TEAMS=["Wholesale", "Front desk", "No such desk"]):
            response = self.client.get(reverse("lead_create"))
            # Inside the block: the choices are a lazy callable, read on iteration.
            choices = [label for _, label in response.context["form"].fields["team"].choices]
        self.assertEqual(sorted(choices), ["Front desk", "Wholesale"])
        self.assertContains(response, '<option value="Wholesale">')


# ------------------------------------------- the Company box's datalist


class CompanySuggestionTests(TestCase):
    """
    `lead_company_suggestions`: the names that already exist, offered to whoever
    is typing into the desk form's Company box.

    TEACHING NOTE — every test here signs in as a VIEWER, because Viewers are the
    whole reason this feature exists: the techs who answer the phone are
    Viewers, and `lead_create` is the one create page a Viewer may use. An
    endpoint that gated on REP or Admin would be gating away from its own
    audience, and a role check added "for consistency" would leave the desk form
    with an input that silently suggests nothing. There is no matching negative
    test here saying a Viewer gets 403, because that is not the design: a Viewer
    can already read every company at /companies/, so the names leak nothing the
    same user could not walk in and read.

    The assertions are made against `response.content`, not against
    `response.context["company_names"]`, wherever the point is what the BROWSER
    would see. This endpoint's entire contract is a fragment of markup, and a view
    that put the right strings in the context and rendered the wrong template
    would pass every context assertion and show nobody a single suggestion.
    """

    def setUp(self):
        self.viewer = make_user("watch1", "Viewer")
        self.client.force_login(self.viewer)

    def suggest(self, query):
        """
        GET the endpoint as the signed-in Viewer. `query=None` omits the
        parameter entirely, which is not the same request as an empty one — an
        absent key and `?company_name=` have to behave identically, and only the
        first of the two is what a browser sends before anybody types.
        """
        data = {} if query is None else {"company_name": query}
        return self.client.get(reverse("lead_company_suggestions"), data)

    def test_a_viewer_is_offered_the_matching_names_and_only_those(self):
        """Spec 1: substring match, and nothing that does not contain it."""
        make_company(name="Lakeside Dental")
        make_company(name="Lakeview Vet")
        make_company(name="Riverbend")

        response = self.suggest("lake")

        self.assertEqual(response.status_code, 200, msg="the signed-in Viewer who types in the box must get an answer")
        body = response.content.decode()
        self.assertIn('value="Lakeside Dental"', body)
        self.assertIn('value="Lakeview Vet"', body)
        self.assertNotIn("Riverbend", body, msg="a company that does not contain the query is not a suggestion")

    def test_the_search_ignores_case(self):
        """
        Spec 2: `icontains`, so "LAKE" finds what "lake" finds.

        TEACHING NOTE — worth pinning because the matching elsewhere in lead
        intake is `iexact` and somebody tidying this endpoint up would reach for
        it, which would make "LAKESIDE" find nothing and the dropdown look broken
        exactly when a person is typing in capitals having given up on autocompletion.
        """
        make_company(name="Lakeside Dental")
        make_company(name="Lakeview Vet")
        make_company(name="Riverbend")

        response = self.suggest("LAKE")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('value="Lakeside Dental"', body)
        self.assertIn('value="Lakeview Vet"', body)

    def test_one_character_nothing_at_all_returns_no_options(self):
        """
        Spec 3: under two characters is under the floor.

        The `None` case is the one that matters. `request.GET.get("company_name",
        "")` returns "" for a missing key, and a view that tested the key's
        presence instead of the string's length would raise on a request that a
        browser makes every single time the desk form loads.
        """
        make_company(name="Lakeside Dental")

        for query in ("l", " L ", "", None):
            with self.subTest(company_name=query):
                response = self.suggest(query)

                self.assertEqual(response.status_code, 200, msg="a too-short query is not an error, it is no suggestions")
                self.assertNotIn("<option", response.content.decode(), msg="a one-letter query returned options")

    def test_ten_names_at_most_and_in_name_order(self):
        """
        Spec 4: the cap, and alphabetical.

        TEACHING NOTE — fifteen companies all called "Lakeside Clinic 01".."15",
        zero-padded so the sort is the one a reader expects rather than SQLite's
        byte order on "10" before "2". Unpadded, this test would still pass the
        COUNT and could not tell you the ordering is right at all, because the
        expected list would itself be in the wrong order.

        Ten, not fifteen, and not everything: a datalist is a hint, and a hint
        with fifteen near-identical entries is a list the tech has to read.
        """
        for n in range(15):
            make_company(name=f"Lakeside Clinic {n:02d}")

        response = self.suggest("lakeside")

        self.assertEqual(response.status_code, 200)
        offered = re.findall(r'value="([^"]*)"', response.content.decode())
        self.assertEqual(
            offered,
            [f"Lakeside Clinic {n:02d}" for n in range(10)],
            msg=f"expected the first ten in name order, got {offered}",
        )

    def test_a_name_containing_html_comes_back_inert(self):
        """
        Spec 5: escaped, never `|safe`.

        THE ONE THAT MATTERS on this endpoint. A company name is attacker-supplied
        — it arrives through the public lead form, which is the most attacked page
        in the app — and the suggestion list is served to a signed-in member of
        staff, who is a much better target than the stranger who typed the name. A
        single `|safe` in the partial turns a stored company name into stored XSS
        fired at the next tech who opens the desk form.

        Asserted on the raw body rather than with `assertContains`, because
        `assertContains` escapes the needle before looking for it, and here the
        needle IS already escaped: passing the escaped string would search for
        `&amp;lt;b&amp;gt;` and pass on nothing.
        """
        make_company(name="<b>Probe</b> & Co")

        response = self.suggest("probe")

        body = response.content.decode()
        self.assertIn('value="&lt;b&gt;Probe&lt;/b&gt; &amp; Co"', body, msg="the option value is not escaped")
        self.assertNotIn("<b>Probe", body, msg="a raw tag in the option list is stored XSS")

    def test_a_stranger_is_sent_to_the_login_page_and_learns_nothing(self):
        """Spec 6: the customer list is behind the login, and the body says nothing."""
        make_company(name="Lakeside Dental")
        make_company(name="Lakeview Vet")
        self.client.logout()

        response = self.suggest("lake")

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"], msg="the login page has to be where they are sent")
        self.assertNotIn("Lakeside Dental", response.content.decode(), msg="a redirect body leaked a company name")
        self.assertNotIn("Lakeview Vet", response.content.decode())

    def test_a_post_is_refused(self):
        """
        Spec 7: 405.

        The counterpart to the decorator order on `lead_claim`: GET-only because a
        GET is what a link can trigger and this endpoint must never act, and
        because nothing here writes. Posted as the Viewer on purpose — an
        anonymous POST answering 302 instead would be the login check firing first,
        which passes this test and means nothing about `@require_GET`.
        """
        make_company(name="Lakeside Dental")

        response = self.client.post(reverse("lead_company_suggestions"), {"company_name": "lake"})

        self.assertEqual(response.status_code, 405)

    def test_the_desk_form_is_wired_to_the_endpoint_and_the_public_form_is_not(self):
        """
        Spec 8: the markup, in both directions.

        Two halves because they fail for different reasons. The desk form needs
        `list`, `hx-get` and the `<datalist>` target to agree with each other; a
        form with the attrs but no datalist element requests suggestions nobody
        can put anywhere, and there is no error message for that. The public form
        half is the SECURITY one: it must not so much as NAME the endpoint, and
        the assertion is on `hx-get` as well as on the URL, because the failure
        mode worth catching is an attribute left behind that would point a
        stranger's keystrokes at the customer list on every character they type.
        """
        url = reverse("lead_company_suggestions")

        response = self.client.get(reverse("lead_create"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('list="company-options"', body, msg="the input has to point at the datalist")
        self.assertIn(f'hx-get="{url}"', body, msg="the input has to ask the endpoint for suggestions")
        self.assertIn('<datalist id="company-options">', body, msg="and the options need somewhere to land")

        self.client.logout()
        response = self.client.get(reverse("lead_submit"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for needle in ("company-options", "hx-get", url):
            self.assertNotIn(needle, body, msg=f"the public form must not mention {needle!r}")

    def test_a_picked_name_does_not_mint_a_second_company(self):
        """
        Spec 9: the round trip the other eight are for.

        `leads._match_or_create_company` matches on `name__iexact`, so the name a
        tech CLICKS is the stored spelling and matches. The bug this feature
        exists to stop is a typed one: "Lakeside Dentl" matches nothing, creates a
        second company that reads identically in /companies/, and splits the
        account's history in half. So the assertion is not that the POST worked —
        it is that the company COUNT is still one and the new deal sits on the row
        that was already there.

        The form is driven through `desktop_payload` so this stays the same POST a
        browser makes after somebody clicks a suggestion, and the company name is
        spelled the way the datalist would have delivered it, byte for byte as
        stored.
        """
        existing = make_company(name="Lakeside Dental")

        response = self.client.post(reverse("lead_create"), desktop_payload(company_name="Lakeside Dental"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Company.objects.filter(name__iexact="lakeside dental").count(),
            1,
            msg="a name taken from the datalist has to hit the existing company, not create a twin",
        )
        deal = Deal.objects.get()
        self.assertEqual(deal.company, existing, msg="the lead lands on the company that already existed")


# ------------------------------------- the queue's Team column (QA-18)


class LeadQueueTeamColumnTests(TestCase):
    """
    QA-18: the Team column has to answer "who SENT this lead", not "where did this
    customer first come from".

    Two columns used to be one column. `Company.source` is where the account
    originally came from — a trade show, a referral, a phone book — and
    `leads._match_or_create_company` refuses to overwrite it, because that is a
    fact about history rather than about the newest phone call. Which left the
    queue reading a field that is wrong for most of its rows: most people who call
    a company about a new order are people who have ordered before, and every
    one of those leads showed the original source instead of the desk that took
    the call. `Deal.source` is the second column, and this class pins what the
    template does with it.

    TEACHING NOTE — the assertions in here are made on ONE ROW, never on the whole
    page. `create_lead` names the deal "<company> — new lead from Front desk", so
    the words "Front desk" appear in that row's own first cell: a page-wide
    `assertContains` would be satisfied by the deal NAME and would happily pass
    against a template that deleted the Team column outright. `team_cell` finds
    the row by the deal's detail URL and reads the third `<td>` out of it.
    """

    def setUp(self):
        cache.clear()
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep")
        self.client.force_login(self.admin)

    def file_lead(self, company_name, team_name):
        """
        One submission through `create_lead` — the one function both forms call, so
        this is the desk form and the public form at once, with no CSRF token and
        no form in the way.
        """
        return create_lead(
            company_name=company_name,
            first_name="Pat",
            last_name="Doe",
            email="pat@crownpoint.example",
            phone="555-0100",
            message="Wants a quote",
            team_name=team_name,
            submitted_by=None,
        )

    def queue(self):
        """The queue page as the signed-in Admin, with its status code checked."""
        response = self.client.get(reverse("lead_queue"))
        self.assertEqual(response.status_code, 200)
        return response

    def team_cell(self, response, deal):
        """
        The Team `<td>` of `deal`'s row, as the browser receives it.

        TEACHING NOTE — the row is found by the deal's own detail URL rather than
        by its name, because the name is attacker-supplied text and two leads off
        the same company share the first half of it. The URL is one row and
        nowhere else on the page.
        """
        url = reverse("deal_detail", args=[deal.pk])
        rows = [chunk for chunk in response.content.decode().split("<tr>") if f'href="{url}"' in chunk]
        self.assertEqual(len(rows), 1, msg=f"expected one row for deal {deal.pk}, found {len(rows)}")
        cells = re.findall(r"<td>(.*?)</td>", rows[0], flags=re.DOTALL)
        self.assertEqual(len(cells), 5, msg="the queue table is Company, Contact, Team, Submitted, Action")
        return cells[2]

    def test_the_team_column_names_the_desk_that_filed_the_lead(self):
        """
        The defect itself: an EXISTING customer whose company came from a
        referral, and a lead filed for them by the front desk.

        TEACHING NOTE — "Referral" is made here rather than fetched from a fixture:
        it is not one of the five teams migration 0004 seeds, it is what the demo
        seed and the CSV importer produce, and that is exactly the shape of real
        account history. Asserted against the database rather than against the
        in-memory objects, because the whole bug is two fields being confused and
        a cached instance could hide a `save()` that was never written.
        """
        referral = LeadSource.objects.create(name="Referral")
        existing = make_company(name="Crown Point Dental", source=referral)

        filed = self.file_lead("Crown Point Dental", team_name="Front desk")

        deal = Deal.objects.get(pk=filed.pk)
        self.assertEqual(deal.source.name, "Front desk", msg="the deal records who sent it")
        self.assertEqual(
            Company.objects.get(pk=existing.pk).source.name,
            "Referral",
            msg="and the company's own origin is left exactly as it was — that is history, not the newest call",
        )
        cell = self.team_cell(self.queue(), deal)
        self.assertIn('<span class="pill">Front desk</span>', cell, msg="the Team cell must name the desk")
        self.assertNotIn("Referral", cell, msg="the company's original source is the wrong answer to this question")

    def test_a_lead_with_no_team_of_its_own_falls_back_to_the_company(self):
        """
        The fallback: a Lead-stage deal with `source=None` — every lead filed
        before this column existed — still shows something.

        TEACHING NOTE — this is the migration's whole compatibility story in one
        assertion. 0009 adds a nullable column and backfills nothing, on purpose,
        so any queue written since then has a pile of NULLs in it, and rendering
        a dash for every one of them would be a queue that suddenly looks broken.
        The fallback costs nothing and keeps those rows reading exactly as they
        did yesterday, which is the only thing a dash-free queue can honestly
        claim about them.
        """
        website = lead_source("Website")
        company = make_company(name="Lakeside Dental", source=website)
        deal = make_deal(company, owner=None, name="Lakeside Dental - new office", stage=Stage.LEAD)
        self.assertIsNone(deal.source, msg="a deal nothing ever set a source on has to stay NULL")

        cell = self.team_cell(self.queue(), deal)

        self.assertIn('<span class="pill">Website</span>', cell, msg="an unattributed lead falls back to the company")

    def test_the_team_column_costs_no_query_per_row(self):
        """
        One row and three rows have to cost the same number of queries.

        TEACHING NOTE — this is what `select_related("source")` in
        `views_leads._unowned_leads` is for. The Team column is a second foreign
        key read inside the same loop as the first one, and a template reaching
        for `deal.source` without the JOIN behind it is an N+1 that only shows up
        on a queue with forty leads in it. The count is the WHOLE request, not
        the queue's query, because counting only `core_deal` would miss the more
        interesting half: `company__source` is the fallback that most rows
        actually render, and an N+1 there would never show up in a count of one
        table's queries.

        Measured as a REP, deliberately. An Admin's page carries one extra query
        per row from the assign dropdown, which is a pre-existing N+1 inside
        `forms_leads.LeadAssignForm` — a ModelChoiceField re-clones its queryset
        for every row it renders — and it has nothing to do with the Team column.
        A Rep sees no dropdown, so every query left in the count is one of ours.
        """
        # Both foreign keys SET, on the company and on every deal: Django never
        # queries for a NULL FK, so a row with no source would cost nothing with or
        # without the JOIN and this count could not tell the difference.
        company = make_company(name="Lakeside Dental", source=lead_source("Website"))
        desk = lead_source("Front desk")
        make_deal(company, owner=None, name="Plan upgrade", stage=Stage.LEAD, source=desk)
        self.client.force_login(self.rep)
        # One throwaway GET first: the very first request of a test session
        # carries the session cookie's own bookkeeping, and counting that as the
        # one-row baseline would make the comparison prove nothing.
        self.queue()
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("lead_queue"))
        one_row = len(queries.captured_queries)

        for n in range(2):
            make_deal(company, owner=None, name=f"Plan upgrade {n}", stage=Stage.LEAD, source=desk)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("lead_queue"))
        three_rows = len(queries.captured_queries)

        self.assertEqual(
            three_rows,
            one_row,
            msg=f"the queue ran {one_row} queries with one row and {three_rows} with three: "
            "every extra row is a missing select_related",
        )
