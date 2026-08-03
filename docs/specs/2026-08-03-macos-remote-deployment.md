# Mac-to-SberLinux remote deployment

## Goal

Provide one operator command, started from the extracted V2 release on macOS,
that uploads and installs V2 on the existing SberLinux application server and
then starts the guarded V2 activation flow.

## Non-goals

- Creating PostgreSQL accounts or granting privileges that require a DBA.
- Changing, restarting, deleting, or repackaging V1.
- Uploading a role-model workbook; V2 imports the active V1 PostgreSQL snapshot.
- Embedding database passwords, GigaChat credentials, or certificates in the ZIP.

## Inputs and outputs

Inputs:

- an extracted offline V2 release containing `wheelhouse/*.whl`;
- SSH access to the current application server;
- existing remote V1 installation and healthy V1 endpoint on port `8000`;
- DSNs for the pre-created V2 runtime, migration-owner, and catalog-import roles.

Outputs:

- application files in remote `~/RoleModelHelperV2`;
- protected remote V2 `.env` and copied V2 certificate files;
- systemd service `rolemodel-helper-v2.service` on port `8001`;
- a user-reviewed catalog publish and dual V1/V2 health verification.

## Constraints

- The Mac script may run from any extracted directory and must not depend on the
  local V1 checkout.
- The remote V1 directory is `~/RoleModelHelper2`; it is read only for health,
  catalog reads from schema `rolemodel_helper`, and certificate copying.
- Upload is staged first. An existing V2 `.env`, `.env.runtime`, certificates,
  and logs must survive a repeat deployment.
- Secrets are entered without terminal echo, transferred over SSH through
  standard input, and never written to shell command arguments or release files.
- The script refuses colliding ports, install directories, services, or schemas.

## Acceptance criteria

1. `bash scripts/deploy_rolemodel_v2_remote.sh` is the only Mac-side command
   required after extracting the ZIP.
2. The script checks the offline bundle, SSH, healthy V1 on `8000`, and a free
   V2 port `8001` before replacing V2 application files.
3. Upload and server operations target only `~/RoleModelHelperV2`; V1 commands
   are read-only and no V1 service restart/stop is present.
4. The remote `.env` contains port `8001`, separate V2 schemas and DSNs, and is
   mode `0600`; an existing protected `.env` is retained unless the operator
   explicitly chooses to replace it.
5. The existing server installer copies GigaChat certificates from remote V1,
   installs the offline wheels, and configures only the V2 service.
6. Activation stays interactive: it shows a dry-run and requires exact
   `PUBLISH`; it verifies health of both ports after starting V2.
7. README gives the exact Downloads-to-project move and Mac launch commands and
   states the DBA prerequisite clearly.
