"""
SQL source adapter — reads rows from a SQL database.

TEACHING NOTE: this adapter uses SQLAlchemy (already in requirements.txt) to
connect to an external database and run a query. The connection string comes
from the CLI `--url` argument. We read the first row to get column names, then
yield one ImportRow per row.

SECURITY NOTE: the connection string may contain credentials. The ImportRun
record redacts passwords before storing them — see engine.py.
"""

from typing import List

from .base import ImportRow, SourceAdapter


class SqlSource(SourceAdapter):
    """
    Reads import data from a SQL database.

    TEACHING NOTE: SQLAlchemy is a Python library that can talk to many database
    engines (Postgres, MySQL, SQLite, etc.) using a connection string. We use
    its "core" layer (not the ORM) because we just need to run a query and get
    rows back — no model mapping needed.

    The `url` is a SQLAlchemy connection string like:
        postgresql://user:pass@host:5432/dbname
        mysql+pymysql://user:pass@host/dbname
        sqlite:///path/to/db.sqlite3
    """

    def __init__(self, url, query=None):
        self.url = url
        self.query = query or "SELECT * FROM companies"
        self._columns = None

    def _get_engine(self):
        """Create a SQLAlchemy engine from the connection string."""
        from sqlalchemy import create_engine
        return create_engine(self.url)

    def get_columns(self) -> List[str]:
        """Return column names from the query result."""
        if self._columns is None:
            from sqlalchemy import text

            # SQLAlchemy 2 refuses a bare string here; `text()` marks it as SQL
            # on purpose (REVIEW-05: this raised on every call).
            engine = self._get_engine()
            try:
                with engine.connect() as conn:
                    self._columns = list(conn.execute(text(self.query)).keys())
            finally:
                engine.dispose()
        return self._columns

    def read_rows(self):
        """
        Yield ImportRow objects from the SQL query.

        TEACHING NOTE: we use a server-side cursor (stream_results=True) so
        large result sets don't get loaded all at once. Each row becomes a dict
        of STRINGS, the same shape the CSV adapter produces — the engine
        shouldn't have to care that a database handed it an int or a date.
        NULL becomes "" (a blank cell), which the engine treats as "no value".
        """
        from sqlalchemy import text
        engine = self._get_engine()
        try:
            with engine.connect() as conn:
                result = conn.execution_options(stream_results=True).execute(text(self.query))
                self._columns = list(result.keys())
                for row_num, row in enumerate(result, start=1):
                    cleaned = {k: ("" if v is None else str(v).strip()) for k, v in row._asdict().items()}
                    yield ImportRow(row_number=row_num, data=cleaned)
        finally:
            engine.dispose()  # close the pool; a CLI run shouldn't leave connections open
