# RoleModel Helper V2: PostgreSQL Catalog Publication and Search Tools

## Context

- The V2 runtime state now uses isolated PostgreSQL persistence.
- The application still loads a synthetic JSON catalog directly into memory.
- Production needs a separately prepared, normalized, versioned V2 catalog
  derived from the corporate role model without writing to V1.
- GigaChat must receive bounded tool results, never the full catalog.

## Goal

- Publish a normalized catalog release into isolated schema
  `rolemodel_v2_catalog`.
- Load one immutable active release into memory.
- Provide typed read-only tools for department, position, profile, system, role,
  and instruction resolution.
- Resolve short numeric department input strictly: department `2` must never
  return department `12`, `20`, or `21`.

## Non-Goals

- Writing to V1 catalog or activating a V1 snapshot.
- Passing the complete department, position, system, or role catalog to
  GigaChat.
- Letting the LLM execute SQL or modify aliases.
- Adding a semantic/vector index before lexical and structured recall are
  measured on the corporate dataset.
- Implementing the bounded GigaChat loop in this change.

## Catalog Schema

Schema `rolemodel_v2_catalog`:

- `catalog_release`
  - version, source hash, status, counts, timestamps;
- `catalog_pointer`
  - singleton active release pointer;
- `catalog_entity`
  - release version, entity type/id, raw name, normalized name, kind, numeric
    marker, normalized city, parent ID, structured JSON payload;
- `catalog_alias`
  - release version, entity type/id, raw/normalized alias, safety class,
    collision count;
- `profile_department`, `profile_position`, `profile_system`, `profile_role`
  - exact release-scoped relations.

## Publication Contract

1. Read/parse input outside the activation transaction.
2. Preserve every source display name and stable source identifier.
3. Derive normalized name, tokens, entity kind, department number, and city.
4. Normalize simple Russian ordinals `первый...двадцатый` to numeric markers.
5. Classify alias collisions within the candidate scope.
6. Insert a `STAGING` release and all entities/relations.
7. Validate referential integrity, counts, duplicate IDs, and required fields.
8. In one transaction, set the release `ACTIVE`, update the singleton pointer,
   and retire the prior active release.
9. A failed publication leaves the previous active release unchanged.
10. Runtime workers pin one loaded release for the whole turn.

## Search Tool Contracts

- `search_departments(query, city?, position?, limit=5)`
- `search_positions(query, city?, department_id?, limit=5)`
- `resolve_profiles(city?, department_id, position_id, system_id?)`
- `search_systems(query?, profile_ids?, limit=5)`
- `search_roles(system_id, profile_ids?, query?, access_level?, limit=5)`
- `get_access_instruction(system_id, role_ids?)`

Every result contains:

- status `FOUND`, `AMBIGUOUS`, `NOT_FOUND`, or `INVALID`;
- active catalog version;
- at most `limit` typed candidates;
- stable ID, display label, score, matched-by evidence, and safe context;
- no arbitrary database row or hidden prompt content.

## Ranking Rules

1. Exact stable ID and exact safe alias outrank all fuzzy matches.
2. Exact structured fields are filters, not soft prompt hints.
3. If a query contains a department number, numeric mismatch is excluded.
4. City mismatch is excluded when city is confirmed.
5. Confirmed department/position/profile relations outrank text similarity.
6. Fuzzy text uses normalized token coverage and character similarity only
   after structured filtering.
7. Auto-selection requires a unique safe/exact candidate or a configured
   confidence and margin; otherwise return `AMBIGUOUS`.

## Acceptance Criteria

1. Catalog schema migration is isolated from V1 and runtime schemas.
2. Publishing a valid release activates it atomically.
3. Publishing an invalid release preserves the previous active release.
4. Raw names and normalized derived fields coexist.
5. `Отдел 2`, `отдел №2`, `номер 2`, `второй отдел`, and `2-й отдел` resolve to
   numeric marker `2`.
6. Numeric query `2` never returns candidates numbered `10`, `11`, `12`, or
   `20`.
7. Multiple valid departments numbered `2` return `AMBIGUOUS` until city or
   another context field distinguishes them.
8. Position and profile resolution uses exact catalog relations.
9. System and role results are restricted by resolved profile access.
10. Tool output contains at most five candidates by default.
11. GigaChat is not called by any search tool.
12. PostgreSQL round-trip preserves the release version and all relation IDs.
13. Existing V2 fast-path tests remain green.

## TDD / Verification

- Unit:
  - query and catalog normalization;
  - numeric hard filtering;
  - alias collision classification;
  - ranking and ambiguity thresholds.
- Temporary PostgreSQL:
  - migration isolation;
  - valid publication and atomic activation;
  - invalid publication last-good behavior;
  - source round-trip and runtime role read-only access.
- Dialogue fixtures:
  - `Отдел 2`;
  - `второй отдел`;
  - same number in two cities;
  - department + position + system + role goal.
- Corporate:
  - real workbook/source snapshot adapter;
  - counts and source checksum;
  - recall@5 and latency on real cardinality.
