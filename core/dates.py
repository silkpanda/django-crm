"""
Day boundaries in the office's time zone, not in UTC.

TEACHING NOTE: with USE_TZ on (settings.py), `timezone.now()` returns the
current moment in UTC (Coordinated Universal Time, the time zone every stored
datetime uses). That's right for storing and comparing moments. It's wrong for
asking "when did today start?", because UTC's midnight is 7 pm in Chicago
(6 pm in winter). The old code did exactly that:

    now.replace(hour=0, minute=0, second=0, microsecond=0)

which is midnight UTC. So "Today" on My Day ran 7 pm to 7 pm, a task due at
9 pm tonight showed under "Next 7 days", and the overdue badge ignored the
clock on the office wall. It was copied into four places, and the tests
built their fixtures with the same line, so view and test agreed on the wrong
answer and nothing ever failed (REVIEW-25).

`timezone.localtime()` converts to TIME_ZONE first, THEN we take midnight, so
"today" means the office's today. Anything that needs a day boundary asks this
module instead of doing its own `.replace(hour=0)`. The same rule covers a bare
DATE, which is what `local_today` is for: see the note there.
"""

from django.utils import timezone


def local_today_start(now=None):
    """Midnight at the start of today in settings.TIME_ZONE, as an aware datetime.

    Adding `timedelta(days=1)` to the result gives tomorrow's midnight, still in
    local time. Python's zoneinfo (the standard library's time-zone database)
    does that arithmetic on the wall clock, so it stays right across a
    daylight-saving change.
    """
    return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)


def local_today(now=None):
    """Today's DATE in settings.TIME_ZONE — not `timezone.now().date()`.

    TEACHING NOTE — the same trap as `local_today_start`, one level up, and the
    reason this exists rather than being written inline where it's needed.
    `timezone.now().date()` returns the date in UTC. In Chicago that is already
    TOMORROW from 7 pm (6 pm in winter), so anything phrased in days — "valid
    until", "is this quote expired?" — silently rolls over one day early in the
    evening, and only in the evening, which is to say it looks fine for six hours
    of every day and then lies.

    The comparison this feeds is `valid_until < local_today()`, so the returned
    value has to be a plain `date`, not an aware datetime: comparing a date to a
    datetime in Python is a TypeError, and quietly reaching for
    `.localdate()` at each call site is how the two get mixed up in the first
    place.
    """
    return timezone.localdate(now)
