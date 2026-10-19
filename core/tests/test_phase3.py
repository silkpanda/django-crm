"""
Phase 3 tests: CSV import wizard, upserts, CSV export, and merge duplicates.

TEACHING NOTE: these tests cover the five Phase 3 features:
  1. CSV import wizard (upload → map → preview → commit)
  2. Upserts (idempotent imports)
  3. CSV export (filtered list views)
  4. Merge duplicates (Admin only)
  5. ImportRun audit records

The tests use the Django test client to simulate browser requests and verify
that the import engine, views, and models work together correctly.
"""

import csv
import io
import json
import os
import pathlib
import tempfile
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import Activity, Company, Contact, Deal, ImportRun, Task

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user


def remove_after(test, path):
    """
    Delete a temp file when the test ends, pass or fail.

    TEACHING NOTE: `NamedTemporaryFile(delete=False)` means YOU delete it. These
    tests didn't, and every `manage.py test` run left ~20 CSVs in %TEMP%.
    `addCleanup` runs even when the test fails, unlike code at the end of it.
    """
    test.addCleanup(lambda: os.path.exists(path) and os.unlink(path))


def remove_wizard_upload_after(test):
    """Wizard tests that stop before commit would otherwise leave their upload behind."""
    from core.views_import import _discard

    test.addCleanup(lambda: _discard(test.client.session.get("import_csv_path")))


class CsvImportTests(TestCase):
    """Tests for the CSV import wizard and upsert engine."""

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.client.login(username="admin", password=PASSWORD)

    def _create_csv(self, rows, fieldnames):
        """Helper: create a CSV file and return its path."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        tmp.close()
        remove_after(self, tmp.name)
        return tmp.name

    def test_import_creates_new_companies(self):
        """Importing a CSV with new companies creates them."""
        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com", "industry": "Tech"}],
            ["name", "domain", "industry"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain", "industry": "industry"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(Company.objects.filter(name="Acme Corp").count(), 1)

    def test_import_is_idempotent(self):
        """Running the same import twice updates instead of duplicating."""
        # Create initial company
        make_company(name="Acme Corp", domain="acme.com")

        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com", "industry": "Tech"}],
            ["name", "domain", "industry"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain", "industry": "industry"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(Company.objects.filter(name="Acme Corp").count(), 1)

    def test_import_matches_on_external_id(self):
        """Companies match on external_id first."""
        make_company(name="Acme Corp", domain="acme.com", external_id="EXT-001")

        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com", "external_id": "EXT-001", "industry": "Tech"}],
            ["name", "domain", "external_id", "industry"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain", "external_id": "external_id", "industry": "industry"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(Company.objects.filter(external_id="EXT-001").count(), 1)

    def test_import_matches_on_domain(self):
        """Companies match on domain when external_id is empty."""
        make_company(name="Acme Corp", domain="acme.com")

        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com", "industry": "Tech"}],
            ["name", "domain", "industry"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain", "industry": "industry"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(Company.objects.filter(domain="acme.com").count(), 1)

    def test_import_matches_on_name(self):
        """Companies match on name when external_id and domain are empty."""
        make_company(name="Acme Corp", domain="")

        csv_path = self._create_csv(
            [{"name": "Acme Corp", "industry": "Tech"}],
            ["name", "industry"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "industry": "industry"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(Company.objects.filter(name="Acme Corp").count(), 1)

    def test_dry_run_writes_nothing(self):
        """Dry run creates an ImportRun but doesn't write companies."""
        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com"}],
            ["name", "domain"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain"}
        engine = ImportEngine(source, mapping, dry_run=True)
        result = engine.run()

        self.assertEqual(Company.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 1)
        self.assertTrue(result.import_run.dry_run)

    def test_import_run_record_created(self):
        """Every import writes an ImportRun record."""
        csv_path = self._create_csv(
            [{"name": "Acme Corp", "domain": "acme.com"}],
            ["name", "domain"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"name": "name", "domain": "domain"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(ImportRun.objects.count(), 1)
        run = result.import_run
        self.assertEqual(run.source_type, "csv")
        self.assertEqual(run.created_count, 1)
        self.assertFalse(run.dry_run)

    def test_contact_import_matches_on_email(self):
        """Contacts match on email."""
        company = make_company(name="Acme Corp", domain="acme.com")
        make_contact(first_name="Pat", last_name="Doe", email="pat@acme.com", company=company)

        csv_path = self._create_csv(
            [{"first_name": "Pat", "last_name": "Doe", "email": "pat@acme.com", "title": "CEO"}],
            ["first_name", "last_name", "email", "title"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"first_name": "first_name", "last_name": "last_name", "email": "email", "title": "title"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(Contact.objects.filter(email="pat@acme.com").count(), 1)

    def test_contact_import_matches_on_external_id(self):
        """Contacts match on external_id first."""
        company = make_company(name="Acme Corp", domain="acme.com")
        make_contact(first_name="Pat", last_name="Doe", email="pat@acme.com", company=company, external_id="C-001")

        csv_path = self._create_csv(
            [{"first_name": "Pat", "last_name": "Doe", "email": "pat@acme.com", "external_id": "C-001", "title": "CEO"}],
            ["first_name", "last_name", "email", "external_id", "title"],
        )
        from core.importers import CsvSource, ImportEngine

        source = CsvSource(csv_path)
        mapping = {"first_name": "first_name", "last_name": "last_name", "email": "email", "external_id": "external_id", "title": "title"}
        engine = ImportEngine(source, mapping)
        result = engine.run()

        self.assertEqual(result.updated, 1)
        self.assertEqual(Contact.objects.filter(external_id="C-001").count(), 1)


class CsvExportTests(TestCase):
    """Tests for CSV export on list views."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        make_company(name="Alpha Corp", domain="alpha.com", owner=self.rep)
        make_company(name="Beta Corp", domain="beta.com", owner=self.rep)

    def test_export_returns_csv(self):
        """Adding ?export=1 to a list view returns a CSV file."""
        resp = self.client.get(reverse("company_list"), {"export": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_export_contains_filtered_data(self):
        """Export only includes companies matching the filter."""
        resp = self.client.get(reverse("company_list"), {"q": "alpha", "export": "1"})
        content = resp.content.decode()
        self.assertIn("Alpha Corp", content)
        self.assertNotIn("Beta Corp", content)

    def test_export_includes_all_fields(self):
        """Export includes all model fields."""
        resp = self.client.get(reverse("company_list"), {"export": "1"})
        content = resp.content.decode()
        # Should have header row with field names
        self.assertIn("name", content)
        self.assertIn("domain", content)
        self.assertIn("lifecycle", content)

    def _rows(self, resp):
        return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))

    def test_export_includes_every_page_not_just_the_first(self):
        """REVIEW-06: 30 companies, page size 25 — all 30 (plus the 2 from setUp) must export."""
        for i in range(30):
            make_company(name=f"Bulk {i:02}", owner=self.rep)
        resp = self.client.get(reverse("company_list"), {"export": "1"})
        self.assertEqual(len(self._rows(resp)) - 1, 32)  # minus the header row

    def test_export_ignores_page_param(self):
        for i in range(30):
            make_company(name=f"Bulk {i:02}", owner=self.rep)
        resp = self.client.get(reverse("company_list"), {"export": "1", "page": "2"})
        self.assertEqual(len(self._rows(resp)) - 1, 32)

    def test_formula_cells_are_neutralised(self):
        """REVIEW-06: a planted formula must come out as text, not something Excel will run."""
        make_contact(owner=self.rep, first_name='=HYPERLINK("http://evil","x")', last_name="Planted")
        resp = self.client.get(reverse("contact_list"), {"q": "Planted", "export": "1"})
        cells = [c for row in self._rows(resp) for c in row]
        self.assertIn('\'=HYPERLINK("http://evil","x")', cells)
        self.assertFalse(any(c.startswith(("=", "+", "-", "@")) for c in cells))

    def test_every_trigger_character_is_neutralised(self):
        from core.views import neutralise_formula

        for payload in ("=1+1", "+1+1", "-1+1", "@SUM(A1)", "\t=1", "\r=1"):
            self.assertTrue(neutralise_formula(payload).startswith("'"), payload)
        self.assertEqual(neutralise_formula("Acme Corp"), "Acme Corp")

    def test_numbers_are_left_alone(self):
        from core.views import neutralise_formula

        self.assertEqual(neutralise_formula(Decimal("-5.00")), Decimal("-5.00"))
        self.assertEqual(neutralise_formula(42), 42)


class MergeDuplicateTests(TestCase):
    """Tests for merging duplicate companies and contacts."""

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.company1 = make_company(name="Acme Corp", domain="acme.com", owner=self.admin)
        self.company2 = make_company(name="Acme Corp", domain="acme.com", owner=self.admin)
        self.contact1 = make_contact(first_name="Pat", last_name="Doe", email="pat@acme.com", company=self.company1, owner=self.admin)
        self.contact2 = make_contact(first_name="Pat", last_name="Doe", email="pat2@acme.com", company=self.company2, owner=self.admin)
        self.deal = make_deal(self.company1, owner=self.admin, name="Test Deal")
        self.activity = Activity.objects.create(body="Test note", company=self.company1, contact=self.contact1, author=self.admin)
        self.task = Task.objects.create(title="Test task", assignee=self.admin, company=self.company1, contact=self.contact1)

    def test_admin_can_merge_companies(self):
        """Admin can merge two companies and child records are re-pointed."""
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.post(
            reverse("company_merge", args=[self.company2.pk]),
            {"survivor_id": self.company1.pk, "loser_id": self.company2.pk},
        )
        self.assertEqual(resp.status_code, 302)
        # Loser is deleted
        self.assertFalse(Company.objects.filter(pk=self.company2.pk).exists())
        # Child records are re-pointed
        self.contact2.refresh_from_db()
        self.assertEqual(self.contact2.company, self.company1)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.company, self.company1)
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.company, self.company1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.company, self.company1)

    def test_rep_cannot_merge_companies(self):
        """Rep cannot merge companies (Admin only)."""
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(
            reverse("company_merge", args=[self.company2.pk]),
            {"survivor_id": self.company1.pk, "loser_id": self.company2.pk},
        )
        self.assertEqual(resp.status_code, 403)
        # Both companies still exist
        self.assertTrue(Company.objects.filter(pk=self.company1.pk).exists())
        self.assertTrue(Company.objects.filter(pk=self.company2.pk).exists())

    def test_admin_can_merge_contacts(self):
        """Admin can merge two contacts and child records are re-pointed."""
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.post(
            reverse("contact_merge", args=[self.contact2.pk]),
            {"survivor_id": self.contact1.pk, "loser_id": self.contact2.pk},
        )
        self.assertEqual(resp.status_code, 302)
        # Loser is deleted
        self.assertFalse(Contact.objects.filter(pk=self.contact2.pk).exists())
        # Child records are re-pointed
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.contact, self.contact1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.contact, self.contact1)

    def test_merge_view_shows_duplicates(self):
        """The merge view shows potential duplicates."""
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("company_merge", args=[self.company1.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme Corp")


class ImportWizardViewTests(TestCase):
    """Tests for the CSV import wizard views."""

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.viewer = make_user("viewer", "Viewer")

    def test_upload_requires_auth(self):
        """Anonymous users cannot access the import wizard."""
        resp = self.client.get(reverse("import_wizard"))
        self.assertEqual(resp.status_code, 302)

    def test_viewer_cannot_import(self):
        """Viewers cannot import data."""
        self.client.login(username="viewer", password=PASSWORD)
        resp = self.client.get(reverse("import_wizard"))
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_access_wizard(self):
        """Admins can access the import wizard."""
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("import_wizard"))
        self.assertEqual(resp.status_code, 200)

    def test_rep_can_access_wizard(self):
        """Reps can access the import wizard."""
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("import_wizard"))
        self.assertEqual(resp.status_code, 200)


class ImportWizardFlowTests(TestCase):
    """
    Walk the wizard end to end through the test client: upload → map → preview → commit.

    TEACHING NOTE: the tests above only ever GET the first page, so they stayed
    green while every later step silently rendered the upload page again
    (REVIEW-09). A multi-step feature needs at least one test that takes every
    step, the way a person would.
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.upload = SimpleUploadedFile
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")
        remove_wizard_upload_after(self)

    def _walk(self, username, body, mapping, filename="data.csv"):
        """Run all four steps as `username`. Returns the preview response."""
        self.client.login(username=username, password=PASSWORD)
        resp = self.client.post(reverse("import_wizard_upload"),
                                {"csv_file": self.upload(filename, body, content_type="text/csv")})
        self.assertRedirects(resp, reverse("import_wizard_map"), fetch_redirect_response=False)
        resp = self.client.get(reverse("import_wizard_map"))
        for field in mapping:
            self.assertContains(resp, f'name="map_{field}"')
        self.client.post(reverse("import_wizard_map"),
                         {"import_type": "company", **{f"map_{f}": col for f, col in mapping.items()}})
        preview = self.client.get(reverse("import_wizard_preview"))
        self.assertEqual(preview.status_code, 200)
        self.client.post(reverse("import_wizard_commit"))
        return preview

    def test_each_url_renders_its_own_step(self):
        self._walk("admin", b"name,industry\r\nAcme,Tech\r\n", {"name": "name", "industry": "industry"})
        self.assertTrue(Company.objects.filter(name="Acme", industry="Tech").exists())
        self.assertEqual(ImportRun.objects.filter(run_by=self.admin, created_count=1).count(), 1)

    def test_excel_bom_header_still_maps(self):
        self._walk("admin", "﻿name\r\nBom Co\r\n".encode("utf-8"), {"name": "name"})
        self.assertTrue(Company.objects.filter(name="Bom Co").exists())

    def test_non_utf8_upload_is_refused_not_a_500(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.post(reverse("import_wizard_upload"),
                                {"csv_file": self.upload("a.csv", "name\r\nCaf\xe9\r\n".encode("latin-1"))})
        self.assertRedirects(resp, reverse("import_wizard"), fetch_redirect_response=False)
        self.assertNotIn("import_csv_path", self.client.session)

    def test_rep_cannot_overwrite_another_reps_company(self):
        """REVIEW-02: the import must obey the same ownership rule as the edit page."""
        theirs = make_company(owner=self.rep2, name="Crown Point", industry="Healthcare")
        preview = self._walk("rep", b"name,industry\r\nCrown Point,PWNED\r\n", {"name": "name", "industry": "industry"})
        self.assertContains(preview, "SKIP")
        theirs.refresh_from_db()
        self.assertEqual(theirs.industry, "Healthcare")

    def test_rep_can_update_their_own_company(self):
        mine = make_company(owner=self.rep, name="Mine Inc", industry="Old")
        self._walk("rep", b"name,industry\r\nMine Inc,New\r\n", {"name": "name", "industry": "industry"})
        mine.refresh_from_db()
        self.assertEqual(mine.industry, "New")

    def test_admin_can_update_anyones_company(self):
        theirs = make_company(owner=self.rep2, name="Crown Point", industry="Healthcare")
        self._walk("admin", b"name,industry\r\nCrown Point,Dental\r\n", {"name": "name", "industry": "industry"})
        theirs.refresh_from_db()
        self.assertEqual(theirs.industry, "Dental")

    def test_rep_owns_what_they_import(self):
        self._walk("rep", b"name\r\nFresh Co\r\n", {"name": "name"})
        self.assertEqual(Company.objects.get(name="Fresh Co").owner, self.rep)


class CredentialRedactionTests(TestCase):
    """
    REVIEW-03: the ImportRun audit label must never hold a database password.

    TEACHING NOTE: the old version passed code review by looking right — a regex
    with "***" in it. It stored `hunter2:***@` because the replacement put the
    password where the username goes. The only way to catch that is to feed it
    a real password and assert the password is gone, which is what these do.
    """

    def redact(self, label):
        from core.importers import ImportEngine, CsvSource

        return ImportEngine(CsvSource("unused.csv"), {})._redact_credentials(label)

    def test_password_is_removed_and_username_kept(self):
        out = self.redact("postgresql://bob:hunter2@db.local:5432/crm")
        self.assertNotIn("hunter2", out)
        self.assertEqual(out, "postgresql://bob:***@db.local:5432/crm")

    def test_password_with_encoded_at_and_colon(self):
        out = self.redact("mysql+pymysql://bob:p%40ss%3Aw0rd@h/db")
        self.assertNotIn("w0rd", out)
        self.assertNotIn("p%40ss", out)

    def test_url_without_password_is_untouched(self):
        self.assertEqual(self.redact("postgresql://bob@db/crm"), "postgresql://bob@db/crm")
        self.assertEqual(self.redact("sqlite:///C:/data/x.db"), "sqlite:///C:/data/x.db")

    def test_csv_path_is_untouched(self):
        # TEACHING NOTE: the path is derived from the real temp directory rather
        # than hardcoded. A literal `C:\Users\<someone>\...` here would put one
        # developer's home directory - their account name, which on Windows is
        # their email prefix often enough - into a public repository, and it would
        # also be wrong on any machine whose temp dir lives elsewhere. This test
        # only needs *a* plausible CSV path, so ask the OS for one.
        path = str(pathlib.Path(tempfile.gettempdir()) / "tmpab.csv")
        self.assertEqual(self.redact(path), path)

    def test_import_run_row_never_stores_the_password(self):
        from core.importers import ImportEngine, SqlSource

        ImportEngine(SqlSource("sqlite://bob:CANARY123@/nowhere.db"), {"name": "name"}).run()
        self.assertFalse(ImportRun.objects.filter(source_label__contains="CANARY123").exists())
        self.assertTrue(ImportRun.objects.filter(source_label__contains="bob:***@").exists())


class RealWorldImportTests(TestCase):
    """
    REVIEW-04 / REVIEW-05: imports shaped like real files, not like the model.

    TEACHING NOTE: every older import test used headers identical to the model's
    field names ({"name": "name"}). That's the one shape where reading the
    mapping backwards still works, so the tests couldn't see the bug. These use
    headers a real export would have: "Company Name", "Ext ID", "E-mail".
    """

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.upload = SimpleUploadedFile
        self.admin = make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)
        remove_wizard_upload_after(self)

    def _engine(self, rows, mapping, **kw):
        from core.importers import CsvSource, ImportEngine

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8")
        writer = csv.writer(tmp)
        writer.writerows(rows)
        tmp.close()
        remove_after(self, tmp.name)
        return ImportEngine(CsvSource(tmp.name), mapping, **kw)

    def _wizard(self, kind, body, mapping):
        self.client.post(reverse("import_wizard_upload"),
                         {"import_type": kind, "csv_file": self.upload("f.csv", body, content_type="text/csv")})
        self.client.post(reverse("import_wizard_map"), {f"map_{f}": col for f, col in mapping.items()})
        preview = self.client.get(reverse("import_wizard_preview"))
        self.client.post(reverse("import_wizard_commit"))
        return preview

    def test_real_headers_upsert_instead_of_duplicating(self):
        body = b"Company Name,Ext ID,Industry\r\nAcme,EXT-1,Tech\r\n"
        mapping = {"name": "Company Name", "external_id": "Ext ID", "industry": "Industry"}
        self._wizard("company", body, mapping)
        self._wizard("company", body.replace(b"Tech", b"Retail"), mapping)
        self.assertEqual(Company.objects.filter(external_id="EXT-1").count(), 1)
        self.assertEqual(Company.objects.get(external_id="EXT-1").industry, "Retail")

    def test_contacts_import_through_the_wizard_and_link_to_their_company(self):
        acme = make_company(name="Acme")
        self._wizard("contact", b"First,Last,E-mail,Employer\r\nPat,Doe,PAT@acme.com,acme\r\n",
                     {"first_name": "First", "last_name": "Last", "email": "E-mail", "company": "Employer"})
        pat = Contact.objects.get(email="pat@acme.com")
        self.assertEqual(pat.company, acme)
        self.assertEqual(pat.owner, self.admin)

    def test_unknown_company_skips_the_row_with_a_reason(self):
        preview = self._wizard("contact", b"First,Employer\r\nPat,Nowhere Inc\r\n",
                               {"first_name": "First", "company": "Employer"})
        self.assertContains(preview, "No company named")
        self.assertFalse(Contact.objects.filter(first_name="Pat").exists())

    def test_ambiguous_name_is_skipped_not_guessed_or_crashed(self):
        make_company(name="Twin", industry="A")
        make_company(name="twin", industry="B")
        result = self._engine([["Name", "Industry"], ["Twin", "C"], ["Solo", "D"]],
                              {"Name": "name", "Industry": "industry"}).run()
        self.assertEqual((result.created, result.skipped), (1, 1))
        self.assertIn("more than one", result.row_errors[0])
        self.assertEqual(Company.objects.filter(industry="C").count(), 0)

    def test_one_bad_row_doesnt_sink_the_file(self):
        result = self._engine([["First", "Email"], ["Ann", "ann@x.test"], ["Bob", "not-an-email"], ["Cy", "cy@x.test"]],
                              {"First": "first_name", "Email": "email"}).run()
        self.assertEqual((result.created, result.skipped), (2, 1))
        self.assertTrue(result.row_errors[0].startswith("Row 2:"))
        self.assertEqual(Contact.objects.count(), 2)

    def test_preview_shows_skip_for_a_row_the_commit_would_reject(self):
        rows = self._engine([["First", "Email"], ["Bob", "not-an-email"]],
                            {"First": "first_name", "Email": "email"}, dry_run=True).preview()
        self.assertEqual(rows[0].verdict, "skip")
        self.assertIn("email", rows[0].errors[0])

    def test_lifecycle_label_is_accepted(self):
        self._engine([["Name", "Stage"], ["Labelled", "Customer"]], {"Name": "name", "Stage": "lifecycle"}).run()
        self.assertEqual(Company.objects.get(name="Labelled").lifecycle, "customer")

    def test_blank_cell_never_wipes_existing_data(self):
        make_company(name="Keep", industry="Healthcare")
        self._engine([["Name", "Industry"], ["Keep", ""]], {"Name": "name", "Industry": "industry"}).run()
        self.assertEqual(Company.objects.get(name="Keep").industry, "Healthcare")

    def test_mapping_to_a_field_outside_the_allowlist_is_refused(self):
        with self.assertRaises(ValueError):
            self._engine([["x"]], {"Owner": "owner_id"})
        with self.assertRaises(ValueError):
            self._engine([["x"]], {"Name": "name", "Pk": "pk"})

    def test_map_step_requires_the_required_field(self):
        self.client.post(reverse("import_wizard_upload"),
                         {"import_type": "company", "csv_file": self.upload("f.csv", b"Industry\r\nTech\r\n")})
        resp = self.client.post(reverse("import_wizard_map"), {"map_industry": "Industry"})
        self.assertRedirects(resp, reverse("import_wizard_map"), fetch_redirect_response=False)

    def test_map_step_rejects_a_column_not_in_the_file(self):
        self.client.post(reverse("import_wizard_upload"),
                         {"import_type": "company", "csv_file": self.upload("f.csv", b"Name\r\nA\r\n")})
        resp = self.client.post(reverse("import_wizard_map"), {"map_name": "Name", "map_industry": "Ghost"})
        self.assertRedirects(resp, reverse("import_wizard_map"), fetch_redirect_response=False)
        self.assertNotIn("import_mapping", self.client.session)


class UploadHygieneTests(TestCase):
    """The upload step: size cap, temp-file cleanup, and odd filenames."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.upload = SimpleUploadedFile
        make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)
        remove_wizard_upload_after(self)

    def _up(self, body=b"Name\r\nA\r\n", name="f.csv"):
        return self.client.post(reverse("import_wizard_upload"),
                                {"import_type": "company", "csv_file": self.upload(name, body)})

    def test_oversized_upload_is_refused(self):
        from django.test import override_settings

        with override_settings(IMPORT_MAX_BYTES=10):
            resp = self._up(b"Name\r\n" + b"A" * 50 + b"\r\n")
        self.assertRedirects(resp, reverse("import_wizard"), fetch_redirect_response=False)
        self.assertNotIn("import_csv_path", self.client.session)

    def test_uppercase_extension_is_accepted(self):
        self._up(name="EXPORT.CSV")
        self.assertIn("import_csv_path", self.client.session)

    def test_temp_file_is_deleted_after_commit(self):
        import os

        self._up()
        path = self.client.session["import_csv_path"]
        self.client.post(reverse("import_wizard_map"), {"map_name": "Name"})
        self.client.post(reverse("import_wizard_commit"))
        self.assertFalse(os.path.exists(path))

    def test_new_upload_deletes_the_abandoned_one(self):
        import os

        self._up()
        first = self.client.session["import_csv_path"]
        self._up(b"Name\r\nB\r\n")
        self.assertFalse(os.path.exists(first))
        self.assertNotEqual(self.client.session["import_csv_path"], first)


class SqlSourceTests(TestCase):
    """REVIEW-05: the SQL adapter had never run once (NameError on every row)."""

    def setUp(self):
        import os

        from sqlalchemy import create_engine, text

        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{self.db}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE companies (co_name TEXT, sector TEXT, seats INTEGER, ext INTEGER)"))
            conn.execute(text("INSERT INTO companies VALUES ('Northwind', 'Retail', 40, 1001), ('Contoso', NULL, 12, 1002)"))
        engine.dispose()
        self.url = f"sqlite:///{self.db}"
        self.mapping = {"co_name": "name", "sector": "industry", "ext": "external_id"}

    def tearDown(self):
        import os

        os.unlink(self.db)

    def test_columns_and_rows_come_back_as_strings(self):
        from core.importers import SqlSource

        source = SqlSource(self.url)
        self.assertEqual(source.get_columns(), ["co_name", "sector", "seats", "ext"])
        rows = list(source.read_rows())
        self.assertEqual([r.row_number for r in rows], [1, 2])
        self.assertEqual(rows[0].data["ext"], "1001")
        self.assertEqual(rows[1].data["sector"], "")  # NULL → blank

    def test_sql_import_creates_then_upserts(self):
        from core.importers import ImportEngine, SqlSource

        first = ImportEngine(SqlSource(self.url), self.mapping).run()
        again = ImportEngine(SqlSource(self.url), self.mapping).run()
        self.assertEqual((first.created, again.updated, again.created), (2, 2, 0))
        self.assertEqual(Company.objects.get(external_id="1001").name, "Northwind")

    def test_cli_sql_import_and_bad_mapping(self):
        from django.core.management import CommandError, call_command

        mapping_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(self.mapping, mapping_file)
        mapping_file.close()
        remove_after(self, mapping_file.name)
        out = io.StringIO()
        call_command("import_data", "--adapter", "sql", "--url", self.url, "--mapping", mapping_file.name, stdout=out)
        self.assertIn("2 created", out.getvalue())

        with open(mapping_file.name, "w") as f:
            json.dump({"co_name": "owner_id"}, f)
        with self.assertRaises(CommandError):
            call_command("import_data", "--adapter", "sql", "--url", self.url, "--mapping", mapping_file.name,
                         stdout=io.StringIO())


class MergeSafetyTests(TestCase):
    """
    REVIEW-08: what the merge used to get wrong.

    TEACHING NOTE: the happy-path tests above re-point children and check the
    loser is gone. These cover the ways a merge can quietly destroy data or
    leave no trace: merging into itself, dropping tags and notes, skipping the
    history log, and offering every blank-domain company as a "duplicate".
    """

    def setUp(self):
        from core.models import Tag

        self.admin = make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)
        self.keep = make_company(name="Acme", domain="acme.com", owner=self.admin)
        self.gone = make_company(name="ACME Inc", domain="acme.com", owner=self.admin,
                                 phone="555-555-0100", notes="Ask for the office manager", external_id="OLD-7")
        self.gone.tags.add(Tag.objects.create(name="Priority"))
        self.contact = make_contact(company=self.gone, first_name="Lee", owner=self.admin)
        self.deal = make_deal(self.gone, owner=self.admin, name="Panel swap")

    def _merge(self, page, survivor):
        return self.client.post(reverse("company_merge", args=[page.pk]), {"survivor_id": survivor.pk})

    def test_merging_into_itself_is_refused_and_deletes_nothing(self):
        resp = self.client.post(reverse("company_merge", args=[self.keep.pk]),
                                {"survivor_id": self.keep.pk, "loser_id": self.keep.pk})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Company.objects.filter(pk=self.keep.pk).exists())

    def test_a_forged_loser_id_is_ignored(self):
        """The page's record is always the one merged away; a POSTed loser_id can't redirect it."""
        bystander = make_company(name="Bystander", owner=self.admin)
        self.client.post(reverse("company_merge", args=[self.gone.pk]),
                         {"survivor_id": self.keep.pk, "loser_id": bystander.pk})
        self.assertTrue(Company.objects.filter(pk=bystander.pk).exists())
        self.assertFalse(Company.objects.filter(pk=self.gone.pk).exists())

    def test_tags_notes_and_blank_fields_carry_over(self):
        self._merge(self.gone, self.keep)
        self.keep.refresh_from_db()
        self.assertEqual(list(self.keep.tags.values_list("name", flat=True)), ["Priority"])
        self.assertIn("Ask for the office manager", self.keep.notes)
        self.assertEqual(self.keep.phone, "555-555-0100")
        self.assertEqual(self.keep.external_id, "OLD-7")  # future imports now match the survivor

    def test_moves_land_in_the_history_log(self):
        self._merge(self.gone, self.keep)
        self.assertEqual(self.contact.history.first().company_id, self.keep.pk)
        self.assertEqual(self.deal.history.first().company_id, self.keep.pk)

    def test_merge_leaves_a_note_on_the_survivor(self):
        self._merge(self.gone, self.keep)
        note = Activity.objects.get(company=self.keep, body__startswith="Merged")
        self.assertIn("ACME Inc", note.body)
        self.assertIn("1 contact", note.body)
        self.assertEqual(note.author, self.admin)

    def test_blank_domain_is_not_a_duplicate_signal(self):
        lonely = make_company(name="Lonely", owner=self.admin)
        make_company(name="Stranger", owner=self.admin)
        resp = self.client.get(reverse("company_merge", args=[lonely.pk]))
        self.assertNotIn("Stranger", [c.name for c in resp.context["duplicates"]])

    def test_contact_without_email_isnt_a_duplicate_of_every_other(self):
        a = make_contact(first_name="Ann", last_name="One", owner=self.admin)
        make_contact(first_name="Bob", last_name="Two", owner=self.admin)
        resp = self.client.get(reverse("contact_merge", args=[a.pk]))
        self.assertEqual(list(resp.context["duplicates"]), [])

    def test_contact_merge_carries_the_email_over(self):
        survivor = make_contact(first_name="Pat", last_name="Doe", owner=self.admin)
        loser = make_contact(first_name="Pat", last_name="Doe", email="pat@acme.com", owner=self.admin)
        self.client.post(reverse("contact_merge", args=[loser.pk]), {"survivor_id": survivor.pk})
        survivor.refresh_from_db()
        self.assertEqual(survivor.email, "pat@acme.com")


class Phase3EntryPointTests(TestCase):
    """
    Found in the browser click-through: import, export, and merge had no buttons.

    TEACHING NOTE: every Phase 3 feature worked (once fixed) but could only be
    reached by typing its URL. A test suite that drives views directly can't
    notice a missing button; only clicking through the real pages does. These
    pin the entry points down, and the permission split on them.
    """

    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.viewer = make_user("viewer", "Viewer")
        self.company = make_company(owner=self.rep, name="Acme")
        make_company(owner=self.rep, name="Beta")

    def test_export_with_a_stale_page_number_still_downloads(self):
        """?page=5 on a one-page list used to 404, because pagination ran before the export."""
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("company_list"), {"export": "1", "page": "5"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode("utf-8-sig").count("\n") - 1, 2)

    def test_list_pages_link_export_with_current_filters(self):
        self.client.login(username="viewer", password=PASSWORD)
        resp = self.client.get(reverse("company_list"), {"q": "acme", "page": "1"})
        self.assertContains(resp, "q=acme&amp;export=1")
        self.assertNotContains(resp, "page=1&amp;export=1")

    def test_import_button_only_for_people_who_can_import(self):
        self.client.login(username="rep", password=PASSWORD)
        self.assertContains(self.client.get(reverse("contact_list")), reverse("import_wizard"))
        self.client.login(username="viewer", password=PASSWORD)
        self.assertNotContains(self.client.get(reverse("contact_list")), reverse("import_wizard"))

    def test_merge_button_only_for_admins(self):
        url = reverse("company_merge", args=[self.company.pk])
        self.client.login(username="rep", password=PASSWORD)
        self.assertNotContains(self.client.get(self.company.get_absolute_url()), url)
        self.client.login(username="admin", password=PASSWORD)
        self.assertContains(self.client.get(self.company.get_absolute_url()), url)
