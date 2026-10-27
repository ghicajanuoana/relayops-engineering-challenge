# SOLUTION

## 1. Identified Problems and Solutions

| # | Component | Symptom | Cause | Solution |
|---|------------|---------|-------|---------|
| 1 | FastAPI listener | Gateway `/health` returned `502`; Nginx resolved `api` but its connection was refused. | Uvicorn listened only on container-local `127.0.0.1:8000`. | Changed the listener to `0.0.0.0` in `api/Dockerfile`. |
| 2 | Worker → receiver | `receiver` resolved to `192.0.2.10`; HTTP requests timed out. | `docker-compose.override.yml` replaced service-name resolution with an incorrect `extra_hosts` entry. | Removed the receiver host override. |
| 3 | Nginx event forwarding | After fixing the API listener, a valid small event returned `422` with a missing-body error. | Nginx disabled request-body forwarding and cleared `Content-Length`. | Removed both directives from `infrastructure/nginx.conf`. |
| 4 | Nginx request size | A request containing a 1 MiB payload returned `413`. | `client_max_body_size` was `1k`. | Set it to `2m` to allow a 1 MB payload plus the event envelope. |
| 5 | PostgreSQL startup | Both workers exited with PostgreSQL connection-refused errors on the first startup. | `depends_on` waited for container startup, not database readiness. | Added a TCP `pg_isready` health check and `service_healthy` database dependencies for API and workers in `docker-compose.yml`. |
| 6 | API idempotency | Live tests reproduced duplicates: a replay returned `202`, and both 12-request races returned twelve `202` responses. | Unconditional inserts and no unique event ID constraint. | Added atomic conflict handling in `api/app/main.py` and uniqueness in `db/002_api_constraints.sql`: identical content returns `200`; conflicting content returns `409`. |
| 7 | Event delivery history | Inspection found GET always returned an empty history, even if attempt rows existed. | The handler returned a placeholder instead of querying `delivery_attempts`. | `api/app/main.py` now returns ordered attempts, nullable HTTP codes/errors, and timestamps. Verified using seeded database history; no worker history-writing functionality was implemented. |
| 8 | API transactions | The migration waited on a relation lock; `pg_stat_activity` showed the old API idle in a GET transaction. Inspection also found requests shared a global connection. | Connections and transactions were not scoped to requests. | `api/app/db.py` creates independent connections; handlers in `api/app/main.py` use context managers for commit, rollback, and close. Stopping the old API released the migration lock. |
| 9 | Request validation | Inspection found non-finite numbers and Unicode nulls could pass model validation despite PostgreSQL JSONB/text restrictions. | Payload values were unrestricted and strings had no null-character check. | Added validation in `api/app/models.py` and JSON-safe `422` error responses in `api/app/main.py`. Unit tests verify invalid requests never open a database connection. |
| 10 | Database integrity | Inspection found no uniqueness, status, attempt-range, field-length, or payload-shape guards beyond the original keys and nullability. | The initial schema did not enforce these invariants. | `db/002_api_constraints.sql` adds event constraints, unique attempt numbers per event, and attempt/HTTP-code checks; the existing foreign key remains. Invalid writes and transaction rollback were tested against PostgreSQL. |
| 11 | Worker HMAC signature | Earlier networking tests observed receiver `401` responses with `signature_valid=false`. | `worker/worker.py` signed `json.dumps` output but let HTTPX serialize the transmitted body separately. | Step 4 serializes once to UTF-8 bytes, signs those bytes, and sends them unchanged. The unchanged receiver accepted signatures, including Unicode payloads. |
| 12 | Worker delivery status | Inspection found any HTTP response, including `401` or `503`, was treated as successful delivery. | The worker returned success without checking the response status. | `worker/worker.py` now marks only `2xx` as delivered and disables redirects. Tests verify non-2xx responses and network errors are recorded as failed. |
| 13 | Worker attempt history | Inspection found the worker never inserted delivery-attempt rows; only final event counters/state were updated. | Delivery outcome recording was missing. | `record_attempt()` in `worker/worker.py` inserts the attempt and updates the event in one transaction. Real PostgreSQL tests verify HTTP codes, network errors, API history, and rollback of both writes on an update failure. |
| 14 | Temporary delivery failures | Step 4 intentionally marked temporary failures failed after one attempt. | Retry classification, backoff, and `Retry-After` handling were deferred. | Step 5 adds these in `worker/worker.py`: retry network errors, timeouts, `429`, and `5xx`; fail permanent errors immediately; stop after three recorded attempts. |
| 15 | HTTP attempt deadline | Inspection found the five-second HTTPX timeout limited individual network phases, not total request duration. | No overall HTTP deadline existed. | `deliver()` now wraps the asynchronous request in `asyncio.wait_for(..., timeout=5)`. A real delayed-response test verifies timeout recording even with HTTPX phase timeouts disabled. |
| 16 | Persistent retry scheduling | Inspection found no stored next-attempt time; recovery of scheduled retries was unavailable. | The schema and pending-event query had no retry schedule. | `db/003_retry_schedule.sql` adds `next_attempt_at` and a partial index. `worker/worker.py` atomically records the schedule with the attempt and selects only due events, allowing other pending events to proceed. |
| 17 | Concurrent worker ownership | A two-worker test reproduced two webhook deliveries for one event; one worker then raised `UniqueViolation` on the attempt-history constraint. | Workers selected pending rows without holding an exclusive claim through delivery. | `fetch_pending()` and `process_pending()` in `worker/worker.py` now use `FOR UPDATE SKIP LOCKED` inside a transaction covering one event's delivery and recording. |
| 18 | In-flight crash recovery | Inspection found no exclusive in-flight ownership; previous recovery tests covered only already-scheduled retries. | Selection and outcome recording were separate transactions, and the delivery crash window was untested. | `worker/worker.py` holds a transactional row lock until commit. A crash rolls back unfinished writes and releases ownership; `tests/test_worker_reliability.py` verifies SIGKILL recovery and allowed redelivery after remote success. |
| 19 | Worker shutdown | Inspection found no graceful SIGTERM handling and shell-form Docker startup. | The polling loop had no stop signal; the shell command did not launch Python directly as the container process. | `run()` in `worker/worker.py` handles SIGTERM/SIGINT, finishes the current attempt, and stops claiming work. `worker/Dockerfile` uses exec-form CMD. Process and container tests verified clean exit. |
| 20 | Request tracing | The baseline gateway health response had no `X-Request-ID`; inspection found no request-ID logging or propagation. | Nginx and FastAPI had no shared request-ID handling. | `infrastructure/nginx.conf` generates or preserves IDs, forwards them, and adds them to every response. New `api/app/observability.py` and changes in `api/app/main.py` return and log IDs, including application failures. |
| 21 | Prometheus metrics | Baseline `GET /metrics` returned `404`. | The endpoint was missing. | `api/app/main.py` now returns all four event-status gauges and the recorded delivery-attempt counter in Prometheus text format. Tests cover empty and populated databases. |
| 22 | PostgreSQL host exposure | `docker compose port db 5432` returned `127.0.0.1:64438`. | The automatically loaded override published PostgreSQL outside its container network to the host. | Removed port publishing from `docker-compose.override.yml`. Runtime inspection confirmed no host binding; API and worker database connections still succeeded. |
| 23 | Hardcoded credentials | Inspection found embedded database credentials, a fixed webhook secret, and an unused example environment configuration. | Compose and application defaults supplied credentials directly. | `scripts/configure.py` generates ignored local secrets; `.env.example` documents settings; `docker-compose.yml`, `api/app/db.py`, and `worker/worker.py` use environment configuration without credential defaults. |
| 24 | PostgreSQL authentication | Inspection found TCP `trust` authentication; changing container environment alone would not update an existing database role password. | Authentication rules and role credentials persist beyond container recreation. | `scripts/configure.py` synchronizes the existing role through its local maintenance socket. `infrastructure/pg_hba.conf` requires SCRAM-SHA-256 for TCP; the database volume and existing user/database names are retained. |
| 25 | Container readiness | Only PostgreSQL had a container health check; other service readiness and worker progress were not checked. | Docker configuration lacked health probes and sufficient health-gated dependencies. | `docker-compose.yml` adds checks for all services. `api/app/healthcheck.py` verifies HTTP and schema access; `worker/worker.py` maintains an atomic heartbeat and checks database access. |
| 26 | Sensitive logging | Inspection found worker startup logged the receiver URL and stored raw network exception text, which could contain credentials. | Logging did not separate safe diagnostic metadata from sensitive configuration. | Worker JSON logs omit URLs and raw exceptions; `api/app/observability.py` and `api/logging.json` redact configured credentials, including tracebacks. Dockerfiles disable redundant API/receiver access logging; runtime logs are rotated. |
| 27 | Reproducibility | Inspection found floating image tags and unpinned transitive Python dependencies. | Top-level requirement pins did not fix the complete runtime environment. | Dockerfiles and Compose pin image digests; service/test `requirements.lock` files pin installed versions. `tests/Dockerfile` provides a consistent test environment, and `.dockerignore` excludes private configuration. |
| 28 | CI automation | `.github/workflows/ci.yml` only printed a TODO. | No automated startup, test gates, reports, or cleanup existed. | The workflow now runs health-gated dependency startup, regressions, two-worker smoke tests, failure diagnostics, JUnit upload, and volume-preserving cleanup using `tests/run_tests.py`. |

## 2. Networking Diagnosis

- Inspected Compose, its automatically loaded override, Dockerfiles, and Nginx configuration. Existing shared networks and service ports matched the intended communication paths; no network topology changes were needed.
- `docker compose ps` initially failed with Docker engine access denied. Retried with elevated execution; Docker became accessible.
- `docker compose up --build -d` started the original stack. `docker compose ps -a` showed both workers exited. `docker compose logs --tail 12 gateway worker` showed PostgreSQL connection-refused errors. After `docker compose start worker`, database queries succeeded.
- `docker compose exec -T gateway sh -c 'nslookup api; wget -S -O - http://api:8000/health'`: before the listener fix, DNS returned the API container address but HTTP failed with connection refused; afterward, HTTP returned `200`.
- Python probes passed through stdin with `docker compose exec -T api python -` confirmed the original API responded on `127.0.0.1:8000`. Worker probes using `docker compose exec -T --index 1 worker python -` confirmed receiver resolution to `192.0.2.10` and an HTTP connection timeout.
- After fixing the listener, API-container Python probes sent JSON through `http://gateway/events`: a small request returned `422` (missing body), and a request with a 1 MiB payload returned `413`. After the Nginx fixes, both returned `202`; GET requests returned `200` with unchanged payloads.
- `docker compose config --quiet` and `docker compose exec -T gateway nginx -t` passed. Reloaded Nginx with `docker compose exec -T gateway nginx -s reload`.
- Ran `docker compose down` followed by `docker compose up --build -d`, preserving the database volume. Compose waited for a healthy database; API and both worker replicas started successfully.
- Final Python probes in API and both worker replicas verified database DNS and `SELECT 1`; worker probes also verified database TCP connectivity, receiver DNS resolving to its container address, and receiver `/health` returning `200`.
- Host `curl.exe` probes returned `200` for `http://localhost:8080/health` and `http://localhost:9000/health`. Final `docker compose ps -a` showed all services running and PostgreSQL healthy.
- Receiver `/received` inspection confirmed the existing workers reached `/webhook`, but their requests returned `401` with `signature_valid=false`. The signature bug remains unfixed; connectivity verification does not establish successful delivery. Database localhost port publishing was also left unchanged.

**Step 7:** Rechecked existing behavior before changing configuration, then verified database isolation and tracing:

- `docker compose exec -T gateway nginx -t` passed. `docker compose exec -T gateway sh -c 'nslookup api; wget -S -O - http://api:8000/health'` resolved the API and returned `200`. Python probes through `docker compose exec -T api python -` confirmed gateway `/health` returned `200` and a 1 MiB payload received `202` through Nginx, then `200` with unchanged content on GET. Existing forwarding, body limit, and health behavior were retained.
- `docker compose port db 5432` identified the localhost binding above. After its removal, `docker compose config --format json` had no database ports. Runtime inspection used:

```powershell
$dbContainer = docker compose ps -q db
docker inspect --format '{{json .HostConfig.PortBindings}} {{json .NetworkSettings.Ports}}' $dbContainer
```

- Inspection returned `{}` and `{"5432/tcp":null}`: PostgreSQL remained exposed inside its container without host publishing. A host Python `socket.create_connection` probe to the former port timed out; it did not establish a connection.
- Python scripts through `docker compose exec -T api python -` and `docker compose run --rm --no-deps -T worker python -` verified `socket.gethostbyname`, TCP connections to `db:5432`, and PostgreSQL `SELECT 1`. The worker-network probe also resolved and connected to `api:8000` and `receiver:9000`, with receiver `/health` returning `200`. The API could reach gateway `/health` with `200`.
- `docker compose exec -T gateway nslookup db` returned `NXDOMAIN`. A Python DNS probe through `docker compose exec -T receiver python -` raised `socket.gaierror` for `db`, confirming that the database name was not resolvable from the receiver network.
- Sent client ID `step7-provided-log-check` and a request without an ID through the gateway. Responses contained one ID each; Nginx and API logs contained the same provided and generated IDs. Read the matching raw log lines with:

```powershell
docker compose logs --since 3m api gateway | Select-String -Pattern 'step7-provided-log-check|545382448a23a1f733e911b0215c3511' | ForEach-Object { $_.Line }
```

## 3. Technical Decisions

- **Idempotency:** PostgreSQL enforces a unique `event_id`. `INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id, status` creates a new event with `202`. On conflict, a separate SELECT compares `type` and JSONB payload and returns the existing current status with `200`, or `409` for different content. Replays do not update timestamps, counters, or delivery state.
- **Content comparison:** JSONB equality ignores object key order and treats equivalent JSON numbers consistently, while distinguishing booleans from numbers and preserving array order. Tests cover these cases.
- **Concurrency:** Under PostgreSQL's default READ COMMITTED isolation, competing inserts wait for the winning transaction. The following SELECT uses a new statement snapshot and sees the committed winner. The unique constraint prevents duplicates across concurrent API requests without process-local locks.
- **Transactions:** Each request owns a connection. Connection context managers commit on success, roll back on exceptions, and close afterward. Tests verify rollback after a constraint violation and no remaining connections after successful and unsuccessful API requests. GET retrieves the event and history in one statement for a consistent snapshot.
- **Constraints and migration:** `db/002_api_constraints.sql` enforces event ID uniqueness, 1–200 character identifiers/types, object payloads, valid statuses, 0–3 event attempts, unique `(event_pk, attempt)`, attempt numbers 1–3, and nullable HTTP codes restricted to 100–599. Fresh database initialization applies both schema files; existing volumes require the new migration once. Run it transactionally with the old API and workers stopped. Invalid legacy data aborts migration rather than being deleted.
- **Validation:** Required fields and their types/lengths are validated before database access. PostgreSQL-incompatible null characters and non-finite numbers are rejected with `422`. Validation responses retain error location, type, and message while omitting values that cannot safely be serialized as JSON.
- **Test isolation:** PostgreSQL integration tests create a uniquely named temporary database, apply both SQL files, and drop it afterward. Live HTTP tests use unique event IDs and remove only their own rows. Workers are stopped during API verification; webhook delivery, retries, and worker concurrency remain outside this step.

- **Step 4 delivery:** `fetch_pending()` reads pending events in ID order with a batch limit. `process_pending()` sends each event once and records its outcome. `main()` polls using a shared HTTPX client configured with a five-second timeout. No work claiming or multiple-worker coordination was added.
- **Step 4 signing:** `serialize_event()` includes only `event_id`, `type`, and `payload` in compact UTF-8 JSON. `sign()` calculates HMAC-SHA256 using `WEBHOOK_SECRET`; `deliver()` sends the identical bytes with `Content-Type: application/json` and `X-RelayOps-Signature: sha256=<hex>`. Receiver implementation and API contract remain unchanged.
- **Step 4 outcomes:** `DeliveryResult` distinguishes HTTP responses from network errors. Only codes 200–299 are successful. Every other HTTP response is recorded with its status code and a null error; network failures have a null status code and an error containing the exception type and description.
- **Step 4 persistence:** `record_attempt()` increments the stored attempt count, inserts its history row, and updates event status and `updated_at` in one transaction. A database failure rolls both writes back; the HTTP request itself cannot be rolled back. Crash recovery and worker concurrency remain deferred.
- **Step 4 failure policy:** All unsuccessful attempts, including `429`, `5xx`, and network errors, currently end in `failed` after one attempt. Later polls select only pending events, so failures are not retried automatically. The old immediate retry loop was removed; retry scheduling, backoff, and `Retry-After` handling are deferred to Step 5.
- **Step 4 test isolation:** API and worker integration tests share the temporary-database fixture in `tests/conftest.py`. Worker integration tests start the existing receiver over real HTTP and run delivery workers sequentially, including one-process tests. `tests/requirements.txt` now includes Uvicorn for the receiver. Container smoke tests explicitly use one worker replica.

- **Step 5 error policy:** Network errors, timeouts, HTTP `429`, and `500`–`599` are temporary. Other non-2xx responses, including redirects, `400`, and `401`, fail immediately. A successful third attempt is delivered; an unsuccessful third attempt is failed. Existing database constraints continue to limit counters and history attempt numbers to three.
- **Step 5 backoff:** `retry_delay()` uses `max(2 ** (attempt - 1), retry_after_seconds)`: one second after attempt 1 and two seconds after attempt 2, or longer when required by `Retry-After`. `parse_retry_after()` accepts non-negative integer seconds; absent or malformed values fall back to exponential backoff. HTTP-date headers are outside this challenge's seconds-only contract.
- **Step 5 deadline:** `deliver()` uses an HTTPX asynchronous client and a five-second `asyncio.wait_for` deadline around the entire HTTP request, in addition to HTTPX phase timeouts. Timeout outcomes have a null HTTP status and a descriptive error. Events are still delivered sequentially by one worker; asynchronous HTTP is used for the deadline, not multiple-event concurrency.
- **Step 5 persistence:** `record_attempt()` commits the history row, counter, status, and next due time together. Temporary failures below the limit remain `pending`; their due time is computed using PostgreSQL `clock_timestamp()` plus the delay after the response. Terminal outcomes clear the schedule. `fetch_pending()` selects only due pending events with fewer than three attempts, ordered by due time and ID. No per-event backoff sleep blocks other pending work.
- **Step 5 restart scope:** A restarted worker reads the saved attempt count and due time instead of resetting them. Tests stop a worker only after its retry transaction commits, then start its replacement. Recovery of an in-flight HTTP delivery and multiple-worker coordination remain deferred to Step 6. Existing failed events are not automatically requeued by the migration.

- **Step 6 ownership:** `FOR UPDATE SKIP LOCKED` combines exclusive row ownership with non-blocking queue selection. Each worker locks one due event, performs its HTTP attempt, and commits its history, counter, status, and retry schedule before taking another event. Other workers skip the locked row, preventing simultaneous normal processing while continuing with other work. Workers do not reserve an entire batch ahead of delivery.
- **Step 6 recovery:** Ownership is the database lock, not a separately committed `processing` flag or expiring lease. On process/connection failure, PostgreSQL rolls back the transaction and releases the lock; a surviving worker can select the unfinished event using its previously committed counter and schedule. API readers see the previous committed state until the outcome commits. No schema migration was needed.
- **Step 6 delivery guarantee:** PostgreSQL cannot atomically commit a transaction with an external HTTP receiver. If the receiver accepts a request and the worker crashes before committing, recovery sends it again. This preserves at-least-once recovery and permits duplicates in that failure window. The three-attempt limit applies to recorded attempts; an aborted transaction can leave an HTTP request without a committed history row. Exactly-once delivery is not claimed.
- **Step 6 trade-off:** Holding a transaction open across HTTP occupies a database connection and row lock during the bounded five-second request plus database work. This costs database resources compared with short claim transactions, but gives automatic rollback-based recovery without lease expiry or stale ownership. Locks cover one event only, and competing workers skip them.
- **Step 6 shutdown:** SIGTERM/SIGINT sets an asynchronous stop event without cancelling the active delivery. The worker records that attempt's success, failure, or timeout, then takes no further work and closes its connections. Idle polling waits on the stop event and wakes immediately. Exec-form Docker CMD lets Python receive the container signal directly.

- **Step 7 request IDs:** Nginx maps an absent/empty client ID to `$request_id` and otherwise preserves it. `proxy_set_header` forwards that value to FastAPI; `add_header ... always` returns it even on gateway errors. Nginx hides the upstream copy to avoid duplicate headers. FastAPI middleware preserves incoming IDs or generates a UUID hex value for direct requests, stores it in request state, and adds the response header. Its unhandled-error handler also returns the ID because those responses are generated outside the user middleware stack.
- **Step 7 logs:** Nginx access logs and API request logs use JSON with request ID, method, path, and status; Nginx also includes upstream status. API logs use the existing Uvicorn logger and record server failures at error level. Worker logging and delivery behavior were unchanged.
- **Step 7 metrics:** `/metrics` uses one PostgreSQL statement for a consistent snapshot, counts stored events by status, and counts delivery-history rows. It emits all four status samples, including zeros, with Prometheus HELP/TYPE lines and `text/plain; version=0.0.4; charset=utf-8`. These reflect committed database states; Step 6's in-flight locks do not introduce a committed `processing` state. `/health` remains the existing database-independent liveness endpoint.
- **Step 7 isolation:** Removed only the override's PostgreSQL port publication. API and workers continue using `db:5432` on the existing backend network; gateway and receiver remain on separate networks without database DNS access. The database volume and schema were retained. The existing 2 MiB Nginx body limit already accommodates a 1 MiB payload plus its JSON envelope and was not changed.

- **Step 8 configuration:** `python scripts/configure.py` generates random database/HMAC secrets in a Git-ignored `.env` and preserves an existing file. Compose requires non-empty secrets rather than supplying insecure defaults. API/worker database connections use standard libpq `PG*` environment variables, avoiding password URL-escaping problems; explicit `DATABASE_URL` remains supported without an embedded fallback. Worker and receiver receive the same configured HMAC key. Private environment files are excluded from the root Docker build context.
- **Step 8 authentication:** Existing installations synchronize the configured password with the existing PostgreSQL role over the container-local maintenance socket before using the updated environment. Mounted authentication rules require SCRAM-SHA-256 for IPv4/IPv6 TCP; trusted Unix-socket maintenance remains confined to the database container. Password changes do not reinitialize data or change database/user names. PostgreSQL remains unpublished on the host.
- **Step 8 health and restart:** PostgreSQL checks TCP readiness; API checks HTTP liveness plus schema access; gateway and receiver check HTTP. Workers check a recent, atomically replaced heartbeat plus schema access. Health-gated dependencies prevent premature startup. Runtime services use `unless-stopped`, with a ten-second worker shutdown grace period. Restart policies handle process exits; an unhealthy status alone does not automatically restart Docker containers. `/health` retains its previous liveness contract.
- **Step 8 logging:** API formatting retains request IDs and redacts known credentials in messages and tracebacks. Workers log event IDs, PID, attempt, status, and exception type without receiver URLs or raw network exception text. Receiver access logging is disabled to avoid query-string logging. Runtime Docker logs rotate at 10 MB with three files per container; output is unbuffered.
- **Step 8 reproducibility:** Python, PostgreSQL, and Nginx base images are pinned by digest. API, worker, receiver, and test dependency locks capture exact installed package versions while retaining source requirements. The test-profile image includes application/schema/test sources and the locked test environment, avoiding ad hoc package installation during CI test execution. Startup, upgrades, and verification are documented in `docs/OPERATIONS.md` and linked from README.
- **Step 8 CI:** Pushes and pull requests run on an Ubuntu runner with read-only repository permissions and pinned checkout/artifact actions. CI generates isolated credentials, starts healthy database/API/gateway/receiver services, and runs regressions with Compose workers absent. It then starts two workers and runs smoke tests. `tests/run_tests.py` constructs connection settings in the process environment, returns pytest's exit code, and writes JUnit XML. Failures collect diagnostics; report upload and `docker compose down` run unconditionally without deleting volumes. No repository secrets or cloud deployment are required.

## 4. Starting and Verifying the System

Step 3 used the existing Compose database and gateway. For an existing volume, stop the old API before applying the migration once:

```powershell
docker compose stop worker
docker compose stop api
docker compose exec -T db psql -U relayops -d relayops --single-transaction -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/002_api_constraints.sql
docker compose up --build -d api
docker compose exec -T gateway nginx -s reload
```

Both migration statements completed with `ALTER TABLE`. On a fresh database volume, initialization applies the migration automatically.

The final scoped test command was:

```powershell
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_TEST_DATABASE_URL=postgresql://relayops:relayops@db:5432/relayops -e RELAYOPS_BASE_URL=http://gateway api sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_models.py tests/test_api_unit.py tests/test_api_integration.py tests/test_api_http.py -v -ra -p no:cacheprovider'
```

- **Final result:** 49 passed in 4.24 seconds, exit code 0, no skips. One third-party Starlette/AnyIO deprecation warning was reported.
- **Unit tests:** 25 passed across `tests/test_models.py` and `tests/test_api_unit.py`, covering field validation, JSON restrictions, and rejection before database access.
- **PostgreSQL integration tests:** 20 passed in `tests/test_api_integration.py`, covering persistence, response fields, current status on replay, content conflicts, ordered history, nullable errors/codes, timestamps, constraints, rollback, connection cleanup, and concurrency.
- **Live HTTP tests:** 4 passed in `tests/test_api_http.py` against Nginx and the rebuilt API, including a 1 MiB payload round trip. Verified `202` for creation, `200` for retrieval and identical replay, `409` for changed content, `422` for invalid input, and `404` for an unknown event.
- **Concurrent requests:** Both PostgreSQL-backed application tests and live HTTP tests ran 12 simultaneous identical submissions and 12 simultaneous conflicting submissions. Each race produced one `202`, with eleven `200` or eleven `409` responses respectively; database queries confirmed exactly one stored event.
- **Before/after evidence:** Running the live HTTP tests against the original API produced 3 failures and 1 pass in 1.40 seconds. Duplicate submissions returned `202` instead of `200`, and each race returned twelve `202` responses. The final suite passed after migration and rebuild.
- **Migration diagnosis:** A `pg_stat_activity` query selecting PID, state, wait events, and query text showed the migration waiting on a relation lock and the old API idle in a GET transaction. `docker compose stop api` released the lock and allowed migration to finish.
- Delivery smoke tests were not run in this step because delivery logic remains unchanged. Workers were left stopped after API verification.

**Step 4:** Ran the API regressions and new worker tests with Compose workers stopped:

```powershell
docker compose stop worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_TEST_DATABASE_URL=postgresql://relayops:relayops@db:5432/relayops -e RELAYOPS_BASE_URL=http://gateway api sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_models.py tests/test_api_unit.py tests/test_api_integration.py tests/test_api_http.py tests/test_worker_unit.py tests/test_worker_integration.py -q -ra --tb=short -p no:cacheprovider'
```

- **Result:** 79 passed in 9.59 seconds, exit code 0, no failures or skips. This includes all 49 Step 3 API tests and 30 worker tests (11 unit and 19 integration tests). One third-party Starlette/AnyIO deprecation warning was reported.
- Worker tests verify a known HMAC-SHA256 vector, exact payload fields, Unicode delivery, successful and unsuccessful HTTP statuses, invalid signatures, connection refusal, timeout recording, ordered/bounded pending selection, atomic database rollback, and no automatic second attempt. Receiver `/received` confirms real HTTP arrivals and signature validity. Separate process tests run one worker at a time for `200` and `503` outcomes.

Built and started exactly one Compose worker, then ran the existing smoke tests:

```powershell
docker compose up --build -d --scale worker=1 worker
docker compose ps worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_BASE_URL=http://api:8000 -e RELAYOPS_RECEIVER_URL=http://receiver:9000 worker sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_smoke.py -v -ra --tb=short -p no:cacheprovider'
docker compose ps worker
docker compose logs --tail 8 worker
```

- **Smoke result:** 2 passed in 1.73 seconds, exit code 0, no failures or skips. These tests use the API and receiver service addresses directly. `docker compose ps worker` showed one running worker; its logs showed attempt 1, HTTP `200`, and status `delivered` for smoke-test events.
- The container running pytest overrides the worker command and does not run another delivery worker. One Compose delivery worker was left running. Continue using `--scale worker=1` until multiple-worker coordination is implemented; the default replica configuration was not changed.
- `git diff --check` passed. API application code, schema, and Mock Receiver were unchanged in Step 4.

**Step 5:** Stopped the Compose worker and ran the scoped regressions against isolated test databases and the unchanged receiver over real HTTP. The final command included added coverage for successful delivery on attempt 3:

```powershell
docker compose stop worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_TEST_DATABASE_URL=postgresql://relayops:relayops@db:5432/relayops -e RELAYOPS_BASE_URL=http://gateway api sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_models.py tests/test_api_unit.py tests/test_api_integration.py tests/test_api_http.py tests/test_worker_unit.py tests/test_worker_integration.py -q -ra --tb=short -p no:cacheprovider'
```

- **Regression result:** 118 passed in 36.45 seconds, exit code 0, no failures or skips. Includes all 49 API tests, 36 worker unit tests, and 33 worker integration tests. One third-party Starlette/AnyIO deprecation warning was reported.
- Tests verify immediate success, temporary HTTP failures followed by success on attempt 2 or 3, permanent failures, connection refusal, timeouts, three-attempt exhaustion, exponential backoff, `Retry-After`, and processing another event before a scheduled retry is due. Step 4 tests were updated to expect scheduled retries for temporary failures instead of the former single-attempt terminal policy.
- **Deadline verification:** Delayed the receiver response by 5.5 seconds and disabled the test client's HTTPX phase timeouts. The worker's five-second overall deadline fired and recorded a null HTTP code with a `TimeoutError`; the event remained pending for retry.
- **Restart verification:** A subprocess worker received `503` with `Retry-After: 3` and committed attempt 1 plus its due time. After stopping that process, a replacement worker delivered attempt 2 with `200`. Assertions confirmed the saved due time was unchanged across restart and the second request arrived no earlier than that time or the three-second delay.

Applied the additive migration once to the existing volume while the worker was stopped:

```powershell
docker compose exec -T db psql -U relayops -d relayops --single-transaction -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/003_retry_schedule.sql
docker compose build worker
docker compose up -d --no-build --scale worker=1 worker
```

- Migration output was `ALTER TABLE`, `UPDATE 7`, and `CREATE INDEX`. Before/after count and checksum queries through `docker compose exec -T db psql -U relayops -d relayops -At` confirmed all original fields in 7 event rows and all 4 delivery-history rows were unchanged. The checksum excluded only the newly added `next_attempt_at` field. An isolated integration test also verified migration preserves existing event data and history.

Ran the existing smoke tests against one Compose worker:

```powershell
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_BASE_URL=http://api:8000 -e RELAYOPS_RECEIVER_URL=http://receiver:9000 worker sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_smoke.py -v -ra --tb=short -p no:cacheprovider'
docker compose ps worker
```

- **Smoke result:** 2 passed in 1.14 seconds, exit code 0, no failures or skips. Compose showed exactly one delivery worker running.
- **Live retry probe:** A Python script passed through stdin to `docker compose exec -T worker python -` configured receiver `/control` with `fail_next=2`, `status_code=503`, and `retry_after=2`, then created an event through the API. API history showed `503 → 503 → 200`, status delivered, and three attempts. Receiver capture verified valid signatures and retry gaps of 2.006 and 2.008 seconds, measured from the previous response to the next arrival.
- **Live permanent failure:** The same probe configured one `400` response. The event became failed with one recorded attempt; receiver inspection after another polling interval showed no retry. Receiver control was reset afterward. `docker compose logs --tail 10 worker` confirmed these outcomes.
- `git diff --check` passed. Only worker code, its two test modules, and the new retry migration changed during implementation. API application code, Mock Receiver, and `SOLUTION.md` were unchanged during Step 5 implementation; one worker replica was left running.

**Step 6:** The targeted `tests/test_worker_reliability.py::test_two_workers_deliver_one_event_only_once` test first failed against the Step 5 worker: the receiver captured two deliveries and one worker exited with a duplicate attempt-history `UniqueViolation`. After the fix, the full regression command was:

```powershell
docker compose stop worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_TEST_DATABASE_URL=postgresql://relayops:relayops@db:5432/relayops -e RELAYOPS_BASE_URL=http://gateway api sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_models.py tests/test_api_unit.py tests/test_api_integration.py tests/test_api_http.py tests/test_worker_unit.py tests/test_worker_integration.py tests/test_worker_reliability.py -q -ra --tb=short -p no:cacheprovider'
```

- **Regression result:** 127 passed in 57.86 seconds, exit code 0, no failures or skips. Includes the previous 118 tests and nine reliability tests. One third-party Starlette/AnyIO deprecation warning was reported.
- **Concurrency:** Two real worker processes delivered one event once and a queue of 20 events once each. An explicitly locked event did not block delivery of another event. A two-worker retry test preserved three recorded attempts (`503 → 503 → 200`) and the required one- and two-second delays.
- **Crash recovery:** SIGKILL during an unfinished delivery left no committed attempt; a replacement worker delivered the event and recorded one outcome. A separate test paused history insertion after receiver success, killed the owner, and verified recovery by a surviving worker. The receiver captured two successful requests while PostgreSQL retained one committed attempt, demonstrating the allowed at-least-once duplicate.
- **Graceful shutdown:** SIGTERM during delivery completed and recorded the current event, left the next event pending, released the lock, and exited with code 0. An idle worker with a 60-second poll interval exited within two seconds. A worker awaiting a delayed response recorded the bounded timeout and retry schedule, released ownership, and exited with code 0 within six seconds.

Built the image and ran the existing smoke tests with two Compose replicas:

```powershell
docker compose build worker
docker compose up -d --no-build --scale worker=2 worker
docker compose ps worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_BASE_URL=http://api:8000 -e RELAYOPS_RECEIVER_URL=http://receiver:9000 worker sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_smoke.py -v -ra --tb=short -p no:cacheprovider'
```

- **Smoke result:** 2 passed in 0.63 seconds, exit code 0, no failures or skips. Compose showed two running workers using `python worker.py` directly. The pytest container did not run an additional delivery worker.

Verified Docker SIGTERM handling and restored both replicas:

```powershell
docker compose stop -t 7 worker
$workerContainers = @(docker compose ps -a -q worker)
docker inspect --format '{{.Name}} exit={{.State.ExitCode}}' $workerContainers
docker compose logs --tail 8 worker
docker compose up -d --no-build --scale worker=2 worker
docker compose ps worker
```

- Both containers reported `exit=0`; both logs included `shutdown requested; finishing current attempt` and `worker stopped`. Final Compose output showed two workers running.
- `git diff --check` passed. Step 6 changed only `worker/worker.py`, `worker/Dockerfile`, and the new `tests/test_worker_reliability.py`; API, Mock Receiver, schema, and solution documentation were unchanged during implementation.

**Step 7:** Recreated the database without host publishing, rebuilt the API, validated/reloaded Nginx, and ran all non-smoke tests with Compose workers stopped:

```powershell
docker compose stop worker
docker compose up --build -d db api
docker compose exec -T gateway nginx -t
docker compose exec -T gateway nginx -s reload
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_TEST_DATABASE_URL=postgresql://relayops:relayops@db:5432/relayops -e RELAYOPS_BASE_URL=http://gateway api sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests --ignore=tests/test_smoke.py -q -ra --tb=short -p no:cacheprovider'
```

- **Regression result:** 155 passed in 57.91 seconds, exit code 0, no failures or skips. Includes all 127 previous regressions and 28 new tests in `tests/test_observability_unit.py`, `tests/test_observability_integration.py`, and `tests/test_gateway_http.py`. One third-party Starlette/AnyIO deprecation warning was reported.
- **Request-ID coverage:** Generated and client-provided IDs were tested on gateway `200`, `202`, `409`, `422`, `404`, `405`, and gateway-generated `413` responses. Tests assert exactly one header and distinct generated IDs across requests. Direct API tests verify matching log entries and IDs on unhandled `500` responses. End-to-end log inspection is recorded in Section 2.
- **Metrics coverage:** Verified all-zero samples for an empty database. Seeded PostgreSQL tests returned pending `2`, processing `1`, delivered `1`, failed `1`, and delivery attempts `3`, with correct text format and no mutation during repeated scrapes. The live gateway returned `/metrics` with `200` and the required content type. Existing 1 MiB payload regression tests also passed.
- **Gateway failure coverage:** After `docker compose stop api`, host Python requests to gateway `/health` using `urllib.request.urlopen` with `timeout=15` received actual `504` responses. Both missing-ID and provided-ID requests returned exactly one correct `X-Request-ID`. `docker compose start api` and Nginx reload restored gateway `/health` to `200`.
- Database DNS/TCP/query checks, host-port removal, and network isolation observations are recorded in Section 2. No schema migration or worker changes were required.

Restored two workers and ran the existing delivery smoke tests:

```powershell
docker compose up -d --no-build --scale worker=2 worker
docker compose run --rm --no-deps -v "${PWD}:/workspace" -w /workspace -e PYTHONDONTWRITEBYTECODE=1 -e RELAYOPS_BASE_URL=http://api:8000 -e RELAYOPS_RECEIVER_URL=http://receiver:9000 worker sh -c 'pip install -q -r tests/requirements.txt && python -m pytest tests/test_smoke.py -v -ra --tb=short -p no:cacheprovider'
docker compose ps
```

- **Smoke result:** 2 passed in 0.64 seconds, exit code 0, no failures or skips. Final Compose output showed all services running, PostgreSQL healthy with only internal `5432/tcp`, and two worker replicas.
- `git diff --check` passed. Step 7 changed Nginx configuration, the Compose override, API tracing/metrics, and three new test files. Worker code, Mock Receiver, schema, and solution documentation were unchanged during implementation.

**Step 8:** Configuration is now required before the first startup. The verified automated startup command was:

```powershell
python scripts/configure.py
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 120
docker compose ps
```

- All six runtime containers became healthy, including two workers. After configuration, the normal foreground startup remains `docker compose up --build`. Re-running configuration preserved the existing `.env` content and synchronized the same database password without deleting data.
- **Upgrade preservation:** Before/after checksum queries through `docker compose exec -T db psql -U relayops -d relayops -At` confirmed all 20 existing event rows and 21 history rows were unchanged. Authentication-rule inspection returned local `trust` and TCP `scram-sha-256`. Integration tests confirmed an incorrect TCP password was rejected and the configured credentials could execute `SELECT 1`.

Ran the same regression and smoke commands used by CI:

```powershell
docker compose stop worker
docker compose --profile test run --build --rm tests regression
docker compose up --build -d --wait --wait-timeout 120
docker compose --profile test run --rm tests smoke
```

- **Regression result:** 166 passed in the final run (59.88 seconds), exit code 0, no failures or skips. Includes all 155 previous regressions and 11 DevOps tests in `tests/test_devops_unit.py` and `tests/test_devops_integration.py`. Tests cover secret redaction, safe network errors, heartbeat validation, and password authentication. One third-party Starlette/AnyIO deprecation warning remained.
- **Main-installation smoke result:** 2 passed in 1.08 seconds, exit code 0, no failures or skips, with two healthy workers and the new credentials/HMAC configuration. JUnit reports were generated under `test-results/`.

Fresh installation was verified in a separate PowerShell session and Compose project with independent secrets, a new volume, and randomly assigned host ports:

```powershell
$env:COMPOSE_PROJECT_NAME='relayops-step8-fresh'
$env:GATEWAY_PORT='0'
$env:RECEIVER_PORT='0'
python scripts/configure.py --env-file .env.step8-fresh
docker compose --env-file .env.step8-fresh -p relayops-step8-fresh config --quiet
docker compose --env-file .env.step8-fresh -p relayops-step8-fresh up --build --wait --wait-timeout 120
docker compose --env-file .env.step8-fresh -p relayops-step8-fresh --profile test run --build --rm tests smoke
docker compose --env-file .env.step8-fresh -p relayops-step8-fresh down
docker compose --env-file .env.step8-fresh -p relayops-step8-fresh up --build --wait --wait-timeout 120
```

- **Fresh-installation smoke result:** 2 passed in 0.59 seconds, exit code 0, no failures or skips. Together with the main installation, four smoke-test executions passed across the two deployments.
- **Restart preservation:** The isolated installation became healthy again with two workers after `down`/`up`. Checksums for its two event rows and two history rows matched before and after restart. Its containers were subsequently stopped with the same project-specific `down` command; its volume was retained. No existing volume was deleted or reinitialized.
- **CI failure gate:** A disposable container ran an intentionally failing temporary test through `tests/run_tests.py`, selecting only that case. It reported one expected failure and propagated exit code 1 through pytest, the launcher, and Compose. This was a deliberate gate check, separate from the passing regression suite; its JUnit report used a separate filename.
- **Additional checks:** Workflow YAML was parsed locally and checked for push/PR triggers, regression/smoke gates, unconditional reports/cleanup, and no error suppression. `python -m pip check` passed in all four runtime/test images. Internal log scans found neither configured password nor HMAC secret; no secret values were printed. `git check-ignore` confirmed private environment files and reports were ignored. Compose validation and `git diff --check` passed.
- **GitHub limitation:** GitHub Actions has not been executed on GitHub. Hosted runner behavior, checkout, secret masking, artifact upload, and displayed job status still require a real push/PR workflow run. Local checks do not establish hosted CI success.
- Final state: the main installation has six healthy runtime containers, including exactly two workers; validation-project data remains in its retained volume. API/worker functional contracts, receiver API, and existing application data were preserved. `SOLUTION.md` was not changed during implementation.

## 5. AI Tools Used

- Used Codex to inspect configuration, reproduce networking failures, edit four configuration files, and run container DNS, HTTP, database, and restart checks. Verified changes against actual command output and reviewed the diff; `git diff --check` passed. No API or worker source functionality was changed.
- Codex initially embedded a Python HTTP probe in a PowerShell command using quoting that did not survive native argument passing. Python received an unquoted URL and raised `SyntaxError`, so that probe provided no connectivity evidence. Codex corrected it by sending the script through a PowerShell literal here-string to `docker compose exec -T api python -`; the corrected probe returned the API health response.
- **Step 3:** Codex implemented the API handlers, request-scoped transactions, validation, SQL migration, and unit/integration/live HTTP tests. Verification used real PostgreSQL, live requests through Nginx, direct database assertions, and concurrent request tests. The live tests first reproduced the original idempotency failures; the final scoped suite passed all 49 tests. Documentation was updated only after implementation and testing.
- **Step 3 mistake:** Codex attempted the migration before stopping the old API. The migration blocked because that API held an idle GET transaction. `pg_stat_activity` identified the lock holder; stopping the API released it, and migration completed. The documented migration procedure now stops the API first.
- **Step 4:** Codex implemented single-attempt worker delivery, exact-byte HMAC signing, response classification, transactional attempt recording, and worker tests. Verification combined all 49 API regressions with 30 worker tests against real PostgreSQL and the unchanged receiver over HTTP, followed by two container smoke tests with one worker replica.
- **Step 4 mistake:** Codex constructed the test receiver URL by concatenating a base URL and `webhook` without a separating slash. The first combined run produced 17 failures and 62 passes; HTTPX reported an invalid port such as `34221webhook`. Replaced concatenation with `base_url.join("/webhook")`. The corrected combined suite passed all 79 tests, followed by both smoke tests.
- **Step 5:** Codex implemented error classification, exponential backoff, seconds-based `Retry-After`, the total HTTP deadline, and transactional retry scheduling. It added unit and real-PostgreSQL/HTTP integration tests, applied the additive migration, and verified existing data using counts and checksums. Verification finished with 118 passing regressions, two passing container smoke tests, and live retry/permanent-failure probes using one worker.
- **Step 5 verification:** Codex identified that the prior HTTPX phase timeout did not enforce a total five-second deadline and added a delayed-response test with phase timeouts disabled. No new implementation or test mistake was identified during Step 5.
- **Step 6:** Codex wrote a real-process concurrency test that reproduced duplicate delivery before implementation, then added transactional row ownership, rollback-based recovery, signal handling, and direct Docker startup. Verification used real PostgreSQL and HTTP, SIGKILL at controlled delivery/commit boundaries, SIGTERM during active and idle work, all 127 regressions, and two smoke tests with two Compose workers. Docker exit codes and logs independently confirmed clean shutdown.
- **Step 6 mistakes:** No new AI implementation or test mistake was identified; the initial test failure intentionally reproduced the existing concurrency race.
- **Step 7:** Codex first verified the existing proxy, 1 MiB payload round trip, and health endpoint, then added tracing, metrics, database isolation, and 28 tests. Verification used 155 passing regressions, two passing smoke tests, actual service logs, live gateway failures, runtime port inspection, and Docker DNS/TCP/database probes. Existing worker and receiver behavior was preserved.
- **Step 7 timeout mistake:** The initial upstream-failure probe used a five-second client timeout, matching Nginx's connection timeout, and expected `502`. It timed out before reading a gateway response. Codex restored the API, reran with a 15-second client timeout and accepted upstream-error codes `502`/`504`, then verified the actual `504` responses and their IDs. No application or Nginx timeout change was needed.
- **Step 7 log-check correction:** A diagnostic matcher initially missed gateway IDs because PowerShell formatted matching lines across multiple display lines. Reading each match's raw `Line` property confirmed both provided and generated IDs in the gateway and API logs.
- **Step 8:** Codex inspected Docker/environment/logging configuration, implemented secret bootstrap and non-destructive authentication updates, health checks, safe logs, dependency locks, a shared test image/runner, CI, and operating instructions. Verification used all 166 regressions, four smoke-test executions across existing/fresh installations, preserved-data checksums, password rejection, health inspection, dependency checks, and secret-free log scans. It also deliberately tested nonzero failure propagation; GitHub-hosted execution remains unverified.
- **Step 8 configuration-order mistake:** Codex initially issued a Compose stop command before creating `.env`. Required-variable validation rejected the command. It generated configuration, synchronized the role password, and reran the stop/start sequence successfully; checksum checks confirmed data preservation.
- **Step 8 diagnostic correction:** The first image-digest command used a quoted Go-template separator that PowerShell did not pass correctly; Docker reported a template parsing error. Replaced it with `{{json .RepoDigests}}` and used the returned digests. Code inspection also prompted atomic heartbeat writes, followed by the final full regression rerun.

## 6. Future Improvements
