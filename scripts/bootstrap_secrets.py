"""Create persistent local credentials before Compose starts application services."""
import argparse
import os
from pathlib import Path
import secrets


def write_private(path: Path, value: str):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def credential(directory: Path, filename: str, variable: str) -> str:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / filename
    configured = os.environ.get(variable)
    value = configured or (path.read_text(encoding="utf-8") if path.exists() else secrets.token_urlsafe(32))
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Credentials must be non-empty single-line values")
    if not path.exists() or path.read_text(encoding="utf-8") != value:
        write_private(path, value)
    path.chmod(0o600)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path, default=Path("/run/relayops-db"))
    parser.add_argument("--webhook-dir", type=Path, default=Path("/run/relayops-webhook"))
    args = parser.parse_args()
    password = credential(args.database_dir, "password", "POSTGRES_PASSWORD")
    credential(args.webhook_dir, "secret", "WEBHOOK_SECRET")
    # libpq health probes run independently of the application's entrypoint.
    escaped = password.replace("\\", "\\\\").replace(":", "\\:")
    write_private(args.database_dir / "pgpass", "*:*:*:*:" + escaped + "\n")
    print("Local credentials ready; existing values retained unless explicitly configured.")


if __name__ == "__main__":
    main()
