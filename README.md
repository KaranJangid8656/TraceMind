# TraceMind

Phases 1–3 of an incident investigation platform: validated log ingestion, deterministic incident detection, evidence-backed correlation, and optional AI-assisted RCA with FastAPI, Pydantic, SQLAlchemy, and PostgreSQL. Historical semantic retrieval and verified-resolution feedback are not implemented yet. Correlations and AI reports are investigation leads, not confirmed causes.

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

## Phase 3: AI-assisted root cause analysis

Phase 3 builds an evidence package from a stored incident, requests a structured RCA hypothesis, validates the output, and persists an immutable report with its exact evidence snapshot. It adds the `rca_reports` table without altering the Phase 1 or Phase 2 tables. Ingestion and incident detection do not automatically invoke an LLM.

### Configure the provider

RCA is disabled by default. Supported values of `RCA_PROVIDER` are `disabled`, `openai`, and `gemini`. Existing APIs work without an API key. If `.env` does not exist, copy `.env.example`; preserve any existing configuration:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Open the file locally and update existing entries rather than adding duplicate keys:

```powershell
notepad .env
```

For Gemini:

```dotenv
RCA_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash-lite
RCA_TIMEOUT_SECONDS=30
RCA_MAX_LOGS=40
RCA_MAX_EVIDENCE_CHARS=40000
RCA_MAX_OUTPUT_TOKENS=4000
```

Replace the API-key placeholder locally. `gemini-3.5-flash-lite` is listed for free-tier text input/output in [Google's pricing documentation](https://ai.google.dev/gemini-api/docs/pricing) and supports [structured outputs](https://ai.google.dev/gemini-api/docs/generate-content/structured-output), checked October 18, 2026. Free-tier eligibility and limits depend on the Google project: check its tier and active limits in [Google AI Studio](https://aistudio.google.com/usage). Keep the project on the Free tier if you want free-tier usage. TraceMind cannot force a paid project to be billed as free. Google explains [project usage tiers and rate limits](https://ai.google.dev/gemini-api/docs/rate-limits); do not assume a fixed free request allowance.

No paid API or Google SDK dependency is required. The Gemini adapter reuses the existing `httpx` dependency, does not enable tools/search, and never automatically retries, switches providers, or falls back to another model when quota is exhausted. A Google HTTP 429 becomes a sanitized HTTP 503; wait for your project's quota to recover rather than repeatedly analyzing the incident. Model availability and free-tier terms can change.

For OpenAI, the existing configuration still works:

```dotenv
RCA_PROVIDER=openai
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-structured-output-capable-model
RCA_TIMEOUT_SECONDS=30
RCA_MAX_LOGS=40
RCA_MAX_EVIDENCE_CHARS=40000
RCA_MAX_OUTPUT_TOKENS=4000
```

Replace both OpenAI placeholders with your own key and an available model that supports strict structured output through the Responses API. Only the selected provider's credentials are required. Keys are kept in the ignored `.env` file; do not commit or paste them into chat. The OpenAI adapter follows the [official OpenAI Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses). To disable new analyses, set `RCA_PROVIDER=disabled`; saved reports remain readable.

Rebuild the API:

```powershell
docker compose up --build -d api
```

Explicit analysis requests send the selected evidence (including log messages and identifiers) to the configured provider. Review what your logs contain before enabling it. OpenAI requests set `store: false`; that OpenAI-specific option is not sent to Gemini. This is not a claim about either provider's retention policies. Google's pricing page marks free-tier usage as eligible for product improvement. No tools, automatic remediation, automatic retries, or live provider calls are enabled in tests.

The Gemini adapter uses the official `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` HTTP endpoint. The API key is sent only in the `x-goog-api-key` header, never in the URL. Model configuration accepts a Gemini model ID or the same ID with a `models/` prefix, which is normalized before use; URLs and arbitrary paths are rejected.

Gemini receives native `systemInstruction`, `contents`, and `generationConfig` fields. The generation configuration uses `responseMimeType: "application/json"` and `responseJsonSchema` containing the schema derived from `RCAOutput`. These fields are documented in [Google's v1beta API definition](https://github.com/googleapis/googleapis/blob/master/google/ai/generativelanguage/v1beta/generative_service.proto) and [REST discovery document](https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta). The adapter sends neither `responseFormat` nor `responseSchema`, avoiding the differing MIME representations shown for `responseFormat` in Google's guide and reference. Local schema references are inlined, nullable text becomes a type array, and string-length constraints unsupported by Google's documented subset are omitted from the generation schema only. The unchanged Pydantic/RCA pipeline still enforces every original constraint, evidence citation, affected service, ranking, confidence limit, and contradiction check before saving a report.

Only a single candidate with `finishReason: STOP` is accepted. Blocked, incomplete, empty, malformed, or unexpected non-text results are rejected. Text parts are joined without including thought parts. The decoded response envelope is limited to 1,000,000 bytes during reading; report text is limited to 100,000 characters. Gemini uses the existing timeout and output-token settings; evidence limits remain provider independent. Public errors never include provider response bodies or API keys.

Gemini HTTP failures produce a warning in the API logs with `http_status`, an allowlisted `provider_status`, and a fixed diagnostic description. The classifier reads at most 16 KiB of the error body and never copies arbitrary provider messages, field names, details, headers, credentials, or incident evidence into logs. Unrecognized errors get a generic description. A failed diagnostic read preserves the original HTTP error mapping, including quota errors. View these warnings with `docker compose logs --tail 50 api`; public API errors remain sanitized.

Google can return HTTP 400 / `INVALID_ARGUMENT` with no field-level explanation. This is logged as `Invalid argument; provider supplied no field details`; it does not establish which field Google rejected. Mocked request-contract tests verify the adapter's wire format and validation, but a successful manual analysis is still needed to verify live provider compatibility.

### Generate and retrieve a report

Use an existing detected incident; you do not need to resubmit the sample logs:

```powershell
$incidentId = 1
$report = Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/incidents/$incidentId/analyze" `
  -ContentType 'application/json' -Body '{"max_logs":40}'
$report.analysis | ConvertTo-Json -Depth 10

Invoke-RestMethod "http://localhost:8000/incidents/$incidentId/analyses?limit=20&offset=0"
Invoke-RestMethod "http://localhost:8000/incidents/$incidentId/analyses/$($report.id)"
```

The request body is optional. If supplied, only `max_logs` (1–100) is accepted; the server's `RCA_MAX_LOGS` remains an upper bound. Every successful POST creates a separate report and makes a new provider call when usable evidence exists. There is no result caching or idempotency key in this phase. GET requests never call the provider and remain available when RCA is disabled.

| Method | Endpoint | Behavior |
|---|---|---|
| POST | `/incidents/{id}/analyze` | Generate, validate and save a report; HTTP 201 |
| GET | `/incidents/{id}/analyses` | Paginated history, newest first |
| GET | `/incidents/{id}/analyses/{report_id}` | One saved report and its evidence snapshot |

### Report contents and validation

The `analysis` object contains:

- `status`: `hypothesis` or `inconclusive`; never `confirmed`.
- `summary`, nullable `probable_cause`, and `supporting_log_ids`.
- `observed_facts`, each with supporting log IDs.
- `ranked_causes`, with contiguous ranks, explanations, confidence, supporting IDs, and contradicting IDs.
- `affected_services`, qualitative `confidence`, `alternative_explanations`, `recommended_checks`, and `evidence_gaps`.

The response also includes provider/model metadata, prompt version, creation time, duration, validation notes, SHA-256 evidence hash, the evidence snapshot, and `causality: "unconfirmed"`.

All citations must be integer IDs from the actual evidence package. A real log elsewhere in the database is not an allowed citation. Invented services, unknown fields, invalid statuses, duplicate citations, contradictory citation roles, and inconsistent cause rankings are rejected. The selected probable cause and its citations must match the first ranked hypothesis. Facts and hypotheses remain separately represented.

Confidence is capped at medium for log-only evidence and reduced to low for sparse or truncated input. If the leading candidate includes contradicting log IDs, the backend changes the result to inconclusive and removes the selected probable cause. This guard depends on the model reporting contradictions; it does not automatically understand every contradiction in natural language.

Structural and reference validation does not prove that cited logs support a claim. The provider must reason about relevance, and a developer must review the result. Test fixtures exercise the pipeline, not real-model diagnosis accuracy.

### Evidence selection and audit history

Evidence includes incident metadata, detection rules, original correlated logs, membership reasons/strengths, and weak relationships whose two log endpoints fit the package. ERROR/CRITICAL records are selected first, ordered by timestamp and ID, followed by lower-severity context. Linked-incident logs use remaining capacity. The final timeline is chronological.

Input is bounded by log count and the character length of the compact evidence JSON. Individual messages are capped at 2,000 characters and may be shortened further to fit. Omitted logs/relationships and truncated messages are recorded as evidence gaps. The original logs are never changed. `affected_services` reflects the incident services represented in the selected evidence, so truncated reports may not cover every service in the full incident.

Saved reports remain historical snapshots when new logs arrive, incident status changes, or another analysis runs. A report does not automatically refresh; generate another report for current evidence. Reports do not change incident status, verify a cause, or save a resolution. If no usable logs fit, a local evidence guard can save an explicitly inconclusive report without making a provider call.

### Failure behavior

| HTTP status | Meaning |
|---|---|
| 404 | Incident/report not found when the request reaches the resource lookup |
| 422 | Invalid request body or pagination |
| 502 | Provider error, refusal, incomplete output, invalid schema, or invalid evidence references |
| 503 | RCA disabled/unconfigured, provider rate limit/quota, or database unavailable |
| 504 | Provider timeout |

Failed provider calls and invalid reports do not create report rows. Provider error details and API keys are not returned to callers. The provider's HTTP timeout applies to network operations. Analysis is synchronous; use background jobs and request controls before scaling to many simultaneous analyses.

### Validate Phase 3

Run the complete suite with SQLite or PostgreSQL using the commands in the Validate section. To focus on RCA:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_rca.py tests/test_llm.py -q
```

Tests inject scripted provider responses and mocked HTTP transports. They cover the sample failure chain, insufficient evidence, declared contradictions, invented and out-of-scope citations, rankings, affected services, evidence limits, report history, timeouts, refusals, malformed output, and sanitized provider errors. OpenAI tests are retained; Gemini tests cover its native request/schema, provider selection, successful and incomplete responses, authentication and quota failures, network errors, redirects, response limits, and end-to-end RCA validation/persistence. PostgreSQL tests additionally verify saved reports survive disposal of the connection pool. All provider calls are mocked; no tests make billable provider requests.

After a manual Gemini analysis, check `$report.provider` is `gemini`, `$report.model` is your configured model ID, every citation belongs to `$report.evidence_snapshot.logs`, and the report can be retrieved through the same history endpoints. A missing/blank Gemini key or model returns 503 without making a provider request. Authentication/configuration errors from Google return 502 with a generic message; quota errors return 503 and timeouts return 504. Failed or invalid responses do not create report rows.

After configuring a live provider, generate a report, inspect every cited log against `evidence_snapshot.logs`, check the difference between observations and hypotheses, and review alternatives and investigation steps. Restart the API and retrieve that same report ID to verify persistence. A successful model call alone is not a diagnosis-quality evaluation; a separate evaluation dataset and human review are still needed.
