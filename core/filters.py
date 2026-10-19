"""
List-page filters, built with the `django-filter` library.

TEACHING NOTE: a FilterSet turns URL query parameters into database filtering.
Declare a filter named `owner`, and django-filter reads ?owner=3 from the URL,
renders a dropdown for it, and narrows the queryset — you don't write that
plumbing yourself. It's the same idea as a ModelForm, but for searching instead
of editing.
"""

import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import BuyingRole, Company, Contact, Deal, Lifecycle, Stage, Tag

User = get_user_model()


class SearchFilterSet(django_filters.FilterSet):
    """A base that adds one free-text search box (`q`) across several columns."""

    q = django_filters.CharFilter(method="search", label="Search")
    search_fields = ()  # each subclass lists which columns the box should scan

    def search(self, queryset, name, value):
        # `method="search"` above points here. We build one big OR across the
        # chosen columns: match the text in the name OR the domain OR the phone...
        # `icontains` is a case-insensitive "contains" (SQL LIKE). The ORM
        # parameterises the value, so this is safe from SQL injection — you're
        # never string-building a query.
        value = value.strip()
        if not value:
            return queryset
        cond = Q()
        for field in self.search_fields:
            cond |= Q(**{f"{field}__icontains": value})
        return queryset.filter(cond)


def active_users(request):
    # A callable queryset so the owner dropdown always reflects current active
    # users. django-filter calls this per request.
    return User.objects.filter(is_active=True).order_by("username")


class CompanyFilter(SearchFilterSet):
    search_fields = ("name", "domain", "industry", "phone")
    owner = django_filters.ModelChoiceFilter(queryset=active_users)
    lifecycle = django_filters.ChoiceFilter(choices=Lifecycle.choices)
    tags = django_filters.ModelChoiceFilter(queryset=Tag.objects.all(), label="Tag")

    class Meta:
        model = Company
        fields = ["q", "owner", "lifecycle", "tags"]


class ContactFilter(SearchFilterSet):
    """
    The contacts list's search box and dropdowns: free-text search (including the
    company's name), owner, lifecycle, tag and buying role.
    """
    # Note "company__name": the double underscore follows the foreign key to the
    # related Company and searches ITS name — the ORM's way of walking relations.
    search_fields = ("first_name", "last_name", "email", "phone", "title", "company__name")
    owner = django_filters.ModelChoiceFilter(queryset=active_users)
    lifecycle = django_filters.ChoiceFilter(choices=Lifecycle.choices)
    tags = django_filters.ModelChoiceFilter(queryset=Tag.objects.all(), label="Tag")
    # `field_name="roles"` points the filter at the many-to-many rather than a
    # field called `role`, so ?role=3 means "contacts holding THAT role".
    #
    # TEACHING NOTE: the dropdown inherits BuyingRole.Meta.ordering, so it lists
    # the seven in the order a sale walks them. `queryset` is a lazy queryset
    # like Tag's, evaluated per request — a role added in the admin appears here
    # with no code change.
    role = django_filters.ModelChoiceFilter(
        queryset=BuyingRole.objects.all(), field_name="roles", label="Buying role"
    )

    class Meta:
        model = Contact
        fields = ["q", "owner", "lifecycle", "tags", "role"]


class DealFilter(SearchFilterSet):
    search_fields = ("name", "company__name")
    owner = django_filters.ModelChoiceFilter(queryset=active_users)
    stage = django_filters.ChoiceFilter(choices=Stage.choices)

    class Meta:
        model = Deal
        fields = ["q", "owner", "stage"]
