# Running and verifying RelayOps

Requirements: Docker with Compose v2. Host Python is not required for startup.
The runtime and tests use pinned Linux images and Python dependency locks.

## First startup or upgrade of an existing installation

```bash
docker compose up --build
```

A one-shot `bootstrap` service creates random database and HMAC credentials in
project-scoped `database_credentials` and `webhook_credentials` Docker volumes.
It has no network access and never prints secret values. Files/directories use
permissions `0600`/`0700`; application mounts are read-only. Subsequent starts reuse
the credentials. The runtime entrypoint loads them and uses `exec` so workers
still receive SIGTERM directly. Independent API health probes use a private
libpq password file; worker probes use the same entrypoint as the worker.

Existing `.env`/shell configuration takes precedence and is retained. Existing
database data is not reinitialized, and bootstrap does not change database roles.
Keep the same database user/name/password and Compose project when upgrading.
Changing a configured password alone does not change an existing PostgreSQL role:
the optional `python scripts/configure.py` maintenance tool can synchronize that
role through its local socket. It is not needed for fresh startup or normal restarts.
Back up the credential volumes together with `pgdata`; do not delete either to
upgrade. Lost credentials for an old database require explicit maintenance.

PostgreSQL TCP authentication uses SCRAM-SHA-256 on both new and existing volumes.
Local Unix-socket maintenance stays trusted inside the database container.
PostgreSQL has no published host port. `.env`, `.env.*`, and test results are
ignored by Git; local environment files are also excluded from Docker build context.
Do not commit secrets or share resolved Compose configuration. Use `config --quiet`
for validation. Shell environment variables override `.env` values as usual.

Defaults: gateway `http://localhost:8080`, receiver `http://localhost:9000`.
`GATEWAY_PORT` and `RECEIVER_PORT` can select other ports for isolated runs.
`POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD`, `WEBHOOK_SECRET`,
`RECEIVER_URL`, `POLL_INTERVAL`, and `BATCH_SIZE` configure the services.
Applications use libpq `PG*` variables, avoiding URL escaping for passwords;
explicit `DATABASE_URL` remains supported for external callers/tests.
The worker and receiver receive the same configured or generated HMAC key.
`.env.example` contains optional overrides; leaving secrets blank selects the
persisted generated credentials. Do not use `POSTGRES_HOST_AUTH_METHOD=trust`.

`up --build` starts two workers. For an automated readiness wait:

```bash
docker compose up --build --wait --wait-timeout 120
docker compose ps
```

API container readiness checks HTTP and database schema access; `/health` remains
a liveness endpoint. Receiver and gateway checks exercise HTTP. Worker health
checks a recent, atomically written heartbeat and database access. PostgreSQL
checks TCP readiness. Restart policies recover exited services; Docker does not
automatically restart a container merely because it becomes unhealthy.

## Tests (same commands as CI)

```bash
docker compose stop worker
docker compose up --build --wait --wait-timeout 120 db receiver api gateway
docker compose --profile test run --build --rm tests regression
docker compose up --build --wait --wait-timeout 120 worker
docker compose --profile test run --rm tests smoke
```

Regression tests include unit tests, temporary PostgreSQL databases, live gateway
checks, real receiver HTTP traffic, and worker subprocess crash/shutdown tests.
Keep Compose workers stopped during this phase so they do not change live API
test events. Smoke tests then exercise the full stack with both workers running.
The test runner returns pytest's exit code and writes `test-results/*.xml`.

GitHub Actions runs these phases on pushes, pull requests, and manual dispatch with
Compose-generated isolated credentials, health-gated startup, failure logs, JUnit artifact upload,
and unconditional container cleanup. It needs no repository secrets or cloud
account. Push-triggered CI, checkout, regression/smoke gates, artifact upload, and
cleanup succeeded in [run 40532562527](https://github.com/ghicajanuoana/relayops-engineering-challenge/actions/runs/40532562527)
for commit `96c16b8` using the earlier explicit-configuration startup. The workflow
now exercises automatic bootstrap without running `configure.py`; this revised
workflow still needs a new hosted run after commit/push. Manual dispatch has not
been exercised during this verification.

## Troubleshooting and data preservation

```bash
docker compose ps -a
docker compose logs --tail 200 api worker gateway receiver db
docker compose down
docker compose up --build --wait --wait-timeout 120
```

`down` retains database and credential volumes. Never use `down -v` to upgrade.
For explicit password maintenance, the optional configuration script starts a
stopped database before synchronizing its role. If existing data predates the API/retry schema,
apply only the missing SQL migrations once with `psql --single-transaction`;
initialization scripts run automatically only on a new volume.

API logs retain request IDs and redact known credentials, including tracebacks.
Worker JSON logs include event IDs, attempt numbers, outcomes, and process IDs;
they omit receiver URLs and raw exception text that might contain credentials.
Receiver access logging is disabled to avoid logging query strings. Docker logs
rotate at 10 MB with three files per service.

Exact installed versions are recorded in each `requirements.lock`; intentional
dependency changes must update the corresponding source requirements and lock
together, then rebuild and rerun tests. Image digests must be updated explicitly.
