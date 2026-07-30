# RoleModel Helper V2: Read-only V1 Snapshot Adapter

## Goal

Build a V2 catalog release from the active V1 ETL snapshot while keeping the
runtime application isolated from V1 tables.

## Non-Goals

- Writing to, migrating, locking, or activating a V1 snapshot.
- Reusing V1 chat/session tables.
- Giving the V2 runtime role access to V1.
- Parsing the source workbook again in the request path.
- Inventing access instructions that do not exist in the source snapshot.

## Inputs / Outputs

- Input: PostgreSQL DSN for a dedicated import role, V1 schema name, active V1
  snapshot.
- Output: validated V2 catalog document, deterministic release version,
  source SHA-256, snapshot ID, and source counts.
- Side effect: none. Publication into V2 is a separate explicit command.

## Constraints

1. Every query is schema-qualified and read-only.
2. The adapter reads exactly one active snapshot in one repeatable-read,
   read-only transaction.
3. Display names are preserved.
4. Stable source codes are preferred for IDs; deterministic hashes are used
   only when a code is absent.
5. Department and position normalization happens during publication, never
   during a user request.
6. Entitlement plus access level becomes a V2 role because V1 access level is
   profile-specific.
7. City is extracted only from explicit `г.` / `город` structure segments;
   missing city remains empty rather than guessed.
8. Unsafe source aliases are not promoted to safe aliases.
9. Runtime startup reads only the active V2 release and never queries V1.

## Acceptance Criteria

1. A fixture V1 snapshot is converted to departments, positions, profiles,
   systems, roles, and exact profile-access relations.
2. The active snapshot file hash becomes the publication source hash.
3. Switching or mutating V1 after extraction cannot mix rows into that
   extracted document.
4. The adapter role can be read-only on V1; the runtime role still has no V1
   privileges.
5. The resulting document passes `PostgresCatalogPublisher` validation and
   round-trips through the isolated V2 catalog schema.
6. A failed extraction or publication leaves the prior V2 active release
   unchanged.

## Verification

- Unit: deterministic IDs and explicit city extraction.
- Temporary PostgreSQL: active V1 fixture -> adapter -> V2 publication ->
  typed search tools.
- Security: V1 sentinel remains unchanged; runtime reader cannot select it.
- Corporate acceptance remains separate: compare extracted counts and
  source SHA-256 with the actual active V1 snapshot.
