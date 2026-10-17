# TraceMind

Phases 1–2 of an incident investigation platform: validated log ingestion, deterministic incident detection, and evidence-backed correlation with FastAPI, Pydantic, SQLAlchemy, and PostgreSQL. Embeddings and AI diagnosis are not implemented yet. Correlations are investigation leads, not confirmed causes.

## Run with Docker

Start Docker Desktop, then run from this directory:

```powershell
docker compose up --build -d
```

API documentation: http://localhost:8000/docs

Database readiness: http://localhost:8000/health

PostgreSQL data persists in the `postgres_data` volume. The example credentials and localhost bindings are intended for local development. This API has no authentication; add access controls before exposing it to a network.

## Run Python locally

Python 3.11+ and a running PostgreSQL server are required. Docker can supply just the database:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
Copy-Item .env.example .env
docker compose up -d db
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

If using an existing PostgreSQL server, set `DATABASE_URL` in `.env`. The database must already exist. Tables and indexes are created on API startup; schema changes will need migrations in a later phase.

## Submit the synthetic dataset

With the API running, in another PowerShell terminal:

```powershell
$sampleBody = Get-Content -Raw data/sample_logs.json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/logs -ContentType 'application/json' -Body $sampleBody
Invoke-RestMethod 'http://localhost:8000/logs?service=payment-service&severity=ERROR&limit=20&offset=0'
Invoke-RestMethod 'http://localhost:8000/logs?start=2026-10-17T10:00:01Z&end=2026-10-17T10:00:03Z'
```

`POST /logs` accepts an object containing `logs`, an array of 1–1,000 records. It returns the saved records with generated IDs and HTTP 201. An invalid record rejects the entire batch with HTTP 422. Repeating an ingestion request creates new records; deduplication is not implemented.

Each record requires `timestamp`, `service`, `severity`, and `message`. Optional fields are `request_id`, `trace_id`, and `exception_type`. Unknown fields are rejected.

- Timestamps must include a timezone and are stored and returned in UTC.
- Service identifiers are trimmed, lowercased, and have spaces/underscores replaced with hyphens (`Payment Service` becomes `payment-service`). Names allow letters, digits, dots, and hyphens, up to 100 characters.
- Severity is case insensitive: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; `WARN` and `FATAL` are normalized to `WARNING` and `CRITICAL`.
- Messages are trimmed, nonempty, and limited to 32,000 characters. Optional identifiers are nonempty when supplied, up to 200 characters.

`GET /logs` returns `{ "items": [...], "total": 4, "limit": 100, "offset": 0 }`. Filters can be combined: `service`, `severity`, inclusive `start` and `end`. Results are ordered by timestamp ascending, then ID. `limit` is 1–1,000; `offset` is nonnegative. When using positive timezone offsets in URLs, encode `+` as `%2B`.

## Validate

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

By default, tests use isolated in-memory SQLite databases. They cover the log API and incident grouping, evidence, identifier conflicts, window boundaries, late arrivals, detection rules, idempotency, status changes, pagination, and transaction rollback.

To run the same suite against PostgreSQL, start the database and set a test URL. Each test creates and removes a randomly named schema; existing application tables and logs are preserved. The database user needs permission to create schemas.

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://tracemind:tracemind@127.0.0.1:5432/tracemind?connect_timeout=5'
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:\TEST_DATABASE_URL
```

## Phase 2: detect and investigate incidents

After upgrading, rebuild the API. Startup adds three new tables (`incidents`, `incident_logs`, `incident_links`) without changing existing log columns or deleting data. `create_all` handles this additive step; future changes to existing tables will require migrations.

```powershell
docker compose up --build -d
Invoke-RestMethod -Method Post http://localhost:8000/incidents/detect
$incidents = Invoke-RestMethod http://localhost:8000/incidents
$incidents.items | Format-Table id, title, severity, status, occurrence_count
$incidentId = $incidents.items[0].id
Invoke-RestMethod "http://localhost:8000/incidents/$incidentId"
```

Detection is explicit: call `POST /incidents/detect` after ingestion. It processes unassigned logs, persists incident memberships and evidence, and returns counts for newly created incidents, assigned logs, and weak relationships. It does not run automatically in `POST /logs`, so the Phase 1 ingestion contract is preserved. Existing Phase 1 logs can be detected without resubmitting them.

With the sample submitted once, its first three errors share one incident via `trace-payment-001`. The unrelated user-service warning remains available in `/logs` and does not seed an incident.

### Correlation and detection rules

1. An exact trace ID match takes precedence over an exact request ID match. Different nonempty trace IDs prevent request-based grouping.
2. Each incident spans at most `CORRELATION_WINDOW_SECONDS` (default 300). A reused identifier outside that span creates another incident; a chain of nearby events cannot grow the time window indefinitely.
3. Errors without either identifier can group by service, exception type, and normalized message. Numeric values, UUIDs, and hexadecimal addresses are normalized. Different error patterns or services stay separate even when timestamps are close.
4. ERROR and CRITICAL events seed incidents. INFO, DEBUG, and WARNING records only attach as context when an identifier matches an active incident within the window.
5. Rules flag repeated error fingerprints, failures across multiple services sharing a trace, and service error bursts (default five errors in 300 seconds). A burst is a signal; it does not merge different error patterns. This is a fixed threshold, not a learned anomaly baseline.
6. A downstream timeout/latency message can have a weak relationship to an earlier error in a configured dependency within the window. The two incidents remain separate, both original log IDs are recorded, and the relationship does not establish causality. Known conflicting trace or request IDs prevent this relationship.

Incident detail includes a timeline of original log records, membership reasons, evidence strength, links to related incidents, and `causality: "unconfirmed"`. `occurrence_count` counts ERROR/CRITICAL records; `related_log_ids` also includes matched lower-severity context. The first incident member has reason `isolated_error`; this describes how it seeded the incident, not a confirmed initiating cause.

Repeated detection is idempotent: a log ID has at most one incident assignment, and existing relationships are not duplicated. Reposting the same raw logs still creates new records, as in Phase 1, and those count as new observations. Detection reruns do not retroactively merge or reassign existing incidents; changing configuration affects new assignments and newly evaluated signals/links.

### Incident API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/incidents/detect` | Run deterministic detection |
| GET | `/incidents` | List incidents with `status`, `severity`, `service`, `limit`, `offset` filters |
| GET | `/incidents/{id}` | Inspect original evidence and weak relationships |
| PATCH | `/incidents/{id}` | Set `status` to `open`, `investigating`, or `resolved` |

Incident lists sort by last seen descending, then ID descending. Severity filters use uppercase values. Missing incident IDs return 404; invalid input returns 422. Database operation failures return 503 without exposing database details.

```powershell
Invoke-RestMethod 'http://localhost:8000/incidents?service=payment-service&status=open&limit=10'
Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/incidents/$incidentId" `
  -ContentType 'application/json' -Body '{"status":"investigating"}'
Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/incidents/$incidentId" `
  -ContentType 'application/json' -Body '{"status":"resolved"}'
```

New failures never attach to resolved incidents. A recurrence creates another incident; you can explicitly reopen an incident through PATCH. Status changes do not verify a root cause or save a resolution.

Copy `.env.example` to `.env` to customize the window, burst threshold, or `SERVICE_DEPENDENCIES` JSON. Use normalized service names in the dependency map: each key is a downstream service and its values are upstream dependencies. Docker reads the optional `.env` file and uses its internal database hostname; rebuild/recreate the API after changing settings.

### Phase 2 persistence check

Record the incident IDs and counts, restart the API, then retrieve them again:

```powershell
docker compose restart api
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/incidents
Invoke-RestMethod -Method Post http://localhost:8000/incidents/detect
```

Expected: original incident IDs and status remain; detection reports zero new assignments when no logs were ingested. Avoid deleting the database volume.

### Current limits

This initial detector scans stored logs and incident memberships in memory, and the detail endpoint returns the full evidence timeline. It is intended for a small development dataset. Before production workloads, add bounded background processing, evidence pagination, calibrated spike baselines, migrations, and authentication. PostgreSQL advisory transaction locking serializes detector runs so concurrent workers cannot double-assign logs. Identifier equality is strong correlation evidence, not proof of a common cause.
