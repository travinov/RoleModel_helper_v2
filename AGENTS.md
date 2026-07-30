# AGENTS Guide

## Scope

RoleModel Helper V2 is an isolated hybrid-agent prototype. V1 is outside this repository and must never be modified by V2 install, migration, or runtime code.

## SDD and TDD

1. Start non-trivial changes by updating a scoped file in `docs/specs/`.
2. Write or update the smallest test first and confirm the expected RED.
3. Implement the minimum GREEN change, then run the relevant module before the full suite.
4. Separate local fixture evidence, database integration evidence, and corporate runtime acceptance.

## Isolation

- Default V2 port: `8001`.
- Default service: `rolemodel-helper-v2.service`.
- Default install directory: `~/RoleModelHelperV2`.
- Never copy `.env`, certificates, workbooks, SQLite state, logs, or chat history into Git.
- Fail closed on V1 port, service, directory, or state-schema collisions.

## Quality

- Catalog facts are deterministic and pinned to one immutable version per turn.
- Only explicit fallback may invoke GigaChat, at most once per turn.
- Preserve state on malformed, stale, hallucinated, or timed-out provider plans.
- Record route, catalog version, cache status, GigaChat count, and component latency.
