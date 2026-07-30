# RoleModel Helper V2: Isolated PostgreSQL Runtime

## Context

- V2 currently persists sessions and turns in a local SQLite file.
- Production V2 must reuse the existing PostgreSQL installation without sharing
  writable V1 state.
- V1 and V2 must remain independently deployable and rollbackable.
- This specification is the persistence foundation for the later catalog,
  search-tool, and bounded-agent-loop specifications.

## Goal

- Persist V2 session state and turn history in an isolated PostgreSQL schema in
  the existing PostgreSQL database/cluster.
- Preserve atomic state revisions, request idempotency, per-session
  serialization, and complete turn diagnostics.
- Prove that V2 migrations and dialogue writes do not modify configured V1
  sentinel objects.

## Non-Goals

- Migrating V1 chat history into V2.
- Sharing writable chat, state, alias, upload, or ETL tables with V1.
- Implementing the production catalog ETL or agent tool loop in this change.
- Creating or configuring the corporate PostgreSQL server itself.
- Claiming corporate runtime acceptance from local temporary-PostgreSQL tests.

## Inputs / Outputs

- Inputs:
  - PostgreSQL DSN supplied outside Git;
  - V2 runtime schema, default `rolemodel_v2_runtime`;
  - configured V1 state schema, default `public`;
  - session ID, request ID, user text, and expected state revision.
- Outputs:
  - created/read session state;
  - idempotent persisted turn response;
  - catalog version and trace ID stored with every turn;
  - explicit startup or migration failure on missing schema or isolation
    collision.

## Constraints

1. The V2 runtime schema must differ from the configured V1 state schema.
2. Production bootstrap must use PostgreSQL and must not silently fall back to
   SQLite.
3. Runtime startup verifies the migration version but does not create or alter
   database objects.
4. Schema creation and upgrades run through a separate migration command.
5. SQL identifiers are composed as identifiers; user/config values are never
   interpolated into SQL text.
6. Runtime transactions lock only the targeted session row.
7. Provider and retrieval work must not hold an open database transaction.
8. `UNIQUE(session_id, request_id)` is the database-level idempotency guard.
9. State revision comparison and the final state/message write occur in the
   same transaction.
10. The runtime database role needs DML only inside the V2 schema.
11. A turn remains pinned to one catalog version and persists its trace ID.
12. Secrets, DSNs, certificates, dumps, and database files remain outside Git.

## Data Model

Schema `rolemodel_v2_runtime`:

- `schema_migration`
  - `version INTEGER PRIMARY KEY`
  - `applied_at TIMESTAMPTZ`
- `session_state`
  - `session_id TEXT PRIMARY KEY`
  - `revision BIGINT NOT NULL`
  - `state_json JSONB NOT NULL`
  - `created_at TIMESTAMPTZ`
  - `updated_at TIMESTAMPTZ`
- `turn_message`
  - `id BIGSERIAL PRIMARY KEY`
  - `session_id TEXT REFERENCES session_state`
  - `request_id TEXT NOT NULL`
  - `user_text TEXT NOT NULL`
  - `response_json JSONB NOT NULL`
  - `catalog_version TEXT NOT NULL`
  - `trace_id TEXT NOT NULL`
  - `created_at TIMESTAMPTZ`
  - unique `(session_id, request_id)`

## Transaction Contract

1. Read an existing response by `(session_id, request_id)` and replay it when
   found.
2. Read the current state outside a write transaction.
3. Run provider/retrieval/agent work without holding a database lock.
4. Begin a transaction and lock the target `session_state` row with
   `SELECT ... FOR UPDATE`.
5. Recheck idempotency and expected revision.
6. Insert the turn and update state/revision atomically.
7. Commit, or roll back both writes on any failure.

## Acceptance Criteria

1. Safe defaults select PostgreSQL schema `rolemodel_v2_runtime`, distinct from
   V1 `public`.
2. Configuration rejects an equal V1/V2 schema and a missing production DSN.
3. Migration creates only the configured V2 schema and records migration
   version `1`.
4. Runtime startup fails closed when the migration is absent or stale.
5. Session creation and retrieval work through PostgreSQL JSONB state.
6. Replaying the same request ID returns the original response and creates one
   turn row.
7. A stale state revision raises a conflict and writes no message or state.
8. Slow work in one session does not serialize another session.
9. Concurrent writes for one session serialize at the database row and only one
   valid revision succeeds.
10. A temporary PostgreSQL integration test proves migration, create, post,
    replay, conflict, and persisted diagnostics.
11. A sentinel table/count in the configured V1 schema is unchanged after V2
    migration and a test dialogue.
12. The installer and environment example no longer configure SQLite state.
13. Full existing V2 unit tests pass after the storage replacement.

## TDD Plan

1. Add configuration tests for PostgreSQL DSN/schema isolation.
2. Add SQL/migration contract tests.
3. Add temporary-PostgreSQL integration tests for acceptance criteria 3–11.
4. Confirm the new tests fail because PostgreSQL storage does not exist.
5. Implement the migration and runtime store.
6. Rerun targeted tests, then the complete suite.

## Verification Levels

- Local unit:
  - configuration and migration SQL contracts;
  - runtime behavior with deterministic fixtures.
- Local PostgreSQL integration:
  - disposable cluster initialized under a temporary directory;
  - V1 sentinel checksum/count before and after V2 work.
- Corporate runtime:
  - same-host V1/V2 health;
  - actual schema owners and grants;
  - backup/restore;
  - database size, connections, CPU, memory, and disk;
  - real workbook/catalog import and dialogue replay.

## Rollout / Rollback

- Run V2 migration with a dedicated migration credential.
- Grant the runtime role access only to `rolemodel_v2_runtime`.
- Start V2 on port `8001` without stopping V1.
- Rollback stops/disables only `rolemodel-helper-v2.service`.
- Dropping or deleting the V2 schema is not part of application rollback.
