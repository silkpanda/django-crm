"""
Data import machinery — the pluggable source adapters and upsert engine.

TEACHING NOTE: this package is the "data in" half of Phase 3. The idea is that
where the data comes FROM (a CSV file, a SQL database, a future API) shouldn't
matter to how we process it. We define a SourceAdapter protocol (a contract:
"give me rows as dicts") and then write one adapter per source type. The import
engine only knows about the protocol, so adding a new source type later means
writing one new adapter file — the engine doesn't change.

The engine handles the actual work: reading rows, matching them against existing
records (upsert), assigning a verdict (new / update / skip), and committing
everything in a single database transaction so a bad row can't leave the import
half-finished.
"""

from .base import SourceAdapter, ImportRow, Verdict
from .csv_source import CsvSource
from .sql_source import SqlSource
from .engine import ImportEngine

__all__ = ["SourceAdapter", "ImportRow", "Verdict", "CsvSource", "SqlSource", "ImportEngine"]
