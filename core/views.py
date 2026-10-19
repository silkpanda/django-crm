"""
Views — the code that turns an incoming web request into a response.

TEACHING NOTE: a "view" receives an HttpRequest (someone clicked a link or
submitted a form) and returns an HttpResponse (usually a rendered HTML page, or
a redirect). Django gives you two styles: plain functions, and CLASS-based views
(CBVs). CBVs like `ListView` and `UpdateView` come pre-wired for the boring 90%
of a CRUD app — fetch a list, paginate it, render a template; load an object,
validate a form, save it, redirect. You customise by overriding small methods.

The pattern below: we write our OWN base classes (RecordListView, etc.) that add
the two things Django's built-ins don't know about — our permission rules and
our sort/filter handling — then each model's real view inherits from those and
just says which model, form, and template to use. Write the shared behaviour
once; configure it many times.
"""

import csv
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, ProtectedError, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from .dates import local_today_start
from .filters import CompanyFilter, ContactFilter, DealFilter
from .forms import ActivityForm, CompanyForm, ContactForm, DealForm, TaskForm
from .history import history_entries
from .models import OPEN_STAGES, Activity, Company, Contact, Deal, Stage, Task, domain_from_website
from .notifications import task_assigned
from .permissions import can_create, can_delete, can_edit, can_log_activity, is_admin
from .scope import can_switch_scope, deal_scope, scoped_deals


# ---------------------------------------------------------------- base classes


def related_names(obj, field_name):
    """
    A many-to-many rendered for a CSV cell: the related names, joined "; ".

    TEACHING NOTE — two different orderings, and the difference is the meaning:

      - `tags` sort ALPHABETICALLY. A tag is a label with no sequence, so the
        only sane export is a stable one: "Beta; Alpha" and "Alpha; Beta" are
        the same company and must produce the same file, whichever row the query
        happened to hit first.
      - `roles` sort by the related model's `order`. "Champion; Blocker" is not
        the same as "Blocker; Champion" — one says the Champion was found first,
        the other says they were blocked. Reusing the alphabetical sort for roles
        would print the sale's actual sequence backwards for most pairs and only
        look right by coincidence for the ones that happen to sort alike.

    The rule is "if the related model has an `order` column, that's the sequence",
    not "if the field happens to be called roles" — so the next ordered lookup
    table in this app exports correctly without touching this function.
    """
    related = getattr(obj, field_name).all()
    if any(f.name == "order" for f in related.model._meta.fields):
        return "; ".join(r.name for r in sorted(related, key=lambda r: (r.order, r.name)))
    return "; ".join(sorted(r.name for r in related))


class RecordListView(ListView):
    """Base for the Companies / Contacts / Deals list pages."""

    # These class attributes are the "knobs" each subclass sets. Defaults here,
    # overridden below per model.
    filterset_class = None
    sort_fields = {}
    default_sort = None
    # QA-11: the column order of the CSV export, and only that. The old export
    # took whatever `model._meta.get_fields()` happened to return, which led with
    # the inherited OwnedRecord fields (owner, lifecycle, source, notes,
    # external_id) and pushed name to the right — so the first thing you could
    # not identify a row by was the row. Subclasses list the columns they mean,
    # in the order a person opening the file wants them. None falls back to the
    # model's own field order for any list view that hasn't opted in yet.
    export_fields = None
    paginate_by = settings.PAGE_SIZE  # Django slices the list into pages of this size for free

    def base_queryset(self):
        # A "queryset" is a lazy database query — it doesn't hit the DB until you
        # actually loop over it or count it. Subclasses override this to add
        # select_related/prefetch_related (see the note in CompanyListView).
        return self.model.objects.all()

    def get_queryset(self):
        # `django-filter` reads the URL's query string (?q=acme&owner=3) and
        # returns only the matching rows. We hand it the request so its dropdowns
        # can be user-aware.
        self.filterset = self.filterset_class(self.request.GET, queryset=self.base_queryset(), request=self.request)
        qs = self.filterset.qs

        # TEACHING NOTE — a subtle but important security detail. We let users
        # sort by column via ?sort=name. But we do NOT feed their raw input into
        # the database. Instead we look it up in `sort_fields`, an allowlist we
        # control, and only use the mapped, known-safe column name. A leading "-"
        # means descending. If they pass a column that isn't in the allowlist
        # (say, ?sort=owner__password to try to leak hashes), we ignore it. This
        # is the pen-test's "SQL/ORM injection via sort" check, held by design.
        sort = self.request.GET.get("sort") or self.default_sort
        key = (sort or "").lstrip("-")
        if key in self.sort_fields:
            qs = qs.order_by(("-" if sort.startswith("-") else "") + self.sort_fields[key])
            self.current_sort = sort
        else:
            self.current_sort = ""
        return qs

    def get_context_data(self, **kwargs):
        # "Context" is the bag of variables handed to the template. Here we also
        # build the query string used by the pagination and column-sort links so
        # that paging keeps your filters, and sorting keeps your page.
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET.copy()  # a mutable copy of the URL's ?params
        params.pop("page", None)
        ctx["filter"] = self.filterset
        ctx["current_sort"] = self.current_sort
        ctx["querystring"] = params.urlencode()
        params.pop("sort", None)
        ctx["sort_base"] = params.urlencode()
        ctx["can_create"] = can_create(self.request.user)  # so the template can show/hide the "New" button
        return ctx

    def get(self, request, *args, **kwargs):
        """
        TEACHING NOTE: if the URL has ?export=1, return a CSV download instead
        of HTML. The export uses the same filters and sort as the screen, but
        NOT the pagination. It branches off here, before ListView ever builds
        the page: exporting the page was REVIEW-06's quiet half (row 26 onward
        never made it into the spreadsheet), and paginating first meant
        `?page=2&export=1` on a one-page list was a 404 instead of a file.
        """
        if request.GET.get("export") == "1":
            return self._export_csv(self.get_export_queryset())
        return super().get(request, *args, **kwargs)

    def get_export_queryset(self):
        """The rows for the CSV: the same queryset as the screen, tags prefetched."""
        qs = self.get_queryset()
        # One extra query for every tag on the file, instead of one per row. The
        # tags cell below walks `obj.tags.all()`, which without this is the
        # classic N+1 — invisible on a 25-row page, miserable on a 5,000-row
        # export. Deal has no tags, and asking for them would be a FieldError —
        # same for roles, which Deal doesn't have either, hence the per-name
        # check rather than a fixed prefetch_related("tags", "roles").
        for name in ("tags", "roles"):
            if any(f.name == name and f.many_to_many for f in self.model._meta.get_fields()):
                qs = qs.prefetch_related(name)
        return qs

    def paginate_queryset(self, queryset, page_size):
        """
        Paginate, but never 404 on a page number (QA-11).

        TEACHING NOTE — Django's default reads ?page=, calls int() on it, and
        turns anything unusable into an Http404. Two of those cases are nobody
        asking a question: ?page=999 is a bookmark somebody kept after rows were
        deleted, and ?page=abc is a fat finger. Answering either with a bare 404
        on a screen that has plenty of rows to show is the view being pedantic
        about a URL.

        `Paginator.get_page` is the tolerant version Django already ships and
        doesn't use here: out of range serves the LAST page, non-numeric serves
        page 1. One call, no version check of our own, same behaviour the moment
        Django changes what an invalid page means.
        """
        paginator = self.get_paginator(
            queryset,
            page_size,
            orphans=self.get_paginate_orphans(),
            allow_empty_first_page=self.get_allow_empty(),
        )
        page = self.kwargs.get(self.page_kwarg) or self.request.GET.get(self.page_kwarg) or 1
        page = paginator.get_page(page)
        return paginator, page, page.object_list, page.has_other_pages()

    def _export_csv(self, queryset):
        """
        Generate a CSV file from the queryset.

        TEACHING NOTE: we use Python's csv module to build the response. The
        first row is the header (field names), then one row per object. We
        use HttpResponse with content_type='text/csv' and a Content-Disposition
        header to trigger a download in the browser.
        """
        model = self.model
        # QA-11: the columns and their order come from the view's `export_fields`
        # when it has one; otherwise fall back to the model's own field order.
        if self.export_fields:
            field_names = list(self.export_fields)
        else:
            field_names = [
                f.name for f in model._meta.get_fields()
                if f.concrete and not f.many_to_many and not f.auto_created
            ]
        # `tags` is a many-to-many, so it has no value on the instance to read —
        # `obj.tags` is a manager. It becomes the names, joined "; ", so the same
        # company exports the same way whichever row the query hit first.
        # `related_names` owns the one deliberate exception: roles export in the
        # order the sale happens in, not alphabetically.
        many_to_many = {f.name for f in model._meta.get_fields() if f.many_to_many}

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{model._meta.model_name}_export.csv"'
        # The byte-order mark tells Excel the file is UTF-8, so "Café" doesn't
        # open as "CafÃ©". The import wizard reads it back fine (utf-8-sig).
        response.write("﻿")

        writer = csv.writer(response)
        writer.writerow(field_names)

        for obj in queryset:
            row = []
            for field_name in field_names:
                if field_name in many_to_many:
                    value = related_names(obj, field_name)
                else:
                    value = getattr(obj, field_name, "")
                    # Handle foreign keys (show the related object's __str__)
                    if hasattr(value, "_meta"):
                        value = str(value)
                row.append(neutralise_formula(value))
            writer.writerow(row)

        return response


# Characters that make Excel, LibreOffice, and Google Sheets treat a cell as a
# formula. Tab and carriage return are on the list because some spreadsheet
# apps strip them and then see the "=" behind them.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def neutralise_formula(value):
    """
    Stop a CSV cell from executing when it's opened in a spreadsheet (REVIEW-06).

    TEACHING NOTE — CSV INJECTION. A CSV is plain text, but Excel reads a cell
    starting with "=" as a formula and runs it. So a Rep who names a contact
    `=HYPERLINK("http://evil","Click")` has planted a live link (or worse, a
    DDE command) in the file an Admin downloads later. Prefixing a single quote
    tells the spreadsheet "this is text". It's the OWASP-recommended fix.

    Only strings are touched. Numbers and dates pass through as themselves, so a
    Decimal deal value stays a number. The trade-off: a phone stored as
    "+1 555..." exports as "'+1 555...". A visible apostrophe on a phone number
    costs far less than a formula running on an Admin's machine.
    """
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGERS):
        return "'" + value
    return value


class RecordCreateView(CreateView):
    """
    The create page every record type shares: one template, one permission gate.

    Subclasses set `model` and `form_class` and get, for free: a 403 for anyone
    `can_create` refuses (checked in `dispatch`, before a form is built), the
    current user handed to the form so OwnerFieldMixin can decide whether to show
    the owner dropdown, and a "Created ..." flash message on success.
    """
    template_name = "core/form.html"

    def get_context_data(self, **kwargs):
        # `_meta.verbose_name` is the model's human name ("company"), used in the
        # page title "New company".
        return {**super().get_context_data(**kwargs), "model_name": self.model._meta.verbose_name}

    def dispatch(self, request, *args, **kwargs):
        # `dispatch` is the very first method a CBV runs for any request. It's the
        # perfect chokepoint for a permission gate: check first, and if the user
        # can't create, raise PermissionDenied (Django turns that into a 403)
        # before any form is even built.
        if not can_create(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        # Pass the current user into the form so OwnerFieldMixin can decide
        # whether to show the owner dropdown. `**super()...` keeps Django's own
        # kwargs and adds ours.
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        # Runs only after the form passed validation. `messages.success` queues a
        # one-time flash message shown on the next page (the green banner).
        response = super().form_valid(form)
        messages.success(self.request, f"Created {self.object}.")
        return response


class RecordUpdateView(UpdateView):
    """
    The edit page every record type shares.

    The permission check lives in `get_object`, not `dispatch`, because it needs
    the row: a Rep may edit only what they own. Showing the form (GET) and saving
    it (POST) both load the row through `get_object`, so one guard covers both.
    """
    template_name = "core/form.html"

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "model_name": self.model._meta.verbose_name}

    def get_object(self, queryset=None):
        # `get_object` loads the row being edited. We override it to enforce the
        # per-object permission: a Rep may edit only what they own. Checking HERE
        # covers both showing the form (GET) and saving it (POST) with one guard,
        # because both paths call get_object.
        obj = super().get_object(queryset)
        if not can_edit(self.request.user, obj):
            raise PermissionDenied
        return obj

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Saved {self.object}.")
        return response


class RecordDeleteView(DeleteView):
    """
    The confirm-and-delete page every record type shares.

    Same per-object gate as RecordUpdateView, via `can_delete`. A delete the
    database refuses because of a PROTECT foreign key comes back to the record's
    page with a message saying what is in the way, rather than a 500.
    """
    template_name = "core/confirm_delete.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not can_delete(self.request.user, obj):
            raise PermissionDenied
        return obj

    def form_valid(self, form):
        # TEACHING NOTE: recall Deal→Company is on_delete=PROTECT. If someone
        # tries to delete a company that still has deals, the database raises
        # ProtectedError. We catch it and show a helpful message instead of
        # letting the app 500. Handling the error the model deliberately raises
        # is how the two layers cooperate.
        name = str(self.object)
        try:
            self.object.delete()
        except ProtectedError:
            messages.error(self.request, f"{name} still has deals. Delete or move those first.")
            return redirect(self.object.get_absolute_url())
        messages.success(self.request, f"Deleted {name}.")
        return redirect(self.get_success_url())


# QA-02: one number for "how far back the timeline goes", read by the detail
# page's first render and by the HTMX list swap. When it changes, it changes
# here, not in two places that drift apart and quietly disagree about what a
# timeline is.
TIMELINE_LIMIT = 100


def timeline_for(obj):
    """
    Every activity that belongs on one record's timeline, newest first.

    TEACHING NOTE — this used to be three `timeline()` methods, one per detail
    view, each correct on its own. Then the HTMX swap needed the same list again
    after logging an entry (QA-02: the browser was deciding the order, not the
    server), and a fourth copy would have been a fourth chance to answer "what
    belongs on a company timeline?" slightly differently. One definition, three
    callers: the company can broaden (its own entries plus its contacts' and
    its deals'), a contact and a deal are narrow (their own).

    The ordering is the model Meta's `-occurred_at, -id`, which is what makes the
    swap correct: a call logged today but dated last week comes back below
    today's note, because the sort happened in the database rather than in the
    browser.
    """
    if isinstance(obj, Company):
        # A company's timeline is broad: activities logged directly on it, PLUS
        # activities on its contacts and its deals. `Q` objects let you build
        # OR conditions; `.distinct()` removes duplicates that the OR-join can
        # produce.
        return Activity.objects.filter(Q(company=obj) | Q(contact__company=obj) | Q(deal__company=obj)).distinct()
    if isinstance(obj, Contact):
        return Activity.objects.filter(contact=obj)
    return Activity.objects.filter(deal=obj)


class RecordDetailView(DetailView):
    """Base for the single-record pages, which all show a timeline + history."""

    timeline_limit = TIMELINE_LIMIT

    def timeline(self):
        # Which activities belong on this record's timeline. One definition for
        # all three records — see timeline_for() above; this stays as a method
        # so a subclass can still narrow it.
        return timeline_for(self.object)

    def task_queryset(self):
        """
        The OPEN tasks hanging off this record, for the tasks panel (QA-06).

        TEACHING NOTE: each detail page used to show the record and nothing else,
        even right after you created a task from it — TaskCreateView bounces you
        back to the deal you came from, and that page carried no sign of the task
        you had just made. It reads as broken even when it isn't; it just never
        answered "did I already do this?".

        `due_at` is nullable, and SQL sorts NULLs first ascending, so a plain
        `order_by("due_at")` floats undated tasks above the ones that are
        actually late — the exact opposite of what a task list should do.
        `nulls_last=True` is the fix. `select_related("assignee")` pulls the
        assignee in the same query instead of one more per row (the N+1 trap).
        """
        return Task.objects.none()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        # Hand the template pre-computed permission answers so it can show or hide
        # the Edit/Delete/log-activity controls. (The server still enforces them;
        # this just avoids showing buttons that would 403.)
        ctx["can_edit"] = can_edit(user, self.object)
        ctx["can_delete"] = can_delete(user, self.object)
        ctx["can_log_activity"] = can_log_activity(user)
        # `select_related` pulls the linked author/contact/deal in the SAME query
        # via a JOIN, instead of firing a new query for each row when the template
        # loops. That's the fix for the classic "N+1 queries" performance trap.
        ctx["activities"] = self.timeline().select_related("author", "contact", "deal")[: self.timeline_limit]
        ctx["activity_form"] = ActivityForm()
        # A little (model_name, pk) tuple the template uses to build the
        # "log activity" POST URL for this exact record.
        ctx["activity_target"] = (self.model._meta.model_name, self.object.pk)
        ctx["history"] = history_entries(self.object)  # the "who changed what" list

        # The tasks panel — same shape on all three detail pages.
        #
        # TEACHING NOTE: `task_rows` is a list of small dicts, not the raw
        # queryset, because the template has to answer two questions per row —
        # "may this viewer complete or edit THIS task" and "is it overdue" — and
        # a template cannot call can_edit() or compare a date against now.
        # Deciding both here is one pass, and it keeps the permission answer in
        # Python where it can be tested. `open_tasks` stays a plain queryset as
        # well, for anything that wants the rows without the flags.
        open_tasks = self.task_queryset()
        now = timezone.now()
        ctx["open_tasks"] = open_tasks
        ctx["task_rows"] = [
            {"task": t, "editable": can_edit(user, t), "overdue": t.due_at is not None and t.due_at < now}
            for t in open_tasks
        ]
        ctx["can_add_task"] = can_create(user)
        ctx["task_target"] = (self.model._meta.model_name, self.object.pk)
        return ctx


# ---------------------------------------------------------------- dashboard

class DashboardView(TemplateView):
    """The landing page: headline numbers, pipeline metrics, and recent activity."""

    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        # One scope for every deal figure on this page (core/scope.py): Reps see
        # their own, Viewers the team, Admins whichever they've switched to. It
        # used to be scoped here for nobody and on the board for everybody, so a
        # Rep's dashboard and their board quoted different numbers (QA-04).
        deals = scoped_deals(self.request)
        open_deals = deals.filter(stage__in=OPEN_STAGES)
        ctx["counts"] = {
            "companies": Company.objects.count(),
            "contacts": Contact.objects.count(),
            "open_deals": open_deals.count(),
            "pipeline_value": open_deals.aggregate(total=Sum("value"))["total"] or 0,
        }
        ctx["my_deals"] = open_deals.filter(owner=user).select_related("company")[:10]
        ctx["recent_activity"] = Activity.objects.select_related("author", "company", "contact", "deal")[:15]

        # Phase 2 metrics
        now = timezone.now()
        # Pipeline value by stage (for the bar chart)
        pipeline_by_stage = {}
        # QA-03: this walks OPEN_STAGES, not every Stage. It was reading `open_deals`
        # while looping all six, so the Won and Lost rows were filtered out of the
        # queryset before they were ever counted — two rows that could only ever
        # say "0 deals $0", sitting under a chart titled "Pipeline by stage". Closed
        # deals have their own panel: win rate, below.
        for stage_value in OPEN_STAGES:
            stage_deals = open_deals.filter(stage=stage_value)
            pipeline_by_stage[stage_value] = {
                "label": Stage(stage_value).label,
                "count": stage_deals.count(),
                "value": stage_deals.aggregate(total=Sum("value"))["total"] or 0,
            }
        ctx["pipeline_by_stage"] = pipeline_by_stage

        # Deals closing this month
        # `expected_close` is a plain date, so compare it with plain dates, and
        # take them from the LOCAL calendar. With UTC's date, from 7 pm on the
        # last day of a month this showed next month's deals (REVIEW-25).
        today = timezone.localdate(now)
        month_start = today.replace(day=1)
        if today.month == 12:
            month_end = month_start.replace(year=today.year + 1, month=1)
        else:
            month_end = month_start.replace(month=today.month + 1)
        ctx["closing_this_month"] = open_deals.filter(
            expected_close__gte=month_start, expected_close__lt=month_end
        ).select_related("company").order_by("expected_close")

        # Win rate over the last 90 days
        # Scoped like everything else, so it answers "how am I doing" about the
        # same set of deals the pipeline value above is counting. A Rep's win
        # rate is their own closed deals; leaving it on the whole table scored
        # them against colleagues they can't see.
        cutoff = now - timedelta(days=90)
        closed_deals = deals.filter(closed_at__gte=cutoff)
        won = closed_deals.filter(stage=Stage.WON).count()
        total_closed = closed_deals.count()
        ctx["win_rate"] = round(won / total_closed * 100) if total_closed else 0
        ctx["won_count"] = won
        ctx["closed_count"] = total_closed

        # Activities per rep this week
        week_start = now - timedelta(days=7)
        ctx["activities_this_week"] = Activity.objects.filter(
            occurred_at__gte=week_start
        ).count()

        # My overdue tasks (local midnight — see core/dates.py)
        today_start = local_today_start(now)
        ctx["my_overdue_tasks"] = Task.objects.filter(
            assignee=user, status=Task.Status.OPEN, due_at__lt=today_start
        ).select_related("company", "deal").order_by("due_at")[:5]
        ctx["overdue_count"] = Task.objects.filter(
            assignee=user, status=Task.Status.OPEN, due_at__lt=today_start
        ).count()

        # So the page can label its numbers, and so the switch is only drawn for
        # the one role allowed to use it.
        ctx["scope"] = deal_scope(self.request)
        ctx["can_switch_scope"] = can_switch_scope(user)
        return ctx


# ---------------------------------------------------------------- companies
# TEACHING NOTE: notice how little each concrete view needs. All the machinery
# lives in the base classes above; here we just wire up the model, form, template,
# and the sort allowlist. This is the payoff of building the bases first.

class CompanyListView(RecordListView):
    model = Company
    filterset_class = CompanyFilter
    template_name = "core/company_list.html"
    # left side = the ?sort= value in the URL; right side = the real, safe column.
    sort_fields = {"name": "name", "lifecycle": "lifecycle", "owner": "owner__username", "updated": "updated_at"}
    default_sort = "name"
    # QA-11: id and name first, because that is how anyone opening the file tells
    # rows apart, and tags last because they're a label rather than an identity.
    # The order mirrors the columns on the screen it was exported from.
    export_fields = [
        "id", "name", "domain", "website", "phone", "industry", "lifecycle",
        "owner", "source", "address", "notes", "external_id", "created_at", "updated_at", "tags",
    ]

    def base_queryset(self):
        # Pre-fetch the owner (one-to-one-ish JOIN) and tags (separate batched
        # query) so the list template doesn't fire a query per row.
        return Company.objects.select_related("owner").prefetch_related("tags")


class CompanyDetailView(RecordDetailView):
    """
    One company's page: its fields, plus the broadest timeline in the app — the
    company's own entries and those of its contacts and deals.
    """
    model = Company
    template_name = "core/company_detail.html"

    def get_queryset(self):
        return Company.objects.select_related("owner", "source").prefetch_related("tags")

    def timeline(self):
        # The company timeline is the broad one — its own entries plus its
        # contacts' and its deals'. See timeline_for() for why that lives in
        # one place rather than three.
        return timeline_for(self.object)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # reverse of Contact.company (related_name="contacts"). `prefetch_related`
        # because the template prints each contact's roles: without it that is
        # one extra query PER CONTACT, on the page where a rep reads the whole
        # buying committee at once.
        ctx["contacts"] = self.object.contacts.prefetch_related("roles")
        ctx["deals"] = self.object.deals.select_related("owner")
        return ctx

    def task_queryset(self):
        # QA-06: a task on one of the company's contacts, or on one of its deals,
        # counts as the company's. Same breadth as `timeline()` above, and for
        # the same reason — the company is the account, and the account page is
        # where you look to find out what's outstanding.
        c = self.object
        return (
            Task.objects.filter(Q(company=c) | Q(contact__company=c) | Q(deal__company=c), status=Task.Status.OPEN)
            .select_related("assignee")
            .order_by(F("due_at").asc(nulls_last=True), "created_at")
            .distinct()
        )


class DuplicateCheckMixin:
    """
    Before saving a record, check whether we already have it, and ask first.

    TEACHING NOTE: the check runs in `form_valid`, i.e. on the real POST, after
    the form has cleaned the input. The first version checked the URL's query
    string on a GET, which no browser ever sends when you fill in a form, so
    the warning never fired. When a likely duplicate turns up, the same form
    re-renders with the user's input intact, a link to the existing record, and
    a "Save anyway" button that resubmits with `confirm_duplicate=1`.

    QA-09: this used to guard creation only, which meant an EDIT could walk a
    company straight onto a domain somebody else already owned with no warning
    at all — and once two companies shared a domain, every later create warned
    about whichever one `.first()` happened to return. Guarding edits is the
    whole fix, and it brings one new hazard with it: a record is never its own
    duplicate. Every lookup therefore goes through `_exclude_self`, or renaming
    a company that already held its domain would warn about itself forever.
    """

    def find_duplicate(self, instance):
        return None

    def _exclude_self(self, queryset):
        """
        Take the record being edited out of its own duplicate lookup.

        TEACHING NOTE: `self.object` is None on create and set on edit, so this
        is one method covering both. Excluding by pk before `.first()` matters:
        filtering after would just drop the wrong record if the edit target ever
        sorted ahead of the real duplicate.
        """
        obj = getattr(self, "object", None)
        if obj is not None and obj.pk:
            return queryset.exclude(pk=obj.pk)
        return queryset

    def form_valid(self, form):
        if not self.request.POST.get("confirm_duplicate"):
            existing = self.find_duplicate(form.instance)
            if existing is not None:
                return self.render_to_response(self.get_context_data(form=form, duplicate=existing))
        return super().form_valid(form)


class CompanyCreateView(DuplicateCheckMixin, RecordCreateView):
    model = Company
    form_class = CompanyForm

    def find_duplicate(self, company):
        # Same domain is the strong signal. With no domain, fall back to name.
        # QA-09: the domain falls back to the website, exactly as Company.save
        # will, so a company entered with only a website is checked on the
        # domain it is about to be stored with instead of quietly slipping past
        # the name match — that was the create-page half of the finding.
        domain = (company.domain or "").strip().lower() or domain_from_website(company.website)
        if domain:
            return self._exclude_self(Company.objects.filter(domain=domain)).first()
        return self._exclude_self(Company.objects.filter(name__iexact=company.name.strip())).first()


class CompanyUpdateView(DuplicateCheckMixin, RecordUpdateView):
    model = Company
    form_class = CompanyForm

    # QA-09: the edit page runs the identical signal to the create page. This is
    # the same function object rather than a second copy on purpose — two
    # hand-written copies of a duplicate rule is exactly how they end up
    # disagreeing, and only one of them gets fixed.
    find_duplicate = CompanyCreateView.find_duplicate


class CompanyDeleteView(RecordDeleteView):
    model = Company
    # `reverse_lazy` (not `reverse`) because this runs at class-definition time,
    # before the URL routes are fully loaded; "lazy" defers the lookup until it's
    # actually needed.
    success_url = reverse_lazy("company_list")


# ---------------------------------------------------------------- contacts

class ContactListView(RecordListView):
    model = Contact
    filterset_class = ContactFilter
    template_name = "core/contact_list.html"
    sort_fields = {"name": "last_name", "company": "company__name", "lifecycle": "lifecycle", "updated": "updated_at"}
    default_sort = "name"
    # QA-11: who they are, then how to reach them, then which company they hang
    # off — the order the detail page reads in. Roles sit next to tags, the two
    # labels that describe a contact rather than a deal.
    export_fields = [
        "id", "first_name", "last_name", "email", "phone", "title", "company", "lifecycle",
        "owner", "source", "notes", "external_id", "created_at", "updated_at", "tags", "roles",
    ]

    def base_queryset(self):
        return Contact.objects.select_related("company", "owner").prefetch_related("tags", "roles")


class ContactDetailView(RecordDetailView):
    """
    One contact's page: their fields, their timeline, their open tasks and the
    deals they are the primary contact on.
    """
    model = Contact
    template_name = "core/contact_detail.html"

    def get_queryset(self):
        return Contact.objects.select_related("company", "owner", "source").prefetch_related("tags", "roles")

    def timeline(self):
        return timeline_for(self.object)

    def task_queryset(self):
        # QA-06: a contact's own tasks. A task on the CONTACT, not one on their
        # company or their deals — those are visible on those pages.
        return (
            Task.objects.filter(contact=self.object, status=Task.Status.OPEN)
            .select_related("assignee")
            .order_by(F("due_at").asc(nulls_last=True), "created_at")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["deals"] = self.object.deals.select_related("company")
        return ctx


class ContactCreateView(DuplicateCheckMixin, RecordCreateView):
    """
    New contact, with a soft duplicate warning on a same-name match and the
    company pre-filled when the page was opened from a company.
    """
    model = Contact
    form_class = ContactForm

    def find_duplicate(self, contact):
        # A repeated email is already refused outright by the unique constraint
        # (the form shows that error itself), so the soft check is same name.
        return Contact.objects.filter(
            first_name__iexact=contact.first_name.strip(), last_name__iexact=(contact.last_name or "").strip()
        ).first()

    def get_initial(self):
        # Pre-fill the company dropdown when you click "Add contact" from a
        # company page (the company id rides in on ?company=5). `.isdigit()`
        # guards against junk in the URL.
        initial = super().get_initial()
        company = self.request.GET.get("company")
        if company and company.isdigit():
            initial["company"] = company
        return initial


class ContactUpdateView(RecordUpdateView):
    model = Contact
    form_class = ContactForm


class ContactDeleteView(RecordDeleteView):
    model = Contact
    success_url = reverse_lazy("contact_list")


# ---------------------------------------------------------------- deals

class DealListView(RecordListView):
    model = Deal
    filterset_class = DealFilter
    template_name = "core/deal_list.html"
    sort_fields = {
        "name": "name", "company": "company__name", "stage": "stage",
        "value": "value", "close": "expected_close", "updated": "updated_at",
    }
    default_sort = "-updated"
    # QA-11: the money and the stage are the reason anyone exports this file, so
    # they sit in the first screenful. A deal has no tags.
    export_fields = [
        "id", "name", "company", "primary_contact", "stage", "value", "probability",
        "expected_close", "owner", "lost_reason", "stage_changed_at", "closed_at",
        "created_at", "updated_at",
    ]

    def base_queryset(self):
        return Deal.objects.select_related("company", "owner")


class DealDetailView(RecordDetailView):
    model = Deal
    template_name = "core/deal_detail.html"

    def get_queryset(self):
        return Deal.objects.select_related("company", "primary_contact", "owner")

    def timeline(self):
        return timeline_for(self.object)

    def task_queryset(self):
        # QA-06: the tasks on THIS deal only.
        return (
            Task.objects.filter(deal=self.object, status=Task.Status.OPEN)
            .select_related("assignee")
            .order_by(F("due_at").asc(nulls_last=True), "created_at")
        )


class DealCreateView(RecordCreateView):
    model = Deal
    form_class = DealForm

    def get_initial(self):
        initial = super().get_initial()
        company = self.request.GET.get("company")
        if company and company.isdigit():
            initial["company"] = company
        return initial


class DealUpdateView(RecordUpdateView):
    model = Deal
    form_class = DealForm


class DealDeleteView(RecordDeleteView):
    model = Deal
    success_url = reverse_lazy("deal_list")


# ---------------------------------------------------------------- activities
# These two are plain FUNCTION views, not classes. Logging and deleting a single
# activity is small and a bit custom (it talks HTMX), so a function is clearer
# than bending a CBV to fit.

# Maps the URL's {target} segment to a model. Whitelisting like this means a URL
# such as /activities/add/user/1/ can't be tricked into touching a model we
# didn't intend — an unknown target just 404s.
ACTIVITY_TARGETS = {"company": Company, "contact": Contact, "deal": Deal}


@require_POST  # decorator: reject GET/HEAD; this endpoint only accepts POST (it changes data)
def activity_create(request, target, pk):
    """Log a note/call/meeting on a record. Called both normally and via HTMX."""
    if not can_log_activity(request.user):
        raise PermissionDenied
    model = ACTIVITY_TARGETS.get(target)
    if model is None:
        raise Http404
    # `get_object_or_404` fetches the parent record or raises a clean 404 if the
    # id doesn't exist — saves you writing the try/except every time.
    parent = get_object_or_404(model, pk=pk)

    # SECURITY NOTE: we build the Activity with the parent set from the URL and
    # the author set to the logged-in user, THEN bind the form. The form only
    # controls kind/body/when. This makes it impossible for the browser to
    # reassign the activity to another record or forge a different author.
    instance = Activity(author=request.user, **{target: parent})
    if target in ("contact", "deal") and parent.company_id:
        instance.company_id = parent.company_id  # also link to the parent's company for the company timeline
    form = ActivityForm(request.POST, instance=instance)

    if form.is_valid():
        activity = form.save()
        # TEACHING NOTE — HTMX. HTMX lets a page update a small piece of itself
        # without a full reload: the browser POSTs, and the server returns just
        # the HTML fragment for that piece. We detect HTMX by its "HX-Request"
        # header. If absent (JS off, or a plain form post), we fall back to a
        # normal redirect. Same endpoint, graceful either way.
        #
        # QA-02: the fragment is now the WHOLE sorted timeline, not the single
        # new entry, and the form swaps it in with innerHTML instead of
        # afterbegin. It used to return one `<li>` and let the browser push it to
        # the top, which meant a call logged today but dated last week landed
        # above today's note — a browser-side insertion order overriding a
        # server-side date sort. Sending the list back means the browser has
        # nothing to decide, and `new_id` is the only thing it needs to
        # highlight.
        if request.headers.get("HX-Request"):
            return render(
                request,
                "core/partials/timeline_items.html",
                {
                    "activities": timeline_for(parent)
                    .select_related("author", "contact", "deal")[:TIMELINE_LIMIT],
                    "new_id": activity.pk,
                },
            )
        messages.success(request, "Activity logged.")
    elif request.headers.get("HX-Request"):
        # On an HTMX validation failure, send back the errors and use HTMX
        # response headers to retarget them into the error slot instead of
        # replacing the timeline.
        response = render(request, "core/partials/activity_errors.html", {"form": form})
        response["HX-Retarget"] = "#activity-form-errors"
        response["HX-Reswap"] = "innerHTML"
        return response
    else:
        messages.error(request, "Activity not saved: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect(parent.get_absolute_url())


@require_POST
def activity_delete(request, pk):
    activity = get_object_or_404(Activity, pk=pk)
    if not can_delete(request.user, activity):
        raise PermissionDenied
    # Remember where to send a non-HTMX caller back to before we delete the row.
    parent = activity.deal or activity.contact or activity.company
    activity.delete()
    if request.headers.get("HX-Request"):
        # Returning an empty body tells HTMX to swap the item out with nothing —
        # the row vanishes from the page with no reload.
        return HttpResponse("")
    messages.success(request, "Activity removed.")
    return redirect(parent.get_absolute_url() if parent else reverse("dashboard"))


# ---------------------------------------------------------------- pipeline (kanban)

def pipeline_columns(request, stages=None):
    """
    Group the pipeline into one entry per stage: the deals in it, the raw total,
    the weighted total, and the count.

    TEACHING NOTE: this function exists so the board and the board's REFRESH can
    never disagree with each other. deal_stage_move recomputes the columns a move
    just changed and ships them back as out-of-band swaps; if that arithmetic
    were written a second time inside the move view, the two copies would drift,
    and a board that contradicts itself is worse than no board at all (PIPE-2).

    `stages` narrows the work to the columns a caller actually needs — the two a
    move touched. Pass None (the default) for the whole board. The order comes
    from the Stage enum either way, so a column never moves position because of
    which stages you happened to ask for.

    It takes the whole request, not the user, because visibility is no longer a
    property of the person alone: it is core/scope.py's answer, and that answer
    for an Admin lives in the session. Pass `request` and deal_stage_move's
    out-of-band totals come back matching the board the drag happened on —
    an Admin on "Mine" refreshes their own columns, not the whole team's. Pass
    `request.user` instead and the refresh would silently widen to the team,
    which is the one thing a move must never do (QA-04).
    """
    choices = list(Stage.choices)
    if stages is not None:
        wanted = set(stages)
        choices = [(value, label) for value, label in choices if value in wanted]
    deals = list(scoped_deals(request, Deal.objects.select_related("company", "owner")))
    columns = {}
    for stage_value, stage_label in choices:
        stage_deals = [d for d in deals if d.stage == stage_value]
        columns[stage_value] = {
            "label": stage_label,
            "deals": stage_deals,
            # The totals add up over the same deal objects the cards render
            # from, so the header can't quote a number the column disagrees with.
            "total": sum(d.value for d in stage_deals),
            "weighted": sum(d.weighted_value for d in stage_deals),
            "count": len(stage_deals),
        }
    return columns


class PipelineView(TemplateView):
    """
    The kanban pipeline board — one column per stage, deals as draggable cards.

    TEACHING NOTE: this view does NOT handle the drag-and-drop itself. It renders
    the initial board (columns + cards) as plain HTML. SortableJS (vendored in
    static/vendor/) makes the cards draggable in the browser. When a card is
    dropped into a new column, SortableJS fires an HTMX POST to deal_stage_move,
    which saves the new stage and returns the updated card HTML plus out-of-band
    swaps for the two column headers and footers the move changed. The board is
    never fully reloaded — HTMX swaps just the moved card and the two columns'
    numbers.
    """

    template_name = "core/pipeline.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # scoped_deals decides which deals this board shows — Admins whatever
        # they've switched to, Reps only their own, Viewers everything. Same
        # call the move view makes, so a refresh can't disagree with the page.
        ctx["stages"] = pipeline_columns(self.request)
        ctx["can_edit"] = can_create(self.request.user)  # only Admins and Reps can move deals
        ctx["scope"] = deal_scope(self.request)
        ctx["can_switch_scope"] = can_switch_scope(self.request.user)
        return ctx


@require_POST
def deal_stage_move(request, pk):
    """
    HTMX endpoint: move a deal to a new stage (kanban drag-and-drop).

    SECURITY NOTE: this is the most important new endpoint in Phase 2. A Rep
    could drag a deal they don't own into a new stage — that's an IDOR. We check
    can_edit() before saving, same as the edit view. The stage value is validated
    against the Stage enum to prevent injection.
    """
    deal = get_object_or_404(Deal, pk=pk)
    if not can_edit(request.user, deal):
        raise PermissionDenied
    new_stage = request.POST.get("stage", "")
    if new_stage not in [s.value for s in Stage]:
        raise Http404
    # Remember where the card came from before overwriting it: the two columns
    # that need their numbers refreshed are the one it left and the one it
    # joined, and after the save that information is gone.
    old_stage = deal.stage
    deal.stage = new_stage
    if new_stage == Stage.LOST:
        # The board prompts for a reason on drop. A blank one keeps whatever
        # reason the deal already had (reopened, then lost again).
        deal.lost_reason = request.POST.get("lost_reason", "").strip() or deal.lost_reason
    elif old_stage == Stage.LOST:
        # PIPE-8: a deal that is back in the pipeline is not a lost deal, so it
        # has no reason for having been lost. Keeping the old one left "went
        # with ADT" sitting on a live opportunity — visible on the deal page, and
        # pre-filling the next prompt if the deal was ever lost again.
        deal.lost_reason = ""
    # REVIEW-07: `save()` alone skips the model's clean(), so the board used to
    # bypass "Lost needs a reason" — the one rule the edit form enforced. The
    # board is just another edit form; it runs the same validation.
    try:
        deal.full_clean()
    except ValidationError as e:
        problem = " ".join(m for msgs in e.message_dict.values() for m in msgs)
        if request.headers.get("HX-Request"):
            # 422 = "understood, but the data isn't acceptable". HTMX won't swap
            # an error response; app.js shows the message and resets the board.
            return HttpResponse(problem, status=422, content_type="text/plain; charset=utf-8")
        messages.error(request, problem)
        return redirect("pipeline")
    deal.save()
    if request.headers.get("HX-Request"):
        fragments = [
            # `can_edit` has to ride along, or the returned card renders without
            # its move form and can't be dragged again until the page reloads.
            render(request, "core/partials/deal_card.html", {"deal": deal, "can_edit": True})
        ]
        # PIPE-2: returning only the card left every column total frozen at its
        # pre-move value, so the board contradicted itself until a reload. Ship
        # the refreshed head and foot for the column the card left AND the one
        # it joined, marked hx-swap-oob so HTMX patches them into the existing
        # elements by id. Re-posting the same stage yields one set, not two
        # fragments fighting over the same id.
        # Passing `request`, not `request.user`: the scope of an Admin's board
        # lives in the session, so the refreshed totals have to be recomputed
        # through the same scope the page was rendered with (QA-04).
        columns = pipeline_columns(request, stages={old_stage, new_stage})
        for stage_value, info in columns.items():
            for partial in ("kanban_col_head", "kanban_col_foot"):
                fragments.append(
                    render(
                        request,
                        f"core/partials/{partial}.html",
                        {"stage_value": stage_value, "info": info, "oob": True},
                    )
                )
        return HttpResponse(b"".join(f.content for f in fragments))
    messages.success(request, f"Moved {deal.name} to {deal.get_stage_display()}.")
    return redirect("pipeline")


# ---------------------------------------------------------------- tasks (My Day)


def task_landing_url(task):
    """
    Where a task view should send you after a save or a delete.

    TEACHING NOTE: Task has no `get_absolute_url`, so a CreateView/UpdateView
    that doesn't override `get_success_url` raises ImproperlyConfigured right
    after a successful save. The rule is "back to the record the task hangs off
    — that's where you were when you made it — else My Day", and it is the same
    rule for create, edit and delete, so it lives here once instead of three
    times. The deal is checked before the contact before the company because
    that's the narrowest link: a task can hang off all three.
    """
    linked = task.deal or task.contact or task.company
    return linked.get_absolute_url() if linked else reverse("task_list")

class TaskListView(ListView):
    """
    The "My Day" view — tasks bucketed into overdue, today, and next 7 days.

    TEACHING NOTE: Python works out the day boundaries, and the database does
    the sorting: each bucket is its own filtered query (`due_at__lt=...`), so
    no task is loaded just to be thrown away. (An earlier version of this note
    said the bucketing happened in Python. It never did.)

    The boundaries are LOCAL midnights from core/dates.py. "Today" means the
    office's today, not UTC's, which starts at 7 pm Chicago time (REVIEW-25).
    """

    template_name = "core/task_list.html"
    context_object_name = "buckets"

    def get_queryset(self):
        user = self.request.user
        tasks = Task.objects.filter(assignee=user, status=Task.Status.OPEN).select_related(
            "company", "contact", "deal"
        )
        today_start = local_today_start()
        today_end = today_start + timedelta(days=1)
        next7_end = today_start + timedelta(days=8)
        overdue = tasks.filter(due_at__lt=today_start).order_by("due_at")
        today = tasks.filter(due_at__gte=today_start, due_at__lt=today_end).order_by("due_at")
        next7 = tasks.filter(due_at__gte=today_end, due_at__lt=next7_end).order_by("due_at")
        no_due = tasks.filter(due_at__isnull=True).order_by("created_at")
        return {
            "overdue": overdue,
            "today": today,
            "next7": next7,
            "no_due": no_due,
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["can_create"] = can_create(user)

        # QA-05: the done side of My Day. `buckets` above is filtered to
        # status=OPEN, so a task ticked by mistake used to leave the screen and
        # never come back — there was nowhere to reopen it from, because the
        # reopen toggle lived inside the loop over OPEN tasks and so could never
        # render. Seven days is long enough to notice the mistake, short enough
        # that the list stays useful.
        ctx["done_recent"] = (
            Task.objects.filter(
                assignee=user,
                status=Task.Status.DONE,
                completed_at__gte=timezone.now() - timedelta(days=7),
            )
            .select_related("company", "contact", "deal")
            .order_by("-completed_at")
        )

        # QA-06: an Admin hands out work and then has no way to see it again.
        # My Day is filtered to `assignee=user`, and no other screen listed
        # anyone's tasks, so the moment an Admin assigns a task to a Rep it
        # leaves the Admin's world entirely.
        #
        # SECURITY NOTE: `team_open` is set for Admins ONLY. A Rep's My Day must
        # never widen to other people's work, so non-Admins get an empty
        # queryset rather than a populated one the template has to remember to
        # hide.
        ctx["team_open"] = (
            Task.objects.filter(status=Task.Status.OPEN)
            .exclude(assignee=user)
            .select_related("assignee", "company", "contact", "deal")
            .order_by(F("due_at").asc(nulls_last=True), "created_at")
            if is_admin(user)
            else Task.objects.none()
        )
        return ctx


class TaskCreateView(CreateView):
    """Create a task, optionally linked to a record from its detail page."""

    model = Task
    form_class = TaskForm
    template_name = "core/form.html"

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "model_name": "task"}

    def dispatch(self, request, *args, **kwargs):
        if not can_create(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_initial(self):
        initial = super().get_initial()
        # Pre-fill the linked record from the URL (?company=5, ?contact=3, ?deal=7)
        for field in ("company", "contact", "deal"):
            val = self.request.GET.get(field)
            if val and val.isdigit():
                initial[field] = val
        return initial

    def form_valid(self, form):
        response = super().form_valid(form)
        # Fires only for a task that actually has somebody to tell. An unassigned
        # task is a to-do somebody intends to file rather than hand over, and
        # there is no bell to put it in until it has an owner.
        task_assigned(self.object, self.object.assignee)
        messages.success(self.request, f"Task created: {self.object.title}.")
        return response

    def get_success_url(self):
        return task_landing_url(self.object)


class TaskUpdateView(RecordUpdateView):
    """Edit a task (QA-05). A task with a typo in the title used to be unfixable."""

    model = Task
    form_class = TaskForm

    def get_success_url(self):
        return task_landing_url(self.object)


class TaskDeleteView(RecordDeleteView):
    """Delete a task (QA-05), for an Admin or the Rep it is assigned to.

    SECURITY NOTE: the rule is per-object and lives in `permissions.can_delete`,
    which RecordDeleteView.get_object enforces on both GET (the confirm page)
    and POST (the delete). The confirm page exists so a stray POST can't destroy
    a task silently — but it is politeness, not the lock. The lock is the
    can_delete check, which is why a Rep POSTing another Rep's task gets a 403
    whether or not they ever saw a button.
    """

    model = Task

    def get_success_url(self):
        return task_landing_url(self.object)


@require_POST
def task_complete(request, pk):
    """Mark a task done (or reopen it). HTMX endpoint for the task list."""
    task = get_object_or_404(Task, pk=pk)
    if not can_edit(request.user, task):
        raise PermissionDenied
    if task.status == Task.Status.DONE:
        task.status = Task.Status.OPEN
    else:
        task.status = Task.Status.DONE
    task.save()
    if request.headers.get("HX-Request"):
        return HttpResponse("")
    messages.success(request, f"Task {'reopened' if task.status == Task.Status.OPEN else 'completed'}: {task.title}.")
    return redirect(task_next_url(request))


def task_next_url(request):
    """
    Where to send the browser after completing a task, honouring a `next`.

    QA-06: the complete checkbox now also appears on company / contact / deal
    pages, and dropping someone back on My Day — away from the record they were
    looking at — made the new panel feel broken. The form carries the page it was
    rendered on in a hidden `next`, and this returns it.

    SECURITY NOTE: `next` is whatever the POST body says, which makes it user
    input, and `redirect()` to an attacker-controlled URL is the textbook open
    redirect: a link that starts on your own domain and bounces off it is the
    best kind of phishing, because the victim never sees the other host.
    `url_has_allowed_host_and_scheme` is Django's own answer to that — it
    accepts only a path on this host, and rejects the `//evil.example` and
    `https://evil.example` shapes as well as plain absolute URLs. Anything it
    says no to falls back to My Day, so a hand-crafted `next` can only ever
    cost you the return trip, never send you off-site.
    """
    target = request.POST.get("next")
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return reverse("task_list")


# ---------------------------------------------------------------- global search

# How many results of each type one search may return. The cap is the right call
# — "a" matches 4,000 companies in a database this size and nobody reads row
# 400 — but a silent cap is a lie about the answer, so the page quotes the true
# count alongside it (QA-10).
SEARCH_RESULT_CAP = 20


class GlobalSearchView(ListView):
    """
    One search box across companies, contacts, and deals.

    TEACHING NOTE: this view searches three models at once and merges the results
    into a single list. Each result type gets a different icon/label in the
    template so the user can tell them apart at a glance.

    QA-10: each type is capped at SEARCH_RESULT_CAP so a common word can't pull
    back ten thousand rows. The cap used to be invisible — the heading counted
    `results|length`, i.e. what survived the cap, so 25 matches read "20 results"
    and five of them were simply gone. So: count first, then truncate, and say so
    on the page. Counting is one extra `.count()` per type, which on SQLite is
    cheaper than the rows it protects the user from being misled about.
    """

    template_name = "core/search_results.html"
    context_object_name = "results"

    def get_queryset(self):
        q = self.request.GET.get("q", "").strip()
        if not q:
            return []
        queries = {
            "Company": Company.objects.filter(
                Q(name__icontains=q) | Q(domain__icontains=q) | Q(industry__icontains=q)
            ),
            "Contact": Contact.objects.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
            ),
            "Deal": Deal.objects.filter(
                Q(name__icontains=q) | Q(company__name__icontains=q)
            ),
        }
        # The TRUE counts, before the cap. `type_counts` is what makes `total`
        # honest and `truncated` answerable at all.
        self.type_counts = {label: qs.count() for label, qs in queries.items()}
        capped = {label: qs[:SEARCH_RESULT_CAP] for label, qs in queries.items()}

        # Merge and sort by name for a unified results list
        results = []
        for c in capped["Company"]:
            results.append({"type": "Company", "obj": c, "name": c.name, "url": c.get_absolute_url()})
        for c in capped["Contact"]:
            results.append({"type": "Contact", "obj": c, "name": c.full_name, "url": c.get_absolute_url()})
        for d in capped["Deal"]:
            results.append({"type": "Deal", "obj": d, "name": d.name, "url": d.get_absolute_url()})
        results.sort(key=lambda r: r["name"].lower())
        return results

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["query"] = self.request.GET.get("q", "").strip()
        # An empty query short-circuits get_queryset before type_counts is ever
        # built, so it defaults rather than raising AttributeError.
        counts = getattr(self, "type_counts", {})
        ctx["type_counts"] = counts
        ctx["total"] = sum(counts.values())
        ctx["truncated"] = any(count > SEARCH_RESULT_CAP for count in counts.values())
        ctx["cap"] = SEARCH_RESULT_CAP
        return ctx


# ---------------------------------------------------------------- merge duplicates

def _repoint(queryset, **changes):
    """
    Move related rows to the survivor ONE save() at a time.

    TEACHING NOTE: `queryset.update(company=survivor)` is one fast SQL statement,
    and that's the problem: it never calls save(), so django-simple-history never
    hears about it. The merge used to leave no trace on any of the contacts,
    deals, activities, or tasks it moved (REVIEW-08). A merge touches tens of
    rows, not millions; being slower and on the record is the right trade.
    """
    moved = 0
    for obj in queryset:
        for field_name, value in changes.items():
            setattr(obj, field_name, value)
        obj.save()
        moved += 1
    return moved


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


class MergeView(TemplateView):
    """
    Merge the record on this page INTO a survivor the Admin picks — Admin only.

    TEACHING NOTE: merging is destructive, so the rules are strict:
      - The page's record is always the one merged away. The survivor comes from
        the form. (The old version also read a `loser_id` from the POST, which a
        hand-made request could set to the survivor itself.)
      - A record can't be merged into itself; that used to delete it outright.
      - Children are re-pointed with save(), so the history log sees every move.
      - Tags are combined, notes are appended, and blank fields on the survivor
        are filled from the loser, so the merge doesn't quietly throw data away.
      - A note on the survivor's timeline records what was merged, by whom.
    All of it runs in one transaction: it either all happens or none of it does.

    SECURITY NOTE: only Admins can merge. A Rep merging companies could
    reassign deals they don't own, which is a privilege escalation.
    """

    model = None
    fill_fields = ()  # plain fields copied onto the survivor when it has none

    def dispatch(self, request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_record(self):
        return get_object_or_404(self.model, pk=self.kwargs["pk"])

    def duplicates_of(self, record):
        raise NotImplementedError

    def move_children(self, loser, survivor):
        """Re-point everything hanging off `loser`. Returns {"contact": 3, ...}."""
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        record = self.get_record()
        ctx[self.model._meta.model_name] = record
        ctx["duplicates"] = self.duplicates_of(record)
        return ctx

    def post(self, request, *args, **kwargs):
        loser = self.get_record()
        """
        Do the merge. The steps and their order are in the class docstring; the one
        order that matters in the code is deleting the loser before copying fields,
        because a contact's email is unique.
        """
        survivor_id = request.POST.get("survivor_id", "")
        if not survivor_id.isdigit():
            messages.error(request, "Pick which record should survive.")
            return redirect(request.path)
        survivor = get_object_or_404(self.model, pk=survivor_id)
        if survivor.pk == loser.pk:
            messages.error(request, "A record can't be merged into itself.")
            return redirect(request.path)

        loser_label, loser_pk = str(loser), loser.pk
        with transaction.atomic():
            moved = self.move_children(loser, survivor)
            survivor.tags.add(*loser.tags.all())
            if loser.notes.strip():
                survivor.notes = f"{survivor.notes}\n\n[Merged from {loser_label}]\n{loser.notes}".strip()
            carried = {f: getattr(loser, f) for f in self.fill_fields if getattr(loser, f) and not getattr(survivor, f)}
            # Delete BEFORE copying fields across: a contact's email is unique,
            # so the survivor can only take it once the loser no longer holds it.
            loser.delete()
            for field_name, value in carried.items():
                setattr(survivor, field_name, value)
            survivor.save()
            summary = ", ".join(_plural(n, what) for what, n in moved.items() if n) or "nothing attached"
            Activity.objects.create(
                kind=Activity.Kind.NOTE,
                author=request.user,
                body=f"Merged “{loser_label}” (#{loser_pk}) into this record. Moved: {summary}.",
                **{self.model._meta.model_name: survivor},
            )

        messages.success(request, f"Merged {loser_label} into {survivor}.")
        return redirect(survivor.get_absolute_url())


class CompanyMergeView(MergeView):
    model = Company
    template_name = "core/merge_companies.html"
    fill_fields = ("domain", "website", "phone", "address", "industry", "external_id", "source")

    def duplicates_of(self, company):
        # Only match on values that are actually there: `domain__iexact=""` would
        # list every company without a domain as a "duplicate".
        match = Q(name__iexact=company.name)
        if company.domain:
            match |= Q(domain__iexact=company.domain)
        return Company.objects.filter(match).exclude(pk=company.pk)

    def move_children(self, loser, survivor):
        return {
            "contact": _repoint(loser.contacts.all(), company=survivor),
            "deal": _repoint(loser.deals.all(), company=survivor),
            "activity": _repoint(Activity.objects.filter(company=loser), company=survivor),
            "task": _repoint(Task.objects.filter(company=loser), company=survivor),
        }


class ContactMergeView(MergeView):
    model = Contact
    template_name = "core/merge_contacts.html"
    fill_fields = ("email", "phone", "title", "company", "external_id", "source", "last_name")

    def duplicates_of(self, contact):
        # `email__iexact=None` compiles to `email IS NULL`, which made every
        # contact without an email a "duplicate" of every other one.
        match = Q(first_name__iexact=contact.first_name, last_name__iexact=contact.last_name)
        if contact.email:
            match |= Q(email__iexact=contact.email)
        return Contact.objects.filter(match).exclude(pk=contact.pk)

    def move_children(self, loser, survivor):
        return {
            "activity": _repoint(Activity.objects.filter(contact=loser), contact=survivor),
            "task": _repoint(Task.objects.filter(contact=loser), contact=survivor),
            "deal": _repoint(Deal.objects.filter(primary_contact=loser), primary_contact=survivor),
        }
