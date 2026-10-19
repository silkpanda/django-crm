"""
Phase 1 view tests: who can reach which page, and what each role can change.

TEACHING NOTE: these drive the app the way a browser does. `self.client` is
Django's fake browser: it sends real requests through the URLs, the middleware
(code that runs on every request, such as the login check), and the views, and
hands back the response. Checking status codes is the point:

- 302 means "redirected". For an anonymous visitor that's the bounce to the
  login page.
- 403 means "logged in, but not allowed". A Viewer who POSTs to create a company
  must get a 403 from the server. Hiding the button isn't enough, because anyone
  can send the request without the page.
- 404 means "no such thing", which is also the answer for a record id that
  doesn't exist.

`test_unknown_sort_field_is_ignored` is a security test, not a tidiness one. The
list pages let `?sort=` pick a column, and `owner__password` is someone probing
whether they can sort by a field they should never touch. The view only accepts
sort fields from a fixed allowlist.
"""

from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import Activity, Company, Deal, Stage

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user


class LoginRequiredTests(TestCase):
    def test_every_page_redirects_anonymous_users(self):
        company = make_company()
        deal = make_deal(company)
        contact = make_contact(company=company)
        urls = [
            reverse("dashboard"), reverse("company_list"), reverse("company_create"),
            reverse("company_detail", args=[company.pk]), reverse("company_update", args=[company.pk]),
            reverse("contact_list"), reverse("contact_detail", args=[contact.pk]),
            reverse("deal_list"), reverse("deal_detail", args=[deal.pk]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn(reverse("login"), resp["Location"], url)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)


class RolePermissionTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", "Admin")
        self.rep = make_user("rep", "Rep")
        self.rep2 = make_user("rep2", "Rep")
        self.viewer = make_user("viewer", "Viewer")
        self.nobody = make_user("nogroup")
        self.company = make_company(owner=self.rep, name="Rep One Co")

    def login(self, user):
        self.client.login(username=user.username, password=PASSWORD)

    def test_viewer_can_read_but_not_write(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("company_detail", args=[self.company.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("company_create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("company_create"), {"name": "X", "lifecycle": "lead"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("company_update", args=[self.company.pk])).status_code, 403)
        resp = self.client.post(reverse("activity_create", args=["company", self.company.pk]), {"kind": "note", "body": "hi"})
        self.assertEqual(resp.status_code, 403)

    def test_user_with_no_group_is_a_viewer(self):
        self.login(self.nobody)
        self.assertEqual(self.client.get(reverse("company_create")).status_code, 403)

    def test_rep_edits_own_record_only(self):
        self.login(self.rep2)
        resp = self.client.post(reverse("company_update", args=[self.company.pk]), {"name": "Hijacked", "lifecycle": "lead"})
        self.assertEqual(resp.status_code, 403)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Rep One Co")

        self.client.logout()
        self.login(self.rep)
        resp = self.client.post(reverse("company_update", args=[self.company.pk]), {"name": "Renamed", "lifecycle": "lead"})
        self.assertEqual(resp.status_code, 302)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Renamed")

    def test_rep_cannot_set_owner_and_owns_what_they_create(self):
        self.login(self.rep)
        self.client.post(reverse("company_create"), {"name": "Mine", "lifecycle": "lead", "owner": self.rep2.pk})
        self.assertEqual(Company.objects.get(name="Mine").owner, self.rep)

    def test_rep_cannot_reassign_owner_on_edit(self):
        self.login(self.rep)
        self.client.post(reverse("company_update", args=[self.company.pk]),
                         {"name": "Rep One Co", "lifecycle": "lead", "owner": self.rep2.pk})
        self.company.refresh_from_db()
        self.assertEqual(self.company.owner, self.rep)

    def test_admin_can_assign_owner(self):
        self.login(self.admin)
        self.client.post(reverse("company_create"), {"name": "Assigned", "lifecycle": "lead", "owner": self.rep2.pk})
        self.assertEqual(Company.objects.get(name="Assigned").owner, self.rep2)

    def test_only_admin_deletes(self):
        self.login(self.rep)
        self.assertEqual(self.client.post(reverse("company_delete", args=[self.company.pk])).status_code, 403)
        self.client.logout()
        self.login(self.admin)
        self.client.post(reverse("company_delete", args=[self.company.pk]))
        self.assertFalse(Company.objects.filter(pk=self.company.pk).exists())

    def test_delete_company_with_deals_is_blocked_with_message(self):
        make_deal(self.company)
        self.login(self.admin)
        resp = self.client.post(reverse("company_delete", args=[self.company.pk]), follow=True)
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertContains(resp, "still has deals")

    def test_rep_can_log_activity_on_any_record(self):
        self.login(self.rep2)
        resp = self.client.post(reverse("activity_create", args=["company", self.company.pk]), {"kind": "call", "body": "Called"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Activity.objects.get().author, self.rep2)

    def test_rep_removes_only_own_activity(self):
        mine = Activity.objects.create(body="mine", company=self.company, author=self.rep2)
        theirs = Activity.objects.create(body="theirs", company=self.company, author=self.rep)
        self.login(self.rep2)
        self.assertEqual(self.client.post(reverse("activity_delete", args=[theirs.pk])).status_code, 403)
        self.client.post(reverse("activity_delete", args=[mine.pk]))
        self.assertFalse(Activity.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Activity.objects.filter(pk=theirs.pk).exists())

    def test_activity_endpoints_reject_get(self):
        self.login(self.rep)
        self.assertEqual(self.client.get(reverse("activity_create", args=["company", self.company.pk])).status_code, 405)

    def test_activity_unknown_target_404(self):
        self.login(self.rep)
        self.assertEqual(self.client.post(reverse("activity_create", args=["user", 1]), {"body": "x"}).status_code, 404)


class DealFormTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep)

    def test_lost_without_reason_rejected(self):
        deal = make_deal(self.company, owner=self.rep)
        resp = self.client.post(reverse("deal_update", args=[deal.pk]),
                                {"name": deal.name, "company": self.company.pk, "stage": Stage.LOST, "value": "10"})
        self.assertEqual(resp.status_code, 200)
        deal.refresh_from_db()
        self.assertNotEqual(deal.stage, Stage.LOST)

    def test_contact_from_other_company_rejected(self):
        other = make_company(name="Other")
        stranger = make_contact(company=other, email="s@other.com")
        resp = self.client.post(reverse("deal_create"), {"name": "D", "company": self.company.pk,
                                                         "primary_contact": stranger.pk, "stage": "lead", "value": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Deal.objects.exists())


class ListViewTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        make_company(name="Alpha Analytics", domain="alpha.com", owner=self.rep)
        make_company(name="Beta Bakery", domain="beta.com")

    def test_search(self):
        resp = self.client.get(reverse("company_list"), {"q": "alpha"})
        self.assertContains(resp, "Alpha Analytics")
        self.assertNotContains(resp, "Beta Bakery")

    def test_unknown_sort_field_is_ignored(self):
        resp = self.client.get(reverse("company_list"), {"sort": "owner__password"})
        self.assertEqual(resp.status_code, 200)

    def test_sort_descending(self):
        resp = self.client.get(reverse("company_list"), {"sort": "-name"})
        body = resp.content.decode()
        self.assertLess(body.index("Beta Bakery"), body.index("Alpha Analytics"))

    def test_history_tab_shows_changes(self):
        company = Company.objects.get(name="Alpha Analytics")
        self.client.post(reverse("company_update", args=[company.pk]), {"name": "Alpha Analytics Co", "lifecycle": "customer"})
        resp = self.client.get(reverse("company_detail", args=[company.pk]))
        self.assertContains(resp, "Alpha Analytics Co")
        self.assertContains(resp, "<del>Lead</del>", html=False)


class ActivityBodyAutoescapeTests(TestCase):
    """
    An activity body is user text, and the timeline renders it as HTML. This is
    the test that would catch somebody "fixing" the timeline by adding `|safe`
    to the body in partials/activity_item.html.

    TEACHING NOTE — the body is written straight to the database here rather
    than round-tripped through anything, because that is the point: markup-shaped
    text sitting in `Activity.body` is a legitimate state. Someone pasting mail
    into a note, or an import bringing in a hostile string, both produce it. Only
    template autoescaping stands between that text and the browser, so the first
    assertion pins that the raw `<script>` really did reach the database — this
    test would silently stop proving anything if a future change started
    sanitising on write.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.contact = make_contact(owner=self.rep, email="dana@reyes.example")
        self.client.login(username="rep", password=PASSWORD)

    def test_encoded_markup_in_a_body_is_inert_on_the_contact_page(self):
        Activity.objects.create(contact=self.contact, author=self.rep,
                                kind=Activity.Kind.EMAIL, body="<script>alert('enc')</script>")

        # Stored verbatim: autoescaping is a template-time concern, not a write-time one.
        stored = Activity.objects.get(contact=self.contact).body
        self.assertIn("<script>", stored)

        html = self.client.get(self.contact.get_absolute_url()).content.decode()
        self.assertNotIn("<script>alert('enc')", html)
        self.assertIn("&lt;script&gt;", html)


class TemplateCommentTests(TestCase):
    """
    Django's `{# ... #}` comment is SINGLE-LINE ONLY. One that wraps onto a second
    line is not a comment at all: the template engine prints it, braces and all,
    onto the page. Two did exactly that (the deal timeline and the contact page's
    buying-role row) until a browser click-through on 2026-10-06 showed the raw
    text to a user. Multi-line notes use `{% comment %}`.
    """

    def test_no_template_has_a_multi_line_hash_comment(self):
        root = Path(settings.BASE_DIR) / "templates"
        offenders = []
        for path in root.rglob("*.html"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                # Every `{#` on the line must be closed on the same line.
                if line.count("{#") > line.count("#}"):
                    offenders.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual(offenders, [], msg="a {# #} comment that wraps renders as page text")
