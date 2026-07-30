#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-$PROJECT_DIR/output/RoleModel_helper_v2-sberlinux9-x86_64.zip}"
BUILD_PYTHON="${RMV2_BUILD_PYTHON:-python3}"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
WHEELHOUSE="$TEMP_DIR/wheelhouse"
mkdir -p "$WHEELHOUSE"

"$BUILD_PYTHON" -m pip download \
  --only-binary=:all: \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 39 \
  --abi cp39 \
  --dest "$WHEELHOUSE" \
  --requirement "$PROJECT_DIR/requirements-release.txt"

"$BUILD_PYTHON" "$PROJECT_DIR/scripts/build_release.py" \
  --root "$PROJECT_DIR" \
  --output "$OUTPUT_PATH" \
  --wheelhouse "$WHEELHOUSE"
