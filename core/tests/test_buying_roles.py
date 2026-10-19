"""
Buying roles — failing tests for the "who is this person in the sale" feature.

WHAT THIS SPECIFIES
-------------------
A contact can hold any number of BUYING ROLES: their part in the sale at their
own company. Champion, Decision Maker, Economic Buyer, Influencer, Technical
Evaluator, End User, Blocker — in that order, because that is the order a deal
walks through them. It is a first-class list of its own, not a tag: "Champion"
is not a word anybody types into a free-text field, it is one of seven choices
the app knows about, it has a position in the sale, and you can filter the
contacts list by it.

  - `BuyingRole` in core.models: `name` unique, an integer `order`, ordered by
    order then name.
  - `Contact.roles`: a many-to-many to it, blank=True.
  - A data migration seeds all seven, so every database — the demo one, a fresh
    deploy, the throwaway test database — starts with the same seven.

WATCH THE WORD "ROLE" IN THIS FILE
----------------------------------
core.permissions.ROLES is ["Admin", "Rep", "Viewer"]: the permission GROUPS a
user belongs to. That is what `make_user(username, role)` takes and what
`user_role` means in every template. A buying role is a different animal
entirely, and this file never uses "role" bare to mean the first one. Which is
why the helper below is `buying_role(name)` and not `role(name)` — one
`role()` helper in the test suite that meant "give me the Viewer group" is how
two meanings end up in one assertion.

THESE TESTS WERE WRITTEN RED FIRST. Nothing in this file changes the app; the
assertions ARE the spec, and turning them green is a separate job. Today every
one of them is red, most of them because `core.models.BuyingRole` does not exist
yet. That is why `BuyingRole` is imported inside `roles_model()` rather than at
the top of the file: a module-level `from core.models import BuyingRole` raises
ImportError while Python is still LOADING this file, and Django's test runner
reports that as one broken module — you would get zero test results and no idea
which of the eleven behaviours were already correct. Importing it inside the
callers means the module loads, all eleven tests are collected and reported, and
each one fails or errors for its own reason.

Notes for whoever turns this green — the traps these tests are aimed at:

  - The seven checkboxes are the whole reason the form field needs
    `forms.CheckboxSelectMultiple`. A ModelMultipleChoiceField left on its
    default widget renders a multi-select box, not checkboxes, and every other
    test in this file would still pass: the POSTs, the CSV, the filter. The
    count of 7 is what catches it.
  - `_export_csv` joins a many-to-many with `sorted(...)`, so `tags` comes out
    alphabetically. Reusing that line for `roles` gives the wrong answer for
    every pair except the alphabetical coincidence in the first half of the
    export test, which is why that test carries a second contact whose role
    order and alphabetical order disagree.
  - simple-history only records a many-to-many if the field is named in
    `HistoricalRecords(m2m_fields=[...])`. Adding `roles` to the model without
    adding it there leaves the History tab silent about the one thing this
    feature is for.
  - "Blocked, no-store, always private" is not what this file is about. This is
    the data model, the form, the two pages, the filter, the export and the
    admin.

`db.sqlite3` is a live demo database and is never touched here: Django builds an
empty test database at the start of every run and drops it at the end. Run this
file with `.venv/Scripts/python.exe manage.py test core.tests.test_buying_roles`.
"""

import csv
import io
import re

from django.contrib import admin
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import Contact

from .factories import PASSWORD, make_company, make_contact, make_user


# ------------------------------------------------------------------- helpers


def roles_model():
    """
    The model, imported lazily because it does not exist yet.

    TEACHING NOTE: see the module docstring — the import lives inside the
    function so this module still loads and all eleven tests still get reported.
    The ImportError each test raises today is the honest red: "cannot import
    name 'BuyingRole' from 'core.models'".
    """
    from core.models import BuyingRole

    return BuyingRole


def buying_role(name):
    """The seeded buying role called `name` (e.g. "Champion")."""
    return roles_model().objects.get(name=name)


# A role pill is `<span class="pill role-pill">Champion</span>` and nothing
# cleverer: the opening tag must carry `role-pill` among its classes, and the
# element's text must be the role's name and nothing else. The capture group and
# the backreference make the closing tag match the opening one, so a tag that
# opens in one element and closes in another can't satisfy this.
ROLE_PILL = r'<(\w+)(?=[^>]*class="[^"]*\brole-pill\b)[^>]*>\s*%s\s*</\1>'


def assert_role_pill(test, html, name):
    """The page shows `name` as a role pill. Returns the match, for positions."""
    match = re.search(ROLE_PILL % re.escape(name), html)
    test.assertIsNotNone(
        match,
        msg=(
            f"no role pill carrying {name!r} on this page. A pill is an element "
            f'whose class includes role-pill wrapping the role\'s name, e.g. '
            f'<span class="pill role-pill">{name}</span>.'
        ),
    )
    return match


def panel(html, marker, stop="<section"):
    """
    One panel's markup and nothing else, so an assertion can't be satisfied by
    a neighbouring one.

    TEACHING NOTE: the detail pages stack `<section class="panel">` blocks, so
    the slice between the panel we mean and the next section tag is exactly that
    panel. Returns "" when the marker isn't there, which makes the caller's
    assertion fail with the page in front of it instead of a ValueError from
    `.index` with no context.
    """
    start = html.find(marker)
    if start == -1:
        return ""
    end = html.find(stop, start + len(marker))
    return html[start:] if end == -1 else html[start:end]


def csv_rows(response):
    """
    An export's rows, as a list of lists of strings.

    `utf-8-sig` because the export writes a byte-order mark for Excel: decoded as
    plain utf-8 the first header cell comes back as "\\ufeffid" and every column
    comparison in the export test fails for a reason that has nothing to do with
    roles.
    """
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def exported_row(rows, contact):
    """The exported row for one contact, found by its id (the export's first column)."""
    return next(row for row in rows[1:] if row[0] == str(contact.pk))


# ---------------------------------------------------- the model and the seed

# The spec's items 1 and 2: the seven rows every database starts with, and the
# uniqueness rule that keeps two people from inventing a second "Champion".


class BuyingRoleModelTests(TestCase):
    def test_the_seven_roles_are_seeded_in_deal_order(self):
        """
        The seed is part of the feature, not a convenience.

        TEACHING NOTE: no `.order_by()` in this query on purpose. The order comes
        from `BuyingRole.Meta.ordering`, so this test also pins that the model's
        default ordering is (order, name) — every dropdown, the form's checkbox
        order and the CSV cell all lean on that one declaration rather than
        sorting for themselves.

        The order is the order a sale walks: Champion sells it, Decision Maker
        signs it, Economic Buyer pays for it, the Influencer has an opinion,
        the Technical Evaluator proves it works, the End User has to live with
        it, and the Blocker has to be dealt with. Blocker last because until it
        is, nothing else matters.
        """
        self.assertEqual(
            list(roles_model().objects.values_list("name", flat=True)),
            [
                "Champion",
                "Decision Maker",
                "Economic Buyer",
                "Influencer",
                "Technical Evaluator",
                "End User",
                "Blocker",
            ],
            msg=(
                "all seven buying roles, seeded by a data migration and ordered "
                "by `order` then `name`. A missing role here means the seed "
                "migration is wrong; a wrong ORDER means Meta.ordering is."
            ),
        )

    def test_a_role_name_cannot_be_used_twice(self):
        """
        Uniqueness has to be enforced by the database, not by a form.

        TEACHING NOTE on the nested `transaction.atomic()`: this test runs
        inside the transaction Django wraps every TestCase in, and an
        IntegrityError poisons it — every later query on the connection fails
        with "current transaction is aborted" until something rolls back. The
        inner atomic() is a savepoint: it absorbs the failure so the test ends
        cleanly. The outer assertRaises just proves the database said no.
        """
        BuyingRole = roles_model()
        self.assertTrue(BuyingRole.objects.filter(name="Champion").exists())

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BuyingRole.objects.create(name="Champion")

        self.assertEqual(
            BuyingRole.objects.filter(name="Champion").count(),
            1,
            msg="the refused insert must leave the original row alone",
        )


# ------------------------------------------------------------- the form

# Spec items 3 and 4: a Rep posts two roles on create, and the form offers all
# seven as checkboxes.


class ContactRoleFormTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)
        self.client.login(username="rep", password=PASSWORD)

    def test_rep_creates_a_contact_holding_two_roles(self):
        champion = buying_role("Champion")
        decision_maker = buying_role("Decision Maker")

        response = self.client.post(
            reverse("contact_create"),
            {
                "first_name": "Dana",
                "last_name": "Whitfield",
                "company": self.company.pk,
                "lifecycle": "lead",
                "roles": [champion.pk, decision_maker.pk],
            },
        )
        # A 200 here is the form refusing the POST, and `roles` is the field
        # under test — a model field that never made it into ContactForm.Meta
        # is ignored silently by a ModelForm, so the contact would save with no
        # roles and only this status code catches it.
        self.assertEqual(response.status_code, 302)

        contact = Contact.objects.get(first_name="Dana")
        self.assertEqual(
            sorted(contact.roles.values_list("name", flat=True)),
            ["Champion", "Decision Maker"],
            msg="the contact's roles are exactly the two that were posted",
        )

    def test_the_contact_form_offers_a_checkbox_per_role(self):
        """
        Seven checkboxes, named `roles`, one per seeded role.

        TEACHING NOTE: this is the assertion that catches the multi-select. A
        ModelMultipleChoiceField left on its default widget renders `<select
        multiple>`, so a page can accept `roles` on the POST perfectly and still
        show a dropdown here. Counting BOTH the `type="checkbox"` and the
        `name="roles"` occurrences is deliberate: it pins the control type AND
        the field name, so neither "checkboxes under some other name" nor "a
        differently-named control" slips through.
        """
        response = self.client.get(reverse("contact_create"))
        self.assertEqual(response.status_code, 200)

        html = response.content.decode()
        self.assertEqual(
            html.count('type="checkbox"'),
            7,
            msg=(
                "the contact form renders one checkbox per buying role, so a "
                "contact can be a Champion and a Decision Maker at the same time. "
                "Zero means the M2M field is on its default multi-select widget."
            ),
        )
        self.assertEqual(
            html.count('name="roles"'),
            7,
            msg='the seven checkboxes have to be posted as one field named "roles"',
        )


# --------------------------------------------------------- the two pages

# Spec items 5 and 6: the pill on the contact, and on the company's Contacts
# panel where a rep actually reads who they are up against.


class RoleDisplayTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)
        self.contact = make_contact(
            owner=self.rep, company=self.company, first_name="Dana", last_name="Whitfield"
        )
        self.client.login(username="rep", password=PASSWORD)

    def test_contact_detail_shows_the_role_as_a_pill(self):
        self.contact.roles.set([buying_role("Champion")])

        response = self.client.get(self.contact.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        assert_role_pill(self, response.content.decode(), "Champion")

    def test_company_detail_contacts_panel_shows_the_role(self):
        """
        On the company page the pill has to sit ON THE CONTACT'S LINE, after
        their name.

        TEACHING NOTE: asserting the whole page would prove nothing here. The
        company detail page carries a Details panel with tags, a timeline and a
        history tab; if the word "Champion" appeared anywhere on it, a pill
        rendered in the wrong place — or no pill at all, with "Champion" only
        in a diff line — would pass. So the assertion is scoped to the Contacts
        panel, and it compares positions inside it: the name, then the pill.
        """
        self.contact.roles.set([buying_role("Champion")])

        response = self.client.get(self.company.get_absolute_url())
        self.assertEqual(response.status_code, 200)

        contacts_panel = panel(response.content.decode(), "<h2>Contacts</h2>")
        self.assertIn(
            self.contact.full_name,
            contacts_panel,
            msg="this contact is not in the company's Contacts panel at all",
        )
        pill = assert_role_pill(self, contacts_panel, "Champion")
        self.assertLess(
            contacts_panel.index(self.contact.full_name),
            pill.start(),
            msg="the pill belongs on the contact's line, after their name",
        )


# ------------------------------------------------------------- the filter

# Spec item 7: ?role=<pk> on the existing contacts list.


class ContactRoleFilterTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.client.login(username="rep", password=PASSWORD)
        self.champion_contact = make_contact(
            owner=self.rep, first_name="Dana", last_name="Whitfield"
        )
        self.other_contact = make_contact(owner=self.rep, first_name="Ray", last_name="Okafor")

    def test_contact_list_returns_only_contacts_holding_that_role(self):
        champion = buying_role("Champion")
        self.champion_contact.roles.set([champion])

        response = self.client.get(reverse("contact_list"), {"role": champion.pk})
        self.assertEqual(response.status_code, 200)
        # django-filter IGNORES a query parameter it has no filter for, so a
        # missing `role` filter does not error — it quietly returns both
        # contacts, which is the bug. Reading the context says so precisely.
        self.assertEqual(
            [c.pk for c in response.context["object_list"]],
            [self.champion_contact.pk],
            msg="?role=<pk> narrows the list to the contacts holding that role",
        )
        self.assertContains(response, "Dana Whitfield")
        self.assertNotContains(response, "Ray Okafor")


# -------------------------------------------------------------- the export

# Spec item 8: a roles column right after tags, in role order.


class ContactRoleExportTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)
        self.client.login(username="rep", password=PASSWORD)

    def export_rows(self):
        response = self.client.get(reverse("contact_list"), {"export": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        return csv_rows(response)

    def test_roles_column_sits_after_tags_and_keeps_role_order(self):
        champion = buying_role("Champion")
        decision_maker = buying_role("Decision Maker")
        contact = make_contact(
            owner=self.rep, company=self.company, first_name="Dana", last_name="Whitfield"
        )
        # Attached in the wrong order on purpose. A M2M has no order of its own,
        # so if the export cell were built from insertion order this would read
        # "Decision Maker; Champion" — which is a different meaning, not a
        # cosmetically different one.
        contact.roles.set([decision_maker, champion])

        header = self.export_rows()[0]
        self.assertIn("tags", header)
        self.assertIn(
            "roles",
            header,
            msg=f"the contacts export has no roles column. Its header is {header}.",
        )
        self.assertEqual(
            header[header.index("tags") + 1],
            "roles",
            msg=(
                f"roles belongs immediately after tags, so the export reads in the "
                f"order a person opens the file for. Header is {header}."
            ),
        )
        column = header.index("roles")
        self.assertEqual(
            exported_row(self.export_rows(), contact)[column],
            "Champion; Decision Maker",
            msg="the cell is the role names in ROLE order, joined the way tags are",
        )

        # The half above can't tell role order from alphabetical order: those two
        # happen to sort the same way. So here is a pair that can't. "Blocker"
        # sorts before "Champion" alphabetically but comes last in the sale, so
        # an export that reuses `_export_csv`'s `sorted(...)` line for every
        # many-to-many reads "Blocker; Champion" and is wrong about the order the
        # sale happens in.
        blocker = buying_role("Blocker")
        late_contact = make_contact(
            owner=self.rep, company=self.company, first_name="Ray", last_name="Okafor"
        )
        late_contact.roles.set([blocker, champion])

        header = self.export_rows()[0]
        self.assertEqual(
            exported_row(self.export_rows(), late_contact)[header.index("roles")],
            "Champion; Blocker",
            msg="role order, not alphabetical: Blocker comes after Champion",
        )


# -------------------------------------------------------- the history panel

# Spec item 9: adding a role is a change to the record, so the record's history
# has to say so.


class RoleHistoryTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.company = make_company(owner=self.rep)
        self.contact = make_contact(
            owner=self.rep, company=self.company, first_name="Dana", last_name="Whitfield"
        )
        self.client.login(username="rep", password=PASSWORD)

    def test_adding_a_role_shows_up_in_the_history_panel(self):
        """
        Who turned this contact into the Champion, and when, is the question the
        history tab exists to answer.

        TEACHING NOTE: this only goes green if `roles` is added to
        `Contact.history = HistoricalRecords(m2m_fields=[...])`. simple-history
        watches the many-to-many fields it is told about and nothing else, and
        `history.py` resolves those link rows to names — so with roles missing
        from that list the edit saves, the pill appears, and the History tab says
        nothing at all about it. Silence is the bug this test pins.
        """
        champion = buying_role("Champion")
        response = self.client.post(
            reverse("contact_update", args=[self.contact.pk]),
            {
                "first_name": self.contact.first_name,
                "last_name": self.contact.last_name,
                "company": self.company.pk,
                "lifecycle": "lead",
                "roles": [champion.pk],
            },
        )
        self.assertEqual(
            response.status_code,
            302,
            msg="the edit has to save before there is any history worth reading",
        )
        self.contact.refresh_from_db()
        self.assertEqual(
            [r.name for r in self.contact.roles.all()],
            ["Champion"],
            msg="the edit really did attach the role",
        )

        page = self.client.get(self.contact.get_absolute_url())
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            "Champion",
            panel(page.content.decode(), 'id="panel-history"'),
            msg=(
                "the history panel has to name the role that was added. It is "
                "scoped to that panel on purpose: the contact's Details panel also "
                "carries a Champion pill, and a whole-page assertion would be "
                "satisfied by that one."
            ),
        )


# ------------------------------------------------------------- permissions

# Spec item 10: a Viewer is read-only, and a refused POST must leave nothing
# half-saved.


class RolePermissionTests(TestCase):
    def setUp(self):
        self.rep = make_user("rep", "Rep")
        self.viewer = make_user("viewer", "Viewer")
        self.company = make_company(owner=self.rep)
        self.contact = make_contact(
            owner=self.rep, company=self.company, first_name="Dana", last_name="Whitfield"
        )

    def test_viewer_cannot_post_roles_to_the_contact_update(self):
        influencer = buying_role("Influencer")
        champion = buying_role("Champion")
        self.contact.roles.set([influencer])

        self.client.login(username="viewer", password=PASSWORD)
        response = self.client.post(
            reverse("contact_update", args=[self.contact.pk]),
            {
                "first_name": self.contact.first_name,
                "last_name": self.contact.last_name,
                "company": self.company.pk,
                "lifecycle": "lead",
                "roles": [champion.pk],
            },
        )
        self.assertEqual(response.status_code, 403)
        # Posted Champion while the contact holds Influencer, so "unchanged" is
        # a real assertion: a server that 403'd but had already written the
        # many-to-many would fail here instead of passing on a coincidence.
        self.contact.refresh_from_db()
        self.assertEqual(
            [r.name for r in self.contact.roles.all()],
            ["Influencer"],
            msg="a refused POST must not half-save the roles it carried",
        )


# ---------------------------------------------------------------- the admin

# Spec item 11: the lookup table is manageable. `SimpleTestCase`, not TestCase:
# reading the admin's registry touches no database.


class RoleAdminTests(SimpleTestCase):
    def test_buying_role_is_registered_in_the_admin(self):
        """
        A lookup table with no admin page gets fixed by someone renaming rows in
        the database, which is how "Influencer" becomes "influencer" and the
        seed migration starts conflicting with live data.
        """
        self.assertTrue(
            admin.site.is_registered(roles_model()),
            msg="BuyingRole belongs in core/admin.py, alongside Tag and LeadSource",
        )
