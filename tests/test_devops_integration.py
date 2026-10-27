import os

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("RELAYOPS_TEST_DATABASE_URL"),
    reason="set RELAYOPS_TEST_DATABASE_URL to verify PostgreSQL authentication",
)


def test_database_tcp_requires_password(database_url):
    params = conninfo_to_dict(database_url)
    params["password"] = "intentionally-invalid-test-password"
    params["connect_timeout"] = "3"
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        psycopg.connect(make_conninfo(**params))
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
