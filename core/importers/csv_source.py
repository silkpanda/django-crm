"""
CSV source adapter — reads rows from a CSV file.

TEACHING NOTE: Python's `csv` module handles the parsing. We use `DictReader`
which turns each row into a dict with the CSV headers as keys. This is the
simplest adapter to understand and the one the CSV wizard uses.
"""

import csv
from typing import List

from .base import ImportRow, SourceAdapter


class CsvSource(SourceAdapter):
    """
    Reads import data from a CSV file.

    TEACHING NOTE: the adapter takes a file path and optional encoding. It reads
    the header row to get column names, then yields one ImportRow per data row.
    The `read_rows` method is a generator — it streams rows instead of loading
    the whole file into memory.
    """

    # `utf-8-sig` reads plain UTF-8 identically, and also strips the byte-order
    # mark Excel writes. With plain "utf-8" that mark glues itself to the first
    # header, so "name" arrives as "﻿name" and never matches the mapping.
    def __init__(self, path, encoding="utf-8-sig"):
        self.path = path
        self.encoding = encoding
        self._columns = None

    def get_columns(self) -> List[str]:
        """Return the CSV header row as a list of column names."""
        if self._columns is None:
            with open(self.path, newline="", encoding=self.encoding) as f:
                reader = csv.DictReader(f)
                # Stripped the same way read_rows strips row keys. A header of
                # " domain" otherwise maps as " domain" and never matches the
                # row's "domain", so the whole column imports blank.
                self._columns = [name.strip() for name in reader.fieldnames or []]
        return self._columns

    def read_rows(self):
        """
        Yield ImportRow objects from the CSV file.

        TEACHING NOTE: `enumerate(reader, start=1)` gives us the row number
        (1-based, matching what a user sees in Excel). We strip whitespace from
        values because CSV files often have trailing spaces.
        """
        with open(self.path, newline="", encoding=self.encoding) as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader, start=1):
                # DictReader files cells beyond the last header under the key
                # None — usually an unquoted comma ("Acme, Inc"), which shifts
                # every value after it into the wrong column. Guessing which cell
                # went where would import wrong data, so the row is reported and
                # the engine skips it. Blank extras ("a,b,") shift nothing and
                # are dropped quietly.
                surplus = [cell for cell in row.pop(None, []) if cell.strip()]
                # Strip whitespace from keys and values — CSVs are messy
                cleaned = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
                errors = []
                if surplus:
                    errors.append(f"This row has {len(surplus)} more cells than there are columns. "
                                  "A value with a comma in it probably needs quotes.")
                yield ImportRow(row_number=row_num, data=cleaned, errors=errors)
