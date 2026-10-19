"""
Django settings for this internal CRM (development defaults).

TEACHING NOTE: this file is the project's control panel — which apps are on, how
the database is reached, security options, and so on. Django imports it once at
startup. `settings_prodlike.py` imports everything from here and then overrides a
few values to be stricter; that's the "base + override" pattern, so the two
environments can't drift apart on the things they share.

Big idea below: SECRETS DON'T LIVE IN CODE. Things like the secret key come from
a .env file (which is gitignored) via the `env()` helper, not hardcoded here.
Anyone who reads the source must not thereby learn the keys.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# BASE_DIR is the project root (two folders up from this file). Building paths
# from it with `/` (pathlib) keeps the app working on any OS.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # read the .env file into environment variables


def env(name, default=None, required=False):
    # Small helper: read a setting from the environment, with an optional default
    # and a "blow up loudly at startup if this is missing" flag. Failing fast
    # beats a confusing error deep in a request later.
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"{name} must be set in .env")
    return value


# TEACHING NOTE — BRANDING IS CONFIGURATION, NOT CODE. Everything the shell says
# about who this belongs to lives here and comes from .env, so a downstream fork
# changes its name, wordmark, footer and palette by editing one file. The defaults
# are the neutral ones: the app with no .env branding block says "CRM" and nothing
# else. `core/context_processors.brand` reads this and the templates render it, so
# there is no literal brand string anywhere in the templates or the CSS.
#
# BRAND_COLORS are the values baked into static/css/tokens.css. Setting them here
# emits a `:root{...}` override after that stylesheet loads, which is why a fork
# needs no CSS edit either. Same order as tokens.css; the context processor
# serialises them in that fixed order.
BRAND_NAME = env("BRAND_NAME", "CRM")
BRAND_WORDMARK = env("BRAND_WORDMARK", "CRM")
BRAND_WORDMARK_ACCENT = env("BRAND_WORDMARK_ACCENT", "")
BRAND_FOOTER = env("BRAND_FOOTER", "internal")
BRAND_EMAIL_FROM = env("BRAND_EMAIL_FROM", f"{BRAND_NAME} <crm@localhost>")
BRAND_COLORS = {
    "red": env("BRAND_RED", "#dc2626"),
    "red_text": env("BRAND_RED_TEXT", "#b91c1c"),
    "blue": env("BRAND_BLUE", "#0d9488"),
    "blue_hover": env("BRAND_BLUE_HOVER", "#0f766e"),
    "blue_text": env("BRAND_BLUE_TEXT", "#0f766e"),
    "navy": env("BRAND_NAVY", "#1e293b"),
    "navy_2": env("BRAND_NAVY_2", "#334155"),
}

# The LeadSource that the no-login form at /leads/submit/ files its leads under.
# A business whose no-login form is its public website keeps "Website"; one whose
# no-login form is an in-house desk (a call centre taking calls) names that
# team instead, so the queue's Team column says where the lead really came from.
# It must name a LeadSource row that exists (migration 0004 seeds Website plus
# the four staff teams), or every submission fails at `LeadSource.objects.get`.
PUBLIC_LEAD_TEAM = env("PUBLIC_LEAD_TEAM", "Website")

# The teams a member of staff may file a lead from on the desk form, comma-
# separated. The default is the four migration 0004 seeds. A business with
# different desks adds them as lead sources in the Django admin and lists them
# here; a name with no LeadSource row behind it is simply not offered.
LEAD_TEAMS = [
    name.strip() for name in env("LEAD_TEAMS", "Sales,Support,Partners,Front desk").split(",") if name.strip()
]


# The secret key signs sessions and CSRF tokens. If it leaks, those protections
# break — hence it's required from .env and never written in the repo.
SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
# DEBUG shows detailed error pages — invaluable in dev, dangerous in production
# (it can leak source and settings). Off by default anywhere but local.
DEBUG = env("DJANGO_DEBUG", "1") == "1"
# The only hostnames the app will answer to. Locking this down blocks a class of
# host-header attacks. This app is internal, so just localhost.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# INSTALLED_APPS: every app (Django's own + third-party + our "core") that's
# active. Django only builds tables, loads templates, and finds commands for apps
# listed here. Order can matter; our "core" goes last so it can override others.
INSTALLED_APPS = [
    "django.contrib.admin",          # the built-in /admin backoffice
    "django.contrib.auth",           # users, groups, permissions
    "django.contrib.contenttypes",   # underpins auth + generic relations
    "django.contrib.sessions",       # server-side sessions (the login cookie)
    "django.contrib.messages",       # one-time flash messages (the green banners)
    "django.contrib.staticfiles",    # serving CSS/JS in dev
    "django.contrib.humanize",       # template filters like intcomma, naturaltime
    "django_filters",                # the list-page filters (see filters.py)
    "simple_history",                # change tracking (see history.py)
    "axes",                          # login brute-force lockout
    "core",                          # THIS app: our models, views, templates
]

# TEACHING NOTE — MIDDLEWARE is a stack every request passes through on the way
# IN and every response on the way OUT, top to bottom then bottom to top. Each
# one does one job: security headers, sessions, CSRF, authentication, etc. ORDER
# MATTERS — e.g. auth must come after sessions (it needs the session to find the
# user), and Axes goes last so it wraps a fully-authenticated request.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",              # security headers
    "django.contrib.sessions.middleware.SessionMiddleware",       # loads the session
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",                  # blocks cross-site form forgery
    "django.contrib.auth.middleware.AuthenticationMiddleware",    # sets request.user
    "django.contrib.auth.middleware.LoginRequiredMiddleware",     # every page needs login unless opted out
    "django.contrib.messages.middleware.MessageMiddleware",
    "core.middleware.NoStoreForSignedInMiddleware",                # signed-in pages are never cached (QA-14)
    "django.middleware.clickjacking.XFrameOptionsMiddleware",     # stops the app being framed
    "simple_history.middleware.HistoryRequestMiddleware",         # records WHO made each change
    "axes.middleware.AxesMiddleware",                             # brute-force lockout (must be last)
]

# TEACHING NOTE: LoginRequiredMiddleware (added in Django 5.1) flips the default
# so EVERY view requires a logged-in user. That's "secure by default": you can't
# forget to protect a page. Views that must be public (the login page itself) opt
# out explicitly. The alternative — remembering to guard each of dozens of views —
# is how real apps end up with one forgotten, wide-open page.

# Authentication backends are tried in order when someone logs in. AxesStandalone
# goes first so a locked-out attempt is rejected before the password is even checked.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",  # the normal username/password check
]

ROOT_URLCONF = "crm_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav",  # our custom one: adds role + can_create everywhere
                "core.context_processors.unread_notifications",  # the sidebar bell's badge
                "core.context_processors.brand",  # name / wordmark / footer / colour overrides, from env
            ],
        },
    },
]

WSGI_APPLICATION = "crm_project.wsgi.application"

# The database. SQLite is a single file — zero setup, perfect for building and
# for a small internal tool. Because ENGINE is the only thing that really changes,
# swapping to PostgreSQL later is a few lines here, not a rewrite. NAME comes from
# .env so tests or other machines can point elsewhere. (Reminder: this file lives
# on local disk, never on a synced drive, which corrupts SQLite mid-write.)
# A relative CRM_DB_PATH is anchored to BASE_DIR, not the working directory:
# SQLite silently creates an empty file wherever a relative path lands, so a
# server started from another folder would run against nothing and say nothing.
DB_PATH = Path(env("CRM_DB_PATH", "db.sqlite3"))
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DB_PATH if DB_PATH.is_absolute() else BASE_DIR / DB_PATH),
    }
}

# Password rules enforced when users are created or change their password. We bump
# the minimum length to 12 — length beats complexity for real-world strength.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# USE_TZ=True stores all datetimes in the database as UTC and converts to
# TIME_ZONE for display. Always leave this on — it saves you from a world of
# daylight-saving and cross-timezone bugs later.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = True
USE_TZ = True

# Static files = CSS/JS/images. STATICFILES_DIRS is where WE keep ours; STATIC_ROOT
# is where `collectstatic` gathers everything for production serving.
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"  # default primary-key type for new models

# Where the login machinery sends people. These names refer to URL names, not paths.
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# In dev, "sending" an email just prints it to the console — no real mail server
# needed to test the password-reset flow. Production would swap in a real backend.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = BRAND_EMAIL_FROM

# TEACHING NOTE — cookie and header hardening, defence in depth:
# HTTPONLY: JavaScript can't read the session cookie, so an XSS bug can't steal it.
# SAMESITE "Lax": the cookie isn't sent on cross-site POSTs, blunting CSRF.
# X_FRAME_OPTIONS DENY: other sites can't embed us in an <iframe> (clickjacking).
# NOSNIFF: browsers must respect our declared content types.
# (The Secure flag — cookies only over HTTPS — is added in settings_prodlike,
# since local dev runs over plain http.)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"


# TEACHING NOTE — WHY THE CACHE IS IN THE DATABASE.
#
# `django.core.cache` is an INTERFACE: the same get/set/incr calls work against a
# dictionary in one process's memory, or a table every process shares. The
# default is the in-memory one, and that default is wrong the moment there is
# more than one process — which is what happens the first time this runs under
# gunicorn or uwsgi. So:
#
#  - SHARED BY EVERY WORKER. The lead form's rate limit counts submissions with
#    `cache.incr`. On a per-process cache each worker keeps its own count, so five
#    submissions become five on every worker and the sixth is never refused.
#    "Refused after five" is only true if all of them are counting in the same
#    place.
#  - SURVIVES A RESTART. A crash-loop or a deploy must not hand every bot a fresh
#    allowance.
#
# `LOCATION` names the TABLE (`crm_cache`), and the table does not exist until
# someone runs `manage.py createcachetable` against that database — once per
# database, and again for every new database. Until then anything touching the
# cache raises "no such table". Django's TEST RUNNER does it for you: it calls
# createcachetable when it builds the throwaway test database, so `manage.py
# test` needs no manual step. It is the LIVE database that needs the command run
# by hand, and the README says so.
#
# There is nothing sensitive in this cache — a lead-submit counter per IP — but
# it is still the rate limiter's state, so treat the table as part of the app's
# security-relevant data.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "crm_cache",
    }
}
PAGE_SIZE = 25  # our own setting, read by the list views for pagination
IMPORT_MAX_BYTES = 5 * 1024 * 1024  # CSV import cap (~50k rows). Checked before the file is read into memory.

# django-axes: after 5 failed logins, lock that account for 15 minutes. The
# nested list means the lockout key is username AND ip together — so one person
# failing to log in can't lock out everyone else sharing the office's IP. (That
# exact bug was caught and fixed during the Phase 1 security pass.)
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # hours (15 minutes)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "lockout.html"

# The Django test client's login() doesn't send a real request object, which the
# Axes backend needs — so we switch Axes off while running the unit tests.
# Lockout behaviour is proven separately by the live attack scripts. `sys.argv`
# is the command line; "test" is in it during `manage.py test`.
if "test" in sys.argv:
    AXES_ENABLED = False
