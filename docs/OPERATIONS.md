# Running and verifying RelayOps

Requirements: Docker with Compose v2 and Python 3 for one-time configuration.
The runtime and tests use pinned Linux images and Python dependency locks.

## First startup or upgrade of an existing installation

```bash
python scripts/configure.py
docker compose config --quiet
docker compose up --build
```

Configuration generates private `.env` secrets and preserves an existing `.env`.
For an existing Compose database, it retains its user/database names and updates
the role password through the database container's local maintenance socket.
It never deletes tables or volumes. Stop application traffic during an upgrade:
changing the role password requires restarting API and worker containers with
the updated environment. Rerunning configuration synchronizes the password again.
Use the same `COMPOSE_PROJECT_NAME` when managing a custom project.

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
The worker and receiver always receive the same configured HMAC key.

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

GitHub Actions runs these phases on pushes and pull requests with generated,
isolated credentials, health-gated startup, failure logs, JUnit artifact upload,
and unconditional container cleanup. It needs no repository secrets or cloud
account. Its checkout, artifact upload, and hosted runner execution still need
verification in a real GitHub run.

## Troubleshooting and data preservation

```bash
docker compose ps -a
docker compose logs --tail 200 api worker gateway receiver db
docker compose down
docker compose up --build --wait --wait-timeout 120
```

`down` retains the database volume. Never use `down -v` to upgrade this project.
For a stopped database, configuration starts its existing container before
synchronizing the role password. If existing data predates the API/retry schema,
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
