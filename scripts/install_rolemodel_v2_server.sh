#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

INSTALL_DIR="${RMV2_INSTALL_DIR:-$HOME/RoleModelHelperV2}"
SERVICE_NAME="${RMV2_SERVICE_NAME:-rolemodel-helper-v2.service}"
APP_PORT="${RMV2_APP_PORT:-8001}"
STATE_SCHEMA="${RMV2_STATE_SCHEMA:-rolemodel_helper_v2}"
STATE_DATABASE_PATH="${RMV2_STATE_DATABASE_PATH:-$INSTALL_DIR/data/state.sqlite3}"
V1_INSTALL_DIR="${RMV2_V1_INSTALL_DIR:-$HOME/RoleModelHelper2}"
V1_SERVICE_NAME="${RMV2_V1_SERVICE_NAME:-rolemodel-helper.service}"
V1_APP_PORT="${RMV2_V1_APP_PORT:-8000}"
V1_STATE_SCHEMA="${RMV2_V1_STATE_SCHEMA:-public}"
V1_STATE_DATABASE_PATH="${RMV2_V1_STATE_DATABASE_PATH:-$V1_INSTALL_DIR/data/state.sqlite3}"

STATE_DATABASE_ABS="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$STATE_DATABASE_PATH")"
V1_STATE_DATABASE_ABS="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$V1_STATE_DATABASE_PATH")"
V1_INSTALL_ABS="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$V1_INSTALL_DIR")"

if [[ "$INSTALL_DIR" == "$V1_INSTALL_DIR" \
   || "$SERVICE_NAME" == "$V1_SERVICE_NAME" \
   || "$APP_PORT" == "$V1_APP_PORT" \
   || "$STATE_SCHEMA" == "$V1_STATE_SCHEMA" \
   || "$STATE_DATABASE_ABS" == "$V1_STATE_DATABASE_ABS" \
   || "$STATE_DATABASE_ABS" == "$V1_INSTALL_ABS"/* ]]; then
  echo "Refusing installation: V2 collides with V1." >&2
  exit 2
fi
if [[ "$PROJECT_DIR" != "$INSTALL_DIR" ]]; then
  echo "Run this script from the final isolated directory: $INSTALL_DIR" >&2
  exit 2
fi
if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq ":${APP_PORT}$"; then
  echo "Refusing installation: port $APP_PORT is already in use." >&2
  exit 2
fi

python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "Created $INSTALL_DIR/.env. Fill in host paths and GigaChat credentials before service start."
fi

UNIT_PATH="/etc/systemd/system/$SERVICE_NAME"
sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=RoleModel Helper V2
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python -m app
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "Installed $SERVICE_NAME. Start only after reviewing $INSTALL_DIR/.env:"
echo "  sudo systemctl start $SERVICE_NAME"
