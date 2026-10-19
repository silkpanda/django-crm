"""
URL routes for the core app.

TEACHING NOTE: each `path()` ties a URL pattern to a view and gives it a NAME.
  - `<int:pk>` is a converter: it matches a number in the URL and passes it to
    the view as `pk` (primary key). `<str:target>` matches text. This is how
    /companies/5/ knows it means company #5.
  - `.as_view()` is how a class-based view is plugged into the URL system.
  - The `name=` is the important part: everywhere else (templates, redirects,
    get_absolute_url) refers to a route by this name, never by its literal path.
    Change a URL here and every reference keeps working. Hardcoded paths rot;
    named routes don't.
"""

from django.urls import include, path

from . import views
from . import views_import
from . import views_leads
from . import views_notifications
from . import views_quotes
from . import views_reports
from . import views_users

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),

    # The four classic CRUD routes, repeated per model: list, create, detail,
    # edit, delete. Read one block and you've read them all.
    path("companies/", views.CompanyListView.as_view(), name="company_list"),
    path("companies/new/", views.CompanyCreateView.as_view(), name="company_create"),
    path("companies/<int:pk>/", views.CompanyDetailView.as_view(), name="company_detail"),
    path("companies/<int:pk>/edit/", views.CompanyUpdateView.as_view(), name="company_update"),
    path("companies/<int:pk>/delete/", views.CompanyDeleteView.as_view(), name="company_delete"),

    path("contacts/", views.ContactListView.as_view(), name="contact_list"),
    path("contacts/new/", views.ContactCreateView.as_view(), name="contact_create"),
    path("contacts/<int:pk>/", views.ContactDetailView.as_view(), name="contact_detail"),
    path("contacts/<int:pk>/edit/", views.ContactUpdateView.as_view(), name="contact_update"),
    path("contacts/<int:pk>/delete/", views.ContactDeleteView.as_view(), name="contact_delete"),

    path("deals/", views.DealListView.as_view(), name="deal_list"),
    path("deals/new/", views.DealCreateView.as_view(), name="deal_create"),
    path("deals/<int:pk>/", views.DealDetailView.as_view(), name="deal_detail"),
    path("deals/<int:pk>/edit/", views.DealUpdateView.as_view(), name="deal_update"),
    path("deals/<int:pk>/delete/", views.DealDeleteView.as_view(), name="deal_delete"),

    # Phase 6, workstream 4 — quotes and their line items. All three are keyed on
    # the DEAL's pk, not the quote's: a deal has at most one quote (the database
    # enforces it, see Quote.deal), so the deal is a complete key, and the deal is
    # the only one of the two a URL could be built from in the first place.
    #
    # The order is the order a person uses them: read it, edit it, change what
    # state it is in. A deal with no quote yet redirects from the first to the
    # second, because the editor is also how a quote comes into existence.
    path("deals/<int:pk>/quote/", views_quotes.QuoteDetailView.as_view(), name="quote_detail"),
    path("deals/<int:pk>/quote/edit/", views_quotes.QuoteEditView.as_view(), name="quote_edit"),
    path("deals/<int:pk>/quote/status/", views_quotes.quote_status_change, name="quote_status"),

    # The HTMX activity endpoints. `target` is "company"/"contact"/"deal"; the
    # view validates it against an allowlist so a bogus target can't reach a model.
    path("activities/add/<str:target>/<int:pk>/", views.activity_create, name="activity_create"),
    path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),

    # Phase 2 — pipeline (kanban) and stage moves
    path("pipeline/", views.PipelineView.as_view(), name="pipeline"),
    path("deals/<int:pk>/move/", views.deal_stage_move, name="deal_stage_move"),

    # Phase 2 — tasks
    path("tasks/", views.TaskListView.as_view(), name="task_list"),
    path("tasks/new/", views.TaskCreateView.as_view(), name="task_create"),
    path("tasks/<int:pk>/complete/", views.task_complete, name="task_complete"),
    # QA-05: the edit and delete routes a task never had. Without them a typo in
    # a title is permanent and a task created by mistake can only be removed
    # from the Django admin. `task_update` reuses RecordUpdateView, which
    # 403s on anything the assignee isn't allowed to touch, so the permission
    # rule is inherited rather than restated.
    path("tasks/<int:pk>/edit/", views.TaskUpdateView.as_view(), name="task_update"),
    path("tasks/<int:pk>/delete/", views.TaskDeleteView.as_view(), name="task_delete"),

    # Phase 2 — global search
    path("search/", views.GlobalSearchView.as_view(), name="global_search"),

    # Phase 6 — reports. Pipeline, conversion, rep performance and forecast, all
    # scoped through the same core/scope.py call the pipeline board uses, so the
    # reports page and the board are looking at the same book.
    path("reports/", views_reports.ReportView.as_view(), name="report_index"),

    # Phase 3 — CSV import wizard. One view, four steps: the third argument to
    # `path()` is a dict of extra kwargs handed to the view, which is how the view
    # knows which step it's on. Leave it off and every URL falls back to the
    # view's default (step="upload") — which is exactly the bug REVIEW-09 was.
    path("import/", views_import.ImportWizardView.as_view(), {"step": "upload"}, name="import_wizard"),
    path("import/upload/", views_import.ImportWizardView.as_view(), {"step": "upload"}, name="import_wizard_upload"),
    path("import/map/", views_import.ImportWizardView.as_view(), {"step": "map"}, name="import_wizard_map"),
    path("import/preview/", views_import.ImportWizardView.as_view(), {"step": "preview"}, name="import_wizard_preview"),
    path("import/commit/", views_import.ImportWizardView.as_view(), {"step": "commit"}, name="import_wizard_commit"),

    # Phase 3 — merge duplicates
    path("companies/<int:pk>/merge/", views.CompanyMergeView.as_view(), name="company_merge"),
    path("contacts/<int:pk>/merge/", views.ContactMergeView.as_view(), name="contact_merge"),

    # Phase 5 — sales-lead intake. Two doors onto the same function: the desk
    # form for staff and the public form for the website, plus the queue and the
    # assignment endpoint that follow a lead through to an owner.
    #
    # TEACHING NOTE — the two PUBLIC routes are the exception to
    # `LoginRequiredMiddleware`, which is on globally and sends everybody else to
    # /accounts/login/. They opt out with `@login_not_required` (see
    # views_leads.py), which is the ONLY supported way to do it; a view cannot
    # opt out by leaving `login_required` alone, because the middleware's
    # default is to require it.
    path("leads/new/", views_leads.lead_create, name="lead_create"),
    path("leads/submit/", views_leads.lead_submit, name="lead_submit"),
    path("leads/thanks/", views_leads.lead_thanks, name="lead_thanks"),
    # The queue and `claim` are Admins and Reps, `assign` is Admin only —
    # enforced in the views, not here. The two static segments are listed before
    # the pk ones so the file reads in the order a person uses it: look at the
    # queue, pick one up, or hand one to somebody else. —
    path("leads/", views_leads.lead_queue, name="lead_queue"),
    # The Company box's suggestions. A static segment listed before the pk ones
    # for the same reason as the two above it: `leads/companies/` cannot parse as
    # an int, so order does not actually decide anything here, but a reader
    # scanning this list should not have to prove that.
    path("leads/companies/", views_leads.company_suggestions, name="lead_company_suggestions"),
    path("leads/<int:pk>/claim/", views_leads.lead_claim, name="lead_claim"),
    path("leads/<int:pk>/assign/", views_leads.lead_assign, name="lead_assign"),

    # Phase 6 — notifications. The static `read-all/` segment is listed BEFORE the
    # `<int:pk>` one for the same reason the statics in `leads/` are: the file
    # should read in the order a person uses it. (Here it happens not to matter —
    # "read-all" cannot parse as an int — but the convention is worth keeping for
    # the day somebody adds a sibling like "unread/" that could.)
    path("notifications/", views_notifications.NotificationListView.as_view(), name="notification_list"),
    path("notifications/read-all/", views_notifications.notification_mark_all_read, name="notification_read_all"),
    path("notifications/<int:pk>/read/", views_notifications.notification_read, name="notification_read"),

    # User management. Admin-only, enforced in views_users.py. Users are
    # deactivated, never deleted, so there is deliberately no delete route.
    path("settings/users/", views_users.UserListView.as_view(), name="user_list"),
    path("settings/users/new/", views_users.UserCreateView.as_view(), name="user_create"),
    path("settings/users/<int:pk>/edit/", views_users.UserUpdateView.as_view(), name="user_update"),
    path("settings/users/<int:pk>/toggle/", views_users.user_toggle_active, name="user_toggle_active"),

    # Phase 6, workstream 3 — the read-only JSON API. One `include()` rather
    # than seven more `path()` lines: the API is a second front door with its own
    # url module (core/api_urls.py), and it is mounted here last because nothing
    # above it competes for the "api/" prefix. GET-only is enforced in the view
    # class, not here — see core/api.py.
    path("api/", include("core.api_urls")),
]
