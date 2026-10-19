"""
Seed the five lead-intake teams.

TEACHING NOTE — this is a data migration, not a schema one. `LeadSource` has
existed since 0001_initial; what was missing was the ROWS. That distinction is
why this file has no CreateModel in it and why `makemigrations` had nothing to
generate: the schema was always ready, the data was not.

It lives in a migration rather than in a management command on purpose. A data
migration is part of the schema history, so it runs on every database exactly
once, in order — the demo one, a fresh deploy, and the throwaway test database
Django builds for every test run. That is what lets a test assert "the seed
exists" instead of "the seed exists if somebody remembered to run the command".

RunPython's second argument is the REVERSE migration, and it is the part people
skip. A migration you cannot reverse is a migration that cannot be tested on a
throwaway branch, and "works forwards, no way back" is not a plan.
"""

from django.db import migrations

# The four desks a member of staff can file a lead from, plus the one public
# source. `settings.LEAD_TEAMS` names the four; a business with other desks adds
# them in the Django admin rather than editing this file. "Website" is not a team anybody picks — it is stamped by the public
# form on every submission it accepts.
LEAD_TEAMS = [
    ("Sales",),
    ("Support",),
    ("Partners",),
    ("Front desk",),
    ("Website",),
]


def seed_lead_teams(apps, schema_editor):
    """
    Create the five teams, skipping any that are already there.

    TEACHING NOTE: `apps.get_model`, never `from core.models import LeadSource`.
    A migration describes the database as it was at that point in history; the
    live model is free to have moved on since, and importing it here would make
    this file break the next time the model changes. The historical registry is
    the version that is true for THIS migration.

    `get_or_create` rather than `create`, so applying this twice — which is
    exactly what happens when two branches merge, or when somebody replays it on
    the demo database by hand — cannot fail on the unique name.
    """
    LeadSource = apps.get_model("core", "LeadSource")
    for (name,) in LEAD_TEAMS:
        LeadSource.objects.get_or_create(name=name)


def unseed_lead_teams(apps, schema_editor):
    """
    The reverse: drop exactly these five names, and nothing else.

    TEACHING NOTE — filtering on the NAME list rather than deleting every
    LeadSource row. An Admin may have added a sixth team ("Wholesale", set up by
    hand in the Django admin) and reversing the migration must not take it with
    it.

    And it must not delete a team that is in use. `Company.source` and
    `Contact.source` are `on_delete=SET_NULL`, so the rows themselves would
    survive — but every lead already attributed to that team would quietly lose
    its attribution, and the company list's "where did this come from" column
    would go blank. A reverse that quietly destroys data is worse than a reverse
    that refuses, so this leaves referenced teams alone and says so.
    """
    LeadSource = apps.get_model("core", "LeadSource")
    Company = apps.get_model("core", "Company")
    Contact = apps.get_model("core", "Contact")
    names = [name for (name,) in LEAD_TEAMS]
    in_use = set(Company.objects.filter(source__name__in=names).values_list("source__name", flat=True))
    in_use |= set(Contact.objects.filter(source__name__in=names).values_list("source__name", flat=True))
    LeadSource.objects.filter(name__in=names).exclude(name__in=in_use).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_buying_roles"),
    ]

    operations = [
        migrations.RunPython(seed_lead_teams, unseed_lead_teams),
    ]