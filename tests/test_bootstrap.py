"""Exercise actual bootstrap/entrypoint programs without Docker or real secrets."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def initialize(tmp_path, monkeypatch, **configured):
    for name in ("POSTGRES_PASSWORD", "WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    return subprocess.run([
        sys.executable, str(ROOT / "scripts/bootstrap_secrets.py"),
        "--database-dir", str(tmp_path / "database"),
        "--webhook-dir", str(tmp_path / "webhook"),
    ], text=True, capture_output=True, check=True)


def test_bootstrap_generates_private_distinct_persistent_secrets(tmp_path, monkeypatch):
    first = initialize(tmp_path, monkeypatch)
    password = tmp_path / "database/password"
    secret = tmp_path / "webhook/secret"
    original = (password.read_text(), secret.read_text())
    assert all(len(value) >= 40 for value in original)
    assert original[0] != original[1]
    second = initialize(tmp_path, monkeypatch)
    assert (password.read_text(), secret.read_text()) == original
    assert all(value not in first.stdout + first.stderr + second.stdout + second.stderr for value in original)
    assert password.stat().st_mode & 0o777 == 0o600
    assert secret.stat().st_mode & 0o777 == 0o600
    assert password.parent.stat().st_mode & 0o777 == 0o700


def test_bootstrap_preserves_explicit_configuration_and_escapes_pgpass(tmp_path, monkeypatch):
    password = r"configured:password\with-escapes"
    secret = "configured-webhook-key-for-test-only"
    initialize(tmp_path, monkeypatch, POSTGRES_PASSWORD=password, WEBHOOK_SECRET=secret)
    initialize(tmp_path, monkeypatch)
    assert (tmp_path / "database/password").read_text() == password
    assert (tmp_path / "webhook/secret").read_text() == secret
    assert (tmp_path / "database/pgpass").read_text() == "*:*:*:*:configured\\:password\\\\with-escapes\n"


@pytest.mark.parametrize("explicit", [False, True])
def test_runtime_entrypoint_loads_files_and_prefers_explicit_environment(tmp_path, monkeypatch, explicit):
    initialize(tmp_path, monkeypatch)
    environment = os.environ.copy()
    for name in ("PGPASSWORD", "POSTGRES_PASSWORD", "WEBHOOK_SECRET"):
        environment.pop(name, None)
    environment.update({
        "RELAYOPS_DATABASE_PASSWORD_FILE": str(tmp_path / "database/password"),
        "RELAYOPS_WEBHOOK_SECRET_FILE": str(tmp_path / "webhook/secret"),
    })
    if explicit:
        environment.update(PGPASSWORD="explicit-test-password", WEBHOOK_SECRET="explicit-test-key")
    # Assert inside the child process so credentials never appear in its output.
    code = (
        "import os,pathlib; "
        "password=" + ("'explicit-test-password'" if explicit else "pathlib.Path(os.environ['RELAYOPS_DATABASE_PASSWORD_FILE']).read_text()") + "; "
        "secret=" + ("'explicit-test-key'" if explicit else "pathlib.Path(os.environ['RELAYOPS_WEBHOOK_SECRET_FILE']).read_text()") + "; "
        "assert os.environ['PGPASSWORD']==os.environ['POSTGRES_PASSWORD']==password; "
        "assert os.environ['WEBHOOK_SECRET']==secret"
    )
    subprocess.run(["/bin/sh", str(ROOT / "scripts/runtime-entrypoint.sh"), sys.executable, "-c", code],
                   env=environment, check=True, capture_output=True)


def test_runtime_entrypoint_fails_if_credential_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("RELAYOPS_DATABASE_PASSWORD_FILE", str(tmp_path / "missing"))
    result = subprocess.run(["/bin/sh", str(ROOT / "scripts/runtime-entrypoint.sh"), "true"],
                            capture_output=True)
    assert result.returncode != 0
