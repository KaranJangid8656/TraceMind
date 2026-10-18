# Gemini HTTP 400 investigation

## Confirmed trigger and change

The configured model accepted minimal text, JSON mode, and a minimal object schema. It rejected the translated RCA schema with HTTP 400 / `INVALID_ARGUMENT`. Google's message was generic and contained no field details. Controlled schema reduction isolated a combination of nested citation-array `maxItems: 100` bounds, rather than an authentication or request-envelope problem.

The translator now omits only these generation-time upper bounds:

- `properties.observed_facts.items.properties.supporting_log_ids.maxItems`
- `properties.ranked_causes.items.properties.supporting_log_ids.maxItems`
- `properties.ranked_causes.items.properties.contradicting_log_ids.maxItems`

The same complete schema then returned HTTP 200. This identifies a failing constraint combination; it does not establish Google's internal implementation or imply that every `maxItems` keyword is unsupported. Pydantic retains and enforces all three limits before reports can be saved. No model, credentials, output-token limits, routes, database models, or evidence validators were changed.

## Request inventory

Endpoint: `POST https://generativelanguage.googleapis.com/v1beta/models/{configured_model}:generateContent`.

Authentication uses the `x-goog-api-key` header. JSON content type is set by httpx. Credentials are never placed in the URL or diagnostics.

Every application-supplied body field:

- `systemInstruction.parts[].text`: fixed investigation instructions.
- `contents[].role`: `user`.
- `contents[].parts[].text`: serialized evidence JSON.
- `generationConfig.candidateCount`: `1`.
- `generationConfig.maxOutputTokens`: existing configured output limit.
- `generationConfig.responseMimeType`: `application/json`.
- `generationConfig.responseJsonSchema`: translated RCA schema.

## Live probe results

All 19 isolation probes used synthetic text, not incident evidence. Each failing branch stopped before adding further production settings. Successful probes below returned HTTP 200 with `finishReason: STOP`; failed probes returned HTTP 400 / `INVALID_ARGUMENT`.

| Variant | Result |
| --- | --- |
| 1. Minimal text | 200 |
| 2. JSON mode without schema | 200 |
| 3. Minimal object schema | 200 |
| 4. Original translated RCA schema | 400 |
| 4a. Minimal object with RCA root keywords | 200 |
| 4b. First five RCA properties, including nullable cause | 200 |
| 4c. Remaining RCA properties | 400 |
| 4d. Confidence, facts, and causes only | 400 |
| 4e. Ranked causes only | 400 |
| 4f. Ranked causes with numeric rank bounds removed | 400 |
| 4g. Ranked causes without citation arrays | 200 |
| 4h. Ranked causes containing only supporting citations | 200 |
| 4i. Ranked causes containing both bounded citation arrays | 400 |
| 4j. Same two arrays, without contradicting-citation upper bound | 200 |
| 4k. Full RCA schema, without that single upper bound | 400 |
| 4l. Full RCA schema, without all three nested citation upper bounds | 200 |
| 5a. Restore candidateCount | 200 |
| 5b. Restore configured maxOutputTokens | 200 |
| 5c. Restore systemInstruction | 200 |

Only the three proven generation-schema bounds were changed. Numeric rank bounds were restored because removing them had not resolved the rejection.

## Translator audit

- Canonical `$defs` and `$ref` are inlined; neither reaches Gemini.
- The sole nullable union becomes `type: ["string", "null"]`; no `anyOf` remains in this generated schema. The nullable property passed a live probe.
- `additionalProperties: false`, required properties, enums, numeric rank limits, and array lower bounds remain intact.
- String `minLength`/`maxLength` were already omitted from generation and remain enforced by Pydantic.
- Root array upper bounds remain intact. No unrelated JSON Schema keywords were removed.

## Production endpoint validation

After rebuilding, the first request to `POST /incidents/1/analyze` got past Gemini's HTTP rejection but TraceMind rejected the generated output with `RCA report probable cause does not match its first hypothesis`. The prompt was then clarified to require an exact verbatim match between `probable_cause` and the first ranked cause. A subsequent live verification succeeded, persisted report ID `1`, and retrieved it from PostgreSQL. The report used `gemini-3.5-flash-lite`, completed in approximately 3.4 seconds, and retained `causality: unconfirmed`.

That report remains labeled `rca-v1` because it was generated before the prompt-version housekeeping update. New reports use `rca-v2`, allowing consumers to distinguish output generated with the exact-match prompt contract. Historical report metadata is not rewritten.

Temporary diagnostics existed only in a container Python process, which was terminated. No diagnostic source files, raw responses, credentials, or incident evidence were saved. Existing bounded, allowlisted production error logging remains enabled.

## Verification

- Full PostgreSQL suite: **182 passed** at the time of the HTTP 400 fix, with one existing Starlette deprecation warning. Tests used isolated schemas and mocked providers. The prompt-version follow-up is verified separately in the current test run.
- Ruff lint: passed.
- Ruff format check: **18 files already formatted**.
- Docker API rebuild: succeeded.
- New regressions cover the rejected/accepted schema contract and local enforcement of all three 100-ID limits, including prevention of report persistence on invalid output.

Official references: [generateContent API](https://ai.google.dev/api/generate-content), [structured output and limitations](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).
