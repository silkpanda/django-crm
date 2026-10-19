"""
The /api/ routes. Its own file so core/urls.py does not grow another block.

TEACHING NOTE — `include()` this from core/urls.py rather than adding seven
`path()` calls there. The HTML app and the JSON app are separate surfaces
hanging off one mount point, they will grow at different rates, and a reader
who opens urls.py to find out how the site is put together should not have to
scroll past a second front door to find the first one. Every name below is
global, not namespaced — the rest of the repo does not use namespaces, so
`reverse("api_deal_list")` reads the same as `reverse("deal_list")`.

Every route is GET-only, enforced by `ApiView.http_method_names` rather than by
what is written here. A URL map cannot keep a method out on its own; the view
class is the thing that can, which is why it is the thing that does.
"""

from django.urls import path

from . import api

urlpatterns = [
    path("", api.ApiIndexView.as_view(), name="api_index"),
    path("companies/", api.CompanyListApi.as_view(), name="api_company_list"),
    path("companies/<int:pk>/", api.CompanyDetailApi.as_view(), name="api_company_detail"),
    path("contacts/", api.ContactListApi.as_view(), name="api_contact_list"),
    path("contacts/<int:pk>/", api.ContactDetailApi.as_view(), name="api_contact_detail"),
    path("deals/", api.DealListApi.as_view(), name="api_deal_list"),
    path("deals/<int:pk>/", api.DealDetailApi.as_view(), name="api_deal_detail"),
]