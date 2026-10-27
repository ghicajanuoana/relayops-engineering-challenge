"""Container readiness: HTTP listener and usable application database schema."""
import urllib.request

from .db import get_conn


def main():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
            if response.status != 200:
                raise ValueError("HTTP unhealthy")
        with get_conn() as conn:
            conn.execute("SELECT next_attempt_at FROM events LIMIT 0")
    except Exception:
        raise SystemExit("API readiness check failed") from None


if __name__ == "__main__":
    main()
