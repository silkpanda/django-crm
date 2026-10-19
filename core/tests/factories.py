"""
Test factories: tiny helpers that build a user, company, contact, deal or task
with sensible defaults, so each test only spells out the fields it cares about.

TEACHING NOTE: `make_company(name="Beta")` in a test says "the name is the point
of this test". Everything it doesn't mention gets a boring default here. Without
these, every test would repeat ten lines of setup and you couldn't tell which
line mattered.

`factory-boy` is listed in requirements-dev.txt, but nothing imports it. Five
plain functions turned out easier to read than factory classes. It's safe to
remove from requirements-dev.txt; don't assume these helpers need it.

PASSWORD is one shared, fake password for every test user. It only ever exists
in the test database, which Django creates empty at the start of each test run
and deletes at the end (that's the "Creating test database" line in the output).
Roles are Django Groups (named bundles of permissions). A migration,
0002_roles.py, creates them, which is why `make_user` can look them up without
making them first.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from core.models import Company, Contact, Deal, Task

PASSWORD = "correct-horse-battery-staple"


def make_user(username, role=None, **extra):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD, **extra)
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


def make_company(owner=None, **kw):
    kw.setdefault("name", "Acme Supply")
    return Company.objects.create(owner=owner, **kw)


def make_contact(owner=None, company=None, **kw):
    kw.setdefault("first_name", "Pat")
    kw.setdefault("last_name", "Doe")
    return Contact.objects.create(owner=owner, company=company, **kw)


def make_deal(company, owner=None, **kw):
    kw.setdefault("name", "Plan upgrade")
    return Deal.objects.create(company=company, owner=owner, **kw)


def make_task(assignee=None, **kw):
    kw.setdefault("title", "Follow up")
    return Task.objects.create(assignee=assignee, **kw)
