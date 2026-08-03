#!/usr/bin/env bash
set -euo pipefail

# Run this script on macOS from an extracted offline RoleModelHelperV2 release.
# It uploads and installs V2 on the existing application server. V1 is only
# read for its health endpoint and GigaChat certificate material.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_TARGET="${RMV2_SSH_TARGET:-CI09479675-lnx-travinov@tsles-assai0001.esrt.sber.ru}"
REMOTE_DIR="${RMV2_REMOTE_DIR:-RoleModelHelperV2}"
V1_REMOTE_DIR="${RMV2_V1_REMOTE_DIR:-RoleModelHelper2}"
APP_PORT="${RMV2_APP_PORT:-8001}"
V1_PORT="${RMV2_V1_APP_PORT:-8000}"
SERVICE_NAME="${RMV2_SERVICE_NAME:-rolemodel-helper-v2.service}"
DB_HOST="${RMV2_DB_HOST:-10.135.162.149}"
DB_PORT="${RMV2_DB_PORT:-5433}"
DB_NAME="${RMV2_DB_NAME:-bdtest}"
DB_USER="${RMV2_DB_USER:-CI09479675-pg-travinov}"
ENV_TRANSFER=""
REMOTE_STAGE=""

cleanup() {
  if [[ -n "$ENV_TRANSFER" && -f "$ENV_TRANSFER" ]]; then
    rm -f -- "$ENV_TRANSFER"
  fi
  if [[ -n "$REMOTE_STAGE" ]]; then
    ssh "$SSH_TARGET" "rm -rf -- \"\$HOME/$REMOTE_STAGE\"" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT HUP INT TERM

die() {
  echo "Ошибка: $*" >&2
  exit 2
}

safe_basename() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] && [[ "$1" != "." ]] && [[ "$1" != ".." ]]
}

for command_name in ssh tar python3; do
  command -v "$command_name" >/dev/null 2>&1 || die "на Mac не найдена команда $command_name"
done
safe_basename "$REMOTE_DIR" || die "небезопасное имя удалённого каталога V2"
safe_basename "$V1_REMOTE_DIR" || die "небезопасное имя удалённого каталога V1"
[[ "$REMOTE_DIR" != "$V1_REMOTE_DIR" ]] || die "каталоги V1 и V2 совпадают"
[[ "$REMOTE_DIR" == "RoleModelHelperV2" ]] || die "production-каталог V2 должен называться RoleModelHelperV2"
[[ "$V1_REMOTE_DIR" == "RoleModelHelper2" ]] || die "ожидался каталог V1 RoleModelHelper2"
[[ "$APP_PORT" == "8001" ]] || die "production V2 должна использовать порт 8001"
[[ "$V1_PORT" == "8000" ]] || die "ожидался порт V1 8000"
[[ "$SERVICE_NAME" == "rolemodel-helper-v2.service" ]] || die "небезопасное имя V2 service"
[[ -f "$PROJECT_DIR/scripts/install_rolemodel_v2_server.sh" ]] || die "не найден server installer"
[[ -f "$PROJECT_DIR/scripts/activate_rolemodel_v2_server.sh" ]] || die "не найден activation script"
compgen -G "$PROJECT_DIR/wheelhouse/*.whl" >/dev/null || \
  die "нет wheelhouse/*.whl: распакуйте итоговый offline ZIP, а не source archive GitHub"

echo "Проверяю SSH и V1 на удалённом сервере..."
ssh "$SSH_TARGET" "bash -s" <<'REMOTE_PREFLIGHT'
set -euo pipefail
command -v python3 >/dev/null
command -v tar >/dev/null
[[ -d "$HOME/RoleModelHelper2" ]]
[[ ! -L "$HOME/RoleModelHelper2" ]]
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=5) as response:
    payload = json.load(response)
if response.status != 200 or not isinstance(payload, dict):
    raise SystemExit("V1 health returned an invalid response")
PY
if [[ -e "$HOME/RoleModelHelperV2" && -L "$HOME/RoleModelHelperV2" ]]; then
  echo "Refusing symlinked V2 target" >&2
  exit 2
fi
if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq ':8001$'; then
  if ! systemctl --user is-active --quiet rolemodel-helper-v2.service 2>/dev/null; then
    echo "Port 8001 is occupied by a process other than the known V2 service" >&2
    exit 2
  fi
fi
REMOTE_PREFLIGHT

HAS_REMOTE_ENV="$(ssh "$SSH_TARGET" 'if [[ -f "$HOME/RoleModelHelperV2/.env" && ! -L "$HOME/RoleModelHelperV2/.env" ]]; then printf yes; else printf no; fi')"

make_dsn() {
  local database_user="$1"
  local password="$2"
  printf '%s' "$password" | python3 -c '
import sys
from urllib.parse import quote
role, host, port, database = sys.argv[1:]
password = sys.stdin.read()
print("postgresql://{}:{}@{}:{}/{}".format(
    quote(role, safe=""), quote(password, safe=""), host, port, quote(database, safe="")
))
' "$database_user" "$DB_HOST" "$DB_PORT" "$DB_NAME"
}

if [[ "$HAS_REMOTE_ENV" == "yes" && "${RMV2_REPLACE_ENV:-0}" != "1" ]]; then
  echo "Сохраняю существующий удалённый .env V2 (RMV2_REPLACE_ENV=1 — заменить)."
else
  echo
  echo "Используется существующая учётная запись PostgreSQL, как в V1."
  echo "База: $DB_HOST:$DB_PORT/$DB_NAME"
  read -r -s -p "Пароль PostgreSQL для $DB_USER: " DB_PASSWORD
  echo
  read -r -s -p "GigaChat Authorization key (Enter, если mTLS достаточно): " GIGACHAT_AUTH_KEY
  echo
  [[ -n "$DB_PASSWORD" ]] || die "пустой пароль PostgreSQL"

  DATABASE_DSN="$(make_dsn "$DB_USER" "$DB_PASSWORD")"
  unset DB_PASSWORD

  ENV_TRANSFER="$(mktemp -t rolemodel-v2-env.XXXXXX)"
  chmod 600 "$ENV_TRANSFER"
  {
    printf '%s\n' \
      'RMV2_APP_HOST=0.0.0.0' \
      'RMV2_APP_PORT=8001' \
      'RMV2_INSTALL_DIR=~/RoleModelHelperV2' \
      'RMV2_SERVICE_NAME=rolemodel-helper-v2.service' \
      "RMV2_DATABASE_DSN=$DATABASE_DSN" \
      "RMV2_MIGRATION_DSN=$DATABASE_DSN" \
      "RMV2_DATABASE_APP_ROLE=$DB_USER" \
      'RMV2_STATE_SCHEMA=rolemodel_v2_runtime' \
      'RMV2_CATALOG_BACKEND=postgres' \
      "RMV2_CATALOG_DSN=$DATABASE_DSN" \
      'RMV2_CATALOG_SCHEMA=rolemodel_v2_catalog' \
      "RMV2_CATALOG_READER_ROLE=$DB_USER" \
      "RMV2_CATALOG_WRITER_ROLE=$DB_USER" \
      "RMV2_CATALOG_IMPORT_DSN=$DATABASE_DSN" \
      'RMV2_V1_APP_PORT=8000' \
      'RMV2_V1_INSTALL_DIR=~/RoleModelHelper2' \
      'RMV2_V1_SERVICE_NAME=rolemodel-helper.service' \
      'RMV2_V1_STATE_SCHEMA=rolemodel_helper' \
      'RMV2_V1_CATALOG_SCHEMA=rolemodel_helper' \
      'RMV2_TLS_VERIFY=true'
    if [[ -n "$GIGACHAT_AUTH_KEY" ]]; then
      printf 'RMV2_GIGACHAT_AUTH_KEY=%s\n' "$GIGACHAT_AUTH_KEY"
    fi
  } >"$ENV_TRANSFER"
  unset DATABASE_DSN GIGACHAT_AUTH_KEY
fi

REMOTE_STAGE="$(ssh "$SSH_TARGET" 'stage=$(mktemp -d "$HOME/.RoleModelHelperV2.deploy.XXXXXX"); basename "$stage"')"
safe_basename "$REMOTE_STAGE" || die "сервер вернул небезопасный staging path"
echo "Передаю offline release в защищённый staging-каталог..."
tar \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='./.env.runtime' \
  --exclude='./certs' \
  --exclude='./logs' \
  --exclude='./output' \
  --exclude='./.venv' \
  --exclude='./__pycache__' \
  -czf - -C "$PROJECT_DIR" . | \
  ssh "$SSH_TARGET" "tar -xzf - -C \"\$HOME/$REMOTE_STAGE\""

ssh "$SSH_TARGET" "bash -s" <<REMOTE_PROMOTE
set -euo pipefail
target="\$HOME/$REMOTE_DIR"
stage="\$HOME/$REMOTE_STAGE"
v1="\$HOME/$V1_REMOTE_DIR"
[[ "\$target" != "\$v1" ]]
[[ -d "\$stage" && ! -L "\$stage" ]]
if [[ -d "\$target" ]]; then
  for preserved in .env .env.runtime certs logs; do
    if [[ -e "\$target/\$preserved" && ! -L "\$target/\$preserved" ]]; then
      rm -rf -- "\$stage/\$preserved"
      cp -a -- "\$target/\$preserved" "\$stage/\$preserved"
    fi
  done
  backup="\$HOME/.RoleModelHelperV2.backup.\$(date +%Y%m%d%H%M%S)"
  mv -- "\$target" "\$backup"
  if ! mv -- "\$stage" "\$target"; then
    mv -- "\$backup" "\$target"
    exit 2
  fi
else
  mv -- "\$stage" "\$target"
fi
chmod 700 "\$target"
REMOTE_PROMOTE
REMOTE_STAGE=""

if [[ -n "$ENV_TRANSFER" ]]; then
  # Secret material travels only on SSH stdin, never in argv or the ZIP.
  ssh "$SSH_TARGET" "umask 077; cat > \"\$HOME/$REMOTE_DIR/.env\"" <"$ENV_TRANSFER"
  rm -f -- "$ENV_TRANSFER"
  ENV_TRANSFER=""
fi

echo "Запускаю удалённые installer и activation. При запросе введите PUBLISH."
ssh -tt "$SSH_TARGET" \
  "cd \"\$HOME/$REMOTE_DIR\" && { ! systemctl --user is-active --quiet '$SERVICE_NAME' 2>/dev/null || systemctl --user stop '$SERVICE_NAME'; } && bash scripts/install_rolemodel_v2_server.sh && bash scripts/activate_rolemodel_v2_server.sh"

echo
echo "Готово: V1 продолжает работать на 8000, V2 установлена на 8001."
