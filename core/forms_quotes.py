"""
The quote editor's formset — every line of a quote, all on one form.

TEACHING NOTE — why a separate forms module rather than two more forms in
`core/forms.py`. The single-record forms in this app are one form per record;
a quote is a parent and an unknown-at-write-time number of children, which is a
formset and not a field. And `core/forms_quotes.py` matching `core/forms_leads.py`
means "the forms for that feature", which is the convention this repo already set
when the lead intake landed.

The editor is a plain formset on purpose. There is no HTMX row-adding in this
workstream: `extra=3` means three empty rows are always on the page, a fourth
line means scrolling past three rows of "Description ______ / Qty __ / Price __",
and the whole quote — notes, valid-until, and every line at once — saves as one
atomic submission. Nothing half-saves, so there is no "I typed three rows and lost
the fourth". The markup already carries `empty_form`, so adding a button that
clones the row template later is a small, isolated job; see FOLLOW-UP below.

FOLLOW-UP (not in this workstream): an "Add line" button that POSTs to an
endpoint returning one rendered `{{ formset.empty_form }}` row. The data model
does not need to change for it, which is the point of building it this way. The
blank row template is already in quote_form.html inside a <template> element, so
the markup it needs is being written now even though the button is not.
"""

from django import forms
from django.forms import inlineformset_factory

from .models import Quote, QuoteLine

# How many blank rows the editor always offers. Three is the smallest number that
# covers the shape of the quotes this business writes — equipment, labour,
# delivery — so nobody has to save a draft to get a fourth row.
EXTRA_LINES = 3


class QuoteForm(forms.ModelForm):
    """
    The quote's OWN fields: when it stops being good, and any note on it.

    TEACHING NOTE — a quote is a parent plus a formset of children, and this is the
    parent form. It is three lines long and it exists because `valid_until` and
    `notes` have to be editable by somebody, and "only the superuser can date a
    quote" is not a feature anybody asked for. What is NOT on it matters more:

      - `status` is absent. A quote's status moves through `core.quotes.set_status`,
        which knows which moves are legal and which are a lie. A dropdown here
        would be the same rule written a second time, as a list, with no refusals.
      - `deal` is absent. The quote belongs to the deal in the URL. A field for it
        would be a field a hand-made POST could point somewhere else, and the
        OneToOneField would then refuse the save with a database error instead of
        the form explaining it.
    """

    class Meta:
        model = Quote
        fields = ["valid_until", "notes"]
        widgets = {
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class QuoteLineForm(forms.ModelForm):
    """
    One line. `position` is NOT here, and that is the point of the omission.

    TEACHING NOTE — the row order is entered, not typed. There is no "position"
    input on the line editor because a Rep should never have to decide what
    number goes in it, and the view writes it from the formset index on every
    save. A `position` field on this form would be a field whose value is
    meaningful, has a default, and is silently overwritten — the exact shape of
    the fields forms.py's docstring says to keep off a form.

    `quantity` and `unit_price` render as `NumberInput` with a `step` of 0.01 so
    the browser offers the right keypad on a phone and stops anyone typing "three"
    where a decimal belongs. Both are still validated server-side; the widget is
    an affordance, not the rule.
    """

    class Meta:
        model = QuoteLine
        # An allowlist, like every other form in this app: `quote` is set by the
        # view from the URL, never by the browser, so a POST carrying
        # `quote=<some other deal's quote>` has nowhere to land.
        fields = ["description", "quantity", "unit_price"]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Three laptops"}),
            "quantity": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "unit_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }


# The factory, built once at import time. `inlineformset_factory` rather than a
# bare `formset_factory` for one reason that matters: it knows the child rows
# BELONG to one parent, so it builds its queryset from the Quote instance passed
# to it and writes the `quote` foreign key itself. A bare formset would hand every
# line in the database to every editor, and getting "only this quote's lines"
# right would become something the view has to remember on every save.
#
# `can_delete=True` puts a checkbox on every row, and it is on by default rather
# than behind a confirm: deleting a line is the most common correction anybody
# makes to a quote, and making it a two-step action to save one click a week is a
# bad trade. `can_order` is deliberately OFF — this app does not drag-reorder line
# items, and the order it does have is the order they were typed (see the
# `position` note in models.py).
QuoteLineFormSet = inlineformset_factory(
    Quote,
    QuoteLine,
    form=QuoteLineForm,
    extra=EXTRA_LINES,
    can_delete=True,
)


def live_line_forms(formset):
    """
    The rows that will exist after this save, in the order they were typed.

    TEACHING NOTE — this function exists because of two documented Django
    behaviours that together make `formset.save(commit=False)` the wrong tool for
    renumbering a quote, and both of them fail SILENTLY:

      1. `save(commit=False)` skips the DELETE rows. `BaseModelFormSet
         .delete_existing` only deletes when `commit` is true, so with
         `commit=False` a ticked checkbox quietly does nothing at all — no error,
         no exception, the line just stays.
      2. `saved_forms` contains only the forms `has_changed()`. Drag a row to a
         new place and change nothing about its contents and there is nothing in
         that list, so a formset whose whole job is POSITION has nothing to number
         — and the order you just asked for is not written.

    Both fall out of reading Django's source, and neither is documented on the
    method. So the rows are selected here instead, using the partition Django
    itself already computed: `initial_forms` are the lines that already exist and
    every one of them survives unless it is ticked, and `extra_forms` are the
    blank rows on the page, of which only the ones somebody actually filled in
    become lines. Iterating `initial_forms` before `extra_forms` is also the
    genuine form order, because a formset's existing rows always come first.
    """
    deleted = formset.deleted_forms
    for form in formset.initial_forms:
        if form not in deleted:
            yield form
    for form in formset.extra_forms:
        if form.has_changed() and form not in deleted:
            yield form