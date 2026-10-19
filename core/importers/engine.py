"""
The import engine — reads rows, matches them against existing records, and commits.

TEACHING NOTE: this is the brain of the import system. It doesn't care where the
data comes from (CSV, SQL, future API) — it only talks to the SourceAdapter
protocol. The engine's job is to:

1. Read rows from the source
2. For each row, try to find an existing record to UPDATE (the "upsert" match)
3. Assign a verdict: NEW (create), UPDATE (match found), SKIP (invalid, ambiguous,
   or not yours to change) — and say why for every SKIP
4. Save each good row inside its own savepoint, so one bad row is skipped and
   reported instead of taking the rest of the file down with it. Only an
   unexpected error (the source itself breaking) rolls back the whole run.
5. Write an ImportRun record with the results

The upsert matching rules are:
  Companies: external_id → domain → name (first match wins)
  Contacts:  external_id → email (first match wins)

These rules are idempotent: running the same import twice updates the same rows
instead of creating duplicates.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Company, Contact, ImportRun, Lifecycle
from core.permissions import can_edit

from .base import ImportRow, SourceAdapter, Verdict


# TEACHING NOTE: the mapping always points the same way, source → model field:
#     {"Company Name": "name", "Web": "domain"}
# The wizard builds it from its dropdowns; the CLI reads it from a JSON file of
# the same shape. REVIEW-04 was this dict being read in BOTH directions by
# different methods, so matching quietly never fired unless the CSV headers
# happened to be the field names themselves — which is exactly what every test
# used, so every test passed.
#
# IMPORTABLE_FIELDS is also an allowlist. Anything else in a mapping (owner_id,
# pk, a typo) is refused up front, so a mapping can never set a field the
# import was never meant to touch.
IMPORTABLE_FIELDS = {
    "company": ("name", "domain", "industry", "lifecycle", "external_id", "website", "phone", "address"),
    "contact": ("first_name", "last_name", "email", "phone", "title", "external_id", "company"),
}
REQUIRED_FIELDS = {"company": "name", "contact": "first_name"}
MODELS = {"company": Company, "contact": Contact}


class RowError(Exception):
    """A problem with one row that should SKIP it (with this message), not stop the import."""


@dataclass
class ImportResult:
    """The outcome of an import run — counts and any errors."""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)       # run-level failures (whole import rolled back)
    row_errors: List[str] = field(default_factory=list)   # "Row 7: why it was skipped"
    import_run: Optional[ImportRun] = None


def describe(error):
    """Turn a ValidationError into one readable line: 'email: Enter a valid email address.'"""
    if hasattr(error, "message_dict"):
        return "; ".join(f"{name}: {' '.join(msgs)}" for name, msgs in error.message_dict.items())
    return " ".join(getattr(error, "messages", [str(error)]))


class ImportEngine:
    """
    Drives the import process: read → match → verdict → commit.

    TEACHING NOTE: the engine is configured with a source adapter and a mapping.
    The mapping tells it which source columns correspond to which model fields,
    and how to match rows against existing records. The same engine works for
    any source type because it only talks to the SourceAdapter protocol.
    """

    def __init__(self, source: SourceAdapter, mapping: Dict, dry_run=False, run_by=None, kind=None,
                 source_label=None):
        """
        Initialize the engine.

        TEACHING NOTE: `dry_run=True` means "show me what would happen but don't
        actually write anything (except the ImportRun record)." This is critical
        for testing — you can run an import against production data and see exactly
        what it would do before committing.

        `run_by` is the user who initiated the import (for the ImportRun audit record,
        and for the ownership check). `kind` is "company" or "contact"; left out, it's
        inferred from the mapping (a contact field in there means a contact import).

        QA-12: `source_label` is the name to record on the ImportRun — the file the
        person actually uploaded. Left out, `run()` falls back to the source's own
        path or URL, which for a wizard upload is a temp path nobody can act on.
        """
        if kind is None:
            kind = "contact" if {"first_name", "email"} & set(mapping.values()) else "company"
        if kind not in IMPORTABLE_FIELDS:
            raise ValueError(f"Unknown import type {kind!r}; use 'company' or 'contact'.")
        unknown = set(mapping.values()) - set(IMPORTABLE_FIELDS[kind])
        if unknown:
            raise ValueError(f"A {kind} import can't set: {', '.join(sorted(unknown))}.")

        self.source = source
        self.mapping = mapping
        self.kind = kind
        self.model = MODELS[kind]
        self.dry_run = dry_run
        self.run_by = run_by
        self.source_label = source_label
        # The reverse view (field → source column) is what matching needs.
        self.columns = {model_field: column for column, model_field in mapping.items()}
        self._companies_by_name = {}  # cache for the contact → company lookup

    # ------------------------------------------------------------ reading a row

    def _value(self, row_data: Dict, model_field: str) -> str:
        """The cleaned text value feeding `model_field`, or "" if unmapped/blank."""
        column = self.columns.get(model_field)
        if not column:
            return ""
        value = row_data.get(column)
        return "" if value is None else str(value).strip()

    def _one(self, queryset, what):
        """
        The single record a lookup found, None if it found nothing.

        Two or more is a SKIP, not a guess: `.get()` would crash the whole run
        with MultipleObjectsReturned, and `.first()` would quietly update
        whichever duplicate the database happened to return first.
        """
        found = list(queryset[:2])
        if len(found) > 1:
            raise RowError(f"Matches more than one {self.kind} by {what}. Add an external ID or domain so it's unambiguous.")
        return found[0] if found else None

    # ------------------------------------------------------------ matching

    def _match(self, row_data: Dict):
        """
        Try to find the existing record this row describes.

        TEACHING NOTE: the matching order matters. We try external_id first
        (most specific), then domain/email, then (companies only) name. The first
        rule that has a value AND finds a record wins.
        """
        model = self.model
        ext_id = self._value(row_data, "external_id")
        if ext_id:
            hit = self._one(model.objects.filter(external_id=ext_id), "external ID")
            if hit:
                return hit
        if self.kind == "company":
            domain = self._value(row_data, "domain").lower()
            if domain:
                hit = self._one(Company.objects.filter(domain=domain), "domain")
                if hit:
                    return hit
            name = self._value(row_data, "name")
            if name:
                return self._one(Company.objects.filter(name__iexact=name), "name")
            return None
        email = self._value(row_data, "email").lower()
        if email:
            return Contact.objects.filter(email=email).first()  # email is unique: at most one
        return None

    # ------------------------------------------------------------ building

    def _company_named(self, name):
        """Resolve a contact's "Company" column to a Company row, by name."""
        key = name.lower()
        if key not in self._companies_by_name:
            hit = self._one(Company.objects.filter(name__iexact=name), "company name")
            if hit is None:
                raise RowError(f"No company named “{name}”. Import companies first, then contacts.")
            self._companies_by_name[key] = hit
        return self._companies_by_name[key]

    def _build(self, row_data: Dict, existing=None):
        """Create or update a record in memory from one row. Nothing is saved here."""
        record = existing or self.model()
        for model_field in IMPORTABLE_FIELDS[self.kind]:
            value = self._value(row_data, model_field)
            if not value:
                continue  # a blank cell never wipes data that's already there
            if model_field == "company":
                record.company = self._company_named(value)
            elif model_field in ("domain", "email"):
                setattr(record, model_field, value.lower())
            elif model_field == "lifecycle":
                # Spreadsheets say "Customer"; the database stores "customer".
                # Accept either the stored value or the label, any case.
                choices = {k.lower(): k for k in Lifecycle.values} | {v.lower(): k for k, v in Lifecycle.choices}
                record.lifecycle = choices.get(value.lower(), value)  # unknown → full_clean says why
            else:
                setattr(record, model_field, value)
        return record

    # ------------------------------------------------------------ running

    def preview(self, limit=20) -> List[ImportRow]:
        """
        Preview the first `limit` rows with their verdicts.

        TEACHING NOTE: this is what the CSV wizard shows before committing. It
        reads rows, matches them, validates them, and assigns verdicts — but
        doesn't write anything. Because validation runs here too, a row the
        commit would skip already shows SKIP (with the reason) in the preview.
        """
        rows = []
        for row in self.source.read_rows():
            if len(rows) >= limit:
                break
            self._process_row(row, save=False)
            rows.append(row)
        return rows

    def run(self) -> ImportResult:
        """
        Execute the full import.

        TEACHING NOTE: rows are saved one savepoint at a time inside one outer
        transaction. A row that fails validation or hits a database constraint
        is SKIPPED and reported ("Row 7: ...") while the rest of the file goes
        in. Only an unexpected error — the file or database connection breaking
        mid-read — rolls back the whole run, so you never get half a file
        because the source died.

        The ImportRun record is written even for dry runs (so you can audit what
        was attempted), but the `dry_run` flag is set so you know it wasn't real.
        """
        result = ImportResult()

        # Write the ImportRun record first (even for dry runs)
        # QA-12: the label is what makes the audit row worth keeping. With no
        # name supplied we fell back to the server's temp path
        # (Temp\crm-import-i113_v5h.csv), which cannot answer "who imported
        # what". Prefer the name the user uploaded.
        #
        # SECURITY NOTE: both branches go through redaction, not just the DSN
        # one. A filename is attacker-controlled text — someone can upload a file
        # literally named "postgresql://user:pw@host/db.csv", and the whole point
        # of redacting before storage is that nobody has to remember which fields
        # were trustworthy. `_redact_credentials` returns a plain filename
        # untouched, so this costs the common case nothing.
        if self.source_label:
            source_label = self._redact_credentials(self.source_label)
        else:
            source_label = self._redact_credentials(
                getattr(self.source, 'path', getattr(self.source, 'url', 'unknown'))
            )

        import_run = ImportRun.objects.create(
            source_type=self._get_source_type(),
            source_label=source_label,
            dry_run=self.dry_run,
            run_by=self.run_by,
        )
        result.import_run = import_run

        try:
            with transaction.atomic():
                for row in self.source.read_rows():
                    self._process_row(row, save=not self.dry_run)
                    if row.verdict == Verdict.NEW:
                        result.created += 1
                    elif row.verdict == Verdict.UPDATE:
                        result.updated += 1
                    else:
                        result.skipped += 1
                        result.row_errors.append(f"Row {row.row_number}: {' '.join(row.errors)}")
        except Exception as e:
            result.errors.append(str(e))
            import_run.errors = str(e)
            # QA-12: stamped on the failure path too. An audit row is read
            # precisely when something went wrong, so leaving finished_at null
            # on exactly that path defeats the point of the column.
            import_run.finished_at = timezone.now()
            import_run.save()
            return result

        # Update the ImportRun with final counts
        import_run.created_count = result.created
        import_run.updated_count = result.updated
        import_run.skipped_count = result.skipped
        import_run.errors = "\n".join(result.row_errors)
        import_run.finished_at = timezone.now()
        import_run.save()

        return result

    def _skip(self, row: ImportRow, reason: str):
        row.verdict = Verdict.SKIP
        row.errors.append(reason)

    def _process_row(self, row: ImportRow, save: bool):
        """
        Process a single row: match, check ownership, validate, and (if `save`) write.

        TEACHING NOTE: this is called by both preview() and run(). The only
        difference is `save`: the preview does every check the commit does,
        then stops short of writing.
        """
        try:
            existing = self._match(row.data)
        except RowError as e:
            return self._skip(row, str(e))

        if existing:
            row.verdict = Verdict.UPDATE
            row.matched_id = existing.pk
            # SECURITY NOTE (REVIEW-02): an import is just another way to edit a
            # record, so it answers to the same rule as the edit page. Without
            # this check a Rep could overwrite any colleague's company with a
            # one-row CSV whose name matched it. `run_by` is None only for the
            # CLI, which is already a shell on the server.
            if self.run_by is not None and not can_edit(self.run_by, existing):
                return self._skip(row, f"Matches “{existing}”, which you don't own. Ask an Admin to import it.")
        else:
            row.verdict = Verdict.NEW

        try:
            record = self._build(row.data, existing)
            # Whoever imports a new record owns it, the same rule the create
            # form applies. Left ownerless, a Rep's own import would be a record
            # they could never edit afterwards.
            if existing is None and record.owner_id is None and self.run_by is not None:
                record.owner = self.run_by
            record.full_clean()
        except RowError as e:
            return self._skip(row, str(e))
        except ValidationError as e:
            return self._skip(row, describe(e))

        if not save:
            return
        try:
            # A savepoint: if the database rejects this one row, only this row
            # rolls back. Without it, the failed statement poisons the outer
            # transaction and every row after it errors out too.
            with transaction.atomic():
                record.save()
        except IntegrityError as e:
            self._skip(row, f"The database refused this row: {e}")

    def _get_source_type(self) -> str:
        """Return the source type string for the ImportRun record."""
        from .csv_source import CsvSource
        from .sql_source import SqlSource
        if isinstance(self.source, CsvSource):
            return "csv"
        elif isinstance(self.source, SqlSource):
            return "sql"
        return "unknown"

    def _redact_credentials(self, source_label: str) -> str:
        """
        Redact passwords from connection strings before storing.

        TEACHING NOTE: database connection strings often contain passwords
        (postgresql://user:PASS@host/db). We must never store those in the
        ImportRun record. This function replaces the password with ***.

        SECURITY NOTE (REVIEW-03): this used to be a hand-written regex whose
        replacement put capture group 1 — the password — where the username
        goes, so `bob:hunter2@` was stored as `hunter2:***@`. Don't parse a
        format you don't own. SQLAlchemy is the thing that will read this URL,
        so SQLAlchemy decides where the password is, including passwords with
        an encoded @ or : in them that trip up regexes.
        """
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(source_label)
        except ArgumentError:
            return source_label  # a file path, not a connection string: nothing to hide
        if url.password is None:
            return source_label  # left as-is: re-rendering would percent-encode "C:" in sqlite paths
        return url.render_as_string(hide_password=True)
