"""
Who is allowed to do what.

TEACHING NOTE: this is the single most important file for security in the app,
so it lives on its own instead of being scattered through the views. The golden
rule of web security: NEVER trust the browser to enforce permissions. Hiding an
"Edit" button in the template stops nothing — anyone can type the edit URL by
hand or POST to it with a script. Real enforcement happens on the SERVER, on
every request, by calling the functions below. The hidden button is just
politeness; these functions are the lock.

We keep every rule as a small, named function (`can_edit`, `can_delete`, ...).
The views ask the question ("can this user edit this object?") and act on the
answer. Because the logic is here and only here, there's one place to read, one
place to test, and one place to fix.

The three roles are stored as Django "groups" (created by a migration):
  Admin  - everything, including deletes and reassigning who owns a record.
  Rep    - sees everything, edits records they own, logs activity anywhere.
  Viewer - read-only. A user in no group is treated as a Viewer (fail closed).
"""

from .models import Activity, Company, Contact, Deal, Task

ADMIN = "Admin"
REP = "Rep"
VIEWER = "Viewer"
ROLES = [ADMIN, REP, VIEWER]


def role_of(user):
    """Return the user's role as a string, or None if they aren't logged in."""
    # An anonymous or deactivated user has no role. Returning None here makes
    # every `can_*` check below fall through to "no" — we "fail closed," which
    # means the safe default is to deny, not to allow.
    if not user.is_authenticated or not user.is_active:
        return None
    # A Django superuser is Admin by definition, group or not.
    if user.is_superuser:
        return ADMIN
    names = set(user.groups.values_list("name", flat=True))
    # If someone was mistakenly added to two role groups, the more powerful one
    # wins by checking Admin before Rep.
    for role in (ADMIN, REP):
        if role in names:
            return role
    # Logged in but in no recognised group → lowest privilege, never a crash.
    return VIEWER


def is_admin(user):
    return role_of(user) == ADMIN


def can_create(user):
    # Admins and Reps can create records; Viewers cannot.
    return role_of(user) in (ADMIN, REP)


def can_log_activity(user):
    # A Rep can log a call on ANY record, even one they don't own — you often
    # take a message for a colleague's account.
    return role_of(user) in (ADMIN, REP)


def can_edit(user, obj):
    """Can `user` edit this specific object? Ownership matters for Reps."""
    role = role_of(user)
    if role == ADMIN:
        return True
    if role != REP:
        return False  # Viewers and the logged-out never edit
    # A Rep may edit only records they own. `isinstance` lets one function cover
    # every record type while asking each its own "who owns you?" question.
    if isinstance(obj, (Company, Contact, Deal)):
        return obj.owner_id == user.pk
    if isinstance(obj, Activity):
        return obj.author_id == user.pk
    if isinstance(obj, Task):
        return obj.assignee_id == user.pk
    return False


def can_delete(user, obj):
    """Deleting is stricter than editing — it's harder to undo."""
    if isinstance(obj, Activity):
        # A Rep may remove their own timeline entries (a typo'd note), but only
        # their own. Everything else is Admin-only.
        return is_admin(user) or (role_of(user) == REP and obj.author_id == user.pk)
    if isinstance(obj, Task):
        # SECURITY NOTE (QA-05): deleting a task is an IDOR waiting to happen, so
        # the check is per-OBJECT, never per-role. "A Rep may delete tasks" would
        # let one Rep destroy another's work by guessing an id; the assignee
        # comparison below is what makes it safe, and it is the same test
        # `can_edit` already makes, so edit and delete can't drift apart.
        #
        # A task IS the work item: a Rep who ticked the wrong thing off, or typed
        # the wrong title, needs to be able to clear their own mess without
        # waiting for an Admin. "Own" a task means being its assignee.
        return is_admin(user) or (role_of(user) == REP and obj.assignee_id == user.pk)
    return is_admin(user)
