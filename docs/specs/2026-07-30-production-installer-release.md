# Production installer and sanitized release

## Goal

Install V2 independently on `127.0.0.1:8001`, reusing only the existing
GigaChat client certificate, private key, and optional CA from V1, and build a
reproducible release archive that never contains credentials.

## Non-goals

- Modifying, stopping, migrating, or packaging V1.
- Copying V1 `.env`, data, logs, workbooks, database files, or deployment
  scripts.
- Disabling TLS verification.

## Inputs and outputs

- Inputs: isolated V2 directory/service/port/schemas; optional explicit V1 env
  file; V1 install directory (default `~/RoleModelHelper2`); a build host able
  to download Linux CPython 3.9 wheels.
- Outputs: regular files in `<V2>/certs/gigachat` with directory mode `0700`
  and file mode `0600`; absolute `RMV2_GIGACHAT_*` paths in the V2 `.env`;
  deterministic ZIP and matching SHA-256 file.

## Constraints

- V1 env files are parsed as data. They are never sourced or evaluated.
- An existing V2 `.env` is also parsed as data. Exported `RMV2_*` values take
  precedence, and the resulting effective install values are validated before
  the installer mutates the target. After preflight, those validated effective
  install values are persisted back without removing unrelated V2 env entries.
- Explicit `RM_GIGACHAT_CERT_FILE`, `RM_GIGACHAT_KEY_FILE`, and optional
  `RM_GIGACHAT_CA_BUNDLE` take precedence over the conventional
  `<V1>/certs/gigachat` files.
- Certificate and key are an inseparable pair. Missing, non-regular, symlinked,
  or unreadable configured inputs fail before destination mutation.
- Installer retains V2 collision guards and always rejects port `8000`.
- Port `8000` and schema `public` remain forbidden even if the configurable
  `RMV2_V1_*` comparison values are changed. Service names must match
  `[A-Za-z0-9_.@-]+.service`, and the derived unit must remain directly below
  the current account's `~/.config/systemd/user` directory.
- A release with `wheelhouse/*.whl` installs with `pip --no-index`; a source
  checkout without `wheelhouse` may use the normal package index.
- Offline wheels target SberLinux 9 x86_64 / CPython 3.9 via
  `manylinux2014_x86_64`, `cp39`, and exact release dependency pins. Wheels
  exist only in the generated archive, never in the Git checkout.
- Before changing `.env`, certificates, `.venv`, or systemd, an offline
  installer verifies that the host interpreter is exactly Python 3.9 and can
  create a venv with a working `ensurepip`.
- The installer itself must run as the current ordinary account. EUID `0`
  (including `sudo bash installer`) fails immediately with guidance to rerun
  without sudo. Installation and activation use only `systemctl --user`; they
  must never require privileged writes or contain a `User=` override.
- Release packaging excludes Git metadata, real env files, virtualenvs,
  certificates/keys/PEM files, caches, logs, databases/dumps, and generated
  output. Symlinks are rejected.
- A source-tree `wheelhouse/` is always excluded. Wheels may enter the archive
  only through the explicit external wheelhouse argument.
- Archive and checksum destinations must not be symlinks, and the direct output
  parent must be a real directory rather than a symlink. Every already-existing
  ancestor in the lexical output/checksum path is checked as well.

## Acceptance criteria

1. A malicious shell expression in a V1 env file is never executed.
2. Valid explicit V1 certificate paths are copied to canonical V2 filenames,
   permissions are restricted, unrelated V2 env lines remain, and absolute V2
   paths are written.
3. Conventional V1 certificate paths work when no explicit paths exist;
   optional CA absence succeeds, while an explicitly configured missing CA
   fails closed.
4. Two builds of an unchanged tree are byte-identical and have the same
   SHA-256. The digest file is accepted by `sha256sum -c`.
5. The archive contains no excluded path, seeded secret, or private-key marker.
6. The offline build injects only regular `.whl` files under `wheelhouse/`, and
   the installer selects the no-index branch whenever that directory is
   present.
7. Malicious V2 env content is not executed; effective unsafe values and
   traversal-style service names fail before target mutation.
8. Existing output/checksum symlinks and a symlinked output parent are rejected
   without modifying their targets.
9. Full-root installer execution exits before target mutation; the user unit is
   written without sudo and contains no system-level `User=` override.
10. A nested symlink ancestor cannot redirect either release artifact outside
    the requested output tree.
