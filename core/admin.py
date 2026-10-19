"""
The Django admin — a free CRUD backoffice for superusers.

TEACHING NOTE: registering a model here (`@admin.register(Company)`) instantly
gives you searchable, filterable, editable admin pages for it — no views or
templates to write. It's a huge productivity win for internal management and for
poking at data while you build. It is NOT the product, though: this app builds
its own user-facing UI with proper role permissions, and the admin stays locked
to superusers. `SimpleHistoryAdmin` adds a "History" button to each record so you
can see and revert past versions.

The little classes below just customise each model's admin: which columns to show
(`list_display`), what the search box scans (`search_fields`), and the sidebar
filters (`list_filter`).
"""

from django.conf import settings
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Activity,
    BuyingRole,
    Company,
    Contact,
    Deal,
    ImportRun,
    LeadSource,
    Notification,
    Quote,
    QuoteLine,
    Tag,
    Task,
)


@admin.register(Company)
class CompanyAdmin(SimpleHistoryAdmin):
    list_display = ("name", "domain", "lifecycle", "owner")
    search_fields = ("name", "domain")
    list_filter = ("lifecycle",)


@admin.register(Contact)
class ContactAdmin(SimpleHistoryAdmin):
    list_display = ("full_name", "email", "company", "owner")
    search_fields = ("first_name", "last_name", "email")


@admin.register(Deal)
class DealAdmin(SimpleHistoryAdmin):
    list_display = ("name", "company", "stage", "value", "owner")
    list_filter = ("stage",)
    readonly_fields = ("stage_changed_at", "closed_at")


@admin.register(Activity)
class ActivityAdmin(SimpleHistoryAdmin):
    list_display = ("kind", "occurred_at", "author", "company", "contact", "deal")
    list_filter = ("kind",)


@admin.register(Task)
class TaskAdmin(SimpleHistoryAdmin):
    list_display = ("title", "due_at", "assignee", "status")


@admin.register(Notification)
class NotificationAdmin(SimpleHistoryAdmin):
    # TEACHING NOTE — the admin is how you answer "why does this Rep have a bell
    # badge saying 4", which is the one support question a notifications table
    # always produces. `read_at` is in `list_filter` because "unread" is the
    # interesting slice and there is no other screen in the app that shows it.
    # `user` is in `search_fields` so you can go straight to one person's mail
    # rather than scrolling the whole table.
    list_display = ("text", "user", "kind", "created_at", "read_at")
    list_filter = ("kind", "read_at")
    search_fields = ("text", "user__username")


class QuoteLineInline(admin.TabularInline):
    """
    TEACHING NOTE — the admin is the one place a quote's lines CAN be edited
    outside the app's own editor, which makes this inline load-bearing rather than
    convenience. Two consequences, both of which the app survives because the total
    is computed and never stored:

      - A line added here gets `position=0`, the model default, so it sorts to the
        front rather than where it looks like it was typed. That is why the editor
        renumbers by formset index on every save rather than trusting whatever
        numbers it finds.
      - The total on the quote page moves the moment this inline is saved, with no
        stale `total` column left behind to contradict it. That is the whole
        argument in core/quotes.py, seen from the least careful direction.

    `extra=0` because the app's own editor offers three blank rows; a fourth blank
    row here would only be somewhere for a superuser to save something empty.
    """

    model = QuoteLine
    extra = 0
    fields = ("position", "description", "quantity", "unit_price")


@admin.register(Quote)
class QuoteAdmin(SimpleHistoryAdmin):
    list_display = ("deal", "status", "valid_until", "updated_at")
    list_filter = ("status",)
    search_fields = ("deal__name", "notes")
    # One row of lines on the quote's own page: a quote is meaningless without its
    # line items, so asking for them separately is one extra click and one extra
    # chance to edit a quote and forget the lines.
    inlines = [QuoteLineInline]


@admin.register(QuoteLine)
class QuoteLineAdmin(SimpleHistoryAdmin):
    # The inline above covers the normal case. This is here for the line whose
    # quote somebody deleted a line from without noticing — findable by searching
    # for its text, which is not possible from inside the quote page once it is
    # gone.
    list_display = ("description", "quote", "position", "quantity", "unit_price")
    search_fields = ("description", "quote__deal__name")


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    # An audit log should be READ, never hand-edited — otherwise it's not much of
    # an audit log. Returning False from these two hooks makes the admin show the
    # import history but forbid adding or changing rows.
    list_display = ("source_type", "source_label", "started_at", "created_count", "updated_count", "dry_run")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(Tag)
admin.site.register(LeadSource)


@admin.register(BuyingRole)
class BuyingRoleAdmin(admin.ModelAdmin):
    # Plain ModelAdmin, not SimpleHistoryAdmin: nobody needs to audit who renamed
    # a lookup row, and this table has no history of its own.
    #
    # The list IS the ladder — `order` is a column and the list is sorted by it,
    # so adding or renumbering a role is a visible edit rather than a guess about
    # which row is 40.
    list_display = ("name", "order")
    ordering = ("order", "name")


admin.site.site_header = f"{settings.BRAND_NAME} administration"
