import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

BASE_URL = os.environ.get("RELAYOPS_BASE_URL", "http://localhost:8080")
RECEIVER_URL = os.environ.get("RELAYOPS_RECEIVER_URL", "http://localhost:9000")


@pytest.fixture(scope="module")
def database_url():
    """Use a dedicated database for each integration test module."""
    admin_url = os.environ["RELAYOPS_TEST_DATABASE_URL"]
    name = "relayops_test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = psycopg.conninfo.make_conninfo(admin_url, dbname=name)
    try:
        with psycopg.connect(url) as conn:
            for path in sorted((ROOT / "db").glob("*.sql")):
                conn.execute(path.read_text(encoding="utf-8"))
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def api():
    try:
        httpx.get(f"{BASE_URL}/health", timeout=2).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"stack not reachable at {BASE_URL}: {exc}")
    with httpx.Client(base_url=BASE_URL, timeout=5) as client:
        yield client


@pytest.fixture
def receiver():
    with httpx.Client(base_url=RECEIVER_URL, timeout=5) as client:
        client.delete("/received")
        yield client
