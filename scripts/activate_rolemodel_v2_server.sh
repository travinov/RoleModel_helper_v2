#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "V2 virtual environment is missing: $PYTHON" >&2
  echo "Run scripts/install_rolemodel_v2_server.sh first." >&2
  exit 2
fi

cd "$PROJECT_DIR"
exec "$PYTHON" "$PROJECT_DIR/scripts/activate_rolemodel_v2_server.py" \
  --project-dir "$PROJECT_DIR" \
  "$@"
