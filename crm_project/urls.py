"""
The ROOT URL map — the app's switchboard.

TEACHING NOTE: when a request comes in, Django walks this list top to bottom and
uses the FIRST pattern whose path matches, handing off to that view (or, via
`include`, to another urls.py). This is the routing layer: it decides which code
runs for which URL. Keeping app-specific routes in each app's own urls.py (here,
core/urls.py) and just `include`-ing them keeps this file short and the apps
self-contained.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),  # Django's built-in backoffice at /admin/
    # Django ships ready-made login/logout/password views. Mounting them at
    # /accounts/ gives us /accounts/login/, /accounts/password_reset/, etc. for free.
    path("accounts/", include("django.contrib.auth.urls")),
    # Everything else is our app. "" means these hang off the site root (/companies/...).
    path("", include("core.urls")),
]
