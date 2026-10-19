"""
Forms — the layer between what a user types and what gets saved.

TEACHING NOTE: a Django `ModelForm` reads a model and builds an HTML form from
it automatically: fields, widgets, and validation all derived from the model
definition. You get "required field" checks, type coercion, and error messages
almost for free. Your job is mostly to say WHICH fields to expose and to add any
rules that span multiple fields or depend on WHO is filling the form out.

The interesting idea in this file is the OwnerFieldMixin, which changes the form
depending on the current user's role.
"""

from django import forms
from django.utils import timezone
from django.contrib.auth import get_user_model

from .models import Activity, Company, Contact, Deal, Stage, Task
from .permissions import is_admin

# Always grab the User model this way, never `from django.contrib.auth.models
# import User`. Projects can swap the user model out, and this respects that.
User = get_user_model()

# Reusable widgets. A "widget" is how a field is rendered in HTML. `type="date"`
# and `type="datetime-local"` make the browser show its native date pickers
# instead of a plain text box — free UI with one attribute.
DATE = forms.DateInput(attrs={"type": "date"})
DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class OwnerFieldMixin:
    """
    TEACHING NOTE — a MIXIN. This isn't a form on its own; it's a bundle of
    behaviour you "mix in" to several forms via multiple inheritance
    (`class CompanyForm(OwnerFieldMixin, forms.ModelForm)`). It solves one
    problem in one place for every record form: the "owner" field.

    The rule: only an Admin may choose who owns a record. A Rep shouldn't even
    SEE the owner dropdown — whatever they create, they own. So:
      - If the user is an Admin, populate the owner dropdown with active users.
      - Otherwise, delete the field entirely and, on save, set owner = the user.

    Deleting the field (not just hiding it) is the secure move: a hidden field
    can still be POSTed by hand. If the field doesn't exist on the form, the
    server ignores any "owner" a Rep tries to smuggle in. That exact attack is
    one of the Phase 1 pen-test checks (owner-field privilege escalation).
    """

    def __init__(self, *args, user=None, **kwargs):
        # `__init__` runs when the form is built. We accept the current `user`
        # (the view passes it in) so the form can adapt to their role.
        super().__init__(*args, **kwargs)
        self.user = user
        if "owner" in self.fields:
            if user is not None and is_admin(user):
                self.fields["owner"].queryset = User.objects.filter(is_active=True).order_by("username")
            else:
                del self.fields["owner"]  # Reps/Viewers never get an owner picker

    def save(self, commit=True):
        # `commit=False` builds the object in memory without hitting the database
        # yet, so we can fill in fields the user wasn't allowed to set.
        obj = super().save(commit=False)
        # If there's no owner picker on this form (i.e. a Rep), stamp the current
        # user as the owner of what they just created.
        if obj.owner_id is None and self.user is not None and "owner" not in self.fields:
            obj.owner = self.user
        if commit:
            obj.save()
            self.save_m2m()  # required after commit=False to save many-to-many (tags)
        return obj


class CompanyForm(OwnerFieldMixin, forms.ModelForm):
    class Meta:
        # `fields` is an allowlist. Listing fields explicitly (never `"__all__"`)
        # means a new sensitive field added to the model later won't silently
        # appear on the form for users to edit. Order here is the display order.
        model = Company
        fields = [
            "name", "domain", "website", "phone", "industry", "lifecycle",
            "source", "tags", "owner", "address", "notes",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3}), "notes": forms.Textarea(attrs={"rows": 4})}


class ContactForm(OwnerFieldMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = [
            "first_name", "last_name", "email", "phone", "title", "company",
            "lifecycle", "source", "tags", "roles", "owner", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            # A many-to-many defaults to a `<select multiple>`, which is the wrong
            # control here for two reasons: a contact genuinely holds several
            # roles at once ("Champion" AND "Technical Evaluator"), and the seven
            # roles are a short fixed list a person should be able to read and
            # tick. The checkbox order comes from BuyingRole.Meta.ordering, so
            # this list is in the order the sale walks them, for free.
            "roles": forms.CheckboxSelectMultiple,
        }


class DealForm(OwnerFieldMixin, forms.ModelForm):
    """
    Create or edit a deal. On top of the model's own rules it refuses a primary
    contact from a different company and a Lost deal with no reason given.
    """
    class Meta:
        model = Deal
        fields = [
            "name", "company", "primary_contact", "stage", "value", "probability",
            "expected_close", "owner", "lost_reason",
        ]
        widgets = {"expected_close": DATE, "lost_reason": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        # TEACHING NOTE: `clean()` on a form validates across fields. `add_error`
        # attaches a message to a specific field so it shows up next to that input.
        # These are UX-facing rules; the model's own clean() is the last line of
        # defence if something reaches it another way.
        data = super().clean()
        contact, company = data.get("primary_contact"), data.get("company")
        # Don't let a deal's primary contact belong to a different company —
        # that's almost always a mistake.
        if contact and company and contact.company_id and contact.company_id != company.pk:
            self.add_error("primary_contact", "That contact belongs to a different company.")
        if data.get("stage") == Stage.LOST and not (data.get("lost_reason") or "").strip():
            self.add_error("lost_reason", "Give a reason when marking a deal Lost.")
        return data


class ActivityForm(forms.ModelForm):
    # We redeclare occurred_at as optional so a user can log a note without
    # fussing over the timestamp; clean_occurred_at fills in "now" if they leave
    # it blank. (The model field has a default, but an empty form value would
    # otherwise fail the "required" check before the default ever applies.)
    occurred_at = forms.DateTimeField(required=False, widget=DATETIME, label="When")

    class Meta:
        model = Activity
        # Note: no company/contact/deal here. The VIEW sets which record this
        # activity belongs to, from the URL — never the browser. That keeps a
        # user from POSTing an activity onto a record they shouldn't touch.
        fields = ["kind", "body", "occurred_at"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "What happened?"})}

    def clean_occurred_at(self):
        # A `clean_<fieldname>` method validates/normalises just that one field.
        # Empty → default to right now.
        return self.cleaned_data.get("occurred_at") or timezone.now()


class TaskForm(forms.ModelForm):
    """
    Same rule as OwnerFieldMixin, applied to a different field: only Admins get
    an assignee dropdown, and a Rep's tasks are assigned to themselves.

    TEACHING NOTE: this used to inherit OwnerFieldMixin, and that crashed every
    save (REVIEW-10). The mixin's save() reads `obj.owner_id`, and a Task has no
    `owner`, only `assignee`. A mixin is a contract about field names, not
    just behaviour: reuse it on a model whose fields don't match and it fails
    at runtime, not when Python loads the class. So the task version of the
    rule lives here, written against the field Task actually has.
    """

    class Meta:
        model = Task
        fields = ["title", "due_at", "assignee", "priority", "company", "contact", "deal"]
        widgets = {
            "due_at": DATETIME,
            "title": forms.TextInput(attrs={"placeholder": "What needs doing?"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if user is not None and is_admin(user):
            self.fields["assignee"].queryset = User.objects.filter(is_active=True).order_by("username")
        else:
            # Deleted, not hidden: a hand-POSTed assignee is ignored because the
            # form no longer has a field to read it into (probe 2.5 checks this).
            del self.fields["assignee"]

    def clean(self):
        # QA-08: a task can hang off a company, a contact AND a deal, and until
        # now nothing checked they agreed with each other. Point a task at
        # "Unrelated Holdings" and at a deal belonging to "Acme Supply" and it
        # saves, because every field is individually valid. DealForm.clean
        # already refuses the same mismatch on a deal's primary contact; this is
        # that rule again, written for the fields a Task actually has.
        #
        # TEACHING NOTE: `clean()` sees `cleaned_data`, so the objects are real
        # model instances with `.company_id` available — no extra query to find
        # out whose deal it is. `add_error` pins the message to the field the
        # user has to change, which is why the errors land on `deal`/`contact`
        # rather than somewhere vague.
        data = super().clean()
        company, contact, deal = data.get("company"), data.get("contact"), data.get("deal")

        # Fill the company in from the link when the user only picked a deal or a
        # contact. Every task is about SOME account, and leaving it blank was how
        # a task ended up invisible on the company page it belonged to. Deal
        # first: it's the narrower of the two links.
        if not company:
            if deal is not None:
                company = data["company"] = deal.company
            elif contact is not None and contact.company_id:
                company = data["company"] = contact.company

        # The checks run AFTER the fill, against the company the task will
        # actually be saved with. Checked before it, a deal-only task with a
        # contact from a third company slipped through: there was no company yet
        # to disagree with.
        if deal and company and deal.company_id != company.pk:
            self.add_error("deal", "That deal belongs to a different company.")
        # A contact with no company of its own can't contradict anything.
        if contact and company and contact.company_id and contact.company_id != company.pk:
            self.add_error("contact", "That contact belongs to a different company.")
        return data

    def save(self, commit=True):
        task = super().save(commit=False)
        # A Rep's task is always theirs. An Admin who leaves the dropdown blank
        # gets it too, since an unassigned task shows up on nobody's My Day.
        if self.user is not None and ("assignee" not in self.fields or task.assignee_id is None):
            task.assignee = self.user
        if commit:
            task.save()
        return task
