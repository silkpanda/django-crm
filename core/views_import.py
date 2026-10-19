"""
CSV import wizard views — upload → map → preview → commit.

TEACHING NOTE: this is the web UI for data import. It's a multi-step wizard that
guides the user through uploading a CSV file, mapping its columns to model fields,
previewing the first 20 rows with verdicts, and then committing the import.

The wizard uses Django's session to store the uploaded file path and mapping
between steps. This is simpler than using django-formtools and keeps the state
on the server (never trust the browser to remember where a file is).

SECURITY NOTE: the wizard answers to the same rules as the edit pages. Only
Admins and Reps get in, a Rep's import can't change a record they don't own
(REVIEW-02), and a mapping can only target the fields in IMPORTABLE_FIELDS.
Uploads are capped in size, must be UTF-8, and the temp file is deleted when
the import finishes, is replaced, or is abandoned for a day.
"""

import os
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.generic import View

from .importers import CsvSource, ImportEngine
from .importers.engine import IMPORTABLE_FIELDS, REQUIRED_FIELDS
from .permissions import can_create

# Human labels for the mapping page. The KEYS come from the engine's allowlist,
# so the page can never offer a field the engine would refuse.
FIELD_LABELS = {
    "name": "Name", "domain": "Domain", "industry": "Industry", "lifecycle": "Lifecycle",
    "external_id": "External ID", "website": "Website", "phone": "Phone", "address": "Address",
    "first_name": "First name", "last_name": "Last name", "email": "Email", "title": "Title",
    "company": "Company (matched by name)",
}

# Temp uploads live in the system temp dir under this prefix, so the sweep below
# only ever touches files this wizard created.
UPLOAD_PREFIX = "crm-import-"
ABANDONED_AFTER = 24 * 60 * 60  # seconds


def _is_our_upload(path):
    """Only delete or read a path that is really one of our temp uploads."""
    p = Path(path)
    return p.parent == Path(tempfile.gettempdir()) and p.name.startswith(UPLOAD_PREFIX)


def _discard(path):
    if path and _is_our_upload(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _sweep_abandoned_uploads():
    """Delete uploads nobody finished within a day (closed tab, walked away)."""
    cutoff = time.time() - ABANDONED_AFTER
    for p in Path(tempfile.gettempdir()).glob(f"{UPLOAD_PREFIX}*.csv"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


class ImportWizardView(View):
    """
    The CSV import wizard — a 4-step process.

    TEACHING NOTE: this is a class-based view that handles all four steps. The
    current step arrives as a kwarg from urls.py. Each step renders a different
    template and passes data to the next step via the session.

    Steps:
      1. upload  — pick companies or contacts, upload a CSV file
      2. map     — user maps CSV columns to model fields
      3. preview — show first 20 rows with verdicts
      4. commit  — run the import
    """

    SESSION_KEYS = ("import_csv_path", "import_csv_name", "import_mapping", "import_type")

    def dispatch(self, request, *args, **kwargs):
        # Only Admins and Reps can import data
        if not can_create(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, step="upload"):
        """Handle GET requests for each wizard step."""
        if step == "upload":
            return self._step_upload(request)
        elif step == "map":
            return self._step_map(request)
        elif step == "preview":
            return self._step_preview(request)
        return redirect("import_wizard")

    def post(self, request, step="upload"):
        """Handle POST requests for each wizard step."""
        if step == "upload":
            return self._handle_upload(request)
        elif step == "map":
            return self._handle_map(request)
        elif step == "commit":
            return self._handle_commit(request)
        return redirect("import_wizard")

    # ------------------------------------------------------------ session state

    def _state(self, request):
        """(csv_path, import_type) from the session, or (None, None) if there's no live upload."""
        path = request.session.get("import_csv_path")
        kind = request.session.get("import_type")
        if not path or kind not in IMPORTABLE_FIELDS or not _is_our_upload(path) or not os.path.exists(path):
            return None, None
        return path, kind

    def _clear(self, request):
        _discard(request.session.get("import_csv_path"))
        for key in self.SESSION_KEYS:
            request.session.pop(key, None)

    def _fields(self, kind):
        return {name: FIELD_LABELS[name] for name in IMPORTABLE_FIELDS[kind]}

    # ------------------------------------------------------------ step 1: upload

    def _step_upload(self, request):
        """Step 1: show the file upload form."""
        return render(request, "core/import_upload.html", {
            "title": "Import CSV — Upload",
            "max_mb": settings.IMPORT_MAX_BYTES // (1024 * 1024),
        })

    def _handle_upload(self, request):
        """Validate the upload, park it in a temp file, and remember it in the session."""
        csv_file = request.FILES.get("csv_file")
        kind = request.POST.get("import_type", "company")
        if kind not in IMPORTABLE_FIELDS:
            messages.error(request, "Choose whether you're importing companies or contacts.")
            return redirect("import_wizard")
        if not csv_file:
            messages.error(request, "Please select a CSV file to upload.")
            return redirect("import_wizard")
        if not csv_file.name.lower().endswith(".csv"):
            messages.error(request, "Only CSV files are supported.")
            return redirect("import_wizard")
        # Checked BEFORE reading it into memory: the UTF-8 check below holds the
        # whole file in RAM, so without a cap one upload could eat the server.
        if csv_file.size > settings.IMPORT_MAX_BYTES:
            messages.error(request, f"That file is over {settings.IMPORT_MAX_BYTES // (1024 * 1024)} MB. Split it and import in parts.")
            return redirect("import_wizard")

        # Refuse anything that isn't valid UTF-8 HERE, at the door, with a message
        # the user can act on. Otherwise the bad byte surfaces two pages later as
        # a UnicodeDecodeError and a 500. `utf-8-sig` also accepts the invisible
        # byte-order mark Excel puts at the start of "CSV UTF-8" exports.
        raw = b"".join(csv_file.chunks())
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            messages.error(request, "That file isn't UTF-8. In Excel, use Save As → \"CSV UTF-8\" and upload again.")
            return redirect("import_wizard")

        # QA-12: an empty file decodes cleanly and has no columns, so it used to
        # walk straight through to the map step and dead-end there with every
        # dropdown blank. Refuse it at the door with the other upload errors,
        # while `text` is already in hand. `strip()` covers both a zero-byte
        # upload and one holding nothing but newlines and spaces — to a CSV
        # reader those are the same empty file, and users should be told so.
        if not text.strip():
            messages.error(request, "That file is empty. Export it again with a header row and at least one record.")
            return redirect("import_wizard")

        # A new upload replaces any unfinished one, and old abandoned ones get swept.
        self._clear(request)
        _sweep_abandoned_uploads()
        with tempfile.NamedTemporaryFile(delete=False, prefix=UPLOAD_PREFIX, suffix=".csv", mode="wb") as tmp:
            tmp.write(raw)

        request.session["import_csv_path"] = tmp.name
        request.session["import_csv_name"] = csv_file.name
        request.session["import_type"] = kind
        return redirect("import_wizard_map")

    # ------------------------------------------------------------ step 2: map

    def _step_map(self, request):
        """Step 2: show the column mapping form."""
        csv_path, kind = self._state(request)
        if not csv_path:
            return redirect("import_wizard")
        return render(request, "core/import_map.html", {
            "title": "Import CSV — Map Columns",
            "columns": CsvSource(csv_path).get_columns(),
            "fields": self._fields(kind),
            "import_type": kind,
            "required": REQUIRED_FIELDS[kind],
            "csv_name": request.session.get("import_csv_name", "unknown.csv"),
        })

    def _handle_map(self, request):
        """Turn the dropdowns into a {source column: model field} mapping."""
        csv_path, kind = self._state(request)
        if not csv_path:
            return redirect("import_wizard")
        columns = set(CsvSource(csv_path).get_columns())

        mapping = {}
        for field_name, label in self._fields(kind).items():
            source_col = request.POST.get(f"map_{field_name}")
            if not source_col:
                continue
            if source_col not in columns:  # a hand-edited POST naming a column that isn't in the file
                messages.error(request, f"“{source_col}” isn't a column in that file.")
                return redirect("import_wizard_map")
            if source_col in mapping:
                messages.error(request, f"“{source_col}” is mapped to two fields. Pick one.")
                return redirect("import_wizard_map")
            mapping[source_col] = field_name

        required = REQUIRED_FIELDS[kind]
        if required not in mapping.values():
            messages.error(request, f"Map a column to {FIELD_LABELS[required]}. Every {kind} needs one.")
            return redirect("import_wizard_map")

        request.session["import_mapping"] = mapping
        return redirect("import_wizard_preview")

    # ------------------------------------------------------------ step 3: preview

    def _step_preview(self, request):
        """Step 3: preview the first 20 rows with verdicts."""
        csv_path, kind = self._state(request)
        mapping = request.session.get("import_mapping")
        if not csv_path or not mapping:
            return redirect("import_wizard")

        # `run_by` matters even for a preview: it has to apply the same
        # ownership check as the commit, or it promises UPDATEs that won't happen.
        engine = ImportEngine(CsvSource(csv_path), mapping, dry_run=True, run_by=request.user, kind=kind)
        return render(request, "core/import_preview.html", {
            "title": "Import CSV — Preview",
            "rows": engine.preview(limit=20),
            "mapping": mapping,
            "import_type": kind,
            "csv_name": request.session.get("import_csv_name", "unknown.csv"),
        })

    # ------------------------------------------------------------ step 4: commit

    def _handle_commit(self, request):
        """Step 4: run the import, then clean up the temp file and session."""
        csv_path, kind = self._state(request)
        mapping = request.session.get("import_mapping")
        if not csv_path or not mapping:
            return redirect("import_wizard")

        # QA-12: pass the name the user uploaded, so the ImportRun audit row can
        # say "my_companies.csv" instead of a temp path. Read it BEFORE
        # `_clear` below, which wipes the session (and the temp file with it).
        engine = ImportEngine(
            CsvSource(csv_path), mapping, dry_run=False, run_by=request.user, kind=kind,
            source_label=request.session.get("import_csv_name"),
        )
        result = engine.run()
        self._clear(request)

        if result.errors:
            messages.error(request, f"Import failed and nothing was saved: {'; '.join(result.errors)}")
            return redirect("import_wizard")
        messages.success(
            request,
            f"Import complete: {result.created} created, {result.updated} updated, {result.skipped} skipped.",
        )
        # Skipped rows get their reasons, not just a count. First five is plenty
        # for a flash message; the full list is on the ImportRun record.
        for reason in result.row_errors[:5]:
            messages.warning(request, reason)
        if len(result.row_errors) > 5:
            messages.warning(request, f"…and {len(result.row_errors) - 5} more skipped rows.")
        return redirect("company_list" if kind == "company" else "contact_list")
