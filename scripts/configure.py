"""Generate private local configuration and synchronize an existing DB password.

Uses only Python's standard library and Docker. Never deletes database data.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def docker(*args, input=None):
    result = subprocess.run(["docker", *args], cwd=ROOT, input=input, text=True, capture_output=True)
    if result.returncode:
        # Docker/psql errors may contain configuration; do not echo their output.
        raise RuntimeError("Docker operation failed; configuration was not printed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    path = ROOT / args.env_file
    if path.parent != ROOT or not path.name.startswith(".env") or path.name == ".env.example":
        raise ValueError("Use a private .env file in the repository root")
    project = os.environ.get("COMPOSE_PROJECT_NAME", re.sub(r"[^a-z0-9_-]", "", ROOT.name.lower()))
    ids = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=db", "--format", "{{.ID}}").splitlines()
    if len(ids) > 1:
        raise RuntimeError("Multiple database containers found; choose COMPOSE_PROJECT_NAME explicitly")
    existing = json.loads(docker("inspect", ids[0]))[0] if ids else None
    old_env = dict(item.split("=", 1) for item in existing["Config"]["Env"]) if existing else {}
    if not path.exists():
        values = {
            "POSTGRES_USER": old_env.get("POSTGRES_USER", os.environ.get("POSTGRES_USER", "relayops")),
            "POSTGRES_DB": old_env.get("POSTGRES_DB", os.environ.get("POSTGRES_DB", "relayops")),
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "WEBHOOK_SECRET": secrets.token_urlsafe(32),
            "RECEIVER_URL": "http://receiver:9000/webhook", "POLL_INTERVAL": "1", "BATCH_SIZE": "10",
            "GATEWAY_PORT": os.environ.get("GATEWAY_PORT", "8080"),
            "RECEIVER_PORT": os.environ.get("RECEIVER_PORT", "9000"),
        }
        with path.open("x", encoding="utf-8") as output:
            output.write("# Generated local configuration. Never commit this file.\n")
            output.writelines(f"{key}={value}\n" for key, value in values.items())
        path.chmod(0o600)
        print(f"Created private {path.name}; secret values are not displayed.")
    else:
        print(f"Preserving existing {path.name}.")

    config = json.loads(docker("compose", "--env-file", str(path), "config", "--format", "json"))
    db_env = config["services"]["db"]["environment"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for secret in (db_env["POSTGRES_PASSWORD"], config["services"]["worker"]["environment"]["WEBHOOK_SECRET"]):
            # Runner control messages register masks before any tests/logs run.
            print("::add-mask::" + secret)
    if existing:
        user = old_env.get("POSTGRES_USER", "postgres")
        database = old_env.get("POSTGRES_DB", user)
        if (user, database) != (db_env["POSTGRES_USER"], db_env["POSTGRES_DB"]):
            raise RuntimeError("Retain the existing database user/name when upgrading; no data was changed")
        if not existing["State"]["Running"]:
            docker("start", ids[0])
        for attempt in range(30):
            ready = subprocess.run(["docker", "exec", ids[0], "pg_isready", "-U", user, "-d", database],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Existing database did not become ready")
        identifier = '"' + user.replace('"', '""') + '"'
        password = "'" + db_env["POSTGRES_PASSWORD"].replace("'", "''") + "'"
        docker("exec", "-i", ids[0], "psql", "--no-psqlrc", "-U", user, "-d", database,
               "-q", "-v", "ON_ERROR_STOP=1",
               input="SET standard_conforming_strings=on; SET password_encryption='scram-sha-256';\n"
                     f"ALTER ROLE {identifier} WITH PASSWORD {password};\n")
        print("Synchronized the existing database role password through its local maintenance socket; data retained.")
    print("Ready: docker compose up --build")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Configuration failed ({type(exc).__name__}). Check Docker availability and .env settings; no volumes were deleted.", file=sys.stderr)
        raise SystemExit(1) from None
