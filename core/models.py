"""
The data model — the heart of the whole app.

TEACHING NOTE: In Django, a "model" is a Python class that maps to one database
table. Each attribute you declare (a `models.CharField`, a `models.ForeignKey`,
etc.) becomes one column. You almost never write SQL by hand: you describe the
shape here, run `makemigrations` (Django writes a migration file describing the
change) and `migrate` (Django applies it to the database). This is called an ORM
— Object-Relational Mapper. You think in Python objects; Django talks SQL for you.

Read this file top to bottom and you'll understand the entire CRM: what a
Company is, what a Deal is, and the rules that keep the data honest.
"""

from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from simple_history.models import HistoricalRecords


# TEACHING NOTE: `TextChoices` is Django's enum. It gives us a fixed menu of
# valid values plus a human-readable label for each. The stored value is the
# short string on the left ("lead"); the label on the right ("Lead") is what a
# user sees in a dropdown. Defining it once here means the choices can't drift
# out of sync between the database, the forms, and the templates.
class Lifecycle(models.TextChoices):
    LEAD = "lead", "Lead"
    PROSPECT = "prospect", "Prospect"
    CUSTOMER = "customer", "Customer"
    CHURNED = "churned", "Churned"


class Stage(models.TextChoices):
    # The sales pipeline, in order. A deal walks down this list from LEAD to a
    # final resting place of WON or LOST.
    LEAD = "lead", "Lead"
    QUALIFIED = "qualified", "Qualified"
    PROPOSAL = "proposal", "Proposal"
    NEGOTIATION = "negotiation", "Negotiation"
    WON = "won", "Won"
    LOST = "lost", "Lost"


# A deal is either still in play (open) or finished (closed). Keeping these two
# lists next to the enum means the rest of the code asks "is this stage open?"
# instead of hardcoding the same four strings in a dozen places.
OPEN_STAGES = [Stage.LEAD, Stage.QUALIFIED, Stage.PROPOSAL, Stage.NEGOTIATION]
CLOSED_STAGES = [Stage.WON, Stage.LOST]

# Default odds of winning at each stage, used to compute a "weighted" pipeline
# value (a $10k deal at 25% counts as $2.5k of likely revenue). A specific deal
# can override its own probability; this is just the sensible starting guess.
STAGE_PROBABILITY = {
    Stage.LEAD: 10,
    Stage.QUALIFIED: 25,
    Stage.PROPOSAL: 50,
    Stage.NEGOTIATION: 75,
    Stage.WON: 100,
    Stage.LOST: 0,
}


class Tag(models.Model):
    # A tiny lookup table. `unique=True` tells the database to reject a second
    # tag with the same name — the rule is enforced in the data, not just in code.
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        # `Meta` is where per-model settings live. `ordering` sets the default
        # sort order for every query unless something asks for a different one.
        ordering = ["name"]

    def __str__(self):
        # `__str__` decides how one row prints itself — in the admin, in
        # dropdowns, anywhere Django needs to show the object as text. Without
        # it you'd see the useless "Tag object (7)".
        return self.name


class LeadSource(models.Model):
    """Where a lead came from — "Referral", "Website", etc. Another lookup table."""

    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BuyingRole(models.Model):
    """
    A contact's part in THIS sale at their own company: Champion, Decision Maker,
    Economic Buyer, Influencer, Technical Evaluator, End User, Blocker.

    TEACHING NOTE — why this is a lookup table and not a Tag, because that is
    the whole design argument:

      - Tags describe the ACCOUNT. "Competitor", "Renewal", "installed-base" are
        true of the company and true of every contact there. They are labels.
      - A buying role describes a PERSON'S PART IN THIS DEAL. Ray is the Champion
        at Acme, which has nothing to do with whether Ray is a Champion anywhere
        else, and nothing to do with the account being called "competitor".
      - And it has to be a FIXED list. Tags are free text, which means "champion",
        "Champion " and "champ" are three different tags, and a contact who is
        the Champion is not findable by anyone who typed the other two. A
        dropdown of seven known answers cannot drift, can be translated, can be
        filtered on, and can be drawn in the order a sale actually walks them.

    The `order` field is that walk: Champion sells it, Decision Maker signs it,
    Economic Buyer pays for it, Influencer has an opinion, Technical Evaluator
    proves it works, End User lives with it, Blocker is dealt with. It is a column
    rather than the creation order so an Admin can insert a role or renumber the
    whole ladder without rewriting rows.
    """

    # `unique=True` puts the "one Champion, one row" rule in the database, where
    # a form can't be bypassed. See Tag for the same move on a free-text table.
    name = models.CharField(max_length=80, unique=True)
    # Nullable on purpose: a role added by hand in the admin without a position
    # still saves, and it sorts to the end rather than the front of the ladder.
    order = models.PositiveSmallIntegerField(
        default=100, help_text="Position in the sale; lower comes first. New roles default to 100, after the seven."
    )

    class Meta:
        # ONE declaration, three consumers: the form's checkbox list, the filter
        # dropdown and the CSV cell all read this instead of each sorting itself.
        ordering = ["order", "name"]
        # Note there is deliberately no `F("order").asc(nulls_last=True)` here.
        # That would fix where an unordered role lands, but SQL databases don't
        # agree on where NULLs go (SQLite sorts them first ascending, Postgres
        # last), and every consumer would then be sorting around the model's own
        # ordering. All seven seeded roles carry a real order, so the ladder is
        # total and the NULL rule never comes up. If someone later needs it, it
        # belongs in one place — here — not in three templates.

    def __str__(self):
        return self.name

    @property
    def slug(self):
        """
        "Decision Maker" -> "decision-maker", for the per-role CSS hook.

        TEACHING NOTE: without this, a template that wanted to style the Champion
        differently from the Blocker would have to write the role NAME into the
        template, and a rename would silently drop the styling. One definition,
        every template agrees: `class="pill role-pill role-{{ role.slug }}"`.
        """
        return slugify(self.name)


class OwnedRecord(models.Model):
    """
    TEACHING NOTE: an ABSTRACT base model. Company and Contact share a bunch of
    identical fields (who owns the record, where the lead came from, timestamps).
    Rather than copy-paste those onto both, we declare them once here and let
    both models inherit. `abstract = True` in Meta means "don't build a table
    for THIS class — just fold its fields into whoever inherits it." It's the
    DRY principle (Don't Repeat Yourself) applied to database columns.
    """

    # TEACHING NOTE: a ForeignKey is a link to another table — here, to the user
    # who owns this record. `settings.AUTH_USER_MODEL` is the polite way to point
    # at Django's User model (never import User directly; projects sometimes swap
    # it out). `on_delete=SET_NULL` answers "what happens to this record if the
    # owning user is deleted?" — we keep the record and just null the owner,
    # rather than deleting the company along with the employee who left.
    # `related_name="+"` tells Django NOT to build the reverse accessor
    # (user.company_set), which we don't need and which would collide across the
    # several owner fields in this app.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    lifecycle = models.CharField(max_length=20, choices=Lifecycle.choices, default=Lifecycle.LEAD)
    source = models.ForeignKey(LeadSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.TextField(blank=True)
    # `external_id` remembers the ID this record had in whatever system we
    # imported it from. It's how a re-import knows "I've seen this row before,
    # update it" instead of creating a duplicate. `db_index=True` builds a
    # database index so looking rows up by this column stays fast.
    external_id = models.CharField(max_length=120, blank=True, db_index=True)
    # `auto_now_add` stamps the time once, at creation. `auto_now` restamps it on
    # every save. Together they give you created/updated timestamps for free.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    # TEACHING NOTE on `blank` vs `null`: `null` is about the DATABASE (is the
    # column allowed to be empty?). `blank` is about FORMS/VALIDATION (is the
    # user allowed to leave the field empty?). For text they usually differ on
    # purpose — see the email field below for the one place it matters a lot.


def domain_from_website(website: str) -> str:
    """
    Read a company domain off its website URL, or "" if there isn't a usable one.

    TEACHING NOTE: people paste "www.widgets.example/about" about as often as a
    full "https://www.widgets.example/about", and `urlsplit` reads a bare
    "host/path" as a path with no host at all. Prefixing "//" when there's no
    scheme pushes the host into `netloc`, so both spellings parse identically.

    The dot test is deliberate. "localhost" or "intranet" is not something two
    companies can collide on, and storing one would manufacture false duplicate
    warnings out of two perfectly different companies sharing an intranet name.
    Anything unparseable returns "" — we would rather store no domain than a
    wrong one, because a wrong domain is actively harmful here: it wires this
    record into every future duplicate check.

    QA-09: this is deliberately ONE function, shared with the duplicate check in
    views.py. The check runs before the record is saved, so it has to work out
    the domain the record is *about* to be stored with rather than re-reading
    whatever happens to be in the column. Two copies of this rule would drift,
    and the drift would show up as a duplicate check that silently misses.
    """
    url = (website or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "//" + url
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        # A malformed IPv6 literal or a bad port. Not a domain we can match on.
        return ""
    if "." not in host:
        return ""
    return host[4:] if host.startswith("www.") else host


class Company(OwnedRecord):
    """
    A customer or prospect organisation — or, for a residential lead, a household.

    `domain` is what duplicate detection matches on, and `save()` fills it in from
    the website when nobody typed one. Contacts and deals hang off a company.
    """
    name = models.CharField(max_length=200)
    domain = models.CharField(max_length=253, blank=True, db_index=True)  # e.g. "acme.com" — used to catch duplicates
    website = models.URLField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    industry = models.CharField(max_length=100, blank=True)
    # A ManyToMany: a company can have many tags, and a tag many companies.
    # Django manages the hidden join table between them for you.
    tags = models.ManyToManyField(Tag, blank=True, related_name="companies")
    # `HistoricalRecords` (from django-simple-history) quietly records a snapshot
    # every time a Company is saved, so the app can show "who changed what, when."
    # `m2m_fields` tells it to track tag changes too, which it wouldn't otherwise.
    history = HistoricalRecords(m2m_fields=[tags])

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "companies"  # otherwise Django says "Companys"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        # The canonical "where do I view this object?" URL. `reverse` looks up
        # the URL by its name (defined in urls.py) instead of hardcoding the
        # path, so if the URL structure changes, this keeps working.
        return reverse("company_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        # TEACHING NOTE: overriding `save()` lets us clean data right before it
        # hits the database, every single time, no matter who saved it. Here we
        # normalise the domain to lowercase and trim spaces so "Acme.COM " and
        # "acme.com" are treated as the same value by the duplicate check.
        # `super().save()` is the line that actually writes the row — forget it
        # and nothing persists.
        self.domain = self.domain.strip().lower()
        # QA-09: a company entered with only a website used to land with
        # domain="", which the duplicate check reads as "no domain" and walks
        # past — so the record was invisible to the very check that exists to
        # catch it. Derive the domain from the website, but never over one the
        # user typed themselves.
        if not self.domain:
            self.domain = domain_from_website(self.website)
        super().save(*args, **kwargs)


class Contact(OwnedRecord):
    """
    A person, usually at a company. Email is optional but unique when present, so
    blank emails are stored as NULL — see the note on the field below.
    """
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    # TEACHING NOTE: the classic "unique but optional" problem. We want at most
    # one contact per email — but plenty of contacts have no email at all. If we
    # stored blanks as empty strings, the second blank would violate uniqueness
    # ("" == ""). The fix: allow the column to be NULL (null=True), and in save()
    # below convert every blank to NULL. Databases treat each NULL as distinct,
    # so many contacts can have "no email" while real addresses stay unique.
    email = models.EmailField(null=True, blank=True, unique=True)
    phone = models.CharField(max_length=40, blank=True)
    title = models.CharField(max_length=120, blank=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="contacts")
    tags = models.ManyToManyField(Tag, blank=True, related_name="contacts")
    # A contact's part in the sale. Separate from `tags` on purpose — see the
    # BuyingRole teaching note for why "Champion" is not a tag.
    roles = models.ManyToManyField(BuyingRole, blank=True, related_name="contacts")
    # `roles` is named here or the History tab stays silent about it. simple-history
    # only snapshots a many-to-many it is told about; add a field to the model and
    # forget this line, and the edit saves, the pill appears, and nobody can ever
    # find out who added the Champion.
    history = HistoricalRecords(m2m_fields=[tags, roles])

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        # A `@property` is a method you call without parentheses (contact.full_name).
        # It's computed on the fly and not stored — great for values derived from
        # other fields.
        return f"{self.first_name} {self.last_name}".strip()

    def get_absolute_url(self):
        return reverse("contact_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        # Normalise the email and turn any blank into a true NULL (see the note
        # on the email field above for why that matters).
        self.email = self.email.strip().lower() if self.email else None
        super().save(*args, **kwargs)


class Deal(models.Model):
    """
    A single sales opportunity.

    TEACHING NOTE — the most important design decision in this file: the pipeline
    STAGE lives on the Deal, not on the Company. Why? Because one company can
    have several deals going at once — last year's contract already Won, and a
    new expansion still in Proposal. If "stage" were a Company field, the moment
    a customer came back for a second sale you'd have to overwrite the first
    one's status and lose the truth. Modelling the deal as its own thing keeps
    each opportunity's history separate. Getting this boundary right early is the
    difference between a schema that grows and one you rip out in six months.
    """

    name = models.CharField(max_length=200)
    # `on_delete=PROTECT` is deliberate and different from the others: you may
    # NOT delete a company that still has deals attached. The database refuses,
    # which stops someone from accidentally erasing the sales history behind an
    # account. The view catches that refusal and shows a friendly message.
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="deals")
    primary_contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.LEAD)
    # Who sent THIS lead. `company.source` is a fact about the ACCOUNT's history —
    # where the customer first came from — and `leads._match_or_create_company`
    # deliberately refuses to overwrite it, so for every lead on an existing
    # customer it names the wrong team. Same shape as `Company.source`; nullable
    # because leads filed before this column existed have no answer to give, and
    # the queue falls back to the company for exactly those.
    source = models.ForeignKey(LeadSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # `DecimalField`, never `FloatField`, for money. Floats can't represent
    # 0.10 exactly and will quietly lose cents over time; Decimal is exact.
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    probability = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Blank uses the stage default.")
    expected_close = models.DateField(null=True, blank=True)
    # `editable=False` keeps these out of forms and the admin — they're set by
    # the code below, not typed by a person.
    stage_changed_at = models.DateTimeField(default=timezone.now, editable=False)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    lost_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-updated_at"]  # the leading "-" means descending: newest first

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("deal_detail", args=[self.pk])

    @property
    def effective_probability(self):
        # TEACHING NOTE: a CLOSED deal's probability is not a prediction, it is
        # a fact. Won is 100% and Lost is 0% no matter what the deal's own
        # probability field says, because a manual probability answers "how
        # likely is this to close?" — a question you stop asking the moment it
        # has. Letting 25% survive into Won put a quarter of a closed-won deal
        # back into the weighted forecast, and into Lost it counted money the
        # business does not have (PIPE-3).
        if self.stage == Stage.WON:
            return 100
        if self.stage == Stage.LOST:
            return 0
        # An open stage: use the deal's own probability if set, otherwise fall
        # back to the stage default. `is not None` matters — a real 0% must not
        # be mistaken for "unset", which `if self.probability:` would do.
        return self.probability if self.probability is not None else STAGE_PROBABILITY[self.stage]

    @property
    def weighted_value(self):
        return self.value * self.effective_probability / 100

    @property
    def is_closed(self):
        return self.stage in CLOSED_STAGES

    def clean(self):
        # TEACHING NOTE: `clean()` is for validation rules that span more than one
        # field, or that are too business-specific for a plain field option. It
        # runs during form validation (via `full_clean`). Raising `ValidationError`
        # here is how you say "these values don't make sense together" and get the
        # message shown next to the right field in the form.
        if self.probability is not None and self.probability > 100:
            raise ValidationError({"probability": "Probability can't exceed 100."})
        if self.stage == Stage.LOST and not self.lost_reason.strip():
            # You can't mark a deal Lost without saying why — that reason is worth
            # money later when you look back at why deals died.
            raise ValidationError({"lost_reason": "Give a reason when marking a deal Lost."})
        if self.value is not None and self.value < 0:
            raise ValidationError({"value": "Value can't be negative."})

    def save(self, *args, **kwargs):
        # TEACHING NOTE: this override does the "side effects" of a stage change.
        # To know if the stage actually changed, we first fetch the OLD stage
        # from the database (the row as it currently exists) before we overwrite
        # it. `self.pk` is None for a brand-new deal that hasn't been saved yet,
        # so we only look up a previous value for existing rows.
        previous = None
        if self.pk:
            # `.values_list(..., flat=True).first()` pulls just the one column we
            # need instead of loading the whole row — cheaper and clearer.
            previous = Deal.objects.filter(pk=self.pk).values_list("stage", flat=True).first()

        # Only restamp "in stage since" when the stage genuinely moved. Editing
        # the deal's value or name must NOT reset this clock.
        if previous is not None and previous != self.stage:
            self.stage_changed_at = timezone.now()

        # Stamp closed_at when a deal first lands on Won/Lost, and clear it if
        # someone reopens the deal back into the active pipeline.
        if self.stage in CLOSED_STAGES:
            if previous not in CLOSED_STAGES or self.closed_at is None:
                self.closed_at = timezone.now()
        else:
            self.closed_at = None

        super().save(*args, **kwargs)


class Activity(models.Model):
    """
    One entry on a record's timeline: a logged call, note, meeting, or (later)
    a synced email. This is the running history of "what have we actually done
    with this customer."
    """

    # TEACHING NOTE: you can nest a TextChoices inside the model when the enum
    # is only used by that model. `Activity.Kind.CALL` reads nicely at the call site.
    class Kind(models.TextChoices):
        NOTE = "note", "Note"
        CALL = "call", "Call"
        MEETING = "meeting", "Meeting"
        EMAIL = "email", "Email"  # an email logged by hand

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.NOTE)
    body = models.TextField()
    occurred_at = models.DateTimeField(default=timezone.now)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # An activity can hang off a company, a contact, a deal, or several at once.
    # `on_delete=CASCADE` here means: if the parent record is deleted, its
    # timeline entries go with it (unlike Deal→Company, where we PROTECT).
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    deal = models.ForeignKey(Deal, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    external_ref = models.CharField(max_length=255, blank=True, db_index=True)  # an id from an outside system, if any
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-occurred_at", "-id"]  # newest first; -id breaks ties within the same timestamp
        verbose_name_plural = "activities"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.body[:40]}"  # get_<field>_display() returns the human label

    def clean(self):
        # An activity floating in space, attached to nothing, is a bug. Require
        # at least one parent. The `_id` suffix checks the foreign key value
        # without triggering a database fetch of the related object.
        if not (self.company_id or self.contact_id or self.deal_id):
            raise ValidationError("An activity must be linked to a company, contact, or deal.")


class Task(models.Model):
    """A to-do with a due date, assigned to a user, optionally tied to a record."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    title = models.CharField(max_length=200)
    due_at = models.DateTimeField(null=True, blank=True)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    deal = models.ForeignKey(Deal, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["status", "due_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Same pattern as Deal.save(): stamp the completion time when a task is
        # first marked done, and clear it if it's reopened. Keeping this in the
        # model (not the view) means it's true no matter HOW the task got saved —
        # admin, form, import, or a future API all get the same behaviour.
        if self.status == self.Status.DONE and self.completed_at is None:
            self.completed_at = timezone.now()
        elif self.status == self.Status.OPEN:
            self.completed_at = None
        super().save(*args, **kwargs)


class Notification(models.Model):
    """
    One thing that happened, addressed to one person.

    TEACHING NOTE — the whole point of this table is that it is PER USER and
    visible to nobody else. Every other model in this file is shared: a deal, a
    contact, a task are company-wide facts that every signed-in user may read.
    A notification is a message ABOUT a user TO a user ("you were given the
    Lakeside Dental lead"), which is why it is keyed on `user` rather than
    attached to a record — and why every read of this table must filter on the
    request's user. A `ListView` with no filter here is not a missing feature,
    it is one person reading another's mail.

    `read_at` is a TIMESTAMP rather than a boolean because the question anybody
    actually asks of a notifications list is "when did they see it", and a
    timestamp costs exactly what a bool costs — same column, more answer. It
    doubles as the unread filter: `read_at__isnull=True` is the same query
    either way, so there is no second source of truth to fall out of step.

    `url` is a plain CharField, not a URLField, and that is deliberate: it holds
    a PATH produced by `reverse()` inside this app ("/deals/7/"), and Django's
    URLField validator would reject a relative path. Every value that lands here
    is written by core/notifications.py from a named route, never by a browser.
    There is no form in this app that accepts one.

    `on_delete=CASCADE` is the one place this file doesn't protect the row. A
    notification is not history worth keeping — the Deal, the Task and the
    Activity that caused it are all still there — and a departing employee's
notifications have no meaning to anybody left.
    """

    # TEACHING NOTE — a nested `Kind` enum, the shape Activity and Task use in
    # this file, so `Notification.Kind.LEAD_ASSIGNED` reads like every other
    # choices constant in the app and no trigger has to spell the raw string. The
    # two values nothing writes yet — `task_due` and `deal_moved` — are listed
    # because a fixed menu that grows a row later is a migration; a fixed menu
    # that has to grow later is a rewrite of every place that switches on it.
    class Kind(models.TextChoices):
        # Listed in the order a lead lives through them: it arrives, then gets an
        # owner. The order only affects choice dropdowns (the bell sorts by time),
        # and rows store the value string, so reordering never touches data.
        NEW_LEAD = "new_lead", "New lead"
        LEAD_ASSIGNED = "lead_assigned", "Lead assigned"
        TASK_ASSIGNED = "task_assigned", "Task assigned"
        TASK_DUE = "task_due", "Task due"
        DEAL_MOVED = "deal_moved", "Deal moved"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.CharField(max_length=200)
    url = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        # TEACHING NOTE — the `-pk` tiebreak is load-bearing, not decoration. Two
        # notifications written in the same transaction share a created_at to the
        # microsecond often enough to matter, and SQLite is free to return tied
        # rows in a different order on a different query. Without the tiebreak
        # the list page reshuffles between requests and any test asserting on
        # order flakes. Activity.Meta carries the same pair for the same reason.
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        # The text IS the notification, so that is what it prints as — in the
        # admin, in a shell, in any future subject line.
        return self.text


class Quote(models.Model):
    """
    What a deal is worth, broken into the things it is worth.

    TEACHING NOTE — WHY THIS EXISTS, since `Deal.value` already holds an amount.
    A deal's value is a single number and it is fine for one: "this opportunity
    is worth $9,800" is a thing anybody can say out loud. It is useless for a
    QUOTE, because a quote is not an amount, it is a list — three laptops at
    $2,400, a fourth at $1,900, setup labour at $1,700 — and the number a
    customer agrees to is a sum of specific rows they can read, argue with, and
    approve one at a time. A quote that is only a number cannot be reviewed.

    TEACHING NOTE — `OneToOneField` to the deal, not a `ForeignKey`. A deal has
    AT MOST ONE quote, and the database is what enforces it. A second quote for
    the same deal would be a data-entry error rather than a versioning feature:
    versioning is a different table with a different set of rules (who sent
    which revision, what was declined, when), and pretending a second row on this
    table is a revision means there is nowhere to say which revision anybody is
    looking at. One deal, one current quote. The version history, when it is
    wanted, is its own problem.

    There is deliberately NO `total` column here. The total is computed from the
    lines by `core.quotes.quote_total` on the way out, every time. See the long
    note in quotes.py for why storing it would be a second, wrongable answer to
    a question the database can answer exactly.

    `related_name="quote"` (singular, no trailing underscore) is what makes the
    reverse side read like English: `deal.quote`. Where there is no quote, the
    attribute raises `RelatedObjectDoesNotExist`, a class that subclasses
    `AttributeError`, so templates and `hasattr` both answer "no quote" rather
    than blowing up — and the quote_detail view is the place that has to cope,
    because it is the one page where its absence is not an error.
    """

    # The life of a quote, in the order it actually happens. Draft is written and
    # rewritten freely; Sent is out of the building; Accepted is agreed; Declined
    # is the answer. `core.quotes.ALLOWED_TRANSITIONS` holds the legal moves
    # between them, so this list is the vocabulary and that one is the grammar.
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"

    deal = models.OneToOneField(Deal, on_delete=models.CASCADE, related_name="quote")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # Nullable rather than defaulted: "this quote is good for ever" is a different
    # statement from "good until the 30th", and a fake default date would blur
    # them. `expired()` in core/quotes.py reads None as "never expires".
    valid_until = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # The terms a customer agreed to are worth the same audit trail as the deal
    # they hang off, and simple_history costs one table.
    history = HistoricalRecords()

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Quote for {self.deal.name}"

    def get_absolute_url(self):
        # Every route in this feature is keyed on the DEAL's pk, not the quote's,
        # because the quote hangs off the deal and nothing links to it directly.
        # So the canonical URL of a quote is the canonical URL of a quote FOR a
        # deal — one number everywhere, and no way to end up with two URLs for
        # the same page.
        return reverse("quote_detail", args=[self.deal_id])


class QuoteLine(models.Model):
    """
    One row of a quote: a thing, how many, and what each one costs.

    TEACHING NOTE — `quantity` is a Decimal and not an integer because this is a
    services business. 0.25 hours of setup labour and 1.5 days of training
    are ordinary line items; an integer quantity would force somebody to write
    "1 hour" and mean fifteen minutes, or invent a "minutes" convention and halve
    it somewhere. Money in a float is the same bug with more digits, so
    `unit_price` is a DecimalField too and no arithmetic anywhere on this app
    touches a float.

    `unit_price` is NOT NULL and has NO default, on purpose, and it is the one
    field here that could be made more convenient by giving it `default=0`. That
    convenience is a trap: a line typed with no price would save as $0.00, the
    total would be $0.00, and the quote would go out looking finished and
    complete. A missing price has to be a form error the person filling it in can
    see, not a zero that quietly survives review. A required field and a
    deliberately absent default are the same decision.

    `position` exists because a formset's row order is the order things were
    TYPED, and that is not a stable property of the quote. Saved in whatever order
    the database happened to return and re-rendered on every page load, a quote
    whose lines jump around between refreshes is a quote nobody trusts enough to
    send. So the order is a stored, explicit thing — written by the view from the
    formset index on every save, and read back from `Meta.ordering`.
    """

    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="lines")
    # Set by the view on save, never typed. A default of 0 is what a row created
    # outside the editor (the admin, a shell, an import) gets, and it sorts to the
    # front rather than the back — see the note above on why that is why the view
    # renumbers on every save.
    position = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=200)
    # TEACHING NOTE — `MinValueValidator(Decimal("0"))` on BOTH money fields, and
    # this is REVIEW-30: probe 6.9 posted `quantity=-5` and `unit_price=-250.00`
    # at the real editor and the app saved both, because a `DecimalField` carries
    # no minimum of its own. The `min="0"` on the widget is an affordance for a
    # phone keypad and not a rule — a hand-made POST never sees a widget, which is
    # the same lesson as the `unit_price` note above from the other direction.
    #
    # Why it matters beyond tidiness: `quote_total()` sums `quantity *
    # unit_price`, so one negative row makes the whole figure a number the sales
    # manager argues about and the customer never agreed to. And because the
    # refusal has to be server-side to be a refusal, it belongs on the FIELD
    # rather than in the form — the admin, the importer and a management command
    # all go through the same validator, and a `forms.py` clean() would cover only
    # the one door this feature has.
    #
    # `Decimal("0")` rather than `0` for the reason `quotes.ZERO` gives: adding an
    # int to a Decimal raises, and this validator compares against whatever the
    # field cleaned to.
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1,
                                   validators=[MinValueValidator(Decimal("0"))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2,
                                     validators=[MinValueValidator(Decimal("0"))])
    history = HistoricalRecords()

    class Meta:
        # `position` first because that is the intended sequence. `-pk`... no:
        # `pk` ASCENDING, because the tiebreak has to agree with what a human
        # means by "the order it was entered". Two rows can legitimately share a
        # position (a row created by the admin gets 0, and an untouched line
        # renumbered from 0 to 1 is a transient tie), and without a tiebreak the
        # database is free to return those two rows in a different order on a
        # different query. `pk` makes the order total, so the same quote renders
        # the same way twice — the same reasoning as Notification.Meta's pair.
        ordering = ["position", "pk"]

    def __str__(self):
        return self.description


class ImportRun(models.Model):
    """
    An audit record of one data import — what source, how many rows created vs
    updated vs skipped, and whether it was a dry run. Phase 3 writes these; the
    model lives here now so the schema is ready.

    Note `source_label`: the importer redacts any password out of a database
    connection string BEFORE storing it here. You never want credentials sitting
    in a table that people can read.
    """

    source_type = models.CharField(max_length=20)
    source_label = models.CharField(max_length=500, help_text="Credentials are redacted before storage.")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    dry_run = models.BooleanField(default=False)
    errors = models.TextField(blank=True)
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source_type} import {self.started_at:%Y-%m-%d %H:%M}"
