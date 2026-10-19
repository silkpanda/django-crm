"""
Phase 6, workstream 3 — the read-only JSON API: shape, scope, and what a client
is allowed to be told.

WHAT THIS SPECIFIES
-------------------
Seven GET routes under /api/ over companies, contacts and deals, session-authed,
serialized by plain functions, paginated 50 to a page. No writes, no tokens, no
DRF.

THE FOUR TESTS THAT MATTER, in the order a real bug would arrive:

  - `AnonymousAccessTests` — the login gate. The failure mode worth pinning is
    not "anonymous gets refused"; it is "anonymous gets refused with an HTML
    login page", which passes a status-code assertion and still breaks every
    client, because `LoginRequiredMiddleware` answers a 302 and a JSON consumer
    either follows it into markup or dies in its parser. So the content type is
    asserted, not just the 403.
  - `DealScopeTests` — the difference between a read API and an exfiltration
    endpoint wearing a JSON header. A Rep's `/api/deals/` must be the same set
    as the pipeline board they look at all day, and their colleague's deal must
    be a 404 rather than a 403.
  - `MoneyIsAStringTests` — `value` as a float would pass a naive
    `assertEqual(1234567.89, ...)` and lose the cents somewhere in transit. The
    test that catches it is the round trip, in the bytes on the wire.
  - `NoSecretsInThePayloadTests` — the payload is walked recursively against a
    denylist, because the serializers are hand-written allowlists and the next
    person to widen one by hand will eventually reach for `_meta.fields`.

ON THE DENYLIST WALK: `serializers.py` emits an explicit key per model rather
than walking `_meta.fields`, precisely so this test can be a denylist. An
allowlist that also had to be checked against a denylist would be two lists to
keep in step; the walk exists to catch the case where somebody adds a key to a
serializer by accident, not to police the design.

ON SUBTESTS: the anonymous-access test and the read-only test iterate with
`subTest`, because "every endpoint" is the requirement. A loop that fails on
the first bad route and reports one line is a test that will be reported as
passing by whoever reruns it.

`db.sqlite3` is a live demo database and is never touched here: Django builds
an empty test database at the start of every run and drops it at the end. Run
this file with `.venv\\Scripts\\python.exe manage.py test core.tests.test_api`.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core import api_urls
from core.models import Company, Contact, Deal

from .factories import PASSWORD, make_company, make_contact, make_deal, make_user

# Keys that must never appear anywhere in a response. Substring matches, so
# `owner_id` and `password_hash` are both covered by one entry each.
FORBIDDEN_KEY_PARTS = ("password", "token", "secret", "owner_id", "history", "import_run", "session")


def documented_routes():
    """
    Every path in core/api_urls.py, as the index prints it.

    TEACHING NOTE — read out of the url module rather than written out again
    here. A hand-copied expected list is a list that stays green forever after
    somebody adds a route and forgets the index, which is the exact failure this
    endpoint exists to prevent. Comparing against the urlconf means the index and
    the routes cannot drift without a test going red.

    `<int:pk>` becomes `<pk>` because the index documents a SHAPE: printing
    `/api/companies/7/` would be a URL that only exists for company 7, and
    printing `/api/companies/0/` would be a URL that 404s.
    """
    return {f"/api/{str(entry.pattern)}".replace("<int:pk>", "<pk>") for entry in api_urls.urlpatterns}


def every_key(value):
    """
    Every dict key in a parsed JSON payload, at any depth.

    TEACHING NOTE — a recursive walk rather than a spot check on the top level.
    The whole class of bug this guards is "somewhere deeper in the structure
    somebody reached for the model", and a test that only looks at the first
    level is a test that passes on exactly the day it is needed.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from every_key(nested)
    elif isinstance(value, list):
        for item in value:
            yield from every_key(item)


class ApiTestCase(TestCase):
    """One signed-in Rep per test class, and a helper for the two URLs that repeat."""

    def setUp(self):
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("dana", "Rep")
        self.other_rep = make_user("sam", "Rep")

    def login(self, user):
        self.assertTrue(self.client.login(username=user.username, password=PASSWORD))
        return user

    def ids_in(self, response):
        return sorted(row["id"] for row in response.json()["results"])


# ------------------------------------------------------------------- the index


class ApiIndexTests(ApiTestCase):
    """Spec 1: /api/ lists what exists, and is behind the login like everything else."""

    def test_it_lists_every_endpoint_with_its_auth_rule(self):
        """
        Spec 1. Compared against the url module itself (see `documented_routes()`),
        so a route added without a line here fails the build instead of quietly
        shipping an index that lies about what exists.
        """
        self.login(self.rep)
        response = self.client.get(reverse("api_index"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        payload = response.json()
        self.assertEqual(
            {entry["path"] for entry in payload["endpoints"]},
            documented_routes(),
            msg="the index and core/api_urls.py disagree about which routes exist",
        )
        for entry in payload["endpoints"]:
            with self.subTest(path=entry["path"]):
                self.assertEqual(entry["method"], "GET", msg="there is no other method under /api/")
                self.assertTrue(entry["auth"], "an endpoint with no stated auth rule is what this page prevents")

    def test_it_says_the_api_is_read_only_and_session_authed(self):
        """
        The two facts a client has to be told before it writes any code: there is
        nothing to write, and there is no token to obtain. Both are in the index
        because a client that has to read the module docstring to find them will
        not read the module docstring.
        """
        self.login(self.rep)
        payload = self.client.get(reverse("api_index")).json()

        self.assertTrue(payload["read_only"])
        self.assertIn("Session", payload["authentication"])
        self.assertIn("no token", payload["authentication"])

    def test_a_browser_gets_the_page_and_anything_else_gets_the_object(self):
        """
        Spec 1's second half, and the reason `/api/` is two answers.

        `wants_html()` reads the raw Accept header rather than `request.accepts()`
        because the callers that matter here send no Accept at all — this test's
        first request is one of them, and it must get JSON, not a web page.
        """
        self.login(self.rep)

        as_object = self.client.get(reverse("api_index"))
        self.assertEqual(as_object["Content-Type"], "application/json")

        as_page = self.client.get(reverse("api_index"), HTTP_ACCEPT="text/html,application/xhtml+xml")
        self.assertEqual(as_page.status_code, 200)
        self.assertIn("text/html", as_page["Content-Type"])
        self.assertContains(as_page, "/api/deals/")

        # Asking for JSON explicitly must not be talked out of it by the browser
        # default — `Accept: */*` is what curl and the test client both send.
        self.assertEqual(self.client.get(reverse("api_index"), HTTP_ACCEPT="*/*")["Content-Type"], "application/json")


class AnonymousAccessTests(ApiTestCase):
    """Spec 2: every route refuses an anonymous caller, in JSON, with no redirect."""

    def setUp(self):
        super().setUp()
        self.company = make_company(name="Acme Supply")
        self.contact = make_contact(company=self.company)
        self.deal = make_deal(self.company, name="Plan upgrade")
        self.routes = [
            reverse("api_index"),
            reverse("api_company_list"),
            reverse("api_company_detail", args=[self.company.pk]),
            reverse("api_contact_list"),
            reverse("api_contact_detail", args=[self.contact.pk]),
            reverse("api_deal_list"),
            reverse("api_deal_detail", args=[self.deal.pk]),
        ]

    def test_every_endpoint_403s_anonymous_with_a_json_body(self):
        """
        Spec 2, and the assertion that matters is the second one.

        `LoginRequiredMiddleware` is on globally and its answer for an anonymous
        request is a 302 to the login page. That is correct for a browser and
        useless to a client: it either follows the redirect into markup or hands
        HTML to a JSON parser. So this asserts the status AND the content type
        AND that the body parses — a 403 whose body is an HTML page would pass a
        status-only assertion and break every consumer.
        """
        for route in self.routes:
            with self.subTest(route=route):
                response = self.client.get(route)

                self.assertEqual(response.status_code, 403, msg=f"{route} must refuse an anonymous caller")
                self.assertEqual(
                    response["Content-Type"],
                    "application/json",
                    msg=f"{route} answered an API client with something it cannot parse",
                )
                self.assertIn("detail", response.json())

    def test_it_does_not_redirect_to_the_login_page(self):
        """
        The same fact from the other side. A 302 is the failure this whole design
        exists to avoid, so it is pinned separately: a future change that
        restores the redirect would turn the test above into a 403/302 mismatch
        that reads like a routing bug rather than what it is.
        """
        response = self.client.get(reverse("api_deal_list"))

        self.assertNotEqual(response.status_code, 302)
        self.assertNotIn("Location", response)


# -------------------------------------------------------------- companies


class CompanyApiTests(ApiTestCase):
    """Spec 3 and 4: the list envelope, one company, and a pk that isn't there."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Lakeside Dental", industry="Healthcare")
        self.other = make_company(owner=self.other_rep, name="Northwind Traders")

    def test_the_list_is_the_documented_envelope(self):
        """
        Spec 3. The four keys are checked by name and by shape, because the whole
        reason this response borrows DRF's shape is that a client written against
        it keeps working if DRF is ever swapped in. A list of bare objects would
        be simpler and would break every one of them.
        """
        self.login(self.rep)
        payload = self.client.get(reverse("api_company_list")).json()

        self.assertEqual(set(payload), {"count", "next", "previous", "results"})
        self.assertEqual(payload["count"], 2)
        self.assertIsNone(payload["next"], msg="two rows fit on one page, so there is no next page")
        self.assertIsNone(payload["previous"])
        self.assertEqual(
            payload["results"][0],
            {
                "id": self.company.pk,
                "name": "Lakeside Dental",
                "domain": "",
                "industry": "Healthcare",
                "lifecycle": "lead",
                "address": "",
                "phone": "",
                "website": "",
                "created_at": self.company.created_at.isoformat(),
            },
            msg="the field list is the contract, and it is an allowlist rather than a model dump",
        )

    def test_a_rep_sees_every_company_not_only_their_own(self):
        """
        Not in the spec's numbered list, and here because it is the one place the
        API could be wrong in the SAFE direction and nobody would notice for
        months. In this app only EDITS are ownership-gated — `CompanyListView`
        hands every company to every signed-in user — so a client that quietly
        filtered here would report a smaller pipeline than the screen beside it.
        `ContactApiTests` says the same about contacts.
        """
        self.login(self.rep)

        self.assertEqual(self.ids_in(self.client.get(reverse("api_company_list"))), sorted(
            [self.company.pk, self.other.pk]
        ))

    def test_detail_returns_one_company(self):
        self.login(self.rep)
        response = self.client.get(reverse("api_company_detail", args=[self.company.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Lakeside Dental")
        self.assertEqual(response.json()["id"], self.company.pk)

    def test_a_pk_that_does_not_exist_is_404_and_says_so_in_json(self):
        self.login(self.rep)
        response = self.client.get(reverse("api_company_detail", args=[Company.objects.count() + 999]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")


# ----------------------------------------------------------------- contacts


class ContactApiTests(ApiTestCase):
    """Spec 5: contacts behave like companies, plus the company name in the payload."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Lakeside Dental")
        self.theirs = make_company(owner=self.other_rep, name="Northwind Traders")
        self.contact = make_contact(
            owner=self.other_rep, company=self.company, first_name="Pat", last_name="Doe", email="pat@example.com"
        )
        self.orphan = make_contact(owner=self.rep, first_name="Ray", last_name="Cole", email=None)

    def test_the_list_carries_the_company_id_and_its_name(self):
        """
        Spec 5. Both, not one: the id is the join key and the name is what a human
        reads, and sending only the id would send every consumer off for a second
        request per row. Asserted on a contact owned by somebody ELSE too, which
        is the case a copy-paste that filtered on owner would get wrong.
        """
        self.login(self.rep)
        payload = self.client.get(reverse("api_contact_list")).json()

        self.assertEqual(payload["count"], 2)
        row = next(r for r in payload["results"] if r["id"] == self.contact.pk)
        self.assertEqual(row["company"], self.company.pk)
        self.assertEqual(row["company_name"], "Lakeside Dental")
        self.assertEqual(row["email"], "pat@example.com")

    def test_a_contact_with_no_company_or_email_is_null_not_an_error(self):
        """
        Both columns are nullable by design — `Contact.save()` turns a blank
        email into NULL precisely so the unique constraint permits many contacts
        without one — so this is the ordinary shape of a row, not an edge case.
        A serializer that emitted "" here would be inventing data, and one that
        raised would be refusing to answer.
        """
        self.login(self.rep)
        row = next(
            r for r in self.client.get(reverse("api_contact_list")).json()["results"] if r["id"] == self.orphan.pk
        )

        self.assertIsNone(row["company"])
        self.assertIsNone(row["company_name"])
        self.assertIsNone(row["email"])

    def test_detail_and_a_missing_pk(self):
        self.login(self.rep)

        found = self.client.get(reverse("api_contact_detail", args=[self.contact.pk]))
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["last_name"], "Doe")

        missing = self.client.get(reverse("api_contact_detail", args=[Contact.objects.count() + 999]))
        self.assertEqual(missing.status_code, 404)


# ------------------------------------------------------------------- scope


class DealScopeTests(ApiTestCase):
    """Spec 6, 7 and 12: the part of this workstream that decides whether it ships."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Lakeside Dental")
        self.mine = make_deal(self.company, owner=self.rep, name="Plan upgrade")
        self.theirs = make_deal(self.company, owner=self.other_rep, name="Hardware refresh")
        self.admin_deal = make_deal(self.company, owner=self.admin, name="Security audit")
        self.unowned = make_deal(self.company, owner=None, name="Website rebuild")

    def test_a_rep_gets_only_their_own_deals(self):
        """
        Spec 6, the exact set rather than a count — a count would pass while the
        filter drifted to "mine plus one of Sam's". `count` also lies when the
        caller's own deals are missing and somebody else's are present, which is
        the version of this bug that reaches a report.
        """
        self.login(self.rep)

        self.assertEqual(self.ids_in(self.client.get(reverse("api_deal_list"))), [self.mine.pk])

    def test_a_rep_cannot_smuggle_the_team_scope_in_the_url(self):
        """
        Spec 12, asserted on the RESULT and not on the parameter being rejected.

        The distinction is the whole test. A Rep is not blocked from typing
        `?scope=team` — `deal_scope()` reads it, ignores it because the role is
        not Admin, and goes on to MINE. Nothing errors and nothing 400s; the
        parameter is simply never able to widen a filter, which is the property
        that matters. A test asserting "the request was refused" would be
        asserting an implementation detail, and would fail if that
        implementation were replaced by a better one.
        """
        self.login(self.rep)

        response = self.client.get(reverse("api_deal_list"), {"scope": "team"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.ids_in(response),
            [self.mine.pk],
            msg="a Rep's ?scope=team must not return a single one of Sam's deals",
        )

    def test_an_admin_gets_the_team(self):
        """Spec 6's other half. Admin is TEAM by default; `?scope=` moves it."""
        self.login(self.admin)

        self.assertEqual(
            self.ids_in(self.client.get(reverse("api_deal_list"))),
            sorted([d.pk for d in Deal.objects.all()]),
        )

    def test_an_admin_asking_for_mine_gets_mine(self):
        """
        Spec 6's third case: the switch itself. The Admin has deals of their own
        and deals of somebody else's here precisely so "mine" cannot pass by
        returning the same set as "team" — a filter that quietly did nothing would
        satisfy the default-scope test and fail this one.
        """
        self.login(self.admin)

        response = self.client.get(reverse("api_deal_list"), {"scope": "mine"})

        self.assertEqual(self.ids_in(response), [self.admin_deal.pk])

    def test_a_rep_asking_for_another_reps_deal_by_pk_gets_404(self):
        """
        Spec 7. 404, not 403, for the reason `views_notifications.py` sets out: a
        403 confirms the row is real to somebody who should not know it exists.
        200 would be worse — that is the exfiltration. All three answers are
        asserted by status, and the 404 must be the one that survives.
        """
        self.login(self.rep)

        response = self.client.get(reverse("api_deal_detail", args=[self.theirs.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Hardware refresh", response.content.decode())

    def test_a_rep_can_still_read_their_own_deal_by_pk(self):
        self.login(self.rep)

        response = self.client.get(reverse("api_deal_detail", args=[self.mine.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Plan upgrade")

    def test_an_admin_can_read_any_deal_by_pk(self):
        self.login(self.admin)

        self.assertEqual(self.client.get(reverse("api_deal_detail", args=[self.theirs.pk])).status_code, 200)

    def test_a_deal_detail_url_cannot_re_scope_an_admins_board(self):
        """
        Not in the spec's list, and here because it is a side effect the API would
        otherwise have on the HTML app.

        `deal_scope()` REMEMBERS `?scope=` in the session — that is how an Admin's
        choice carries from the board to the next screen. If a detail URL honoured
        it, then `curl /api/deals/5/?scope=mine` would silently re-scope the
        pipeline board the next time that Admin opened it, from a URL that has
        nothing to do with the board. So the parameter is dropped on the detail
        route and only the remembered value is read; the assertion is on the
        session, because "the board changed" is not visible from inside this
        request.
        """
        self.login(self.admin)

        self.client.get(reverse("api_deal_detail", args=[self.theirs.pk]), {"scope": "mine"})

        self.assertNotIn(
            "deal_scope", self.client.session, msg="a detail URL must not write the board's scope"
        )


# ------------------------------------------------------------------- money


class MoneyIsAStringTests(ApiTestCase):
    """Spec 8 and 9: a deal value that loses its cents is a bug with a UI."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Lakeside Dental")
        self.deal = make_deal(
            self.company, owner=self.rep, name="Plan upgrade", value=Decimal("1234567.89"), stage="proposal"
        )

    def test_the_value_is_a_json_string_and_keeps_its_cents(self):
        """
        Spec 8. Both halves, and the second is the one that matters.

        `assertEqual(1234567.89, ...)` would PASS against a float — Python prints
        it as `1234567.89` and looks perfect while the value on the wire is the
        nearest IEEE 754 double, which is not the number we stored. So the test
        reads the raw bytes and looks for the cents as text: a float cannot
        produce that substring. `isinstance(..., str)` then pins the declared type
        for a client that trusts the schema.
        """
        self.login(self.rep)
        response = self.client.get(reverse("api_deal_detail", args=[self.deal.pk]))

        payload = response.json()
        self.assertIsInstance(payload["value"], str, msg="money is a string on this API, not a JSON number")
        self.assertEqual(payload["value"], "1234567.89")
        self.assertIn("1234567.89", response.content.decode(), msg="the cents have to survive onto the wire")

    def test_weighted_value_is_present_and_is_a_string(self):
        """
        Spec 9. It is a computed property on the model, not a column, and it is
        serialized anyway: a client forecasting a pipeline should not have to
        reimplement `value * effective_probability / 100` in a language whose
        numbers are all doubles — that is the round-trip bug above, one step
        removed. Proposal defaults to 50%, so half of 1234567.89 is 617283.945.
        """
        self.login(self.rep)

        payload = self.client.get(reverse("api_deal_detail", args=[self.deal.pk])).json()

        self.assertIn("weighted_value", payload)
        self.assertIsInstance(payload["weighted_value"], str)
        self.assertEqual(Decimal(payload["weighted_value"]), self.deal.weighted_value)

    def test_probability_is_the_raw_stored_value_not_the_resolved_one(self):
        """
        Not in the spec's list, and here because the field is ambiguous on purpose:
        a null means "unset, use the stage default", and quietly sending the
        resolved 50 instead would lose the difference between "somebody set 50%"
        and "this deal happens to be in Proposal". `weighted_value` has already
        applied the resolution for anyone who needs it.
        """
        self.login(self.rep)

        payload = self.client.get(reverse("api_deal_detail", args=[self.deal.pk])).json()

        self.assertIsNone(payload["probability"])
        self.assertEqual(payload["stage"], "proposal")
        self.assertEqual(self.deal.effective_probability, 50, msg="...which the model resolves, just not here")


# --------------------------------------------------------------- pagination


class PaginationTests(ApiTestCase):
    """Spec 10 and 11: pages move, a page past the end is empty, and `next` is usable."""

    def setUp(self):
        super().setUp()
        # 51 rows against a page size of 50: two pages, the second holding one.
        # bulk_create because `Company.save()` and its duplicate-domain check are
        # not what this test is about, and 51 of them one at a time is slow.
        Company.objects.bulk_create(
            [Company(name=f"Company {n:02d}", owner=self.rep) for n in range(51)]
        )

    def test_page_one_and_page_two_are_different_pages(self):
        self.login(self.rep)

        first = self.client.get(reverse("api_company_list"), {"page": 1}).json()
        second = self.client.get(reverse("api_company_list"), {"page": 2}).json()

        self.assertEqual(first["count"], 51)
        self.assertEqual(len(first["results"]), 50)
        self.assertEqual(len(second["results"]), 1)
        self.assertNotEqual(
            self.ids_in(self.client.get(reverse("api_company_list"), {"page": 1})),
            self.ids_in(self.client.get(reverse("api_company_list"), {"page": 2})),
            msg="two pages of one list must not hand back the same fifty rows",
        )

    def test_next_is_an_absolute_url_carrying_page_two(self):
        """
        Spec 11. Absolute because a relative one makes every client supply its own
        scheme and host, and the ones behind a proxy guess wrong. Page 2 in the
        string because a `next` that quietly pointed at page 1 would look fine in
        a loop and never terminate.
        """
        self.login(self.rep)

        payload = self.client.get(reverse("api_company_list"), {"page": 1}).json()

        self.assertIsNotNone(payload["next"])
        self.assertTrue(payload["next"].startswith("http://"), msg=f"expected an absolute URL, got {payload['next']}")
        self.assertIn("page=2", payload["next"])

    def test_previous_points_back_at_page_one(self):
        self.login(self.rep)

        payload = self.client.get(reverse("api_company_list"), {"page": 2}).json()

        self.assertIn("page=1", payload["previous"])
        self.assertIsNone(payload["next"], msg="51 rows over two pages: the last page has no next")

    def test_a_page_past_the_end_is_an_empty_200_not_a_404(self):
        """
        Spec 10. A client walking `count` rows in pages of 50 asks for the page
        after the last one the moment a row is deleted underneath it. A 404 there
        means every such client grows an "is this the end?" special case forever;
        an empty list is the answer, and it is the one the client can already
        handle because it is the answer for every other empty list.
        """
        self.login(self.rep)

        response = self.client.get(reverse("api_company_list"), {"page": 99})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
        self.assertEqual(response.json()["count"], 51, msg="the count still describes the list, not the empty page")

    def test_a_junk_page_number_lands_on_page_one(self):
        """
        A fat finger, not a question. `?page=abc` and `?page=0` are the two a
        person actually types by accident, and both answerable without an error —
        which is the same reasoning `RecordListView.paginate_queryset` gives,
        except that it goes on to serve the LAST page for a number past the end
        and this deliberately does not.
        """
        self.login(self.rep)

        for junk in ("abc", "0", "-4", ""):
            with self.subTest(page=junk):
                response = self.client.get(reverse("api_company_list"), {"page": junk})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["results"]), 50)


# ------------------------------------------------------------ what is not there


class NoSecretsInThePayloadTests(ApiTestCase):
    """Spec 13: nothing private leaves the building, at any depth."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.contact = make_contact(company=self.company, owner=self.rep)
        self.deal = make_deal(
            self.company, owner=self.rep, name="Plan upgrade", value=Decimal("50000.00"), stage="proposal"
        )
        self.routes = [
            reverse("api_index"),
            reverse("api_company_list"),
            reverse("api_company_detail", args=[self.company.pk]),
            reverse("api_contact_list"),
            reverse("api_contact_detail", args=[self.contact.pk]),
            reverse("api_deal_list"),
            reverse("api_deal_detail", args=[self.deal.pk]),
        ]

    def test_no_password_hash_token_or_history_anywhere_in_the_payload(self):
        """
        Spec 13. The walk is recursive and the match is a substring, so one entry
        covers `password`, `password_hash` and `user_password_confirm` — the sort
        of near-miss a denylist of exact key names would let through.
        """
        self.login(self.rep)

        for route in self.routes:
            with self.subTest(route=route):
                payload = self.client.get(route).json()
                offenders = [
                    key
                    for key in every_key(payload)
                    if key.startswith("_") or any(part in key.lower() for part in FORBIDDEN_KEY_PARTS)
                ]
                self.assertEqual(offenders, [], msg=f"{route} leaked {offenders}")

    def test_the_owner_is_a_username_and_never_a_raw_user_id(self):
        """
        Spec 13's other half, spelled out because it is the plausible mistake: a
        serializer that reached for the FK would naturally write `owner_id`, which
        is a number that means nothing outside this database and invites clients
        to hardcode it. The username is the thing a Rep says out loud, and it is
        what `DealListView.sort_fields["owner"]` already sorts by.
        """
        self.login(self.rep)

        payload = self.client.get(reverse("api_deal_detail", args=[self.deal.pk])).json()

        self.assertEqual(payload["owner"], "dana")
        self.assertNotEqual(payload["owner"], self.rep.pk)
        self.assertNotIn("owner_id", payload)


class ReadOnlyTests(ApiTestCase):
    """Spec 14: there is no method here that changes anything."""

    def setUp(self):
        super().setUp()
        self.company = make_company(owner=self.rep, name="Acme Supply")
        self.deal = make_deal(self.company, owner=self.rep, name="Plan upgrade", value=Decimal("50000.00"))

    def test_every_route_refuses_every_writing_method_with_405(self):
        """
        Spec 14. The loop, not one call, because "no POST anywhere under /api/"
        is the requirement and a single POST to /api/companies/ proves nothing
        about /api/deals/.

        The row counts are asserted afterwards as well, because a 405 that still
        wrote the row would satisfy the status assertion while doing the exact
        thing the endpoint is not supposed to do. The status is enforced by
        `ApiView.http_method_names`, which means there is no handler a write
        could have hidden in.
        """
        self.login(self.rep)
        before = (Company.objects.count(), Deal.objects.count())
        routes = [
            reverse("api_index"),
            reverse("api_company_list"),
            reverse("api_company_detail", args=[self.company.pk]),
            reverse("api_contact_list"),
            reverse("api_deal_list"),
            reverse("api_deal_detail", args=[self.deal.pk]),
        ]

        for route in routes:
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(route=route, method=method):
                    response = getattr(self.client, method)(route, {"name": "Injected"})

                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response["Content-Type"], "application/json")

        self.assertEqual((Company.objects.count(), Deal.objects.count()), before)

    def test_the_405_body_names_the_methods_that_are_allowed(self):
        """
        Django's own 405 is an HTML body with the allowed methods in a header, so
        a client has to guess. `allow` in the body is the one thing it can act on,
        and it is what tells somebody who typed POST at a read API what to do.
        """
        self.login(self.rep)

        payload = self.client.post(reverse("api_company_list")).json()

        self.assertIn("read-only", payload["detail"])
        self.assertEqual(payload["allow"], ["GET", "HEAD", "OPTIONS"])

    def test_a_get_to_a_detail_route_changes_nothing_either(self):
        """A read endpoint has no side effects, so this is the quiet version of the above."""
        self.login(self.rep)

        response = self.client.get(reverse("api_deal_detail", args=[self.deal.pk]))

        self.assertEqual(response.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.value, Decimal("50000.00"))