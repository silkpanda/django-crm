"""
Batch E — deploy hardening: failing tests for QA-14, QA-07, and the branded 404.

Batch C was the timeline, batch D was data integrity. Batch E is the deploy
batch — the findings that only bite once the app is on a real server in front of
real browsers instead of the dev pane.

  QA-14  Signed-in pages are cacheable, so Back after sign-out shows data.
        `/companies/` as demo_rep returns no `Cache-Control` header at all. Sign
        out on a shared office PC, the next person presses Back, and the browser
        redraws the customer list straight from its own cache. The server side
        is fine (after logout the URL 302s to the login page) — the browser just
        never has to ask it. Nothing in the repo sets the header; the fix is
        middleware, or `never_cache` on the views, applied to every
        authenticated response and not just the obvious ones.
  QA-07  Static URLs are bare (`/static/js/app.js`) with no content hash, so
        after every deploy browsers keep running the previous app.js and app.css
        until their cache expires. The fix is
        `STORAGES["staticfiles"] = ManifestStaticFilesStorage` in
        settings_prodlike — but settings_prodlike sets no STORAGES at all, so
        the hashed filenames can never appear.
  404    The branded 404.html has never actually been exercised. Everything in
        this repo runs with DEBUG=1, so a missing URL shows Django's debug page
        and the branded one is dead code the sweep never looked at. The moment
        DEBUG is off on the VM, a mistyped link is a stack trace in front of a
        customer.

WHAT THIS BATCH COVERS — the parts of deploy hardening that DON'T depend on the
VM. Everything that needs the box itself is out of scope for a Django test
client and out of scope here: nginx, the systemd unit, the TLS certificate, the
output of `collectstatic`, and any request that has to travel over real HTTPS.
What IS in scope is everything the app decides for itself — the headers it sets,
the settings it hands the deploy, and the page it renders when DEBUG is off.
All three findings live in that space, and all three can be proven from a test.

THESE TESTS WERE WRITTEN RED FIRST. Nothing in this file changes the app; the
assertions are the spec and turning them green is a separate job. Expected red
today: the QA-14 header tests and both settings_prodlike storage tests.

Two of the green ones are load-bearing and a fix that breaks either has gone too
far:

  - the login page still renders anonymously. QA-14 is about signed-in data, not
    about banning caching everywhere, and test_login_page_renders_anonymously
    pins the narrow reading of the rule.
  - dev settings stay on the unhashed backend. runserver serves files straight
    off disk and has no manifest to read; switching dev over would break every
    local `{% static %}` lookup while fixing nothing about production. That test
    is the tripwire for a QA-07 fix that gets applied to the wrong file.

The two 404 tests are green, and that is a thin result. They prove 404.html
renders and carries the brand and a way back — which, given the sweep ran with
DEBUG=1 the whole time, was genuinely never checked before. They say nothing
about whether the page reads well, and nothing about a 404 raised from anywhere
other than a resolver miss or `get_object_or_404`.

Two notes for whoever writes the QA-14 fix:

  - it is one rule and many pages. These tests pin a list, a dashboard, a detail
    page, a CSV export and an HTMX fragment, because a fix that decorates
    `CompanyListView` alone leaves the export cacheable — and the export is the
    response most likely to get saved onto someone's desktop.
  - the header has to say `private` as well as `no-store`. `no-store` alone stops
    a disk write; `private` stops a shared proxy in a small office from holding
    a rep's customer list for the next person.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_batch_e`.
"""

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

import crm_project.settings_prodlike as prod
from core.models import Stage

from .factories import PASSWORD, make_company, make_deal, make_user

MANIFEST_BACKEND = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"


# ------------------------------------------------------------------ helpers


def assert_not_cached(test, response):
    """
    QA-14 in one place: an authenticated response must carry `no-store` AND
    `private` in its Cache-Control header.

    TEACHING NOTE: the header is read with a `""` default on purpose. A missing
    Cache-Control is the actual bug (the header is absent today), and letting
    that raise a TypeError instead of failing the assertion would bury the real
    finding under a stack trace in the test runner. `.headers` is the
    case-insensitive view Django gives you for this; `response["Cache-Control"]`
    raises if it isn't there at all.
    """
    header = response.headers.get("Cache-Control", "")
    test.assertIn(
        "no-store",
        header,
        f"expected Cache-Control to forbid storage, got {header!r}",
    )
    test.assertIn(
        "private",
        header,
        f"expected Cache-Control to forbid shared caches, got {header!r}",
    )


# -------------------------------------------------------------------- QA-14


class SignedInPagesAreNotCacheableTests(TestCase):
    """
    QA-14 — a signed-in page must not be storable by the browser or a proxy.

    Every test here runs as a Rep on real data, because the finding is "what
    does an authenticated response look like", and an anonymous request never
    carries customer rows in the first place.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.company = make_company(owner=self.rep)

    def test_company_list_is_not_cacheable(self):
        """The page from the finding itself: /companies/ as demo_rep."""
        response = self.client.get(reverse("company_list"))
        self.assertEqual(response.status_code, 200)
        assert_not_cached(self, response)

    def test_other_signed_in_pages_are_not_cacheable(self):
        """
        The rest of the surface, one subTest each so a partial fix reports which
        page still leaks.

        The CSV export earns its place. It is the one response here that gets
        saved to disk and emailed around, and it is a streaming download rather
        than an HTML page, so a fix written as "add never_cache to the template
        views" skips it without anyone noticing until a rep's customer export
        turns up in a shared folder.
        """
        pages = [
            ("dashboard", reverse("dashboard")),
            ("pipeline", reverse("pipeline")),
            ("company_detail", reverse("company_detail", args=[self.company.pk])),
            ("companies CSV export", reverse("company_list") + "?export=1"),
        ]
        for label, url in pages:
            with self.subTest(page=label):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                assert_not_cached(self, response)

    def test_login_page_renders_anonymously(self):
        """
        The other half of QA-14, and the guard on its fix: the login page still
        serves an anonymous visitor.

        Deliberately NO Cache-Control assertion here, in either direction. The
        rule QA-14 actually states is about signed-in data; a fix that blanket-
        applies no-store to every response including the login page is wasteful
        but not wrong, and this test must not fail it. Asserting the header is
        ABSENT would be worse — it would lock in the narrower fix and block the
        middleware approach the finding actually recommends.
        """
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/login.html")

    def test_htmx_stage_move_response_is_not_cacheable(self):
        """
        The HTMX endpoint too — this is the one people forget.

        A move returns a card partial plus out-of-band column headers and
        footers, and HTMX caches the response of the last request for an element
        by default. A cached swap fragment is a board that quietly stops
        updating for one user, and the response carries no `Cache-Control` today,
        so there is nothing stopping it.
        """
        deal = make_deal(self.company, owner=self.rep, stage=Stage.LEAD)
        response = self.client.post(
            reverse("deal_stage_move", args=[deal.pk]),
            {"stage": Stage.NEGOTIATION.value},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        assert_not_cached(self, response)


# --------------------------------------------------------------------- QA-07


class HashedStaticFilesInProductionTests(SimpleTestCase):
    """
    QA-07 — production must serve content-hashed static files.

    `SimpleTestCase`, not `TestCase`: these read settings and touch no
    database, and a test that needs a DB to look at a dict is a test asking for
    the wrong fix.

    TEACHING NOTE on `settings_prodlike`: it does `from .settings import *`, so
    everything in dev is inherited and only the hardened handful is restated.
    That is why these tests import the module and read `prod.STORAGES` directly
    rather than switching `DJANGO_SETTINGS_MODULE` — switching would swap the
    live test settings out from under the running suite, and `@override_settings`
    can't reach a module attribute either. Reading the module is the honest
    version of "what would the VM actually load".
    """

    def test_prodlike_uses_the_manifest_storage(self):
        """QA-07 — hashed filenames, so every deploy is a new URL."""
        self.assertEqual(prod.STORAGES["staticfiles"]["BACKEND"], MANIFEST_BACKEND)

    def test_prodlike_keeps_the_default_file_storage(self):
        """
        QA-07's other half, and the classic way to break it.

        STORAGES is a dict of TWO keys, not one. Assigning
        `STORAGES = {"staticfiles": {...}}` replaces the whole mapping and
        silently drops "default" — which is the file storage for user uploads.
        The symptom isn't a crash at boot, it's a `FileField` quietly saving to
        a relative path at the next deploy. Assert the key survives, and that
        it still names a backend, so a fix that hardcodes one string can't pass
        by leaving an empty dict behind.
        """
        self.assertIn("default", prod.STORAGES)
        self.assertIn("BACKEND", prod.STORAGES["default"])

    def test_dev_settings_are_not_the_manifest_storage(self):
        """
        The tripwire. Dev must keep serving unhashed files.

        runserver resolves `{% static %}` straight from STATICFILES_DIRS and has
        no manifest to read; on the manifest backend a dev run either raises
        `ValueError: Missing staticfiles manifest entry` on the first page
        render or requires a collectstatic after every template tweak. This test
        stays green by design — it is here so a QA-07 fix that gets applied to
        crm_project/settings.py instead of settings_prodlike.py is caught here
        rather than by the next person to run the dev server.
        """
        self.assertNotEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            MANIFEST_BACKEND,
            "dev settings must keep the unhashed backend; the override belongs "
            "in settings_prodlike only",
        )


# ------------------------------------------------------------------ the 404


class Branded404WithDebugOffTests(TestCase):
    """
    The branded 404.html, under the one condition it was written for.

    TEACHING NOTE: every page in this repo has so far been requested with
    DEBUG=1, which makes Django render its own debug page for a 404 and never
    touches templates/404.html. `@override_settings(DEBUG=False)` flips it for
    one test, which is as close to the VM as a test client gets. The point of
    the "no Traceback" assertion is that it stays true after whatever gets
    changed here later: the branded page has to survive being reached from a
    missing URL AND from a missing record, and both have to keep working.
    """

    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)

    def assert_branded_404(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        # "CRM" proves the brand chrome rendered rather than a bare template.
        self.assertContains(response, settings.BRAND_NAME, status_code=404)
        # href="/" is the resolved dashboard route — a 404 with no way back is
        # a dead end for the user who hit it.
        self.assertContains(response, 'href="/"', status_code=404)
        self.assertNotContains(response, "Traceback", status_code=404)

    @override_settings(DEBUG=False)
    def test_missing_url_renders_the_branded_404(self):
        """A bad link: the 404 the sweep never saw, because DEBUG was on."""
        self.assert_branded_404(self.client.get("/no-such-page/"))

    @override_settings(DEBUG=False)
    def test_missing_record_renders_the_branded_404(self):
        """
        A deleted company, which is the case users actually hit: a bookmark or
        an email link pointing at a record that was merged away or removed.

        Raising Http404 here is correct and stays correct — the fix is the page
        the 404 renders, not making the missing record render. This asserts
        exactly that, so a future change that quietly special-cases 404.html
        into something else has to argue with the test.
        """
        self.assert_branded_404(self.client.get(reverse("company_detail", args=[999999])))