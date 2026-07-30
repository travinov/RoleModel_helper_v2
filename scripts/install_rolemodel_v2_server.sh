#!/usr/bin/env bash
set -euo pipefail

if (( EUID == 0 )); then
  echo "Не запускайте весь installer через sudo; повторите запуск обычным пользователем." >&2
  exit 2
fi
CURRENT_USER="$(id -un)"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_INPUT="$PROJECT_DIR/.env.example"
if [[ -e "$PROJECT_DIR/.env" || -L "$PROJECT_DIR/.env" ]]; then
  ENV_INPUT="$PROJECT_DIR/.env"
fi
# Data-only preflight resolves and validates RMV2_DATABASE_DSN and all other
# installation settings; this script never sources either V2 or V1 env files.
PREFLIGHT_OUTPUT="$(
  python3 "$PROJECT_DIR/scripts/installer_preflight.py" \
    --env-file "$ENV_INPUT" \
    --project-dir "$PROJECT_DIR" \
    --current-user "$CURRENT_USER" \
    --emit
)"
while IFS=$'\t' read -r key value; do
  case "$key" in
    APP_PORT) APP_PORT="$value" ;;
    INSTALL_DIR) INSTALL_DIR="$value" ;;
    SERVICE_NAME) SERVICE_NAME="$value" ;;
    DATABASE_DSN) DATABASE_DSN="$value" ;;
    STATE_SCHEMA) STATE_SCHEMA="$value" ;;
    CATALOG_SCHEMA) CATALOG_SCHEMA="$value" ;;
    V1_INSTALL_DIR) V1_INSTALL_DIR="$value" ;;
    V1_SERVICE_NAME) V1_SERVICE_NAME="$value" ;;
    V1_APP_PORT) V1_APP_PORT="$value" ;;
    V1_STATE_SCHEMA) V1_STATE_SCHEMA="$value" ;;
    V1_CATALOG_SCHEMA) V1_CATALOG_SCHEMA="$value" ;;
    V1_ENV_FILE) V1_ENV_FILE="$value" ;;
    RUN_USER) RUN_USER="$value" ;;
    *) echo "Refusing unknown preflight key: $key" >&2; exit 2 ;;
  esac
done <<<"$PREFLIGHT_OUTPUT"

SERVICE_NAME_RE='^[A-Za-z0-9_.@-]+[.]service$'
if [[ ! "$SERVICE_NAME" =~ $SERVICE_NAME_RE ]]; then
  echo "Refusing unsafe systemd service name." >&2
  exit 2
fi
SYSTEMD_DIR="/etc/systemd/system"
UNIT_PATH="$SYSTEMD_DIR/$SERVICE_NAME"
CANONICAL_UNIT_PATH="$(readlink -m "$UNIT_PATH")"
if [[ "$(dirname "$CANONICAL_UNIT_PATH")" != "$SYSTEMD_DIR" ]]; then
  echo "Refusing systemd unit outside $SYSTEMD_DIR." >&2
  exit 2
fi
if [[ -L "$UNIT_PATH" ]]; then
  echo "Refusing symlinked systemd unit path." >&2
  exit 2
fi

if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq ":${APP_PORT}$"; then
  echo "Refusing installation: port $APP_PORT is already in use." >&2
  exit 2
fi

if compgen -G "$INSTALL_DIR/wheelhouse/*.whl" >/dev/null; then
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else "offline bundle requires Python 3.9")'
  OFFLINE_PREFLIGHT_DIR="$(mktemp -d)"
  trap 'rm -rf -- "$OFFLINE_PREFLIGHT_DIR"' EXIT
  python3 -m venv "$OFFLINE_PREFLIGHT_DIR/venv"
  "$OFFLINE_PREFLIGHT_DIR/venv/bin/python" -m ensurepip --version >/dev/null
  rm -rf -- "$OFFLINE_PREFLIGHT_DIR"
  trap - EXIT
fi

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "Created $INSTALL_DIR/.env."
fi
chmod 600 "$INSTALL_DIR/.env"
python3 "$PROJECT_DIR/scripts/installer_preflight.py" \
  --env-file "$INSTALL_DIR/.env" \
  --project-dir "$PROJECT_DIR" \
  --current-user "$CURRENT_USER" \
  --write-effective "$INSTALL_DIR/.env"

CERT_ARGS=(
  --v1-install-dir "$V1_INSTALL_DIR"
  --v2-install-dir "$INSTALL_DIR"
  --v2-env-file "$INSTALL_DIR/.env"
)
if [[ -n "$V1_ENV_FILE" ]]; then
  CERT_ARGS+=(--v1-env-file "$V1_ENV_FILE")
fi
python3 \
  "$INSTALL_DIR/scripts/prepare_gigachat_certs.py" \
  "${CERT_ARGS[@]}"

python3 -m venv "$INSTALL_DIR/.venv"
if compgen -G "$INSTALL_DIR/wheelhouse/*.whl" >/dev/null; then
  "$INSTALL_DIR/.venv/bin/python" -m pip install \
    --no-index \
    --find-links "$INSTALL_DIR/wheelhouse" \
    --requirement "$INSTALL_DIR/requirements-release.txt"
else
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
fi

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=RoleModel Helper V2
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python -m app
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "Installed $SERVICE_NAME. Review $INSTALL_DIR/.env, then migrate V2 only:"
echo "  $INSTALL_DIR/.venv/bin/python -m app.runtime.migrate"
echo "  $INSTALL_DIR/.venv/bin/python -m app.catalog.migrate"
echo "Validate the active V1 snapshot and counts without writing:"
echo "  $INSTALL_DIR/.venv/bin/python -m app.catalog.publish --dry-run"
echo "Publish only after the dry-run counts and SHA-256 are approved:"
echo "  $INSTALL_DIR/.venv/bin/python -m app.catalog.publish"
echo "Start the service only after the migration succeeds:"
echo "  sudo systemctl start $SERVICE_NAME"
