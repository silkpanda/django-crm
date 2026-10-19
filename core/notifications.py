"""
The three things that make a notification appear.

TEACHING NOTE: this module exists because "what happened" is not a view's job.
`create_lead` lives in core/leads.py rather than in the two views that call it
for exactly this reason — a rule written in a view is a rule the next view
reinvents slightly differently. Assignment and task creation are the same shape:
one place decides WHAT a notification says and where it points, and the views
only decide WHEN.

Every function here is deliberately quiet. A notification is a courtesy, and a
courtesy that can roll back somebody's work is worse than no courtesy at all, so
nothing in here raises into its caller and nothing in here writes before the
transaction that caused it has actually committed.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.urls import reverse

from .dates import local_today_start
from .models import Notification, Task
from .permissions import ADMIN, REP

User = get_user_model()


def _notify(user, kind, text, url=""):
    """
    Write one notification for `user`, once the surrounding transaction commits.

    THE ROLLBACK RULE. `transaction.on_commit` is the whole safety property here.
    A lead assignment writes a deal owner and a task inside a transaction; if that
    transaction rolls back, a notification written eagerly would outlive the thing
    it claims happened — "Lakeside Dental assigned to you" sitting in a Rep's
    bell for a lead nobody was ever given. Deferring the write to the commit means
    the notification cannot exist unless the event did.

    THE ANONYMOUS RULE. No role is checked *here*, and `user is None` returns
    early rather than falling back to "somebody". An unowned lead is not an edge
    case in this app, it is the queue's entire design (see the module docstring in
    core/leads.py): nobody owns it, so there is nobody to tell. Inventing a
    recipient would mean deciding that some role ought to be notified, and that
    decision belongs to whoever owns the queue.

    THE RETURN VALUE, and why it is sometimes None even on success: outside a
    transaction Django runs the callback immediately and this hands back the
    saved row. Inside one, the write has not happened yet, so there is nothing to
    return. Callers get either "here is the notification" or "not yet", never a
    half-written object. Tests that assert on a trigger therefore run inside
    `TestCase.captureOnCommitCallbacks`, which is the same idea made explicit.
    """
    if user is None:
        return None
    written = {}

    def write():
        written["notification"] = Notification.objects.create(
            user=user, kind=kind, text=text, url=url
        )

    transaction.on_commit(write)
    return written.get("notification")


def new_lead(deal, submitted_by=None):
    """
    Tell every salesperson a lead has arrived, and point them at the queue.

    TEACHING NOTE — WHY NOTIFY ON ARRIVAL. `create_lead` deliberately leaves the
    deal unowned, which means the new-leads queue is the ONLY thing that will ever
    pick it up. A queue nobody opens is not a queue, it is a landfill: the lead is
    there, correct and complete, and it is never worked. So somebody has to make
    the noise, and the bell is the cheapest noise that scales — it costs one row
    per salesperson rather than one phone call per lead, and it cannot ring during
    a funeral or a customer meeting. This is the piece that replaces central
    station texting a Rep "there's a dental lead on the board": same information,
    no human having to remember to pass it on, and it works at 2am on a Sunday for
    a lead that arrived at 2am on a Sunday. The pickup is not automatic — the
    notification is an invitation to the queue, and `lead_claim` is where a Rep
    actually takes it. Nothing goes by email or SMS, deliberately: this is a
    proof of concept with no mail sending configured, and a trigger that silently
    depends on an unconfigured channel is a trigger nobody can test.

    WHY THE SUBMITTER IS EXCLUDED. A Rep who took the call and filed the lead
    knows about the lead — they are looking at the form they just submitted. Being
    told about your own typing is the fastest way to teach a sales floor that the
    bell is noise, and then the one notification that mattered (somebody ELSE's
    lead) is the one they stopped reading. Skipped only when `submitted_by` is
    set: on the public website form nobody is signed in, so there is nobody to
    skip and everybody who can act is told.

    WHY VIEWERS ARE EXCLUDED. This is the rule `_notify` above refuses to make, and
    the reason is that a trigger which knows the app's roles can answer it. A
    Viewer cannot open the new-leads queue (views_leads.lead_queue raises
    PermissionDenied for them) and cannot claim a lead, so a notification is a
    dead link plus a reason to distrust the bell. Silence is the correct output
    for a person who has nothing they could do about the news. Superusers are
    included because `permissions.role_of()` calls them Admin, group or not, and
    this queryset is that same rule expressed as SQL rather than a per-user Python
    check — one query, not one per user. `distinct()` is required because
    `groups__name__in` is a JOIN and somebody in both role groups would otherwise
    be notified twice.

    WHY THE LINK GOES TO THE QUEUE AND NOT THE DEAL. The action this notification
    asks for is "pick this up", and the Pick up button is on the queue. Link to the
    deal instead and the Rep arrives at a read-only record page with no way to take
    it and no list of the leads they are not looking at — the link would be a
    dead end dressed as a shortcut. `lead_assigned` links to the deal because by
    then the deal IS somebody's job and the work is recorded there; that is the
    opposite situation, and the difference between the two links is the whole
    difference between "there is work to claim" and "here is your work".

    THE TEXT IS TRUNCATED, and not for tidiness. `Notification.text` is 200
    characters and `Company.name` is ALLOWED to be exactly 200, so the company
    alone fills the column and "New lead: " pushes it over. An over-length value
    is not truncated by the database — it is refused, and this write happens inside
    an `on_commit` callback, by which point the lead is saved and committed and the
    exception surfaces to a view that has already returned success. The lead would
    be on the board with nobody notified. Reading the limit off the model rather
    than hardcoding 200 means widening the column later cannot leave this behind.
    """
    recipients = User.objects.filter(is_active=True).filter(
        Q(groups__name__in=[ADMIN, REP]) | Q(is_superuser=True)
    )
    if submitted_by is not None:
        recipients = recipients.exclude(pk=submitted_by.pk)

    # Read off the model rather than hardcoded — see THE TEXT IS TRUNCATED above.
    limit = Notification._meta.get_field("text").max_length
    text = f"New lead: {deal.company}"
    if len(text) > limit:
        text = text[: limit - 1] + "…"

    # One `_notify` per person, so each row is deferred individually and this
    # function stays as quiet as every other trigger in the module.
    for user in recipients.distinct():
        _notify(user, Notification.Kind.NEW_LEAD, text, reverse("lead_queue"))


def lead_assigned(lead_deal, assigned_to):
    """
    Tell `assigned_to` a lead is theirs, and point them at the deal.

    The text names the COMPANY, not the deal. An Admin's queue is a list of deals
    whose names all read like "Lakeside Dental - new office", and the Rep who has to
    ring them needs the name the customer answers to. The deal is where the work
    is recorded, so that is where the link goes.
    """
    return _notify(
        assigned_to,
        Notification.Kind.LEAD_ASSIGNED,
        f"New lead assigned: {lead_deal.company}",
        reverse("deal_detail", args=[lead_deal.pk]),
    )


def task_assigned(task_obj, assignee):
    """
    Tell `assignee` a task is waiting on them, and point them at My Day.

    URL is the task list rather than a record page because a task's own subject
    is the thing: "Follow up" hanging off four records is four notifications that
    each link somewhere different, and the one page where every task assigned to
    somebody is visible is My Day. `lead_assign` does NOT call this — it writes the
    lead notification instead, because a lead arriving already carries a task and
    telling the Rep twice is how people learn to ignore the bell.
    """
    return _notify(
        assignee,
        Notification.Kind.TASK_ASSIGNED,
        f"Task assigned: {task_obj.title}",
        reverse("task_list"),
    )


def task_due_tomorrow(user):
    """
    `user`'s open tasks whose due date falls on tomorrow, soonest first.

    TEACHING NOTE — this is a QUERY, not a trigger, and the distinction is the
    whole design decision. Nothing in this project runs on a schedule: there is
    no cron, no management command, no worker, and adding one is a bigger call
    than it looks (who runs it, in which timezone, what happens when it misses).
    So the question "which of my tasks are due tomorrow" is answered here as a
    plain function a caller can ask, and turning the answer into notifications is
    left to whichever mechanism eventually exists. Writing the helper now means
    that later work is a one-line call rather than a re-derivation of the day
    boundaries, which is the part that is easy to get subtly wrong.

    The boundaries are LOCAL midnights from core/dates.py, for the reason spelled
    out there: "tomorrow" has to mean the office's tomorrow, and UTC's midnight is
    7 pm in Chicago. Open tasks only, matching My Day's own filter — a task that
    was finished yesterday is not due anything tomorrow.
    """
    if user is None:
        return Task.objects.none()
    tomorrow_start = local_today_start() + timedelta(days=1)
    return (
        Task.objects.filter(
            assignee=user,
            status=Task.Status.OPEN,
            due_at__gte=tomorrow_start,
            due_at__lt=tomorrow_start + timedelta(days=1),
        )
        .select_related("company", "contact", "deal")
        .order_by("due_at")
    )