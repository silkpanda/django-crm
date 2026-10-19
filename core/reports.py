"""
Reporting — the numbers, and the argument about what they mean.

TEACHING NOTE — why this is a module and not a pile of `.aggregate()` calls in a
view. The dashboard already sums the pipeline (`DashboardView`), and it is allowed
to: one screen, one number, recomputed on every load, and nobody argues about it.
A report is different. A report is a table somebody will make a decision from and
then quote back to you in a meeting, so the definition of each column has to live
in one readable place rather than being reassembled by each view that wants it.
"Conversion rate from Lead to Won is 12%" is the kind of sentence that ends a
career; the person who wrote the definition is the one who gets asked.

Everything here is computed in Python from querysets, never stored, for the same
reason `core/quotes.py` does not store a total: a stored number is a second
answer to a question the data can answer exactly, and it goes stale silently.

THE THREE REPORTS, AND WHAT EACH IS FOR

1. `pipeline_by_stage` — what is in the pipe right now. Value, count and weighted
   value per stage. This is the dashboard's number over again, deliberately, so
   that a disagreement about the forecast can be located: either they disagree
   about the deals, or about the weighting.

2. `stage_conversion` — of the deals that ever reached stage A, what fraction went
   on to reach stage B. Built from `HistoricalDeal`, NOT from the current stage of
   each deal. That distinction is the whole report and it is easy to get wrong.

3. `rep_performance` — per owner: open deals, weighted pipeline, won, lost, win
   rate, won value.

WHY CONVERSION NEEDS THE HISTORY TABLE, which is the subtle one. The obvious
implementation is "count deals whose stage is Won, divide by deals whose stage is
anything" — and that answers a completely different question. It measures how many
of the deals you happen to have *right now* were won. It silently drops every
deal that was lost, and every deal still open, from the denominator, so the rate
is inflated by construction and rises as the pipeline empties. A rep whose deals
all close loses their wins from the numerator too, so their rate collapses
precisely when they did well.

The honest denominator is "deals that ever REACHED stage A", which means asking
the history table what stages each deal has occupied. `HistoricalDeal` records one
row per save with `history_type` marking it as a creation, a change or a
deletion, and each row carries the stage as it was at that moment. Reconstructing
"which deals ever passed through Negotiation" from those rows is exact. Doing it
from `Deal.stage` is not possible at all, because the current row holds only the
current value.

So a conversion report is a statement about history, and history is a table this
app has been quietly keeping since Phase 1 for exactly this kind of question.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .models import CLOSED_STAGES, OPEN_STAGES, Deal, Stage
from .scope import scoped_deals


def pipeline_by_stage(deals):
    """
    Count, raw value and weighted value for every stage, in pipeline order.

    TEACHING NOTE — `open_value` sums `Deal.weighted_value` rather than computing
    it here. The property is on the model because the pipeline board, the
    dashboard and this report all need the same answer, and three places each
    doing `value * effective_probability / 100` is three chances to disagree about
    what a forecast is. One definition, on the model.

    Every stage appears, including the ones with no deals. A report whose rows
    disappear when a column empties is a report somebody screenshots, and then
    the screenshot is wrong by next Tuesday.
    """
    rows = {
        stage: {"count": 0, "value": Decimal("0"), "weighted_value": Decimal("0")}
        for stage in Stage
    }
    for deal in deals:
        row = rows.get(deal.stage)
        if row is None:
            continue
        row["count"] += 1
        row["value"] += deal.value or Decimal("0")
        row["weighted_value"] += deal.weighted_value or Decimal("0")
    return rows


def stage_conversion(stages=None, since=None, deal_ids=None):
    """
    Of the deals that ever reached each stage, the fraction that moved on.

    `stages` defaults to the open pipeline plus WON, which is the set a funnel is
    normally drawn from. LOST is excluded from the DENOMINATOR deliberately: a
    deal lost at Lead never reached Negotiation, so counting it as "reached Lead"
    and then not counting it as "reached Negotiation" is correct, but a report
    that includes a Lost row next to the others invites the reader to add the
    column as if the losses were a stage everyone passed through. It is not.

    `since` filters on the history row's date, so a report can answer "this
    quarter" without the caller having to write the date arithmetic.

    `deal_ids` restricts the funnel to a SET OF DEALS, and it is not optional
    decoration — see REVIEW-31. `report_for()` used to call this with no scope at
    all, so a Rep's conversion table was built from `Deal.history.all()`: every
    deal the office has ever had, on a page whose other three tables were
    correctly scoped. `None` keeps the unscoped behaviour the arithmetic tests
    use; every path that serves a user passes the caller's ids.

    TEACHING NOTE — filtering the HISTORY rows by id rather than the deals by
    owner-then-history is what makes this cheap and correct at once. The history
    table is already the whole funnel; adding `id__in` keeps it one query, and it
    drops the history of deals that no longer exist — rows `history_type='-'`
    records, and orphans left by a hard delete — which is a second, quieter leak
    in the same place: a deleted deal's stages stayed in everybody's funnel.

    TEACHING NOTE — `reached[stage]` is a SET of deal ids. Sets, because the
    question is "did this deal ever stand here", which is a yes/no per deal, and a
    deal that bounced back and forth through Negotiation must still count once. A
    list would double-count it and inflate the denominator for every stage it
    revisited — which is exactly the deal a hard negotiation produces.
    """
    stages = list(stages) if stages is not None else [*OPEN_STAGES, Stage.WON]
    reached = {stage: set() for stage in stages}
    order = {stage: index for index, stage in enumerate(stages)}

    rows = Deal.history.all()
    if deal_ids is not None:
        rows = rows.filter(id__in=list(deal_ids))
    if since is not None:
        rows = rows.filter(history_date__gte=since)
    # `+` is simple_history's marker for a created row, `~` for a changed one.
    # DELETED rows are excluded: a deleted deal's history is not evidence of a
    # funnel, it is evidence that somebody pressed delete.
    for deal_id, stage in rows.filter(history_type__in=["+", "~"]).values_list("id", "stage"):
        if stage in reached:
            reached[stage].add(deal_id)

    result = []
    for stage in stages:
        denominator = len(reached[stage])
        # Advanced = reached THIS stage AND reached some later one. The
        # intersection is what makes it a funnel rather than two unrelated counts:
        # a deal that reached Negotiation but never touched Proposal has not
        # "advanced from Proposal", and counting it as though it had would
        # inflate the Proposal row with deals that skipped it entirely.
        later = [reached[s] for s in stages if order[s] > order[stage]]
        # UNION, not intersection, and the difference is the whole report. "Advanced
        # from Qualified" means "reached Qualified AND reached something after
        # it" — a deal that stopped at Proposal advanced and should be counted.
        # Intersecting the later sets instead asks for deals that reached EVERY
        # later stage, which is only ever true of a deal that reached Won, so
        # every non-Won row reads as zero and the funnel flattens into a single
        # bar.
        #
        # `set().union(*[])` on the last stage is the empty set, and the empty
        # set intersected with anything is empty, which is the right answer for a
        # stage nothing follows — but the explicit branch says so, rather than
        # relying on that coincidence.
        if later:
            numerator = len(reached[stage] & set().union(*later))
        else:
            numerator = 0
        result.append({
            "stage": stage,
            "label": Stage(stage).label,
            "reached": denominator,
            "advanced": numerator,
            "rate": (numerator / denominator) if denominator else None,
        })
    return result


def rep_performance(deals):
    """
    Per-owner rollup: open deals, weighted pipeline, won, lost, win rate, won value.

    TEACHING NOTE — the `owner is None` row is not a bug and not an afterthought.
    A lead that arrived through the public form is deliberately left unowned (see
    `core/leads.py`), so on any given day there are open deals that belong to
    nobody and are sitting in the queue. Collapsing them into a bucket named
    "unassigned" is how an Admin finds out the queue is not empty, which is the
    report doing its job. Dropping them instead would make every rep's numbers
    look better than the office's actual position.

    A rep with no deals appears with zeros rather than being absent, so the table
    reads as a roster. A performance report that silently omits the person who
    closed nothing is a report about the people who happened to close something.
    """
    by_owner = defaultdict(lambda: {
        "open_deals": 0,
        "open_value": Decimal("0"),
        "open_weighted": Decimal("0"),
        "won": 0,
        "won_value": Decimal("0"),
        "lost": 0,
        "lost_value": Decimal("0"),
    })
    # Touch every owner we know about, so somebody with nothing closed is still
    # on the page. Doing this from the deal set alone would omit them.
    for deal in deals:
        key = deal.owner.username if deal.owner_id else "unassigned"
        row = by_owner[key]
        if deal.stage in CLOSED_STAGES:
            if deal.stage == Stage.WON:
                row["won"] += 1
                row["won_value"] += deal.value or Decimal("0")
            else:
                row["lost"] += 1
                row["lost_value"] += deal.value or Decimal("0")
        else:
            row["open_deals"] += 1
            row["open_value"] += deal.value or Decimal("0")
            row["open_weighted"] += deal.weighted_value or Decimal("0")

    result = []
    for key in sorted(by_owner):
        row = by_owner[key]
        closed = row["won"] + row["lost"]
        row["closed"] = closed
        row["win_rate"] = (row["won"] / closed) if closed else None
        result.append({"owner": key, **row})
    return result


def forecast(deals, horizon_days=90):
    """
    Weighted pipeline plus what is already closed, over a date horizon.

    `horizon_days` filters on `expected_close`. A deal with no expected date is
    counted in the total but reported as `undated`, because "we have $X in the
    pipe" is true whether or not anybody promised a date, and a forecast that
    quietly drops undated deals understates the pipeline to a number the board
    already knows is wrong.

    TEACHING NOTE — `expected_close` is a date and the comparison is made against
    LOCAL today from `core.dates.py`, not UTC. At 7pm in Chicago `timezone.now()`
    has already rolled over to tomorrow, so a UTC comparison starts excluding
    deals a day early, every evening, and nobody can work out why the forecast
    drops before they leave.
    """
    from .dates import local_today

    today = local_today()
    limit = today + timedelta(days=horizon_days)
    rows = {
        "open_count": 0,
        "open_value": Decimal("0"),
        "open_weighted": Decimal("0"),
        "undated": 0,
        "undated_weighted": Decimal("0"),
        "won_value": Decimal("0"),
    }
    for deal in deals:
        if deal.stage in CLOSED_STAGES:
            if deal.stage == Stage.WON:
                rows["won_value"] += deal.value or Decimal("0")
            continue
        if deal.expected_close is None:
            rows["undated"] += 1
            rows["undated_weighted"] += deal.weighted_value or Decimal("0")
            rows["open_count"] += 1
            rows["open_value"] += deal.value or Decimal("0")
            continue
        if today <= deal.expected_close <= limit:
            rows["open_count"] += 1
            rows["open_value"] += deal.value or Decimal("0")
            rows["open_weighted"] += deal.weighted_value or Decimal("0")
    return rows


def report_for(user, request=None):
    """
    Every report for one user, already scoped.

    The single place the scope filter is applied. A report view that forgot to
    call `scoped_deals` would show an Admin a Rep's "win rate" with no deals in
    it, or — far worse — show a Rep the whole office's performance. One entry
    point means there is one place to forget, and this is it.

    SECURITY NOTE — ONE `scoped_deals()` CALL IS NOT SCOPE. It was not, and probe
    6.14 is the receipt: `pipeline`, `reps` and `forecast` take the scoped list,
    while `conversion` took no arguments at all and read `Deal.history.all()`. So
    a Rep saw three correct tables and one built from every deal in the office —
    which means a Rep could read a colleague's stage history, and the three
    correct tables made it look deliberate. `stage_conversion(deal_ids=...)` is now
    given the same list's ids, and the ids are threaded from the one queryset
    rather than re-derived, so there is no second place to forget.

    `request` is optional so a management command or a test can produce the same
    numbers without a request object; `scoped_deals` only reads `request.user`.
    """
    if request is None:
        # `scoped_deals` -> `deal_scope` reads `request.user` and, for an Admin,
        # `request.session` (that is where the remembered Team/Mine choice lives).
        # A duck-typed stand-in carrying all three means this function stays
        # callable from a management command, where there is no request at all,
        # without dragging in `RequestFactory` to build a fake one.
        class _Request:
            user = None
            GET = {}
            session = {}

        request = _Request()
        request.user = user
    deals = list(scoped_deals(request, Deal.objects.select_related("owner", "company")))
    deal_ids = [deal.pk for deal in deals]
    return {
        "pipeline": pipeline_by_stage(deals),
        "conversion": stage_conversion(deal_ids=deal_ids),
        "reps": rep_performance(deals),
        "forecast": forecast(deals),
    }