"""
CLI command: import data from a CSV file or SQL database.

TEACHING NOTE: this is a Django management command — a script you run with
`manage.py import_data`. It's the "data in" command for power users who want to
import from a database connection string rather than the web UI.

Usage:
    python manage.py import_data --adapter csv --file data.csv --mapping map.json
    python manage.py import_data --adapter sql --url postgresql://... --mapping map.json --dry-run

The --dry-run flag shows what would happen without writing anything (except the
ImportRun audit record). Always dry-run first.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.importers import CsvSource, SqlSource, ImportEngine


class Command(BaseCommand):
    help = "Import companies or contacts from a CSV file or SQL database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--adapter",
            required=True,
            choices=["csv", "sql"],
            help="The source adapter type.",
        )
        parser.add_argument(
            "--file",
            help="Path to the CSV file (required for --adapter csv).",
        )
        parser.add_argument(
            "--url",
            help="Database connection string (required for --adapter sql).",
        )
        parser.add_argument(
            "--query",
            help="SQL query to run (optional, defaults to SELECT * FROM companies).",
        )
        parser.add_argument(
            "--mapping",
            required=True,
            help="Path to the column mapping JSON file.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would happen without writing data.",
        )
        parser.add_argument(
            "--type",
            choices=["company", "contact"],
            help="What the rows are. Inferred from the mapping if left out.",
        )

    def handle(self, *args, **opts):
        # Load the mapping file
        mapping_path = Path(opts["mapping"])
        if not mapping_path.exists():
            raise CommandError(f"Mapping file not found: {mapping_path}")
        mapping = json.loads(mapping_path.read_text())

        # Create the source adapter
        if opts["adapter"] == "csv":
            if not opts["file"]:
                raise CommandError("--file is required for CSV imports")
            source = CsvSource(opts["file"])
        elif opts["adapter"] == "sql":
            if not opts["url"]:
                raise CommandError("--url is required for SQL imports")
            source = SqlSource(opts["url"], query=opts.get("query"))
        else:
            raise CommandError(f"Unknown adapter: {opts['adapter']}")

        # Run the import. A mapping that names a field the import can't set
        # (a typo, or something like owner_id) is refused here, before any row.
        try:
            engine = ImportEngine(source, mapping, dry_run=opts["dry_run"], kind=opts["type"])
        except ValueError as e:
            raise CommandError(str(e))

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no data will be written"))
            rows = engine.preview(limit=20)
            for row in rows:
                self.stdout.write(f"  Row {row.row_number}: {row.verdict.upper()}")
                if row.errors:
                    for err in row.errors:
                        self.stdout.write(f"    ERROR: {err}")
            self.stdout.write(f"\nPreview complete. {len(rows)} rows shown.")
        else:
            result = engine.run()
            if result.errors:
                # The whole run rolled back. Don't print a green "complete" over it.
                self.stdout.write(self.style.ERROR("Import FAILED. Nothing was written."))
                for err in result.errors:
                    self.stdout.write(self.style.ERROR(f"  ERROR: {err}"))
                return
            self.stdout.write(self.style.SUCCESS(
                f"Import complete: {result.created} created, "
                f"{result.updated} updated, {result.skipped} skipped."
            ))
            for err in result.row_errors:
                self.stdout.write(self.style.WARNING(f"  SKIPPED {err}"))
