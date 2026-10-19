"""
A "context processor" injects variables into EVERY template automatically.

TEACHING NOTE: normally a template only see the context its view hands it. But
some values are needed on every page — here, the current user's role (shown in
the top-bar chip), whether they can create records (shown/hidden "New"
buttons), their overdue task count (the red badge on the sidebar Tasks
link), and the brand name/wordmark/footer/colours that the whole shell is
built from. Rather than add these in every single view, we register one
function (wired up in settings.py TEMPLATES) that runs on every render and
adds them globally. Keep these cheap: they run on every page.
"""

import re

from django.conf import settings
from django.utils.safestring import mark_safe

from .dates import local_today_start
from .models import Deal, Notification, Stage, Task
from .permissions import ADMIN, REP, can_create, role_of


# The neutral palette, mirroring the defaults declared in static/css/tokens.css.
# This is only reached if an operator sets e.g. BRAND_RED to something that
# sanitises away to nothing — see _color_style. It exists because a malformed
# custom property is worse than a wrong one: the browser discards the whole
# declaration and the colour silently reverts to `unset`. Keep this in step
# with tokens.css.
_NEUTRAL_COLORS = {
    "red": "#dc2626",
    "red_text": "#b91c1c",
    "blue": "#0d9488",
    "blue_hover": "#0f766e",
    "blue_text": "#0f766e",
    "navy": "#1e293b",
    "navy_2": "#334155",
}

# An explicit order rather than BRAND_COLORS' insertion order, so reordering the
# dict in settings.py cannot silently change the emitted string.
_COLOR_ORDER = ("red", "red_text", "blue", "blue_hover", "blue_text", "navy", "navy_2")

# Characters a CSS colour literal may legally contain: hex digits, the functional
# notations (rgb(), hsl(), oklch(), and percentage/degree arguments) and spaces.
# Everything else in a BRAND_* value is dropped, not escaped — escaping would not
# help, because the sink is a <style> block rather than an attribute.
_COLOR_SAFE = re.compile(r"[^A-Za-z0-9#(),.% -]")


def _color_style():
    """The `:root` body that overrides tokens.css, as one safe CSS string.

    Returns something like `--brand-red:#dc2626;--brand-blue:#0d9488;`.

    TEACHING NOTE — this is the one place in the app where a value read from the
    environment is deliberately NOT HTML-escaped, so it is worth spelling out why.
    It is interpolated into a <style> block, where `mark_safe` is exactly right:
    the string IS CSS, and escaping its colons and semicolons would break it. The
    values are deployment config read from .env by whoever runs the deployment —
    nothing in a request can reach them — and that is the argument for trusting
    them.

    It is also an argument for not being casual about it. One typo in a .env puts
    broken CSS on every page for everyone, so each value is filtered down to
    characters a colour can actually contain and falls back to the neutral default
    when nothing survives. A `<style>` sink is not a place to be relaxed, even
    when the input is trusted.
    """
    parts = []
    for key in _COLOR_ORDER:
        raw = settings.BRAND_COLORS.get(key) or ""
        value = _COLOR_SAFE.sub("", raw)
        if not value:
            value = _NEUTRAL_COLORS[key]
        parts.append(f"--brand-{key.replace('_', '-')}:{value}")
    return mark_safe(";".join(parts) + ";")


def brand(request):
    """Wordmark, page suffix and colour overrides, from settings/env.

    Runs on every render, so it does no queries and no work beyond building
    a small dict from settings. Everything here is already in memory.

    TEACHING NOTE: this is what makes the fork a config change. The templates and
    the CSS contain no literal brand name — they ask for `brand_name` and
    `var(--brand-red)` — so BRAND_NAME in .env is the only thing that decides
    what the app calls itself. The four text values stay ordinary autoescaped
    strings and are NOT marked safe; only the CSS block below is.
    """
    return {
        "brand_name": settings.BRAND_NAME,
        "brand_wordmark": settings.BRAND_WORDMARK,
        "brand_wordmark_accent": settings.BRAND_WORDMARK_ACCENT,
        "brand_footer": settings.BRAND_FOOTER,
        "brand_color_style": _color_style(),
    }


def unread_notifications(request):
    """
    How many of this user's notifications nobody has read — the sidebar bell.

    TEACHING NOTE — this is a SEPARATE processor from `nav` above, and the split
    is about honesty rather than tidiness: `nav` answers "what may this user do,
    and what is waiting for them", while this answers one question about one
    table. Merging them would make the bell's cost invisible, and its cost is the
    thing to watch.

    A context processor runs on EVERY render, including the public lead form,
    which overrides base.html's body block but still gets its context built. So
    two rules, and both are load-bearing:

      - AnonymousUser returns 0 immediately, with no query at all. There is no
        such thing as an anonymous user's notifications, and a COUNT() against a
        table nobody can see is a free query on the most attacked page in the app.
      - The count is a `.count()`, never a list that is then measured. Loading
        fifty notification rows to print a number on every page of the app is the
        kind of thing that is invisible in development and unbearable by the time
        the table is real.

    The filter is `read_at__isnull=True` rather than a boolean, which is the same
    one query either way — see the read_at note in core/models.py.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"unread_notifications": 0}
    return {"unread_notifications": Notification.objects.filter(user=user, read_at__isnull=True).count()}


def nav(request):
    """
    The sidebar's numbers and switches, added to every page's template context.

    `user_role` and `user_can_create` decide which links and buttons show,
    `overdue_task_count` is the Tasks badge, and `new_lead_count` (Admins and
    Reps) is the Leads badge. Anonymous requests — the login page — get nothing.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}  # nothing to add for the login page
    result = {"user_role": role_of(user), "user_can_create": can_create(user)}
    # Overdue task count for the sidebar badge. Only query if the user is
    # authenticated — anonymous users have no tasks.
    # Local midnight, not UTC's — see core/dates.py (REVIEW-25).
    today_start = local_today_start()
    result["overdue_task_count"] = Task.objects.filter(
        assignee=user, status=Task.Status.OPEN, due_at__lt=today_start
    ).count()

    # New-lead count for the Leads badge, ADMINS AND REPS.
    #
    # TEACHING NOTE — the same two conditions the queue page filters on
    # (`views_leads._unowned_leads`): unowned AND still in the Lead stage. A
    # badge has to mean exactly what the page behind it shows, so when one of
    # them is changed both are changed; the queue's queryset is the definition
    # and this counts it.
    #
    # Why Admins and Reps and not Viewers: Reps may now open the queue and pick a
    # lead up for themselves (views_leads.lead_claim), so a Rep is somebody the
    # number is FOR and hiding it would just mean a Rep never finds out there is
    # work waiting. A Viewer can neither see the queue nor act on it, so the
    # count is a fact about the business's backlog that they have no use for —
    # and every context processor runs on every page render, so the role test is
    # what keeps the COUNT() off every Viewer's pages entirely, query included.
    # Same shape as the role-gated sidebar links, for the same reason.
    if role_of(user) in (ADMIN, REP):
        result["new_lead_count"] = Deal.objects.filter(stage=Stage.LEAD, owner__isnull=True).count()
    return result
