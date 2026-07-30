# RoleModel Helper V2: Production Turn Observability

## Goal

Persist low-cardinality turn route/outcome and latency fields in PostgreSQL so
same-host V1/V2 performance can be measured without parsing application logs.

## Non-Goals

- Exporting user text, prompts, credentials, or catalog payloads to metrics.
- Adding an unauthenticated metrics endpoint.
- Claiming corporate latency before deployment.

## Inputs / Outputs

- Input: validated turn response diagnostics.
- Output: route, outcome, GigaChat call count, total duration, GigaChat
  duration, timestamp, and catalog version in the V2 runtime schema.

## Constraints

1. Migration remains isolated to `rolemodel_v2_runtime`.
2. Metrics are committed atomically with the turn and its state revision.
3. Idempotent replay does not create a second observation.
4. Values are validated with database checks.
5. No credential, certificate, prompt, or full catalog field is added.

## Acceptance Criteria

1. Schema version 2 adds typed telemetry columns and a time/route index.
2. A deterministic turn persists `gigachat_calls=0`.
3. A provider turn persists at most `gigachat_calls=1`.
4. Invalid negative duration/call values are rejected by PostgreSQL.
5. V1 sentinel permissions and data remain unchanged.
6. A SQL summary can compute count and percentile latency by route.
