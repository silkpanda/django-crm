# CRM

A small internal CRM: companies, contacts, deals with a sales pipeline, an
activity timeline, tasks, a dashboard, CSV/SQL import, lead intake, quotes,
notifications, reports and a read-only JSON API. Django 5 + SQLite, in a branded shell.

Built in phases, each gated by tests **and** an active security pass.

## Status
- **Phase 1 — Foundation & records:** DONE. Auth + roles, companies/contacts/deals
  CRUD, timeline, change history, branded shell. 34 tests green; Phase 1 security gate passed.
- **Phase 2 — Sales workflow:** DONE. Kanban pipeline (drag between stages; Lost asks
  for a reason), tasks / My Day, global search, duplicate warning on create, dashboard.
- **Phase 3 — Data in and out:** DONE. CSV import wizard (companies or contacts,
  upload → map → preview → commit), SQL import CLI, filtered CSV export, Admin merge.
- **Phase 4 — Office 365 email connector:** REMOVED 2026-10-06. Real mail is out
  of scope for this proof of concept and will come back, if it does, as its own
  project with its own gate. An email can still be logged by hand as a timeline
  activity.
- **Phase 5 — Lead intake:** DONE. Public `/leads/submit/` form (honeypot, CSRF,
  5 per IP per hour), buying roles on contacts, and a `/leads/` queue open to
  Admins and Reps: a Rep CLAIMS an unowned lead for themselves (one conditional
  UPDATE, so two Reps can't both get it), an Admin can still ASSIGN it to
  anyone, and either way the owner gets a "Contact new lead" task due tomorrow.
  A new lead rings an in-app bell notification for Reps and Admins.
  `/leads/new/` has a company-name typeahead (`/leads/companies/`, login
  required); the queue's Team column reads `Deal.source`, falling back to the
  company's original source for leads filed before migration 0009.
- **Phase 6 — Notifications, quotes, reports, JSON API:** DONE. In-app
  notification bell, per-deal quotes with server-computed totals, scoped
  reports, and a read-only JSON API.
- 442 tests green (3 skip without the browser extra), as of 2026-10-07.

## Importing
In the app: **Companies** or **Contacts** → **Import CSV**. Files must be UTF-8
("CSV UTF-8" in Excel's Save As) and at most 5 MB. Import companies before
contacts; a contact's company column is matched by company name. Reps can only
update records they own; rows they can't touch are skipped with the reason.

From a database (Admin, command line):
```bash
.venv\Scripts\python manage.py import_data --adapter sql --url "postgresql://user:pass@host/db" ^
    --query "SELECT name, domain FROM accounts" --mapping map.json --type company --dry-run
```
`map.json` maps source column → CRM field, e.g. `{"name": "name", "domain": "domain"}`.
Allowed fields — company: name, domain, industry, lifecycle, external_id, website,
phone, address; contact: first_name, last_name, email, phone, title, external_id,
company. Always `--dry-run` first. The connection password is never stored.

## Setup
Every command below is run from the project root. Copy-paste the block; it takes
you from a fresh clone to a running app and a green suite. The database starts
empty: there is no sample data.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"   # paste into .env
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createcachetable      # the rate limiter's table — see Lead intake
.venv\Scripts\python manage.py createsuperuser        # your real admin login, if you want one
.venv\Scripts\python manage.py runserver 127.0.0.1:8766
```

`DJANGO_SECRET_KEY` is required and has no fallback: with it missing, `manage.py`
dies in settings before it finds a single test. That is deliberate — an app that
quietly invents a key in production is an app whose sessions everyone can forge.
Generate one per checkout and keep it in `.env`, which is gitignored.

**Port 8766, and why it isn't 8765.** This repo's dev server runs on
`127.0.0.1:8766`. 8765 belongs to downstream branded builds — the ones with real
records in them — so the two can run side by side without colliding.

The SQLite DB is `db.sqlite3` beside the code (override with `CRM_DB_PATH`), and
it is gitignored. A relative `CRM_DB_PATH` is resolved against the project
folder, not the directory the server was started from. Nothing here touches a
shared drive.

## Branding
Branding is configured by env vars, not by code. There is no literal brand
string in any template and no colour literal in `app.css` — the shell asks for
`brand_name` and reads `var(--brand-*)`, so re-branding is an edit to `.env`
and nothing else. Every variable is optional; with none of them set the app
ships a neutral slate/teal shell and the wordmark "CRM".

| Variable | Default | What it sets |
|---|---|---|
| `BRAND_NAME` | `CRM` | Page `<title>`, the sidebar wordmark, the admin header, and the `<title>` fallback on every page. |
| `BRAND_WORDMARK` | `CRM` | The large word in the sidebar and on the sign-in card. |
| `BRAND_WORDMARK_ACCENT` | *(empty)* | The second word beside it, in the brand red. Leave empty for a one-word wordmark. |
| `BRAND_FOOTER` | `internal` | The small line at the bottom of the sidebar, printed after `BRAND_NAME`. |
| `BRAND_EMAIL_FROM` | `<BRAND_NAME> <crm@localhost>` | Envelope From address for outgoing mail. |
| `BRAND_RED` | `#dc2626` | Wordmark accent, Lost stage, destructive buttons. |
| `BRAND_RED_TEXT` | `#b91c1c` | Red as small text on white (6.47:1). |
| `BRAND_BLUE` | `#0d9488` | Primary button fill, focus ring, call-kind chip. |
| `BRAND_BLUE_HOVER` | `#0f766e` | The button's hover fill. |
| `BRAND_BLUE_TEXT` | `#0f766e` | Link colour, qualified stage text (5.47:1 on white). |
| `BRAND_NAVY` | `#1e293b` | Sidebar and auth background. |
| `BRAND_NAVY_2` | `#334155` | Nav hover and active tint. |

The colours emit as a `:root` override after the stylesheets load, so a fork
changes all seven or none — a partial palette is how you end up with a red
button nobody can read. A value that contains nothing a colour can legally
contain falls back to the default above rather than emitting broken CSS. Ratios
and the reasoning behind the defaults are in `STYLEGUIDE.md`.

`templates/500.html` is the one deliberate exception: it renders with an empty
context when things are already broken, so it hard-codes a copy of the palette
and takes no overrides. Re-brand it by hand.

## Roles
Assign users to one group in the Django admin (`/admin/`):
- **Admin** — everything, including deletes and reassigning record owners. Superusers are always Admin.
- **Rep** — sees everything, edits records they own, logs activity on any record.
- **Viewer** — read-only. A user in no group is treated as a Viewer.

## Lead intake
Two doors onto the same thing — a lead becomes a Company, a Contact and an
unowned Deal in the Lead stage, with the submitter's message on its timeline.

| URL | Who |
|---|---|
| `/leads/new/` | Any signed-in user, **including a Viewer** — the phone rings for whoever picks up, and a read-only user who shrugged and took a message is worth more than a rule that sends them to voicemail. |
| `/leads/submit/` | Public. This is the only page the internet can reach. |
| `/leads/` | Admins and Reps. The new-leads queue: unowned deals still at the Lead stage. A Rep CLAIMS a lead for themselves; an Admin can assign it to anyone. Either way the owner gets a "Contact new lead: \<company\>" task, due tomorrow. |

No email is sent. It is a proof of concept: the message sits in the queue and
somebody rings the customer. What the system does do is ring an in-app bell for
Reps and Admins when a lead arrives, so nobody finds out by keeping their finger
on the inbox.

`PUBLIC_LEAD_TEAM` (env var or setting, default `"Website"`) names the lead
source the no-login form files its leads under, in place of the team picker the
desk form has. It must name a lead source that already exists:
`create_lead()` looks it up with `LeadSource.objects.get(name=...)`, so an
unknown name raises rather than quietly inventing a new source.

`LEAD_TEAMS` (env var or setting, comma-separated, default
`Sales,Support,Partners,Front desk`) is that team picker: the lead sources a
member of staff can file a lead from on `/leads/new/`. Migration 0004 creates the
four defaults. For different desks, add them as lead sources in the Django admin
and list them here. A name with no lead source behind it is left off the picker.

**Setup, once per database:** the rate limit lives in the database-backed cache,
and that cache lives in a table:
```bash
.venv\Scripts\python manage.py createcachetable
```
Skip it and anything that reads the cache raises `no such table` — including the
public lead form. Run it again for every new database. `manage.py test` needs no
step: Django's test runner creates the table when it builds its throwaway test
database.

The public form is limited to **5 submissions per IP per hour**. It counts
`REMOTE_ADDR` (not `X-Forwarded-For`, which the client controls), keyed as
`lead-submit:<ip>` in the cache. Behind a real reverse proxy, terminate
`X-Forwarded-For` in the proxy and read it there — trusting the header from the
request itself would let anyone mint a fresh allowance per POST.

## Tests
```bash
.venv\Scripts\python manage.py test core
```
Two files need extras and skip (loudly, with the install command) when
the extra is missing: `core/tests/test_browser.py` (PIPE-9's
double-drop, driven in real Playwright Chromium at phone width —
`pip install -r requirements-browser.txt` + `python -m playwright
install chromium`) and the optional-extra guards elsewhere. Everything
else runs with the base environment.

## Security gate
```bash
set DJANGO_SETTINGS_MODULE=crm_project.settings_prodlike && .venv\Scripts\python manage.py check --deploy
.venv\Scripts\bandit -r core crm_project -x core/tests
.venv\Scripts\pip-audit
```

## Notes
- The SQLite DB lives beside the code, deliberately **not** on Google Drive —
  Drive syncing a DB file mid-write corrupts it.
- Prod-like settings (`crm_project/settings_prodlike.py`) turn DEBUG off and add
  secure-cookie / HSTS headers. Dev settings bind to 127.0.0.1 and use the
  console email backend.
- Prod-like settings also hash static filenames (`ManifestStaticFilesStorage`).
  Run `collectstatic` on every deploy — the manifest is built while collecting —
  and point the web server at `STATIC_ROOT`, not the source `static/` dir.

## License
MIT. See `LICENSE`.
