# Guided production activation

## Goal

Provide one post-install command that validates the V2 configuration, creates
only V2 PostgreSQL objects, previews the active V1 role-model snapshot, requires
explicit approval before publishing it, starts only V2, and verifies both
services.

## Non-goals

- Guessing database passwords, creating DBA-owned login roles, or copying the
  complete V1 `.env`.
- Loading a workbook directly into V2.
- Importing static instructions that do not exist in the V1 ETL snapshot.
- Stopping, restarting, migrating, or writing to V1.

## Inputs and outputs

- Input: `~/RoleModelHelperV2/.env`, the installed `.venv`, active V1
  PostgreSQL snapshot, and an interactive `PUBLISH` confirmation.
- Output: migrated V2 runtime/catalog schemas, one atomically published V2
  catalog release, running `rolemodel-helper-v2.service`, and a dual-health
  report.

## Constraints

- Parse `.env` as data; never source or evaluate it.
- Run as an ordinary account. Use `sudo` only for the named V2 systemd service.
- Require port `8001`, non-`public` V2 schemas, PostgreSQL DSNs, TLS verification,
  and regular non-symlink GigaChat certificate/key files.
- Check V1 health before any migration.
- Execute `app.catalog.publish --dry-run` and show its version, source SHA-256,
  and counts before any catalog publication.
- Without exact confirmation, exit after dry-run without publishing or starting.
- If V2 health fails after start, stop only `rolemodel-helper-v2.service`.
- Recheck V1 health after V2 starts.
- Before service start, atomically create `.env.runtime` with mode `0600` and
  omit migration/import DSNs and catalog-writer identity. The systemd unit must
  read `.env.runtime`, not the privileged activation `.env`.

## Acceptance criteria

1. Malicious shell syntax in `.env` is never executed.
2. Missing/unsafe settings fail before migration or systemd calls.
3. Successful activation runs runtime migration, catalog migration, dry-run,
   publication, V2 restart, V2 health, and final V1 health in that order.
4. Declined confirmation performs no publication or systemd mutation.
5. V2 health failure triggers a stop of only the configured V2 service.
6. A dry-run with zero instructions emits an explicit limitation warning.
7. The running service environment contains runtime/catalog-reader settings but
   no migration DSN, catalog-import DSN, or catalog-writer role.
