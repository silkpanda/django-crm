"""
Regression tests for three review findings: two in the CSV import, one in the
quote editor.

TEACHING NOTE: each test here was run against the unfixed code first and seen
to fail for the reason in its docstring. A regression test that has never
failed has never proved it can catch anything.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.importers import CsvSource
from core.models import Company, ImportRun, QuoteLine

from .factories import PASSWORD, make_company, make_deal, make_user
from .test_phase3 import remove_wizard_upload_after
from .test_quotes import lines_post, make_quote


class CsvShapeTests(TestCase):
    """CSV files that are slightly malformed in the ways real exports are."""

    def setUp(self):
        make_user("admin", "Admin")
        self.client.login(username="admin", password=PASSWORD)
        remove_wizard_upload_after(self)

    def _upload(self, body):
        resp = self.client.post(reverse("import_wizard_upload"),
                                {"csv_file": SimpleUploadedFile("f.csv", body, content_type="text/csv")})
        self.assertRedirects(resp, reverse("import_wizard_map"), fetch_redirect_response=False)

    def _column(self, name):
        """The column the map page offers for `name`, exactly as the wizard lists it."""
        columns = CsvSource(self.client.session["import_csv_path"]).get_columns()
        return next(c for c in columns if c.strip() == name)

    def test_a_row_with_more_cells_than_headers_is_skipped_not_a_500(self):
        """
        `Acme, Inc` without quotes is three cells under two headers. DictReader
        files the extra cell under the key None, and `None.strip()` used to crash
        the preview with AttributeError and roll back the whole commit. The row
        is skipped with a reason; the good row still imports.
        """
        self._upload(b"name,domain\r\nAcme, Inc,acme.com\r\nGood Co,good.com\r\n")
        self.client.post(reverse("import_wizard_map"),
                         {"map_name": self._column("name"), "map_domain": self._column("domain")})
        preview = self.client.get(reverse("import_wizard_preview"))
        self.assertContains(preview, "more cells than")
        self.client.post(reverse("import_wizard_commit"))
        self.assertTrue(Company.objects.filter(name="Good Co", domain="good.com").exists())
        self.assertFalse(Company.objects.filter(name="Acme").exists())
        run = ImportRun.objects.get()
        self.assertEqual((run.created_count, run.skipped_count), (1, 1))
        self.assertIn("Row 1:", run.errors)

    def test_a_header_with_a_leading_space_still_imports_its_column(self):
        """
        `name, domain` has a header " domain". Rows were keyed by the stripped
        name and the mapping by the unstripped one, so the lookup missed and
        every domain imported blank.
        """
        self._upload(b"name, domain\r\nAcme,acme.com\r\n")
        self.client.post(reverse("import_wizard_map"),
                         {"map_name": self._column("name"), "map_domain": self._column("domain")})
        self.client.get(reverse("import_wizard_preview"))
        self.client.post(reverse("import_wizard_commit"))
        self.assertEqual(Company.objects.get(name="Acme").domain, "acme.com")


class QuoteDeleteBlankRowTests(TestCase):
    """DELETE ticked on a blank extra row: there is nothing to delete, so nothing happens."""

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.deal = make_deal(make_company(), owner=self.rep)
        self.quote = make_quote(self.deal)
        self.client.force_login(self.rep)

    def test_deleting_an_unsaved_extra_row_is_ignored_not_a_500(self):
        """
        An extra row was never saved, so its instance has no pk and `.delete()`
        raised ValueError. The template never offers that box on a blank row; a
        hand-made POST can still send it.
        """
        response = self.client.post(
            reverse("quote_edit", args=[self.deal.pk]),
            lines_post({"description": "", "quantity": "", "unit_price": "", "delete": True}),
        )
        self.assertRedirects(response, reverse("quote_detail", args=[self.deal.pk]))
        self.assertFalse(QuoteLine.objects.exists())
