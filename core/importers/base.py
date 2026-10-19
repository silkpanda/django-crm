"""
The SourceAdapter protocol and shared import types.

TEACHING NOTE: a "protocol" is a contract — it says "anything that wants to be
an import source must provide these methods." The import engine only ever talks
to this protocol, so it doesn't care whether the data comes from a CSV file, a
SQL database, or a future API. You write a new adapter by implementing `read_rows`
and `get_columns`, and the engine works with it unchanged.

`ImportRow` is one row of source data plus the engine's verdict for it.
`Verdict` is the three possible outcomes: NEW (create), UPDATE (match found),
SKIP (invalid or duplicate-within-file).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Verdict:
    """The three possible outcomes for an imported row."""
    NEW = "new"
    UPDATE = "update"
    SKIP = "skip"


@dataclass
class ImportRow:
    """
    One row of source data, paired with the engine's verdict.

    TEACHING NOTE: a dataclass is a class that mainly holds data. It auto-generates
    __init__, __repr__, and __eq__ so you don't write boilerplate. The `field(default=...)`
    sets defaults for fields that aren't required at construction time.
    """
    row_number: int          # 1-based row number in the source (for error messages)
    data: Dict[str, Any]     # the raw column→value dict from the source
    verdict: str = Verdict.NEW
    matched_id: Optional[int] = None   # the PK of the existing record this row matched
    errors: List[str] = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.errors


class SourceAdapter:
    """
    The contract every import source must fulfill.

    TEACHING NOTE: this is an abstract base class. You never instantiate it
    directly — you subclass it and implement the two methods. The import engine
    type-hints against this class, so any adapter that implements the contract
    works without the engine knowing the concrete type.
    """

    def get_columns(self) -> List[str]:
        """
        Return the column names this source provides.

        TEACHING NOTE: the CSV wizard uses this to populate the column-mapping
        dropdown. The SQL adapter returns the column names from the first row.
        """
        raise NotImplementedError

    def read_rows(self):
        """
        Yield ImportRow objects, one per row in the source.

        TEACHING NOTE: this is a generator (uses `yield`). It streams rows one
        at a time instead of loading the whole file into memory — important for
        large imports. Each ImportRow wraps the raw dict plus the row number.
        """
        raise NotImplementedError
