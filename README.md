# TraceMind

Phase 1 of an incident investigation platform: validated log ingestion and filtered retrieval with FastAPI, Pydantic, SQLAlchemy, and PostgreSQL. Incident correlation, embeddings, and AI diagnosis are future phases and are not implemented here.

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

Tests use isolated in-memory SQLite databases to validate API behavior without an external service. They cover persistence across requests, normalization, combined filters, time boundaries, pagination, atomic validation rejection, and invalid input. They do not prove PostgreSQL connectivity; validate that separately using Docker and the sample requests above.

The sample logs illustrate connection exhaustion and downstream failures. Phase 1 only stores these events; it does not infer causality or establish a root cause.
