"""
Deal scope — whose deals the dashboard's figures and the pipeline board count.

TEACHING NOTE: one question, one answer. Before this module the answer lived in
two places: the board filtered by owner, the dashboard counted everything, and
the same person got two different numbers on two screens (QA-04). "Scope" is
that answer, and it is decided HERE — in one small file the views ask instead of
re-deriving. The rule:

    Rep    -> MINE. Always their own deals, on every screen.
    Viewer -> TEAM. Always every deal. The role is read-only, owns nothing, so
              "mine" would be a permanently empty dashboard.
    Admin  -> TEAM by default, with a switch. `?scope=mine` / `?scope=team` sets
              it and the choice is remembered in the session, so it carries from
              the dashboard to the board without repeating the parameter.

That last part is the point of the session: an Admin who flips to "Mine" then
drags a card across the board should still be looking at their own numbers. The
board's out-of-band column totals are recomputed after each move, and they read
the scope back out of the same session, so the refreshed header can't contradict
the board it is sitting on (the PIPE-2 failure, at a new altitude).

SECURITY NOTE: the scope is decided HERE, from the user's role, on the server —
it is never trusted from the URL. `?scope=` is only ever READ, and only for an
Admin; a Rep's `?scope=team` is discarded and they still get MINE, so the
parameter can't widen anything. Both the URL value and the stored session value
are checked against the two-word allowlist before use, because anything read out
of a request is user input. Hiding the switch in the template is the politeness;
`scoped_deals()` is the lock.
"""

from .models import Deal
from .permissions import REP, is_admin, role_of

TEAM, MINE = "team", "mine"

# Where an Admin's choice is remembered between requests (dashboard -> board).
SESSION_KEY = "deal_scope"

# The only two values either the URL or the session may contribute. Anything
# else is ignored, which means a garbage value degrades to TEAM rather than to a
# filter nobody tested.
CHOICES = (TEAM, MINE)


def deal_scope(request) -> str:
    """Return TEAM or MINE for this request — the dashboard's and board's view."""
    role = role_of(request.user)
    if role == REP:
        # Not "admins only". A Rep always gets their own deals, so there is no
        # parameter for them to type and nothing for them to be talked into.
        return MINE
    if not is_admin(request.user):
        # Viewer, or logged out: TEAM. Read-only roles see the whole pipeline,
        # which is what fixes the empty board QA-13 reported.
        return TEAM
    # Admin: the URL wins if it names a real scope, and the choice is written
    # back so the next request — with no parameter, e.g. the board after a
    # redirect — lands on the same one.
    requested = request.GET.get("scope")
    if requested in CHOICES:
        request.session[SESSION_KEY] = requested
        return requested
    remembered = request.session.get(SESSION_KEY)
    return remembered if remembered in CHOICES else TEAM


def can_switch_scope(user) -> bool:
    """Only an Admin gets the Team/Mine toggle. Everyone else's scope is fixed."""
    return is_admin(user)


def scoped_deals(request, queryset=None):
    """
    The deals this request is allowed to count. Pass a queryset to keep any
    select_related/prefetch the caller already set up; the scope filter is
    applied on top of it, never instead of it.
    """
    if queryset is None:
        queryset = Deal.objects.all()
    if deal_scope(request) == MINE:
        return queryset.filter(owner=request.user)
    return queryset
