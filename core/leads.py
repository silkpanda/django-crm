"""
Lead intake — the one place a lead becomes three records.

TEACHING NOTE: every other page in this app starts from a record somebody was
already looking at. This one starts from a phone call, or from a stranger on the
public website, so it has to answer "what do we make of this?" before it can
save anything: a Company (matched on name, else created), a Contact (matched on
email, else created), and a Deal to hang the sale off. That decision lives in
ONE function here rather than in a view, because two views call it (the desk
form and the public form) and a rule written twice is a rule that drifts.

The match rules are deliberately forgiving about case and deliberately strict
about ownership:

  - Company matched `iexact` on name. "acme supply" and "Acme Supply" are
    the same customer, and creating a second one splits their history in half.
  - Contact matched `iexact` on email. `Contact.save()` lowercases what it
    stores, so an EXACT match here would fail on every caps-lock submission
    ("PAT@ACME.EXAMPLE" against a row holding "pat@acme.example") and quietly
    mint a duplicate contact.
  - The Deal is left with `owner=None`. That is not laziness, it is the whole
    design: an unowned lead is what the new-leads queue is made of. A lead that
    arrived already owned would be nobody's job, and the badge would be empty
    forever.

`transaction.atomic()` wraps the whole thing. Three rows that depend on each
other (the contact needs the company, the deal needs both, the note needs the
deal) must all land or none: a lead whose note survived but whose deal did not
would be a message with nothing attached to it and nobody able to find it again.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Activity, Company, Contact, Deal, LeadSource, Stage, Task
from .notifications import new_lead

# The one LeadSource that is not a staff team. It is what a submission from the
# public website is attributed to, and it is deliberately NOT offered on the desk
# form's dropdown: a Rep filing a lead that claims it came from the website would
# put a number in the website column that no website visitor earned.
PUBLIC_TEAM = "Website"


def public_team():
    """
    The team the no-login form files under: `settings.PUBLIC_LEAD_TEAM`, which
    defaults to PUBLIC_TEAM. Read per call rather than at import, so a deployment
    (or a test's override_settings) changes it without a code edit.
    """
    return settings.PUBLIC_LEAD_TEAM


def staff_teams():
    """
    The LeadSource rows a member of staff may pick from: the names in
    `settings.LEAD_TEAMS`, by default the four teams seeded by migration 0004.
    Ordering comes from `LeadSource.Meta.ordering`.

    Named explicitly, because LeadSource is shared: it also holds whatever an
    Admin adds and whatever the CSV importer creates (a "Referral" source, say),
    and none of those are a team someone works on. "Every source except Website"
    put Referral on the desk form's dropdown (found in review). Read per call,
    not at import, so `override_settings` reaches it.
    """
    return LeadSource.objects.filter(name__in=settings.LEAD_TEAMS)


def _match_or_create_company(company_name, source):
    """
    The Company this lead is about, matched case-insensitively by name.

    TEACHING NOTE — why `source` is only FILLED IN and never overwritten. A lead
    arriving for "Acme Supply" may well match an account somebody opened
    three years ago and sourced from a trade show. Overwriting that with
    "Website" because a stranger filled in a form rewrites where the customer
    came from, which is a fact about history, not about the newest contact. So
    `source` is stamped on creation, and left alone afterwards — except when it
    is empty, where there is no history to lose and the attribution is useful.
    """
    company = Company.objects.filter(name__iexact=company_name).first()
    if company is not None:
        if company.source_id is None:
            company.source = source
            company.save(update_fields=["source"])
        return company
    return Company.objects.create(name=company_name, source=source)


def _match_or_create_contact(company, *, first_name, last_name, email, phone, source):
    """
    The Contact on this lead, matched case-insensitively by email.

    No email means no way to match on, so it is always a new person — which is
    right: two different people can share a name and no company name, and
    merging them on a guess is worse than a duplicate.

    `email or None` rather than `email or ""`: the column is nullable and unique,
    and blank contact emails are stored as NULL precisely so that a second one
    doesn't collide with the first (see the note on Contact.email in models.py).
    """
    if email:
        contact = Contact.objects.filter(email__iexact=email).first()
        if contact is not None:
            return contact
    return Contact.objects.create(
        first_name=first_name,
        last_name=last_name,
        email=email or None,
        phone=phone or "",
        company=company,
        source=source,
    )


@transaction.atomic
def create_lead(
    *,
    company_name,
    first_name,
    last_name,
    email,
    phone,
    message,
    team_name,
    submitted_by,
    match_company=True,
    external_ref="",
):
    """
    Turn one lead submission into a Company, a Contact, a Deal and a note.

    Returns the new Deal. `submitted_by` is the staff member who filed it, or
    None for the public website — a timeline entry with no author is a real
    thing here, not a bug: nobody signed in to write that message.

    It also notifies the sales team through `notifications.new_lead`, which is the
    only reason anything ever finds these rows — see that function for why the
    queue needs to make a noise. Firing it HERE rather than in the two views is the
    same argument as everything else in this module: the desk form and the public
    form are two doors onto one room, and a lead has to arrive complete from
    either of them. A rule in a view is a rule the other view forgets.

    SECURITY NOTE — the deal's `owner`, `stage` and `value` are set HERE, from
    constants, and never from anything a browser sent. `stage` is `Stage.LEAD`
    because a new opportunity is at the top of the pipeline, and `value` keeps
    the model's own zero default because nobody has quoted a price yet. An
    attacker who POSTs `stage=won&owner=<someone else>` to the public form gets
    that key ignored, because this function has no parameter to put it in.

    The Activity body is the submitter's message and NOTHING else — the team's
    name is deliberately not prefixed onto it. The attribution lives on
    `company.source`, `contact.source` and `deal.source`, and it shows on the
    Leads queue, and a timeline note that reads "Lead from Website: Wants
    a quote" instead of what the customer actually wrote is a paraphrase in
    the one place the whole point is that it is a quotation. (The Phase 5
    probe and the deal page's escaping test both read this field expecting the
    message verbatim.)

    `match_company=False` always creates a fresh Company instead of matching on
    name. It is for a caller whose form has a person's name and no company name:
    two different "John Smith" households are not one customer, and merging them
    on a guess is worse than a duplicate an Admin can merge later.

    `external_ref` is stamped on the note, and it is how a caller that may send
    the same lead twice (a sender that retries) finds the deal it
    already made. The note rather than the Contact, because the note is the one
    record this function ALWAYS creates new; a contact can be matched to an
    existing one, and an existing row's reference belongs to whoever made it.

    The body is plain text on purpose. It is not HTML, it is never marked safe,
    and the template escapes it — so a lead saying "our printers are
    <script>alert(1)</script> great" is stored and displayed as those literal
    characters rather than run.
    """
    source = LeadSource.objects.get(name=team_name)

    if match_company:
        company = _match_or_create_company(company_name, source)
    else:
        company = Company.objects.create(name=company_name, source=source)
    contact = _match_or_create_contact(
        company,
        first_name=first_name,
        last_name=last_name or "",
        email=email,
        phone=phone,
        source=source,
    )

    deal = Deal.objects.create(
        name=f"{company.name} — new lead from {source.name}",
        company=company,
        primary_contact=contact,
        owner=None,          # the new-leads queue is "unowned", see the module note
        stage=Stage.LEAD,
        # Who SENT this lead, which is not `company.source` — that one is where the
        # customer first came from and `_match_or_create_company` leaves it alone.
        source=source,
    )

    Activity.objects.create(
        kind=Activity.Kind.NOTE,
        body=message,
        deal=deal,
        # The company and contact too, not just the deal, so the note shows on
        # the account's timeline as well — otherwise a lead that gets deleted or
        # won leaves no trace of what first brought the customer in.
        company=company,
        contact=contact,
        author=submitted_by,
        external_ref=external_ref,
    )
    new_lead(deal, submitted_by)
    return deal


def create_lead_task(deal, owner):
    """
    The to-do that comes with handing `owner` a lead, due tomorrow. Returns it.

    TEACHING NOTE — this is its own function because two different views need it
    and the to-do belongs to the deal, not to either of them. `lead_assign` (an
    Admin hands a lead to somebody else) and `lead_claim` (a Rep picks one up for
    themselves) disagree about who ends up owning it and about whether anybody
    needs telling; the task is identical in both. Written twice, the due date and
    the priority become two values free to drift apart, and the drift shows up as
    one of the two paths quietly raising a low-priority task three weeks out.

    HIGH and tomorrow are chosen, not inherited. A brand new lead is the most
    time-sensitive row in the pipeline and the one most likely to be forgotten,
    because whoever filed it has already picked up the phone again. A deal with an
    owner and no task is a lead that silently never gets called.
    """
    return Task.objects.create(
        title=f"Contact new lead: {deal.company}",
        assignee=owner,
        deal=deal,
        company=deal.company,
        due_at=timezone.now() + timedelta(days=1),
        priority=Task.Priority.HIGH,
    )