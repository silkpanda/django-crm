"""
Turns raw django-simple-history snapshots into a readable "what changed" list.

TEACHING NOTE: django-simple-history stores a full copy of a row every time it's
saved. That's great for auditing but ugly to read raw — it's just rows of values.
This module diffs each snapshot against the one before it and produces a friendly
list like "Stage: Lead -> Proposal, Value: 2400 -> 3100" with the labels and
related-object names filled in. The detail page's History tab renders this.
"""

from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist

# Auto-updated bookkeeping fields would be noise in a human-facing change log,
# so we hide them.
HIDDEN_FIELDS = {"updated_at", "created_at", "stage_changed_at"}


def _display(model, field_name, value):
    """Render one stored value the way a person expects to see it."""
    if value in (None, ""):
        return "—"
    try:
        field = model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return str(value)
    # Many-to-many (tags) are stored as a list of link rows; resolve them to names.
    if field.many_to_many:
        ids = [row.get(field.m2m_reverse_field_name()) for row in value] if value else []
        names = field.related_model.objects.filter(pk__in=ids).values_list("name", flat=True)
        return ", ".join(sorted(names)) or "—"
    # A choices field stores "lead" but should show "Lead".
    if field.choices:
        return dict(field.flatchoices).get(value, value)
    # A foreign key stores an id; show the linked object's name instead.
    if field.is_relation:
        try:
            return str(field.related_model.objects.get(pk=value))
        except ObjectDoesNotExist:
            return f"(deleted #{value})"
    return str(value)


def _label(model, field_name):
    # Turn "expected_close" into the model's own "Expected close" label.
    try:
        return model._meta.get_field(field_name).verbose_name.capitalize()
    except FieldDoesNotExist:
        return field_name


def history_entries(obj, limit=50):
    """
    The "History" list for a record page: newest first, at most `limit` entries.

    Each entry is {"when", "who", "type", "changes"}, where `changes` is a list
    of {"field", "old", "new"} with labels and values already made readable. A
    save that changed only hidden bookkeeping fields is left out entirely.
    """
    # Pull limit+1 snapshots so the oldest one in our window still has a previous
    # snapshot to diff against.
    model = type(obj)
    records = list(obj.history.select_related("history_user")[: limit + 1])
    entries = []
    for i, rec in enumerate(records[:limit]):
        prev = records[i + 1] if i + 1 < len(records) else None
        changes = []
        if prev is not None:
            # `diff_against` (a simple-history helper) returns the fields that
            # differ between two snapshots. We format each into old -> new.
            for change in rec.diff_against(prev).changes:
                if change.field in HIDDEN_FIELDS:
                    continue
                changes.append({
                    "field": _label(model, change.field),
                    "old": _display(model, change.field, change.old),
                    "new": _display(model, change.field, change.new),
                })
            if not changes:
                continue  # nothing user-visible changed in this save; skip it
        entries.append({
            "when": rec.history_date,
            "who": rec.history_user,
            "type": rec.get_history_type_display(),  # Created / Changed / Deleted
            "changes": changes,
        })
    return entries
