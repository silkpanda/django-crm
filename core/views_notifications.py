"""
The notifications list and the two endpoints that mark it read.

TEACHING NOTE — three views, one subject, and all three exist because of the same
fact: a Notification is private to its `user`. Every query here filters on
`request.user`, and `notification_read` looks its row up with `pk` AND `user` in
one `get()` rather than fetching by pk and comparing ownership afterwards. Those
are the same result on a correct request and very different results on a hostile
one — a fetch-then-check is a window where the code holds a row it has no right
to hold, and it is the exact shape of IDOR this project has already been bitten by
twice.

The consequence worth stating plainly: somebody else's notification is a 404 here,
never a 403. A 403 answers "this exists, and you may not have it", which is
confirmation that a row they were not meant to know about is real. 404 says only
"no such thing for you", and is indistinguishable from a pk that never existed.
"""

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from .models import Notification

# Fifty unread rows is about as far back as anybody scrolls before deciding they
# have missed something and want the filter instead. It is a page size, not a
# limit — the list is complete, it is just paged.
NOTIFICATIONS_PER_PAGE = 50


class NotificationListView(LoginRequiredMixin, ListView):
    """
    This user's notifications, newest first.

    SECURITY NOTE — `get_queryset` filtering to `request.user` is the only thing
    standing between this page and being an inbox everyone can read. There is no
    role check anywhere in this file, and there does not need to be one: a Rep
    seeing their own notifications and an Admin seeing theirs is not a permission
    difference, it is the same permission. `LoginRequiredMixin` supplies the login
    gate; the filter supplies the ownership.

    Newest-first comes from `Notification.Meta.ordering`, not from here, so the
    sidebar badge, this page and any future query all agree about what "latest"
    means. See the note on `-pk` in models.py for why the tiebreak is there.
    """

    model = Notification
    template_name = "core/notifications.html"
    context_object_name = "notifications"
    paginate_by = NOTIFICATIONS_PER_PAGE

    def get_queryset(self):
        # SECURITY NOTE — this filter IS the permission. No role check exists or
        # is needed: whether you may see a notification depends on nothing but
        # whether it is yours. Remove this line and every Rep in the office is
        # holding every other Rep's mail, which is what test 8 is for.
        return Notification.objects.filter(user=self.request.user)


@login_required
@require_POST
def notification_read(request, pk):
    """
    Mark ONE notification read, then send the browser on to whatever it was about.

    Marking an already-read notification read again is a no-op rather than an
    error: two browser tabs, or a back button, both produce a second POST of a
    POST that already succeeded, and neither is a reason to show anybody a red
    page. The `update()` is scoped to unread rows so the second POST does not even
    rewrite `read_at` and move the "seen at" time.

    SECURITY NOTE on the redirect: `url` is a stored value, and redirecting to a
    stored value is the open redirect from `views.task_next_url` wearing a
    different hat. Every value in the column is written by core/notifications.py
    from `reverse()`, so in practice it is always one of our own paths — but the
    admin can edit this model, so it is checked rather than trusted, with the same
    `url_has_allowed_host_and_scheme` helper and the same fallback to the list.

    The write is a queryset `update()` rather than `save()`, so simple-history
    hears nothing — and that is the right trade here for once: the only change
    this row ever gets is a person reading their own mail, and `read_at` is that
    fact recorded in the row itself. A history entry saying "rep1 marked rep1's
    notification read" would be the same sentence twice.
    """
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if notification.read_at is None:
        Notification.objects.filter(pk=notification.pk, read_at__isnull=True).update(read_at=timezone.now())
    if notification.url and url_has_allowed_host_and_scheme(
        notification.url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(notification.url)
    return redirect("notification_list")


@login_required
@require_POST
def notification_mark_all_read(request):
    """
    Mark every one of THIS user's unread notifications read, in one query.

    The bell needs this: without it, a Rep who has been away for a week comes
    back to a badge reading 23 and has to click through 23 rows one at a time to
    get it to zero. It touches nothing belonging to anybody else — the filter is
    the whole of the authorisation, the same one line the other two views use.
    """
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notification_list")