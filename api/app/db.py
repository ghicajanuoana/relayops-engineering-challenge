import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://relayops:relayops@db:5432/relayops"
)

def get_conn() -> psycopg.Connection:
    """Return a request-owned connection; its context commits or rolls back."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)
