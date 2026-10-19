"""
Hardened settings — what the app would look like in production.

TEACHING NOTE — the "base + override" pattern. This file does
`from .settings import *` to inherit EVERYTHING in the dev settings, then
overrides just the handful of values that must be stricter when the app faces the
real internet. Two benefits: the shared stuff (apps, database, models) can never
drift between environments, and you can read this short file to see exactly what
changes for production. The security gate runs `check --deploy` against THIS
module to confirm the hardening is real.

Run it with:  DJANGO_SETTINGS_MODULE=crm_project.settings_prodlike
"""

from .settings import *  # noqa: F401,F403  (star-import is the whole point here)

# Never show the detailed debug error pages in production — they leak source,
# settings, and stack traces to anyone who triggers an error.
DEBUG = False

# "Secure" = these cookies are only ever sent over HTTPS, so they can't be
# sniffed on an open network. Off in dev (plain http), on here.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Tighten session lifetime: log out when the browser closes, and cap any session
# at 8 hours (a work day) even if the browser stays open.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_AGE = 60 * 60 * 8

# HSTS tells browsers "only ever talk to me over HTTPS, for the next 30 days."
# It defends against downgrade attacks. Only safe once you're truly serving HTTPS.
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Redirect any plain-http request to https. Toggleable via .env so a staging box
# without a certificate can turn it off.
SECURE_SSL_REDIRECT = env("DJANGO_SSL_REDIRECT", "1") == "1"  # noqa: F405

# Serve static files with a content hash in the filename (QA-07).
#
# TEACHING NOTE — why hashing fixes "the browser keeps running last week's
# app.js". A bare URL like /static/js/app.js is a fixed address: the file behind
# it changes on every deploy, but the address doesn't, so the browser has no
# reason to go back and ask. With ManifestStaticFilesStorage the name IS the
# hash of the contents — app.<hash>.js — so a changed file is a brand new URL and
# the old one is simply never requested again. Every release gets its own URL.
#
# Two things this depends on, both now in the README's deploy notes:
#   - `collectstatic` has to run on EVERY deploy. The manifest is built while
#     collecting; without it, template rendering raises "Missing staticfiles
#     manifest entry".
#   - the web server has to serve STATIC_ROOT, not the source static/ dir.
#
# NOTE the `{**BASE, ...}` spread rather than a fresh dict. STORAGES has TWO
# keys, and replacing the whole mapping silently drops "default" — the storage
# for user file uploads. The symptom isn't a crash at boot, it's a FileField
# quietly saving somewhere new at the next deploy. Base settings don't define a
# STORAGES at all, so fall back to Django's own defaults for "default" to
# survive. Only "staticfiles" changes; dev keeps the unhashed backend, because
# runserver serves straight off disk and has no manifest to read.
from django.conf import global_settings

STORAGES = {
    **globals().get("STORAGES", global_settings.STORAGES),
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
