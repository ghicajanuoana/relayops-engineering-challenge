"""One test entry point for local Docker runs and GitHub Actions."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from psycopg.conninfo import make_conninfo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["regression", "smoke"], nargs="?", default="regression")
    args, extra = parser.parse_known_args()
    # Keep credentials in the process environment, not shell commands or URLs.
    conninfo = make_conninfo(**{
        key: os.environ[name] for key, name in (
            ("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
            ("password", "PGPASSWORD"), ("dbname", "PGDATABASE"),
        )
    })
    os.environ["DATABASE_URL"] = conninfo
    os.environ["RELAYOPS_TEST_DATABASE_URL"] = conninfo
    Path("/test-results").mkdir(exist_ok=True)
    selection = ["tests", "--ignore=tests/test_smoke.py"] if args.mode == "regression" else ["tests/test_smoke.py"]
    return subprocess.call([
        sys.executable, "-m", "pytest", *selection, "-q", "-ra", "--tb=short", "-p", "no:cacheprovider",
        f"--junitxml=/test-results/{args.mode}.xml", *extra,
    ])


if __name__ == "__main__":
    raise SystemExit(main())
