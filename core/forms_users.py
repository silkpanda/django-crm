"""
Forms for the Settings -> Users pages (Admin only).

TEACHING NOTE: a role in this app is membership in exactly one Group (Admin, Rep
or Viewer), so the form exposes it as a single dropdown and `save()` makes the
user's groups match it. `groups.set([...])` replaces the whole list, which is the
point: you can't end up as Rep AND Admin because an old group was left behind.

The lockout guard is one rule, not two. The person editing is always an active
Admin (the view 403s everyone else), so refusing to let them demote or deactivate
THEMSELVES is enough to guarantee at least one active Admin always exists. A
separate "last admin" count would be dead code.
"""

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group

from .permissions import ADMIN, REP, ROLES, VIEWER

User = get_user_model()


def stored_role(user):
    """The role a user's groups say they have, even if they're deactivated.

    `role_of()` returns None for inactive users (they have no powers), which is
    right for permission checks and wrong for an edit form that has to show what
    the role WOULD be on reactivation.
    """
    names = set(user.groups.values_list("name", flat=True))
    if ADMIN in names:
        return ADMIN
    if REP in names:
        return REP
    return VIEWER


class UserForm(forms.ModelForm):
    """
    Create or edit a user account, role included — the Admin-only Users screen.

    Creating asks for a password (twice, run through Django's validators against
    the new user's own name and email). Editing never shows the password fields;
    that is the Password page's job. The role is stored as group membership and
    is locked for superusers, who are Admin whatever their groups say. An Admin
    editing themselves cannot deactivate or demote their own account; another
    Admin has to do that.
    """
    role = forms.ChoiceField(choices=[(r, r) for r in ROLES], initial=VIEWER)
    password1 = forms.CharField(
        label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="At least 12 characters. Give it to them in person; there is no invite email yet.",
    )
    password2 = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active"]

    def __init__(self, *args, acting_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.acting_user = acting_user
        self.editing = bool(self.instance.pk)
        if self.editing:
            # Editing never touches the password; they change it on the
            # Password link, or reset flow.
            del self.fields["password1"], self.fields["password2"]
            self.fields["role"].initial = stored_role(self.instance)
            if self.instance.is_superuser:
                # role_of() makes every superuser an Admin whatever their groups
                # say, so a dropdown here would be a control that does nothing.
                self.fields["role"].disabled = True
                self.fields["role"].help_text = "Superuser: always Admin."
        else:
            self.fields["is_active"].initial = True
        self.fields["email"].required = True
        self.fields["role"].widget.attrs.update(autofocus=False)

    def clean_username(self):
        username = self.cleaned_data["username"]
        clash = User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("That username is taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        if not self.editing:
            p1, p2 = cleaned.get("password1"), cleaned.get("password2")
            if p1 and p2 and p1 != p2:
                self.add_error("password2", "The two passwords don't match.")
            elif p1:
                candidate = User(username=cleaned.get("username", ""), email=cleaned.get("email", ""),
                                 first_name=cleaned.get("first_name", ""), last_name=cleaned.get("last_name", ""))
                try:
                    password_validation.validate_password(p1, candidate)
                except forms.ValidationError as exc:
                    self.add_error("password1", exc)
        elif self.acting_user is not None and self.instance.pk == self.acting_user.pk:
            if not cleaned.get("is_active", True):
                self.add_error("is_active", "You can't deactivate your own account.")
            if not self.fields["role"].disabled and cleaned.get("role") != ADMIN:
                self.add_error("role", "You can't demote yourself. Ask another Admin.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        if not self.editing:
            user.set_password(self.cleaned_data["password1"])
        user.save()
        if not self.fields["role"].disabled:
            user.groups.set(Group.objects.filter(name=self.cleaned_data["role"]))
        return user
