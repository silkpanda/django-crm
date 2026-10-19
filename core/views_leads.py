"""
Lead intake: the desk form, the public form, the new-leads queue and assignment.

TEACHING NOTE — this file is function-based while views.py is almost entirely
class-based, and that is not an inconsistency to tidy up. The desk form would fit
a `CreateView`; the public form would not, because it has to do four things
before it can decide whether it is saving anything (rate limit, honeypot, CSRF,
validate) and a CBV's `form_valid()` is the wrong seam for the first three.

The permission gate is a role check plus a raised `PermissionDenied`, the same lock
views_users.py uses. `lead_assign` is Admin only, because handing a lead to a
colleague is a management decision; `lead_queue` and `lead_claim` are open to
Reps as well, because a salesperson who can see a lead nobody owns and cannot
work it is just a queue of phone calls nobody is allowed to make. Hiding the
queue link in the sidebar is politeness; the checks below are the door.

SECURITY NOTE — every defence on the PUBLIC entry point, and what each one stops:

  1. CSRF (`CsrfViewMiddleware`, on globally). The form still carries a token,
     so a third-party page cannot make a visitor's browser POST a lead into
     ours. Stops: a rival quietly funnelling fake leads through our domain.
  2. Rate limit, 5 per IP per hour, in the cache. Stops: one script posting
     ten thousand leads, which buries the real ones and fills the pipeline with
     rubbish nobody assigned.
  3. Honeypot `website`, checked before anything is written, and answered with
     the SAME thank-you page a real submission gets. Stops: naive form-filling
     bots — and, just as importantly, stops them LEARNING: a bot that got a 400
     or an error page would know the field was watched and fill the next form
     differently.
  4. Field allowlist. `owner`, `stage` and `value` are not on either form, so a
     POST carrying them has nowhere to put them; `create_lead` sets those three
     itself, from constants. Stops: "close this lead as won and give it to my
     account" in a single request.
  5. No email, no session, no user data on the response. The public pages render
     through `registration/auth_base.html`, which overrides base.html's whole
     body block, so there is no sidebar, no nav, no name and no role anywhere in
     the HTML. Stops: an anonymous visitor learning the shape of the staff side,
     or that the company runs a CRM at all.
  6. The message is stored as plain text and rendered through normal template
     escaping. There is no `|safe` on this path and there must never be one.
     Stops: a lead whose message is `<script>alert(1)</script>` running in the
     browser of the staff member who opens it.

None of these replace the others. The honeypot does nothing about a bot that
reads the HTML before deciding what to fill in; the rate limit does nothing
about a human submitting one lead an hour; the field allowlist does nothing
about a bot at all. Defence in depth means each of them has a job.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms_leads import LeadAssignForm, LeadForm, PublicLeadForm
from .leads import create_lead, create_lead_task, public_team
from .models import Company, Deal, Stage
from .notifications import lead_assigned
from .permissions import ADMIN, REP, is_admin, role_of

# Five public submissions an hour from one IP, and the window they are counted
# over. Both are settings-ish, but they are only ever read here, so they live
# here; the README says the same numbers so the two can be checked against each
# other.
LEAD_RATE_LIMIT = 5
LEAD_RATE_TIMEOUT = 3600  # seconds

# The suggestion endpoint's two numbers: the shortest thing worth searching on,
# and the most names it will ever hand back. Two characters is the floor because
# one letter matches most of the book, and a dropdown holding every company in
# the CRM is not a suggestion. Ten is the cap because somebody three letters into
# a name wants the first handful back, not a scrollbar.
COMPANY_SUGGEST_MIN_CHARS = 2
COMPANY_SUGGEST_LIMIT = 10


def _rate_limited(ip):
    """
    Count this POST against `ip` and say whether it is over the limit.

    TEACHING NOTE — `cache.add` first, then `cache.incr`. `incr` on a key that
    does not exist raises, because incrementing nothing is a bug rather than a
    zero; `add` is the atomic "create it at 1 if it isn't there" that decides
    which of two simultaneous first-requests owns the key. The `except ValueError`
    covers the window where the key expires between the two calls, which is the
    one race a per-hour limiter actually has.

    `timeout` goes on the `add` only: that sets the window, and re-setting it on
    every increment would make the hour restart on every POST, so a client
    posting steadily would never be limited at all.
    """
    key = f"lead-submit:{ip}"
    if cache.add(key, 1, LEAD_RATE_TIMEOUT):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, LEAD_RATE_TIMEOUT)
        return 1


def _lead_kwargs(form, team_name, submitted_by):
    """
    The `create_lead` arguments, read from a validated form.

    TEACHING NOTE — an explicit list, one key per form field, rather than
    `**form.cleaned_data`. `cleaned_data` contains whatever the FORM declared,
    so passing it straight through means the form decides what the intake
    function is told, and adding a field to the form would silently start
    feeding it. Spelling the arguments out means a new form field is inert until
    somebody decides what it means.
    """
    return {
        "company_name": form.cleaned_data["company_name"],
        "first_name": form.cleaned_data["first_name"],
        "last_name": form.cleaned_data.get("last_name", ""),
        "email": form.cleaned_data.get("email", ""),
        "phone": form.cleaned_data.get("phone", ""),
        "message": form.cleaned_data["message"],
        "team_name": team_name,
        "submitted_by": submitted_by,
    }


# ------------------------------------------------------------------ desk form


@login_required
def lead_create(request):
    """
    Log a lead somebody reported — any signed-in role may, including a Viewer.

    TEACHING NOTE — no `can_create()` check here, and that is deliberate. Every
    other create page in this app is gated on it, and it returns False for a
    Viewer, because a Viewer creating records would be a data-quality problem.
    A lead is different: the phone rings for whoever is nearest, and a caller
    who reached a read-only user who shrugged and took a message is worth more
    than a rule that sends them to voicemail. The VIEWER can create leads and
    nothing else — a Rep's or Admin's other powers are untouched, and this view
    writes no more than one company, contact, deal and note.

    `login_required` rather than `LoginRequiredMixin`, because this is a function
    view; the decorator sets the same `login_required` attribute the global
    `LoginRequiredMiddleware` looks for, so the two mechanisms agree.
    """
    form = LeadForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        deal = create_lead(**_lead_kwargs(form, form.cleaned_data["team"], request.user))
        messages.success(request, f"Lead logged for {deal.company}. Sales has been notified.")
        return redirect(deal.get_absolute_url())
    return render(request, "core/lead_form.html", {"form": form, "model_name": "lead"})


# ------------------------------------------------- the desk form's datalist


@login_required
@require_GET
def company_suggestions(request):
    """
    Company names that already exist, for the Company box's `<datalist>`.

    TEACHING NOTE — `login_required` and NO role check on top of it. Every other
    endpoint in this file that can see company records gates on ADMIN or REP and
    403s a Viewer. This one does not, and that is not a hole: the people filing
    these leads ARE the Viewers (see `lead_create` above), and a Viewer can
    already read every company through `views.CompanyListView`. Restricting whose
    names a signed-in user may be shown would protect nothing /companies/ does not
    already hand over — it would only break the one form whose entire job is
    helping a tech pick the existing customer instead of minting a near-twin.
    The duplicate this prevents (`leads._match_or_create_company` matches
    `name__iexact`, so "Lakeside Dentl" becomes a second company that looks
    identical in the list) is a data-quality bug, and no amount of login
    requirements stops a typo.

    The two-character floor is what makes this a suggestion rather than noise:
    `icontains` on a single letter matches most of the book, and a list of
    everything is worse than no list, because the tech has to read it to find the
    name they already typed. The cap of ten is the same instinct — an alphabetical
    prefix is what a person is scanning for, so the first ten are the useful ones
    and the rest are noise with a scrollbar.

    `@login_required` OUTSIDE `@require_GET`, the same order and the same reason
    as `lead_claim` and `lead_assign`: a stranger who POSTs here gets "who are
    you?" (302) rather than free confirmation that this endpoint exists (405).
    GET-only also means nothing here can be triggered by a link in somebody's
    email, and nothing here writes.

    SECURITY NOTE — this hands a signed-in user a list of customer names, so the
    two things keeping that honest are the login requirement above and the
    template's escaping (see `partials/company_options.html`). There is no `|safe`
    on that path and there must never be one. A company name is typed in by
    strangers — through this very form, and through the public one — so an
    unescaped name in the option list would be stored XSS: the payload would sit
    in the database until the next tech opened the desk form.
    """
    q = request.GET.get("company_name", "").strip()
    names = []
    if len(q) >= COMPANY_SUGGEST_MIN_CHARS:
        names = list(
            Company.objects.filter(name__icontains=q)
            .order_by("name")
            .values_list("name", flat=True)[:COMPANY_SUGGEST_LIMIT]
        )
    # The SAME template either way. A too-short query renders an empty fragment
    # rather than a different response shape, so the caller has one thing to swap
    # in and there is no branch for it to get wrong.
    return render(
        request,
        "core/partials/company_options.html",
        {"company_names": names},
    )


# ---------------------------------------------------------------- public form


@login_not_required
def lead_submit(request):
    """
    The website form. Public, rate limited, and the most attacked page in the app.

    TEACHING NOTE on the ORDER of the four checks below, because it is the whole
    design and getting it wrong inverts the app's behaviour:

      rate limit -> honeypot -> validate -> save

    The rate limit counts every POST including rubbish, because that is what a
    rate limit is for. The honeypot is checked on the RAW POST body, before the
    form is validated, so a bot gets the thank-you page whether or not the rest
    of what it sent made sense — check it after `is_valid()` and a bot that fills
    the honeypot but posts an invalid message gets an error page, and learns.
    Validation comes last because a bot that never gets that far never needs it.
    """
    if request.method == "POST":
        # 2. Rate limit. `REMOTE_ADDR` rather than X-Forwarded-For, on purpose:
        # XFF is a header the CLIENT sets, so trusting it would let an attacker
        # write a fresh IP onto every request and the limit would never bite.
        # Behind a real proxy that header has to be set by the proxy, not the app.
        count = _rate_limited(request.META.get("REMOTE_ADDR", ""))
        if count > LEAD_RATE_LIMIT:
            return HttpResponse(
                "Too many submissions from this address. Please call us instead.",
                status=429,
                content_type="text/plain; charset=utf-8",
            )

        # 3. Honeypot, read straight off the POST and stripped, so a field
        # holding only whitespace still counts as empty (password managers and
        # browser restore do that) while anything a bot typed does not.
        if request.POST.get("website", "").strip():
            return redirect("lead_thanks")

        # 4. Validate.
        form = PublicLeadForm(request.POST)
        if form.is_valid():
            # 5. Save. The team comes from SETTINGS, never the POST: this form has
            # no team field, so nothing a visitor sends can pick where it is filed.
            create_lead(**_lead_kwargs(form, public_team(), None))
            return redirect("lead_thanks")
    else:
        form = PublicLeadForm()

    return render(request, "leads/public_form.html", {"form": form})


@login_not_required
def lead_thanks(request):
    """
    Where both a real submission and a trapped bot end up.

    TEACHING NOTE — one page for both is the point. A bot that is told the same
    thing a customer is told learns nothing about which of its submissions
    worked. A separate "spam detected" page would be a free oracle telling every
    scraper on the internet exactly which of its tricks had been noticed.

    No email is sent (see the README): the visitor's message is in the queue and
    somebody will ring them, and "we will be in touch shortly" is a promise this
    proof of concept cannot keep on its own.
    """
    return render(request, "leads/thanks.html")


# --------------------------------------------- the queue, claiming and assigning


def _unowned_leads():
    """
    The new-leads queue: every lead nobody has picked up yet.

    TEACHING NOTE — two conditions, and the second is not optional. An owned
    lead is somebody's work in progress; a deal that has moved past Lead has
    been qualified (or lost) and is not waiting on anybody. Filtering on
    `owner__isnull=True` alone would keep re-offering deals a Rep is already
    working; filtering on stage alone would list every Lead-stage deal in the
    database forever, since nothing retires them.

    This is the same queryset the sidebar badge counts, deliberately: a badge
    that disagrees with the page it links to is worse than no badge.
    """
    return (
        Deal.objects.filter(stage=Stage.LEAD, owner__isnull=True)
        # One JOIN for the company, both sources and the contact: the queue renders
        # all four in every row, and without this it is four extra queries per
        # row — the N+1 trap. `source` is the lead's own team and `company__source`
        # is the fallback for leads filed before Deal.source existed; both are read
        # in the same row, so both have to be in the same query.
        .select_related("company", "company__source", "source", "primary_contact")
        .order_by("created_at")
    )


def _queue_context(request, form=None, form_error=""):
    """The queue page's context. One place, so the GET and the refused POST agree."""
    return {
        "deals": _unowned_leads(),
        "assign_form": form or LeadAssignForm(),
        "assign_error": form_error,
    }


@login_required
def lead_queue(request):
    """
    Every new lead, waiting for an owner. Admins and Reps; a Viewer gets 403.

    TEACHING NOTE — the queue widened from Admin-only to include Reps, and the
    assign control inside it did NOT widen with it (see lead_queue.html). Those
    are two different decisions: seeing the queue is what lets a Rep do their
    job, and handing a lead to a colleague is a decision about somebody else's
    diary. A Rep gets a "Pick up" button on every row and no dropdown of other
    people's names.
    """
    if role_of(request.user) not in (ADMIN, REP):
        raise PermissionDenied
    return render(request, "core/lead_queue.html", _queue_context(request))


@login_required
@require_POST
def lead_claim(request, pk):
    """
    Take a new lead for yourself: it becomes yours, and you get a to-do about it.

    TEACHING NOTE — `@login_required` OUTSIDE `@require_POST` on purpose, the same
    order and the same reason as `lead_assign` below: an anonymous visitor who
    POSTs here gets bounced to the login page (302, "who are you?") rather than
    told the endpoint only accepts POSTs (405, "this exists"). Answering 405 to a
    stranger is free reconnaissance.

    SECURITY NOTE — this is ONE conditional UPDATE and deliberately not
    read-check-save. The obvious implementation is to load the deal, test whether
    it is unowned, and save an owner onto it, and that version is broken: two Reps
    clicking the same row at the same moment both read it as unowned, both pass
    the test, and the later `save()` silently overwrites the earlier one. Nobody
    is ever told they lost, the loser keeps the to-do that was just created for
    them, and two salespeople both believe the lead is theirs.

    Putting the conditions in the WHERE clause fixes that at the layer where the
    race actually lives. The database evaluates the filter and writes as one
    statement, so of two simultaneous UPDATEs exactly one matches a row and gets
    1 back; the other matches nothing and gets 0. `claimed` is not a boolean, it
    is the row count, and the code branches on it.

    The same filter is the authorisation as well as the lock. `owner__isnull=True`
    is what stops a Rep taking a lead somebody else already owns, and
    `stage=Stage.LEAD` stops them picking up a deal that has been qualified,
    quoted or lost. Neither check needs its own test before the UPDATE — a deal
    that fails either one simply is not in the queryset, so there is no code path
    where a stale read could have said otherwise.

    No notification is sent. The person picking the lead up is the person who
    would be reading it, and `lead_assigned` exists to tell somebody ELSE their
    diary changed.
    """
    if role_of(request.user) not in (ADMIN, REP):
        raise PermissionDenied

    with transaction.atomic():
        claimed = Deal.objects.filter(
            pk=pk, owner__isnull=True, stage=Stage.LEAD
        ).update(owner=request.user, updated_at=timezone.now())

        if claimed == 1:
            deal = Deal.objects.get(pk=pk)
            # `QuerySet.update()` writes no simple-history row, so on its own the
            # claim would vanish from the deal's change log. The race is already
            # won at this point; this save writes the same values back purely so
            # the history records WHO took the lead, and when.
            deal.save(update_fields=["owner", "updated_at"])
            create_lead_task(deal, request.user)
            messages.success(request, f"You picked up {deal.company}.")
            return redirect(deal.get_absolute_url())

        # Zero rows changed, and the reason matters: either there is no such deal,
        # or it is no longer claimable. `get_object_or_404` draws the line, so a
        # guessed id is a 404 rather than a lie that says "somebody beat you to
        # it" about a row that never existed.
        get_object_or_404(Deal, pk=pk)
        messages.error(request, "That lead was already picked up by someone else.")
        return redirect("lead_queue")


@login_required
@require_POST
def lead_assign(request, pk):
    """
    Give a new lead an owner, and the owner something to do about it.

    TEACHING NOTE — `@login_required` OUTSIDE `@require_POST` on purpose: an
    anonymous visitor who POSTs here should be bounced to the login page (302,
    "who are you?") rather than told the endpoint only accepts POSTs (405,
    "this exists"). Answering 405 to a stranger is free reconnaissance.

    Assignment and its task are created together, but only the task genuinely
    needs the transaction — the deal's owner and the reminder to act on it are
    one act, and a deal assigned with no task is a lead that silently never
    gets called. The task itself comes from `leads.create_lead_task`, the same
    one `lead_claim` uses, because the two routes differ in WHO gets the lead and
    not in what the new owner is handed.

    This route stays Admin-only even though Reps may now claim leads for
    themselves. Assigning is not "claiming with extra steps": it puts a call on
    somebody else's diary without asking them, which is exactly the thing a role
    boundary is for. The lead-assigned notification exists to soften that, but
    softening it is not the same as authorising it.

    The notification is fired HERE rather than inside `create_lead_task`,
    and only `lead_assigned` and not `task_assigned` as well: this one act has
    two possible notifications and the Rep needs exactly one of them. A lead
    arriving already carries its own to-do, so telling them "a lead is yours"
    with a link to the deal says more in one line than "a task is yours" with a
    link to a list they have to scan to find out what about.
    """
    if not is_admin(request.user):
        raise PermissionDenied

    deal = get_object_or_404(Deal, pk=pk)
    form = LeadAssignForm(request.POST)
    if not form.is_valid():
        # Refused, and said so: an owner who is not in the dropdown produces a
        # validation error ("Select a valid choice") that this re-render shows.
        # Dropping the POST silently would be kinder to the attacker and worse
        # to whoever is trying to assign a lead and cannot work out why nothing
        # happened.
        return render(
            request,
            "core/lead_queue.html",
            _queue_context(request, form=form, form_error="That user cannot be given a lead."),
            status=400,
        )

    owner = form.cleaned_data["owner"]
    deal.owner = owner
    deal.save(update_fields=["owner", "updated_at"])
    create_lead_task(deal, owner)
    lead_assigned(deal, owner)
    messages.success(request, f"{deal.company} assigned to {owner.get_full_name() or owner.username}.")
    return redirect("lead_queue")
