"""
The two lead forms and the one that assigns a lead.

TEACHING NOTE — these are PLAIN forms (`forms.Form`), not ModelForms. The rest of
the app builds its forms from a model, because a form there saves that model.
These don't: `LeadForm` saves a Company, a Contact and a Deal at once, and which
fields it takes is a product decision rather than a reflection of a schema. A
ModelForm over Deal here would have quietly put `stage`, `value` and `owner` in
the markup — three fields a visitor must never be able to set, and three the
Phase 5 probe (5.1) posts straight at this form to see if they are ignored.

`strip` on every field is a real bug class rather than politeness: without it,
"  " is a non-empty `company_name`, so "Lakeside Dental " misses the case-
insensitive match on "Lakeside Dental" and creates a duplicate company that
looks identical in the list. Stripping first means the match sees what the
person meant.

The honeypot is a field a human never sees and never fills in. See
`PublicLeadForm` for why it is hidden by CSS rather than `display: none`.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.urls import reverse

from .leads import staff_teams

User = get_user_model()


def company_suggest_attrs():
    """
    The widget attrs that wire a Company box to `lead_company_suggestions`.

    TEACHING NOTE — a FUNCTION, not a dict built at module level, because one of
    the six values is a `reverse()`. The URLConf imports this module (urls.py
    imports views_leads, which imports this), so calling `reverse()` while this
    file is still being imported asks for a route list that does not exist yet.
    Called from `__init__`, the URLConf is long since loaded.

    It is a function for the second reason too: `PublicLeadForm` has to REMOVE
    exactly these six keys, and the two lists must be the same six. One source
    beats two hand-maintained sets of strings that drift apart on the first edit
    and leave a public form quietly wired to the customer list.

    `hx-trigger="input changed delay:250ms"` is the debounce. Without the delay
    every keystroke is a request, so typing "Lakeside" is eight round trips and
    the suggestion list flickers through every prefix on the way; `input` and
    `changed` together are what make it fire for typing and for a browser
    autofill/paste, which fires only `input`.
    """
    return {
        "list": "company-options",
        # The browser's OWN autofill would race the datalist and offer the last
        # company somebody else typed. Off says "this box knows its own options".
        "autocomplete": "off",
        "hx-get": reverse("lead_company_suggestions"),
        "hx-trigger": "input changed delay:250ms",
        "hx-target": "#company-options",
        # innerHTML, not outerHTML: it replaces the datalist's children and
        # leaves the <datalist> tag itself standing, which is why the partial is
        # bare <option> lines and nothing else.
        "hx-swap": "innerHTML",
    }


def staff_team_choices():
    """
    `(value, label)` pairs for the desk form's team dropdown.

    TEACHING NOTE — a CALLABLE, not a list built at import time or in
    `__init__`. Django only calls it when somebody actually reads `field.choices`,
    which happens when the widget renders or when a submission is validated.
    That buys two things:

      - Importing this module touches no database, so `makemigrations`, a
        management command and the test runner can all import it against a
        database that has not been migrated yet.
      - `PublicLeadForm` deletes its `team` field, so this query never runs for
        the public form at all. Building the list eagerly would mean the page
        strangers load ran a query whose only result it threw away.
    """
    return [(source.name, source.name) for source in staff_teams().order_by("name")]


class LeadForm(forms.Form):
    """
    A lead taken by a member of staff, who names the team it came in on.

    The length limits are the MODELS' limits (Company.name is 200,
    Contact.first_name 100, Contact.last_name 100, Contact.phone 40), restated
    here so a person who pasted a novel into the message box is told at the form
    rather than by a database error. `required=False` on the optional three is
    the point of them: last name, email and phone are all things a caller
    frequently cannot supply.
    """

    company_name = forms.CharField(max_length=200, label="Company")
    first_name = forms.CharField(max_length=100, label="First name")
    last_name = forms.CharField(max_length=100, required=False, label="Last name")
    email = forms.EmailField(required=False, label="Email")
    phone = forms.CharField(max_length=40, required=False, label="Phone")
    message = forms.CharField(
        max_length=2000,
        label="What do they need?",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    team = forms.ChoiceField(label="Team", choices=staff_team_choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set on the FIELD, not in a clean_<name>() method: one line covers every
        # field including any added later, and there is no per-field method to
        # forget. Without it, "  " is a non-empty company_name, "  Lakeside
        # Dental  " misses the case-insensitive match on "Lakeside Dental", and
        # the lead creates a duplicate company that looks identical in the list.
        for field in self.fields.values():
            field.strip = True

        # The Company box gets a `<datalist>` of names that already exist, filled
        # by HTMX from `lead_company_suggestions`. Set on the WIDGET rather than
        # in the template, because that is where every other attribute of this
        # field lives and because the desk form's markup then knows nothing about
        # the feature — lead_form.html only has to supply the empty `<datalist>`
        # the endpoint's options land in.
        #
        # TEACHING NOTE — the win is the EXACT match. `leads.create_lead` finds
        # the company with `name__iexact`, so a suggestion a tech clicks hands
        # back the stored spelling byte for byte, while typing the same name by
        # hand can miss on a trailing space or a swapped letter and mint a second
        # company that looks identical in /companies/. The datalist is not a
        # search box bolted on for convenience; it is the one place the two paths
        # cannot disagree.
        self.fields["company_name"].widget.attrs.update(company_suggest_attrs())


class PublicLeadForm(LeadForm):
    """
    The same fields, minus the team (there is only ever one team on this form)
    plus a honeypot.

    TEACHING NOTE — THE HONEYPOT. `website` is an empty text input that is put
    far off-screen and given no label a person would read. A real browser never
    touches it and never types into it. Most naive form-filling bots, though,
    fill in every field they find, and a filled honeypot is how we know.

    It is hidden with an off-screen position rather than `display: none` or
    `visibility: hidden`, because a bot that is checking whether a field is
    hidden before filling it in will skip a field it cannot see and pass. Being
    invisible and being absent-from-the-DOM are different things, and the
    defence needs the first. `tabindex="-1"` is the accessibility half: the field
    exists in the tab order for a screen reader that reads the label, but no
    keyboard user can land in an invisible box.

    `required=False` is what makes it a trap rather than a wall — a bot that
    filled it is then redirected to the thank-you page and told nothing, while a
    human who somehow reached it (a password manager, a browser restore) sees a
    validation error like any other.
    """

    website = forms.CharField(
        required=False,
        label="Leave this empty",
        widget=forms.TextInput(
            attrs={"class": "hp", "autocomplete": "off", "tabindex": "-1"}
        ),
        help_text="This field is for spam robots and should be left blank.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No team picker on the public form, and not merely a hidden one: the
        # field is removed, so a POST naming `team=Front desk` has nowhere to
        # land. `views_leads.lead_submit` passes `leads.public_team()` in its place.
        del self.fields["team"]

        # SECURITY NOTE — and NO company suggestions on the public form. The six
        # attrs are removed rather than merely left unset, because
        # `PublicLeadForm` INHERITS `LeadForm.__init__` and so inherits the
        # wiring; deleting only what this form adds is not an option here, the
        # whole point is that it adds nothing and inherits everything else.
        #
        # Removing them is what stops an anonymous visitor's keystrokes being sent
        # to /leads/companies/, and the reason to remove them rather than rely on
        # the endpoint refusing is that a wire to a 302 is still a wire: the
        # stranger's browser would learn the URL exists, learn that it is
        # login-gated, and get a login redirect on every character typed. This
        # form is for people with no account, so there is nothing to log in with.
        #
        # It also removes the only way a public page could be turned into an
        # oracle for the customer list. The endpoint's own `@login_required` is
        # the door and stays where it is, but a 200-for-signed-in-users endpoint
        # that the ANONYMOUS form points at would mean the next thing somebody
        # does is delete that decorator to "fix" the form, and then the whole
        # book of company names answers anyone who asks. Two independent refusals
        # beat one that a well-meaning edit can remove.
        for attr in company_suggest_attrs():
            self.fields["company_name"].widget.attrs.pop(attr, None)


class LeadAssignForm(forms.Form):
    """
    Who should own this new lead?

    TEACHING NOTE — the queryset is the permission, not a filter the view
    applies afterwards. An inactive user cannot sign in, so a lead assigned to
    one is a lead nobody ever works; a Viewer may not edit records they do not
    own, so handing them one makes it unworkable too. Leaving either out of the
    dropdown makes the POST carrying their id a validation error, which is the
    server saying no rather than the browser being politely ignored.

    `distinct()` is needed because `groups__name__in` is a JOIN, and a user in
    two role groups would otherwise appear twice in the dropdown.
    """

    owner = forms.ModelChoiceField(
        label="Assign to",
        queryset=User.objects.none(),
        empty_label=None,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = (
            User.objects.filter(is_active=True, groups__name__in=["Admin", "Rep"])
            .distinct()
            .order_by("username")
        )