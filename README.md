# TraceMind

TraceMind is an incident-investigation backend that ingests structured application logs, groups related failures into incidents, and generates evidence-backed root-cause hypotheses. It combines deterministic correlation with optional LLM analysis while preserving the original logs and clearly labeling causality as unconfirmed.

The repository currently implements Phases 1–3:

| Phase | Capability | Status |
|---|---|---|
| 1 | Validated log ingestion, normalization, storage, filtering, and pagination | Complete |
| 2 | Deterministic incident detection, cross-service correlation, evidence storage, and incident lifecycle | Complete |
| 3 | Structured AI-assisted RCA with Gemini or OpenAI, strict validation, citations, and report persistence | Complete |
| 4 | Future expansion | Deferred |

Phase 4 will be designed and implemented later. Historical semantic retrieval, verified-resolution feedback, a React dashboard, background queues, and automated remediation are not part of the current application.

## Why TraceMind

A log viewer can show that several services failed near the same time, but it does not explain whether the failures belong to the same incident or which evidence supports a possible cause. TraceMind separates that work into two controlled stages:

1. Deterministic code groups and relates logs using identifiers, time boundaries, fingerprints, and configured service dependencies.
2. An optional LLM explains a bounded evidence package and returns a structured hypothesis whose citations and semantics are validated before storage.

The LLM does not decide which raw logs belong to an incident, modify incidents, confirm root causes, or execute remediation.

## Architecture

### System context

```mermaid
flowchart LR
    A[Applications and synthetic log producers] -->|POST /logs| B[FastAPI API]
    U[Developer or operator] -->|REST requests| B
    B --> C[Validation and normalization]
    C --> D[(PostgreSQL)]
    B --> E[Incident correlation engine]
    E --> D
    B --> F[RCA evidence builder]
    F --> G[LLM provider adapter]
    G -->|Gemini generateContent| H[Google Gemini API]
    G -->|OpenAI Responses API| I[OpenAI API]
    G --> J[Structured output validation]
    J --> D
    D -->|Logs, incidents, evidence, reports| B
```

PostgreSQL is the system of record. Provider calls are synchronous and occur only when a client explicitly requests analysis. `POST /logs` and `POST /incidents/detect` never invoke an LLM.

### Internal component architecture

```mermaid
flowchart TB
    subgraph API[FastAPI application]
        MAIN[app/main.py<br/>startup, health, log routes]
        IR[app/incidents.py<br/>incident REST API]
        RR[app/rca_routes.py<br/>RCA REST API]
    end

    subgraph DOMAIN[Domain services]
        CORR[app/correlation.py<br/>deterministic grouping and links]
        RCA[app/rca.py<br/>evidence selection, validation, persistence]
        LLM[app/llm.py<br/>Gemini/OpenAI adapters]
    end

    subgraph CONTRACTS[Contracts and configuration]
        SC[app/schemas.py<br/>logs and incidents]
        RS[app/rca_schemas.py<br/>strict RCA output]
        CFG[app/config.py<br/>environment settings]
    end

    DBL[app/database.py<br/>engine and sessions]
    MODELS[app/models.py<br/>SQLAlchemy models]
    PG[(PostgreSQL)]

    MAIN --> SC
    MAIN --> DBL
    IR --> CORR
    IR --> SC
    RR --> RCA
    RCA --> RS
    RCA --> LLM
    CORR --> MODELS
    RCA --> MODELS
    DBL --> PG
    MODELS --> DBL
    CFG --> CORR
    CFG --> RCA
    CFG --> LLM
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| FastAPI routes | Validate HTTP input, invoke domain services, serialize responses, and map failures to sanitized status codes |
| Pydantic schemas | Enforce request and response types, bounds, enums, unknown-field rejection, and strict RCA output structure |
| Correlation engine | Assign unprocessed logs to incidents, calculate fingerprints, apply identifier and time rules, emit signals, and store weak relationships |
| RCA service | Select bounded evidence, call the provider, parse output, enforce semantic and citation rules, and save immutable reports |
| Provider adapters | Translate the common RCA contract to Gemini or OpenAI request formats and safely handle provider failures |
| SQLAlchemy models | Represent logs, incidents, memberships, incident links, and saved RCA reports |
| PostgreSQL | Persist source evidence and investigation history across API restarts |

### Data model

```mermaid
erDiagram
    LOG_RECORDS ||--o| INCIDENT_LOGS : assigned_through
    INCIDENTS ||--o{ INCIDENT_LOGS : contains
    INCIDENTS ||--o{ RCA_REPORTS : analyzed_as
    INCIDENTS ||--o{ INCIDENT_LINKS : source
    INCIDENTS ||--o{ INCIDENT_LINKS : target

    LOG_RECORDS {
        bigint id PK
        timestamptz timestamp
        string service
        string severity
        text message
        string request_id
        string trace_id
        string exception_type
    }

    INCIDENTS {
        bigint id PK
        string title
        string severity
        string status
        timestamptz first_seen
        timestamptz last_seen
        integer occurrence_count
        json detection_rules
        timestamptz created_at
        timestamptz updated_at
    }

    INCIDENT_LOGS {
        bigint log_id PK, FK
        bigint incident_id FK
        string reason
        string strength
        bigint related_log_id FK
    }

    INCIDENT_LINKS {
        bigint upstream_log_id PK, FK
        bigint downstream_log_id PK, FK
        bigint upstream_incident_id FK
        bigint downstream_incident_id FK
        string reason
        string strength
    }

    RCA_REPORTS {
        bigint id PK
        bigint incident_id FK
        string provider
        string model
        string prompt_version
        json analysis
        json evidence_snapshot
        string evidence_hash
        json validation_notes
        integer duration_ms
        timestamptz created_at
    }
```

`incident_logs` ensures that each log belongs to at most one incident. Incident links record weaker cross-incident evidence without merging those incidents. Each RCA report stores the exact evidence snapshot and its SHA-256 hash, so later log ingestion or status changes do not rewrite previous analysis history.

### End-to-end processing flow

```mermaid
sequenceDiagram
    actor Client
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Detector as Correlation engine
    participant RCA as RCA service
    participant LLM as Configured LLM

    Client->>API: POST /logs
    API->>API: Validate and normalize entire batch
    API->>DB: Insert log records atomically
    API-->>Client: 201 saved logs

    Client->>API: POST /incidents/detect
    API->>Detector: Process unassigned logs
    Detector->>DB: Persist incidents, memberships, links
    API-->>Client: Detection counts

    Client->>API: POST /incidents/{id}/analyze
    API->>RCA: Build bounded evidence package
    RCA->>DB: Read incident, logs, rules, and links
    RCA->>LLM: Structured analysis request
    LLM-->>RCA: JSON report candidate
    RCA->>RCA: Validate schema, citations, services, ranks, confidence, contradictions
    RCA->>DB: Save report and immutable evidence snapshot
    API-->>Client: 201 RCA report
```

If provider output fails any validation step, no report row is created.

## Technology stack

| Area | Technology |
|---|---|
| API | Python 3.11+, FastAPI, Uvicorn |
| Validation and settings | Pydantic, pydantic-settings |
| Persistence | PostgreSQL 17, SQLAlchemy 2, psycopg 3 |
| Provider HTTP | HTTPX |
| AI providers | Google Gemini `generateContent`, OpenAI Responses API |
| Dependency management | uv 0.13.1 with a universal lock |
| Testing and quality | pytest, Ruff |
| Runtime packaging | Docker, Docker Compose |

No Kafka, Kubernetes, Redis, vector database, provider SDK, or frontend is required by the current implementation.

## Quick start with Docker

Requirements: Docker Desktop and PowerShell.

```powershell
docker compose build api
docker compose up -d --wait db
docker compose run --rm api python -m app.migrations upgrade
docker compose up -d --wait api
Invoke-RestMethod http://localhost:8000/health
```

These commands assume a new empty database. For an existing unversioned database, complete the
baseline procedure below before starting the updated API.

Open the interactive API documentation at [http://localhost:8000/docs](http://localhost:8000/docs).

The Compose stack exposes the API and PostgreSQL only on localhost. Database data persists in the
Compose volume `postgres_data`, named `tracemind_postgres_data` when the default project name is
used. The included database credentials are for local development. The API has no authentication
and should not be exposed publicly.

Useful lifecycle commands:

```powershell
docker compose ps
docker compose logs --tail 100 api
docker compose restart api
docker compose down
```

Both services use `restart: unless-stopped`. Docker restarts them after a process, daemon, or host
restart, while an explicit `docker compose stop` keeps them stopped. `docker compose down` still
removes the containers and network normally while preserving the named database volume.

`docker compose ps` reports `(healthy)` for both `db` and `api` when PostgreSQL accepts connections
and the API's internal `http://127.0.0.1:8000/health` probe returns HTTP 200. The API probe uses the
Python standard library included in the image; HTTP 503 and connection failures make the container
unhealthy. Inspect the latest probe output when troubleshooting:

```powershell
docker inspect --format='{{json .State.Health}}' tracemind-api-1
docker compose logs --tail 100 api db
```

`docker compose down` preserves the named database volume. Do not add `--volumes` when you want to retain data.

To verify persistence with a harmless log record, save its returned ID and retrieve it after both a
restart and a container recreation:

```powershell
$marker = "phase5-persistence-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())"
$body = @{ logs = @(@{
  timestamp = [DateTime]::UtcNow.ToString('o')
  service = $marker
  severity = 'INFO'
  message = 'Phase 5 persistence verification'
}) } | ConvertTo-Json -Depth 4
$record = (Invoke-RestMethod -Method Post -Uri http://localhost:8000/logs `
  -ContentType 'application/json' -Body $body)[0]

docker compose restart db
docker compose up -d --wait
(Invoke-RestMethod "http://localhost:8000/logs?service=$marker").items.id

docker compose up -d --force-recreate db
docker compose up -d --wait
(Invoke-RestMethod "http://localhost:8000/logs?service=$marker").items.id
```

Both retrieved IDs should equal `$record.id`. These commands recreate the PostgreSQL container but
do not remove `tracemind_postgres_data`. Never add `--volumes` to this verification workflow.

## Local Python setup

Python 3.13 and PostgreSQL are used for the verified development workflow. Install the project-required
uv version explicitly, then synchronize runtime and development dependencies from `uv.lock`:

```powershell
py -3.13 -m pip install --user uv==0.13.1
uv --version
uv lock --check
uv sync --frozen --extra dev --python 3.13
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d db
uv run --frozen uvicorn app.main:app --reload
```

`--frozen` makes `uv.lock` the installation source of truth and prevents an install or command from
silently changing dependency resolution. `uv lock --check` separately verifies that the lock still
matches `pyproject.toml`. The lock contains the runtime packages and the optional `dev` extra used by
pytest and Ruff; Docker installs only the runtime subset.

When using another PostgreSQL instance, set `DATABASE_URL` in `.env`. The database must already
exist. Alembic owns the reviewable schema history. API startup verifies the recorded migration head
and never creates, upgrades, or stamps the schema.

## Database migrations

The initial revision is `20261019_01`. On a new empty database, apply it before starting the API:

```powershell
uv run --frozen python -m app.migrations upgrade
uv run --frozen alembic current
uv run --frozen alembic check
```

`alembic check` must report no new upgrade operations after every model or migration change. The
Docker image includes `alembic.ini` and the migration scripts, so the same commands can be used by a
dedicated deployment migration job. For a new Docker database, run the migration command once before
starting or restarting API replicas:

```powershell
docker compose run --rm api python -m app.migrations upgrade
docker compose up -d --wait
```

The TraceMind migration command takes a PostgreSQL session advisory lock before upgrading and releases
it after success or failure. PostgreSQL also releases the lock if the migration connection terminates.
This serializes concurrent command invocations without holding a lock during normal API operation.
API startup compares the database revision with the head loaded from the packaged Alembic scripts. An
unbaselined, behind, incompatible, or unreachable database blocks startup with a sanitized error; the
API never stamps or upgrades it automatically.

An existing unversioned database must not run `alembic upgrade head`: the initial revision would try
to create tables that already exist. Adopt migrations during a maintenance window instead:

1. Back up the database and stop all application writes, API replicas, and migration runners.
2. Compare its tables, columns, constraints, and indexes with the initial revision and current model
   metadata. Resolve every difference explicitly.
3. When the schemas match, run `alembic stamp 20261019_01` once. Stamping creates the version marker
   without replaying the initial schema operations; it does not create, alter, or repair tables.
4. Confirm `alembic current` reports `20261019_01 (head)` and `alembic check` reports no changes before
   restarting application writes.

Never use `stamp` merely to bypass a mismatch. It is safe only after the existing schema has been
reviewed and confirmed equivalent to the revision being recorded.

## Configuration

Copy `.env.example` to `.env` and edit it locally. Never commit `.env` or API keys.

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_URL` | Local TraceMind PostgreSQL URL | SQLAlchemy connection URL |
| `CORRELATION_WINDOW_SECONDS` | `300` | Maximum fixed span for one incident |
| `ERROR_BURST_THRESHOLD` | `5` | Error count that adds a burst signal |
| `SERVICE_DEPENDENCIES` | Payment/user to database | JSON map from downstream service to upstream dependencies |
| `RCA_PROVIDER` | `disabled` | `disabled`, `gemini`, or `openai` |
| `GEMINI_API_KEY` | Empty | Gemini credential used only when Gemini is selected |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model ID |
| `OPENAI_API_KEY` | Empty | OpenAI credential used only when OpenAI is selected |
| `OPENAI_MODEL` | Empty | Structured-output-capable OpenAI model |
| `RCA_TIMEOUT_SECONDS` | `30` | Provider network timeout |
| `RCA_MAX_LOGS` | `40` | Maximum selected logs per analysis |
| `RCA_MAX_EVIDENCE_CHARS` | `40000` | Maximum compact evidence JSON size |
| `RCA_MAX_OUTPUT_TOKENS` | `4000` | Provider output-token limit |

Docker overrides `DATABASE_URL` inside the API container so it can reach the `db` service. Rebuild or recreate the API after changing provider settings:

```powershell
docker compose up -d --build api
```

The Dockerfile pins Python 3.13.16 and the uv 0.13.1 installer image by immutable multi-platform
digest. Compose pins PostgreSQL 17.11 by digest. A tag keeps the selected version readable; the digest
ensures a build or pull retrieves the reviewed image content.

### Updating dependencies and images

Make dependency updates deliberately from a clean working tree:

```powershell
uv --version
uv lock --upgrade --python 3.13
uv lock --check
uv sync --frozen --extra dev --python 3.13
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Review both `pyproject.toml` and `uv.lock`, including platform markers and unexpected transitive
changes. To update a container base, inspect the registry manifest, place the verified tag and
multi-platform digest in the Dockerfile or Compose file, then rebuild and run the complete checks:

```powershell
docker buildx imagetools inspect python:3.13.16-slim
docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.13.1
docker buildx imagetools inspect postgres:17.11
docker compose config --quiet
docker compose build --no-cache api
docker compose up -d --wait
Invoke-RestMethod http://localhost:8000/health
```

Do not merge a lock or digest update until the PostgreSQL-backed test suite, Ruff checks, clean image
build, and container health verification all pass. Image updates must not remove the existing
`postgres_data` volume.

## Phase 1: log ingestion

### Log contract

`POST /logs` accepts an object containing a `logs` array with 1–1,000 records. Every record requires `timestamp`, `service`, `severity`, and `message`. Optional fields are `request_id`, `trace_id`, and `exception_type`. Unknown fields are rejected. Validation is atomic: one invalid entry rejects the complete batch with HTTP 422.

Normalization rules:

- Timestamps must contain timezone information and are stored and returned in UTC.
- Service names are trimmed, lowercased, and normalized from spaces or underscores to hyphens.
- Services may contain letters, digits, dots, and hyphens and are limited to 100 characters.
- Severity is case insensitive. `WARN` becomes `WARNING`; `FATAL` becomes `CRITICAL`.
- Messages are trimmed, required to be nonempty, and limited to 32,000 characters.
- Optional identifiers must be nonempty when supplied and are limited to 200 characters.

Ingestion does not deduplicate records. Repeating the same request stores another set of observations.

### Ingest and query sample logs

```powershell
$sampleBody = Get-Content -Raw data/sample_logs.json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/logs `
  -ContentType 'application/json' -Body $sampleBody

Invoke-RestMethod 'http://localhost:8000/logs?service=payment-service&severity=ERROR&limit=20&offset=0'
Invoke-RestMethod 'http://localhost:8000/logs?start=2026-10-17T10:00:01Z&end=2026-10-17T10:00:03Z'
```

`GET /logs` supports combinable `service`, `severity`, inclusive `start`, inclusive `end`, `limit`, and `offset` parameters. Results sort by timestamp and then ID in ascending order. Encode `+` as `%2B` when a timestamp with a positive offset appears in a URL.

## Phase 2: incident detection and correlation

Detection is explicit. Call it after ingestion:

```powershell
$detection = Invoke-RestMethod -Method Post http://localhost:8000/incidents/detect
$incidents = Invoke-RestMethod http://localhost:8000/incidents
$incidents.items | Format-Table id, title, severity, status, occurrence_count
$incidentId = $incidents.items[0].id
Invoke-RestMethod "http://localhost:8000/incidents/$incidentId"
```

The detector processes unassigned logs and returns counts for created incidents, assigned logs, and weak relationships. Existing Phase 1 logs can be detected without being submitted again.

### Correlation precedence and safeguards

1. Exact `trace_id` matches provide the strongest grouping rule.
2. Exact `request_id` matches are used when trace evidence does not conflict.
3. Different nonempty trace IDs prevent grouping through a shared request ID.
4. An incident has a fixed maximum span controlled by `CORRELATION_WINDOW_SECONDS`; chains of nearby events cannot extend it indefinitely.
5. Identifier-free errors can group only when service, exception type, and normalized message fingerprint match within the window.
6. Fingerprinting normalizes changing numeric values, UUIDs, and hexadecimal addresses so repeated forms of one error can group.
7. ERROR and CRITICAL logs seed incidents. Lower-severity records attach only as identifier-matched context.
8. Repeated fingerprints, multi-service traces, and error bursts add transparent detection signals.
9. A timeout or latency event in a downstream service may link to an earlier dependency failure as weak evidence. It does not merge the incidents or prove causality.
10. Conflicting request or trace identifiers block weak dependency links.

The sample dataset groups the database and payment errors sharing `trace-payment-001`. Its unrelated user-service warning remains queryable but does not seed that incident.

Detection is idempotent for stored log IDs. It does not reassign or retroactively merge existing incidents. New failures do not attach to resolved incidents; recurrence creates another incident.

### Incident lifecycle

Statuses are `open`, `investigating`, and `resolved`.

```powershell
Invoke-RestMethod 'http://localhost:8000/incidents?service=payment-service&status=open&limit=10'

Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/incidents/$incidentId" `
  -ContentType 'application/json' -Body '{"status":"investigating"}'

Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/incidents/$incidentId" `
  -ContentType 'application/json' -Body '{"status":"resolved"}'
```

A status change records workflow state only. It does not confirm a cause or save a verified resolution.

## Phase 3: evidence-backed AI-assisted RCA

Phase 3 builds a bounded evidence package from an existing incident and asks the selected provider for a strict JSON report. The backend then validates that report before saving it.

### RCA pipeline

1. Load the incident, its original logs, membership reasons, detection rules, and eligible weak relationships.
2. Select ERROR/CRITICAL logs first, followed by lower-severity context, while applying log-count and character limits.
3. Record evidence omissions and truncation as explicit evidence gaps.
4. Send the evidence and system instructions to the selected provider.
5. Decode and validate the response with `RCAOutput`.
6. Verify every cited log ID belongs to the supplied evidence snapshot and reject duplicate citation IDs.
7. Reject invented services, overlapping supporting/contradicting citations, invalid ranks, unsupported statuses, and unknown fields.
8. For a hypothesis, require at least one observed fact, reject duplicate cause text case-insensitively, and require confidence to be non-increasing by rank.
9. Require `probable_cause` to equal the first ranked cause and require its top-level citation list to match the first cause's citations exactly, including order.
10. Convert a report with contradictory evidence on its first-ranked cause to inconclusive, clear the selected cause and top-level citations, and lower confidence.
11. Merge snapshot omission and truncation gaps into the final report, remove duplicates while preserving order, and keep at most 20 gaps.
12. Persist the analysis, evidence snapshot, hash, provider, model, prompt version, validation notes, and total duration in one report transaction.

Reports can have `hypothesis` or `inconclusive` status. They cannot claim `confirmed`. Every API response retains `causality: "unconfirmed"`.

### Gemini configuration

```dotenv
RCA_PROVIDER=gemini
GEMINI_API_KEY=your-local-key
GEMINI_MODEL=gemini-3.5-flash-lite
RCA_TIMEOUT_SECONDS=30
RCA_MAX_LOGS=40
RCA_MAX_EVIDENCE_CHARS=40000
RCA_MAX_OUTPUT_TOKENS=4000
```

The adapter uses Google's official `v1beta/models/{model}:generateContent` endpoint through HTTPX. It sends the key in the `x-goog-api-key` header and uses native `systemInstruction`, `contents`, and `generationConfig` fields. JSON output is requested with `responseMimeType: application/json` and a translated `responseJsonSchema`.

The generated schema retains closed objects, required fields, enums, nullability, minimum array sizes, root array limits, and numeric rank bounds. Controlled live testing found that Google's schema validator rejected the complete RCA schema when three nested citation arrays included `maxItems`. Those three generation-time bounds are omitted, while the unchanged Pydantic model still enforces their 100-ID limits before persistence. Details and probe results are recorded in [docs/gemini-investigation.md](docs/gemini-investigation.md).

Only one candidate ending with `STOP` is accepted. Blocked, incomplete, empty, oversized, malformed, or unexpected responses are rejected. There are no automatic retries, provider fallbacks, model switches, tools, or search calls.

Gemini availability, free-tier eligibility, and limits depend on the Google project. Consult [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), and [structured output documentation](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

### OpenAI configuration

```dotenv
RCA_PROVIDER=openai
OPENAI_API_KEY=your-local-key
OPENAI_MODEL=your-structured-output-capable-model
RCA_TIMEOUT_SECONDS=30
RCA_MAX_LOGS=40
RCA_MAX_EVIDENCE_CHARS=40000
RCA_MAX_OUTPUT_TOKENS=4000
```

The OpenAI adapter uses the Responses API with strict structured output and `store: false`. Only the selected provider requires credentials. Set `RCA_PROVIDER=disabled` to stop new analyses while keeping saved reports readable.

### Generate and retrieve an RCA report

```powershell
$incidentId = 1
$report = Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/incidents/$incidentId/analyze" `
  -ContentType 'application/json' -Body '{"max_logs":40}'

$report.analysis | ConvertTo-Json -Depth 10
Invoke-RestMethod "http://localhost:8000/incidents/$incidentId/analyses?limit=20&offset=0"
Invoke-RestMethod "http://localhost:8000/incidents/$incidentId/analyses/$($report.id)"
```

The request body is optional. `max_logs` must be between 1 and 100 and cannot exceed the server limit. Every successful request creates a new immutable report and makes a provider call when usable evidence exists. Retrieval never calls the provider.

New reports use prompt version `rca-v2`, which explicitly requires the probable cause to be a verbatim copy of the first ranked cause. Existing reports retain the prompt version stored at creation.

### Trust and failure boundaries

- Logs, identifiers, exception text, and incident titles are treated as untrusted data, not instructions.
- Provider credentials are never put in URLs, response bodies, or application diagnostics.
- Public failures do not expose provider bodies, authorization headers, incident evidence, or API keys.
- Gemini diagnostics log only HTTP status, an allowlisted provider status, and a bounded classified description.
- OpenAI diagnostics use fixed failure categories and omit response bodies and exception details.
- RCA orchestration records provider-call elapsed time with a monotonic clock for successful and failed calls. Validation and persistence failures use stable categories.
- Diagnostic records include bounded provider, model, prompt-version, status, and timing fields where applicable. They exclude full prompts, raw evidence, raw provider output, authorization headers, API keys, and unsanitized response bodies.
- Provider response envelopes and extracted report text have hard size limits.
- Report metadata is created only after provider output passes validation. A report is returned and logged as persisted only after the database commit succeeds; persistence errors roll back and retain the controlled API error.
- Failed calls, rejected output, and failed commits do not leave report rows.
- A cited log proves only that the log was supplied and referenced; human review is still required to judge whether it supports the claim.

Provider requests send selected incident evidence to the configured external service. Review log contents and the provider's data terms before enabling RCA.

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Check API and database readiness |
| POST | `/logs` | Validate, normalize, and store a log batch |
| GET | `/logs` | Filter and paginate stored logs |
| POST | `/incidents/detect` | Run deterministic detection over unassigned logs |
| GET | `/incidents` | Filter and paginate incidents |
| GET | `/incidents/{id}` | Retrieve an incident timeline, evidence, and relationships |
| PATCH | `/incidents/{id}` | Change incident workflow status |
| POST | `/incidents/{id}/analyze` | Generate, validate, and save an RCA report |
| GET | `/incidents/{id}/analyses` | Retrieve paginated report history, newest first |
| GET | `/incidents/{id}/analyses/{report_id}` | Retrieve one report and its evidence snapshot |

Common error behavior:

| Status | Meaning |
|---:|---|
| 404 | Requested incident or report does not exist |
| 422 | Request validation failed |
| 502 | Provider/configuration error, refused or malformed output, or failed RCA validation |
| 503 | Database unavailable, RCA disabled, missing provider configuration, or provider quota/rate limit |
| 504 | Provider request timed out |

## Testing and validation

The suite uses mocked provider transports; automated tests never make live Gemini or OpenAI calls.

### Fast local suite

```powershell
uv lock --check
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Without `TEST_DATABASE_URL`, tests use isolated in-memory SQLite databases.

### PostgreSQL suite

Start the database and run tests against randomly named schemas. Existing application data is not changed.

```powershell
docker compose up -d db
$env:TEST_DATABASE_URL = 'postgresql+psycopg://tracemind:tracemind@127.0.0.1:5432/tracemind?connect_timeout=5'
uv run --frozen pytest -q
Remove-Item Env:\TEST_DATABASE_URL
```

The database user must be allowed to create and drop schemas.

### Focused RCA and provider suite

```powershell
uv run --frozen pytest tests/test_llm.py tests/test_rca.py -q
```

### Deterministic RCA benchmark

Run the fixture-backed RCA contract benchmark without provider credentials or network access:

```powershell
uv run --frozen pytest -m rca_benchmark -q
```

The benchmark reuses the main RCA regression tests for a valid hypothesis, the local no-evidence
result, invalid citations, missing observed facts, duplicate causes, increasing confidence by rank,
contradictory primary evidence, provider timeout, omission disclosure, and rejection without
persistence. It checks deterministic contract behavior rather than subjective causal accuracy.

Run the same static checks used for RCA changes:

```powershell
uv run --frozen ruff check app/rca.py app/rca_schemas.py app/llm.py tests/test_rca.py tests/test_llm.py
uv run --frozen ruff format --check app/rca.py app/rca_schemas.py app/llm.py tests/test_rca.py tests/test_llm.py
git diff --check
```

Coverage includes validation and normalization, filtering, atomic batches, deterministic correlation, identifier conflicts, fixed windows, weak relationships, idempotency, lifecycle changes, pagination, transaction rollback, evidence bounds, immutable report history, Gemini and OpenAI wire contracts, malformed output, citation integrity, exact cause matching, quota and authentication failures, timeouts, response limits, secret sanitization, and PostgreSQL persistence.

The fixture-backed benchmark makes no live Gemini or OpenAI requests. It verifies deterministic
contract and persistence behavior; it does not measure causal accuracy, throughput, or latency.

## Persistence verification

```powershell
$before = Invoke-RestMethod http://localhost:8000/incidents
docker compose restart api
Invoke-RestMethod http://localhost:8000/health
$after = Invoke-RestMethod http://localhost:8000/incidents
```

Incident IDs, statuses, logs, and saved RCA reports should remain available. Running detection again without new logs should produce zero new assignments.

## Current limitations

- Detection scans stored logs and memberships in memory and is intended for a development-scale dataset.
- RCA requests are synchronous.
- Provider calls run inline with the request; there is no queue, background execution, or cancellation API.
- Incident detail returns the complete evidence timeline rather than a paginated timeline.
- Burst detection uses a fixed threshold, not a learned baseline.
- Identifier equality and temporal order are correlation evidence, not causal proof.
- There is no authentication, authorization, tenant isolation, rate limiting, or audit identity.
- API startup verifies the Alembic head but does not apply migrations. Existing unversioned databases
  require the documented one-time schema review and baseline before the updated API can start.
- There is no semantic search over previous incidents or verified resolution knowledge base.
- There is no dashboard or background worker.
- The deterministic RCA benchmark uses mocked providers and fixed fixtures. It does not evaluate live-provider behavior, causal accuracy, throughput, or latency.
- The PostgreSQL persistence test is skipped when `TEST_DATABASE_URL` is unavailable; the remaining tests use isolated in-memory SQLite databases.
- TraceMind does not confirm causes or execute remediation.

PostgreSQL advisory transaction locking serializes detector runs so concurrent workers cannot assign the same stored log twice.

## Repository layout

```text
app/
  config.py          Environment-backed settings
  correlation.py     Detection and correlation rules
  database.py        SQLAlchemy engine, session, and metadata
  incidents.py       Incident API routes
  llm.py             Gemini and OpenAI provider adapters
  main.py            FastAPI application, health, and log routes
  models.py          Persistent database models
  rca.py             Evidence construction, validation, and report persistence
  rca_routes.py      RCA API routes
  rca_schemas.py     Strict RCA contracts
  schemas.py         Log and incident contracts
data/
  sample_logs.json   Synthetic database/payment failure scenario
docs/
  gemini-investigation.md  Live Gemini request-contract investigation
tests/
  conftest.py
  test_logs.py
  test_incidents.py
  test_llm.py
  test_rca.py
compose.yaml
Dockerfile
pyproject.toml
```

## Phase 4

Phase 4's deterministic RCA validation and reliability audit is complete. The implemented boundary
includes ordered citation consistency, observed-fact requirements for hypotheses, duplicate-cause
rejection, non-increasing confidence by rank, contradictory-evidence handling, normalized evidence
gaps, persistence rollback coverage, immutable report metadata, sanitized structured diagnostics,
provider no-retry tests, and the fixture-backed `rca_benchmark` suite.

This phase establishes repeatable contract validation and failure handling. It does not establish
that a provider's proposed cause is correct, compare model quality, benchmark request throughput or
latency, exercise live provider services, add reviewed causes or resolutions, or make RCA processing
asynchronous. Those remain separate product and evaluation concerns.
