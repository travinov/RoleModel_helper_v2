# RoleModel Helper V2: Fast Hybrid Agent

## Context

- Problem: users wait too long for answers in V1, while the request path can make several sequential GigaChat calls and repeatedly scan broad PostgreSQL datasets.
- Why now: V2 must provide a measurably faster and clearer experience without destabilizing the current production service.
- Evidence: V1 has a mandatory LLM turn interpreter, additional sequential LLM classifiers/rerankers, per-operation PostgreSQL connections, broad fetch-and-rank searches, an extra UI session GET after POST, and no component latency metrics.
- Related V1 areas: `app/agent/service.py`, `app/repositories/search_repository.py`, `app/services/gigachat.py`, `frontend/src/chat/ChatApp.tsx`.

## Goal

- Deliver an independently runnable V2 that answers common role/access queries through a deterministic cached fast path and uses at most one GigaChat planning call for an ambiguous turn.
- Make latency and route selection observable enough to compare V1 and V2 on the same corporate host.

## Non-Goals

- Replacing or updating V1.
- Sharing V1 chat state, deployment service, writable schema, `.env`, certificates, logs, uploads, or backups.
- A full admin upload UI or a new ETL implementation in the first vertical slice.
- Vector RAG, autonomous multi-agent execution, or prompt-only routing.
- Claiming a production latency improvement before a same-host benchmark.

## Change Category Checklist

- [x] Chat API contract
- [x] Agent state machine/slots/phase transitions
- [x] Dialogue benchmark/replay expectations
- [x] Operational checks
- [ ] ETL/workbook parsing or validation behavior
- [ ] Static instruction upload behavior

## Requirements

### Functional

1. Support `SYSTEM_DISCOVERY`, `ROLE_DISCOVERY`, `ROLE_ACQUISITION`, and `INSTRUCTION_LOOKUP`.
2. Route deterministic turns as `HANDLED` or `NEEDS_CLARIFICATION`; route only genuinely ambiguous turns as `FALLBACK`.
3. Handle exact safe aliases, pending slot values, candidate selection, “show more”, reset, and system change without GigaChat.
4. Never auto-resolve a weak or unsafe candidate; ask a bounded clarification instead.
5. Use deterministic catalog data for factual role/system answers. GigaChat output is planning input, never the source of catalog facts.
6. Return the complete updated turn/session payload from message POST so the UI does not need a critical-path GET.
7. Show immediate progress feedback in the UI and distinguish search, clarification, and provider failure.

### Technical constraints

1. New repository: `RoleModel_helper_v2`.
2. Default app port: `8001`; default server directory: `~/RoleModelHelperV2`; default service: `rolemodel-helper-v2.service`.
3. V2 must use its own database or, at minimum, its own writable schema. Startup must fail closed when the configured V2 state schema equals a configured V1 schema.
4. No secrets or certificates are copied into the repository. TLS verification defaults to enabled.
5. Catalog retrieval uses an immutable, versioned in-memory bundle loaded atomically from an independently prepared V2 catalog source.
6. Catalog refresh is single-flight. A failed refresh preserves the last good version.
7. One turn is pinned to one catalog version.
8. A warm deterministic turn performs no GigaChat call and no catalog reload.
9. An ambiguous turn performs at most one structured GigaChat planning call with a bounded deadline and context.
10. Blocking provider/database clients never run directly on the async event loop.
11. Turn persistence is idempotent by `request_id` and increments `state_revision` monotonically.

### Operational constraints

1. V1 and V2 must be able to run simultaneously.
2. Installer/startup scripts must reject V1 service names, directories, state schemas, and port `8000`.
3. Health reports liveness separately from catalog readiness and GigaChat configuration.
4. Deployment is not accepted until both V1 and V2 health endpoints succeed simultaneously on the target host.

## Inputs / Outputs

- Inputs: user text, `request_id`, session state/revision, active V2 catalog version, optional GigaChat structured plan.
- Outputs: assistant text, typed intent/action, resolved slots, pending question, structured factual answer, catalog version, trace ID, route source, and latency summary.
- Side effects: isolated V2 session/message/turn-trace persistence only.

## Domain Invariants

- Preserved:
  - Access levels remain `1` (default) and `2` (on request).
  - Intent and phase transitions are explicit and replayable.
  - `state_revision` is monotonic.
  - Unknown/stale systems are not silently accepted.
  - Instruction answers retain source/citation compatibility when enabled.
- Intentionally changed:
  - LLM interpretation is no longer mandatory.
  - Catalog facts are served from a pinned immutable bundle.
  - API responses include route and latency diagnostics.

## Acceptance Criteria

1. Given an exact safe system alias and a role-list request, V2 returns a deterministic answer or the next required slot with `gigachat_calls = 0`.
2. Given a pending slot, selection, “show more”, reset, or system change, V2 does not call GigaChat.
3. Given an ambiguous complex turn, V2 calls GigaChat exactly once and schema-validates its plan.
4. Given malformed, timed-out, hallucinated, or stale GigaChat output, V2 preserves pre-turn state and returns an explicit retry/clarification result.
5. Given catalog version `v42`, repeated warm queries are cache hits. Publishing `v43` invalidates query results atomically without mixing versions in an in-flight turn.
6. Given a failed `v43` refresh, the service continues with last-good `v42` and reports degraded freshness.
7. Message POST returns the updated state and final answer; the supplied UI does not issue a required follow-up session GET.
8. Every turn records `trace_id`, route, outcome, catalog version, cache hit, GigaChat call count, and component durations.
9. With a fake catalog and provider, 200 warm deterministic turns have local in-process p95 below 50 ms and make zero GigaChat calls. This is a regression guard, not a production claim.
10. On the corporate target, a documented same-host benchmark records V1/V2 p50/p95/p99. Provisional V2 acceptance is warm fast-path p95 at or below 400 ms and p99 at or below 750 ms at concurrency 20.
11. V2 starts on `127.0.0.1:8001` by default, while V1 remains untouched.
12. A PostgreSQL integration check proves V2 migrations/writes do not modify V1 sentinel tables or counts.

## Test Plan (TDD)

- RED commands:
  - `/usr/bin/python3 -m unittest tests.test_fast_path`
  - `/usr/bin/python3 -m unittest tests.test_catalog_cache`
  - `/usr/bin/python3 -m unittest tests.test_gigachat_fallback`
  - `/usr/bin/python3 -m unittest tests.test_api_contract`
  - `/usr/bin/python3 -m unittest tests.test_latency_telemetry`
- Expected RED failure: V2 production modules do not yet exist.
- GREEN command: rerun the exact failing module after each minimal implementation.
- Regression checks:
  - `/usr/bin/python3 -m unittest discover -s tests -p "test_*.py"`
  - `python scripts/benchmark_fast_path.py --turns 200`
- PostgreSQL-enabled checks are separately marked and must not be reported as passing when the database is unavailable.

## Benchmark / Replay Plan

- Port sanitized V1 fixtures for exact/partial/unsafe aliases, pending slots, candidate paging, intent guardrails, and unknown systems.
- Include the negative ordinal case: `СМР 1` in Samara must not match departments `№10` or `№11`.
- Record JSON reports with environment, catalog version, concurrency, turn count, route counts, GigaChat counts, and p50/p95/p99.

## DB / Snapshot Verification

- Verify the active V2 catalog version before and after warmup.
- Verify state writes are qualified to the V2 state schema.
- Verify a V1 sentinel checksum/count is unchanged after migration and a test dialogue.
- Verify a persisted turn stores the catalog version and trace ID it used.

## Rollout / Verification

- Local:
  1. Run unit tests with a deterministic fixture catalog.
  2. Run V2 on port `8001` and smoke-test health/session/message endpoints.
  3. Run the local fast-path benchmark.
- Corporate test host:
  1. Preflight the port, directory, service, database/schema, certificates, and V1 health.
  2. Install V2 without stopping V1.
  3. Run dual-health, isolated-write, dialogue replay, and same-host latency checks.
- Rollback: stop and disable only `rolemodel-helper-v2.service`; retain V1 and its data unchanged.

## Change Notes

- Key decisions:
  - Optimize the number of sequential remote calls before tuning prompts.
  - Prefer deterministic factual composition over LLM prose for catalog answers.
  - Use last-good immutable catalog data rather than partial refreshes.
  - Keep the first vertical slice small enough to benchmark.
- Open risks:
  - Temporary local PostgreSQL verifies isolation and transaction behavior, but
    production cardinality and query plans remain unmeasured until the
    corporate catalog is connected.
  - Port `8001` must still be checked on the target server.
  - Corporate certificate behavior needs a real GigaChat smoke test.
