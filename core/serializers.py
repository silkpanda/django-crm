"""
Model instance -> JSON-ready dict. The whole of the API's field mapping.

TEACHING NOTE — why functions and not classes. Every serializer framework
starts with a `ModelSerializer` class per model and a registry to look them up.
There is no polymorphism to dispatch on here (no `include`, no polymorphic
deals, no nested writes), so a class would be three methods of boilerplate per
model and a registry that has to be kept in step with it. Three functions that
each take an instance and return a dict is the whole requirement; the moment a
fourth thing needs to be serialized the registry can arrive with it.

THE ONE DECISION IN THIS FILE: money is a STRING.

`Deal.value` is a `DecimalField`, and Python's `json` module has no idea. Hand
it a Decimal and it raises `TypeError`; convert it to `float` first, as most
code does reflexively, and it emits a JSON number — which is an IEEE 754 double
on the far side. A deal worth $1,234,567.89 does not survive that: the nearest
double to it is off in the cents, and the client is told a number we never
stored. Python will not warn you. It will print it, look correct, and quietly
lose the money. So `_money()` returns `str(value)`, and the wire format says
"this is a decimal string" rather than pretending to a precision JSON cannot
carry.

The same reasoning is why `weighted_value` — a computed property, not a column
— is serialized at all. A client that wants the forecast number should not have
to reimplement `value * effective_probability / 100`, and reimplementing it in
JavaScript means doing the arithmetic in doubles, which is the bug above one
step removed. The server already worked it out exactly; ship the answer.

WHAT IS DELIBERATELY ABSENT. Each function lists its keys explicitly rather
than walking `_meta.fields`. The walk is shorter to write and it is wrong: it
would emit `owner_id` (a raw FK id, which the API answers with a username),
`notes` and `external_id`, and — the one that matters — `simple_history`'s
`history` manager. An allowlist of what leaves the building is the only version
of this that stays true as somebody adds a column. Test 13 in test_api.py walks
the parsed response and fails if a key from the denylist ever appears, so the
next person to widen this has to update a list in two places on purpose.
"""

from datetime import date, datetime
from decimal import Decimal


def _money(value: Decimal | None) -> str | None:
    """
    A Decimal as a string, for the reason in the module docstring.

    TEACHING NOTE — the None guard is not defensive programming for its own
    sake. `Deal.value` is `default=0` and NOT NULL, so a saved deal always has
    one; but `weighted_value` is arithmetic on a value the caller may have set
    to None in an unsaved instance, and a serializer that renders `str(None)`
    as the four-character string "None" has shipped a value no client can parse.
    One `is None` test, and null stays null.
    """
    return None if value is None else str(value)


def _iso(value: date | datetime | None) -> str | None:
    """
    A date or datetime as ISO 8601 — `2026-03-04`, `2026-03-04T14:05:00-06:00`.

    TEACHING NOTE — `isoformat()` rather than `str()`. They agree for dates, but
    `str()` on a datetime is locale- and implementation-dependent ("March 4,
    2026 2:05 PM" on one machine, something else on the next), and the API is a
    contract with a program on the other end. ISO 8601 sorts lexically, parses
    in every language, and cannot change shape under a locale setting. The
    offset is in there because `USE_TZ = True`: everything is stored UTC and
    rendered in America/Chicago, so the client can convert to whatever its user
    actually sees.
    """
    return None if value is None else value.isoformat()


def company_json(obj) -> dict:
    """
    One Company.

    TEACHING NOTE — `address` is the model's only address column: a single
    free-text `TextField` holding whatever the Rep typed. There is no city,
    state or zip column on this model to serialize, and inventing three in a
    serializer would mean asking every client to handle keys that are always
    null while the real address text sat whole in one field beside them. What
    is stored is what goes out.

    `lifecycle` is the raw value ("prospect"), not the label ("Prospect"), for
    the same reason `Deal.stage` is: a machine consuming this has no use for a
    translated string, and the label is one `get_lifecycle_display()` away for
    anybody who does.
    """
    return {
        "id": obj.pk,
        "name": obj.name,
        "domain": obj.domain,
        "industry": obj.industry,
        "lifecycle": obj.lifecycle,
        "address": obj.address,
        "phone": obj.phone,
        "website": obj.website,
        "created_at": _iso(obj.created_at),
    }


def contact_json(obj) -> dict:
    """
    One Contact, carrying its company's id AND its company's name.

    TEACHING NOTE — why both. The id is the stable key and the only thing a
    client can join on; the name is what a human reads in a list, and sending
    only the id would mean every consumer writing a second request to put a
    name next to it. They are two columns of the same join, and `company_name`
    is free because the list query already has to `select_related("company")` —
    which is exactly what `ContactListView.base_queryset` does on the screen.
    Read it that way rather than one extra query per row.

    `email` is nullable by design (blank contacts are stored as NULL so the
    unique constraint permits many of them — see the note on Contact.email),
    so null here is the normal shape of a contact with no address, not an error.
    """
    return {
        "id": obj.pk,
        "company": obj.company_id,
        "company_name": obj.company.name if obj.company_id else None,
        "first_name": obj.first_name,
        "last_name": obj.last_name,
        "email": obj.email,
        "phone": obj.phone,
        "title": obj.title,
        "created_at": _iso(obj.created_at),
    }


def deal_json(obj) -> dict:
    """
    One Deal. The richest of the three, because it is the record with the money.

    TEACHING NOTE — `name` and `title` are the same string under two keys, and
    that is a deliberate wart rather than an accident. The column is `Deal.name`
    and every template, CSV export and `__str__` in the app calls it that;
    a client written against the published API contract reads `title`. Emitting
    both costs one line and means neither audience has to be told they were
    wrong. If the API contract ever changes, this is the line to delete.

    `probability` is the RAW stored value, so it is null on a deal whose odds
    are the stage default. It is not `Deal.effective_probability`, which
    resolves that null through `STAGE_PROBABILITY` and forces Won to 100 / Lost
    to 0 — the real number, but a different field, and shipping it under this
    name would lose the distinction between "somebody set 50%" and "the stage
    happens to be Proposal". `weighted_value` below has already applied the
    effective probability, which is what a client adding up a forecast wants.

    `contact` is `primary_contact_id` — the model's field is `primary_contact`,
    and the API says `contact` because on the wire it is just the id of the
    one contact this deal hangs off.

    `owner` is a USERNAME or null, never `owner_id`. The raw integer is banned
    here on purpose: a user id is a number that means nothing outside this
    database and invites clients to hardcode it, whereas a username is the
    thing a Rep says out loud on the phone ("that's Dana's deal") and matches
    what `DealListView.sort_fields["owner"]` already sorts by.
    """
    return {
        "id": obj.pk,
        "name": obj.name,
        "title": obj.name,
        "company": obj.company_id,
        "company_name": obj.company.name,
        "contact": obj.primary_contact_id,
        "stage": str(obj.stage),
        "value": _money(obj.value),
        "probability": obj.probability,
        "weighted_value": _money(obj.weighted_value),
        "owner": obj.owner.username if obj.owner_id else None,
        "expected_close": _iso(obj.expected_close),
        "created_at": _iso(obj.created_at),
    }