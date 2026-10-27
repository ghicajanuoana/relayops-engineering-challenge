import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn() -> psycopg.Connection:
    """Return a request-owned connection; its context commits or rolls back."""
    # Empty conninfo uses standard PGHOST/PGUSER/PGPASSWORD/PGDATABASE variables.
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5)
