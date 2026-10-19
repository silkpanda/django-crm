"""
Batch D — data you can trust later: failing tests for QA-09 and QA-12.

Batch C was the timeline and the small UI. Batch D is the two findings about the
data itself: a duplicate check that only guards one direction, and an import
audit trail that can't say what it ran.

  QA-09  Create warns when a company's domain already belongs to someone else.
         EDIT DOES NOT — `CompanyUpdateView` (views.py:603) has no
         `DuplicateCheckMixin`, so editing a company onto a taken domain saves
         silently. You end up with two companies on one domain, and every later
         create then warns about whichever one `.first()` happens to return.
         The same finding names the other half: a company entered with nothing
         but a website stores `domain=""`, so it is invisible to the very check
         that exists to catch it. Domain should be derived from the website
         when the domain field is left blank.
  QA-12  The `ImportRun` row exists to answer "who imported what, and did it
         finish". Right now it answers neither: `finished_at` is never written
         by anything in the repo, and `source_label` holds the server's temp
         path (`...\\Temp\\crm-import-i113_v5h.csv`) instead of the name the
         user actually uploaded (`my_companies.csv`) — which the wizard already
         parks in the session two lines above where it forgets to use it. Same
         area, lower stakes: an empty CSV passes upload and dead-ends on the
         map step with nothing to map.

THESE TESTS WERE WRITTEN RED FIRST. Every assertion below describes behaviour
the app does not have yet, on purpose: the tests are the spec, and turning them
green is a later, separate job. Nothing in this file changes the app.

Expect six of the ten red today. Two of the four green ones are deliberate, and
a fix that breaks either has gone too far:

  - renaming a company while leaving its domain alone. A record is never its
    own duplicate, so this must keep saving without a warning.
  - an explicit domain surviving an unrelated website. Deriving the domain from
    the website must not quietly clobber a domain the user typed themselves.

The other two are green by accident, not by design, and will stay green until
the work lands — read them as "not yet proven" rather than "done":

  - "save anyway lets the user insist" passes today because the edit page has
    no duplicate check at all, which makes insisting the only path. It turns
    green the moment QA-09 is fixed, and that is the point: the escape hatch
    has to survive the fix, or users get trapped on a warning they can't clear.
  - "an unusable website leaves the domain alone" passes because nothing reads
    the website yet. It exists to pin the failure mode a fix must not invent.

One warning for whoever writes the fix: the shared create/edit template says
"Create anyway" (form.html:27), and the edit tests here look for "Save anyway".
Adding the check to the edit page is not on its own enough to go green — the
button has to read correctly on an edit form too.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_batch_d`.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.models import Company, ImportRun

from .factories import PASSWORD, make_company, make_user


# ------------------------------------------------------------------ helpers


def discard_upload_after(test):
    """
    Delete the wizard's temp upload when the test ends, pass or fail.

    TEACHING NOTE: the wizard writes every accepted upload to a file in the
    system temp dir and only deletes it at commit. Several tests below stop
    before commit on purpose (that's the behaviour under test), so without this
    each run would leave a stray `crm-import-*.csv` behind. `_discard`
    checks the path really is one of ours before it unlinks anything, and it
    tolerates `None` — so this is safe even for a test whose upload was
    rejected and never created a file.
    """
    from core.views_import import _discard

    test.addCleanup(lambda: _discard(test.client.session.get("import_csv_path")))


# ------------------------------------------------- QA-09 part one: the edit


class DuplicateCheckOnEditTests(TestCase):
    """
    QA-09: editing a company onto another company's domain has to warn too.

    TEACHING NOTE: these POST *every* `CompanyForm` field, where most of the
    suite posts just three. That is deliberate. A stripped-down POST would let
    a check pass for the wrong reason — if the fix ends up keyed off a field
    these tests leave out entirely, a partial POST would skip it and look green
    while the browser, which posts all eleven, still walked straight past.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.a = make_company(owner=self.rep, name="Acme", domain="acme.example")
        self.b = make_company(owner=self.rep, name="B", domain="bravo.example")

    def _edit(self, company, **over):
        """
        POST the company edit form for `company`, every field filled in.

        NOTE: `tags` is deliberately absent rather than sent as "". It is a
        ModelMultipleChoiceField, which rejects "" as a choice, so posting it
        would fail validation and re-render the form with an error — the tests
        would then be measuring the broken POST, not the duplicate check. A
        browser leaves the key out entirely when nothing is selected, which is
        exactly what this does. Same story for `owner`, which the form drops
        for non-Admins, and `source`, where "" is the real "no source" option.
        """
        body = {
            "name": company.name,
            "domain": company.domain,
            "website": company.website,
            "phone": company.phone,
            "industry": company.industry,
            "lifecycle": "lead",
            "source": "",
            "owner": "",
            "address": company.address,
            "notes": company.notes,
        }
        body.update(over)
        return self.client.post(reverse("company_update", args=[company.pk]), body)

    def test_edit_onto_a_taken_domain_warns_and_saves_nothing(self):
        resp = self._edit(self.b, domain="ACME.example")

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.a.name)
        self.assertContains(resp, self.a.get_absolute_url())
        self.assertContains(resp, "Save anyway")

        self.b.refresh_from_db()
        self.assertEqual(self.b.domain, "bravo.example")

    def test_save_anyway_lets_the_user_insist(self):
        resp = self._edit(self.b, domain="ACME.example", confirm_duplicate="1")

        self.assertEqual(resp.status_code, 302)
        self.b.refresh_from_db()
        self.assertEqual(self.b.domain, "acme.example")

    def test_renaming_a_company_is_not_its_own_duplicate(self):
        resp = self._edit(self.b, name="B renamed")  # domain left at "bravo.example"

        # A warning re-renders the form, so 302 alone already means "saved".
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(self.b.get_absolute_url())
        self.assertNotContains(page, "already exists")
        self.assertNotContains(page, "Save anyway")

        self.b.refresh_from_db()
        self.assertEqual(self.b.name, "B renamed")
        self.assertEqual(self.b.domain, "bravo.example")


# --------------------------------------------- QA-09 part two: the website


class DomainFromWebsiteTests(TestCase):
    """
    QA-09: a domain nobody typed in should still be readable off the website.

    `Company.save()` strips and lowercases, and that is all it does. A company
    created with nothing but a website lands with `domain=""`, which the
    duplicate check treats as "no domain" and falls past to the name match.
    """

    def test_blank_domain_is_taken_from_the_website(self):
        company = Company.objects.create(name="W", website="https://www.widgets.example/about")

        self.assertEqual(company.domain, "widgets.example")

    def test_an_explicit_domain_is_not_overwritten_by_the_website(self):
        company = Company.objects.create(
            name="W", domain="other.example", website="https://www.widgets.example/about"
        )

        self.assertEqual(company.domain, "other.example")

    def test_an_unusable_website_leaves_the_domain_alone(self):
        company = Company.objects.create(name="W", website="not a url")

        self.assertEqual(company.domain, "")


# --------------------------------------------- QA-09 part three: the create


class DuplicateCheckOnCreateTests(TestCase):
    """
    QA-09: the create page already has the check. It just never sees a domain
    the user didn't type into the domain box, which is the gap the website half
    of the finding leaves open.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.a = make_company(owner=self.rep, name="Acme", domain="acme.example")

    def test_a_website_matching_an_existing_domain_warns_on_create(self):
        resp = self.client.post(reverse("company_create"), {
            "name": "Dup2",
            "domain": "",
            "website": "http://acme.example/contact",
            "lifecycle": "lead",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.a.name)
        self.assertFalse(Company.objects.filter(name="Dup2").exists())
        self.assertEqual(Company.objects.count(), 1)


# ---------------------------------------------------- QA-12: the audit trail


class ImportAuditTrailTests(TestCase):
    """
    QA-12: an ImportRun has to be able to answer "which file, and did it
    finish". Both answers live on a row that currently answers neither.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        discard_upload_after(self)

    def _walk(self, filename, body, mapping):
        """
        upload -> map -> preview -> commit, driven over HTTP the way a person
        walks it. Mirrors `ImportWizardFlowTests._walk` in test_phase3.py.
        """
        self.client.post(
            reverse("import_wizard_upload"),
            {"import_type": "company",
             "csv_file": SimpleUploadedFile(filename, body, content_type="text/csv")},
        )
        self.client.post(
            reverse("import_wizard_map"),
            {"import_type": "company", **{f"map_{f}": col for f, col in mapping.items()}},
        )
        self.client.get(reverse("import_wizard_preview"))
        self.client.post(reverse("import_wizard_commit"))

    def test_a_finished_import_records_the_uploaded_filename(self):
        self._walk("my_companies.csv", b"Name\r\nAcme Two\r\n", {"name": "Name"})

        # Preview runs first and also writes a row, so ask for the real one.
        run = ImportRun.objects.filter(dry_run=False).latest("started_at")
        self.assertEqual(run.source_label, "my_companies.csv")
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.created_count, 1)
        self.assertTrue(Company.objects.filter(name="Acme Two").exists())


# ------------------------------------------------ QA-12: the empty upload


class EmptyUploadTests(TestCase):
    """
    QA-12, low bullet: an empty CSV should be refused at the door, where the
    other upload rejections live, instead of being carried to the map step with
    no columns to pick from.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        discard_upload_after(self)

    def _assert_refused(self, body):
        resp = self.client.post(
            reverse("import_wizard_upload"),
            {"import_type": "company",
             "csv_file": SimpleUploadedFile("empty.csv", body, content_type="text/csv")},
        )

        # Back to the start of the wizard, not forward to the map step.
        self.assertRedirects(resp, reverse("import_wizard"), fetch_redirect_response=False)
        self.assertNotIn("import_csv_path", self.client.session)

        # Follow it to read the flash the rep is actually left looking at.
        page = self.client.get(resp["Location"])
        self.assertIn("empty", page.content.decode().lower())

    def test_an_empty_file_is_refused(self):
        self._assert_refused(b"")

    def test_a_whitespace_only_file_is_refused(self):
        self._assert_refused(b"\r\n  \r\n")