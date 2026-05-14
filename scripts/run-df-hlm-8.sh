#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="${DF_HLM_8_LOCK_DIR:-/tmp/df-hlm-8.lock}"
SCRIPT_PATH="$ROOT_DIR/src/claude_design_renderer.py"
CONFIG_PATH="$ROOT_DIR/config.yaml"

if [[ -e "$ROOT_DIR/STOP.flag" ]]; then
  echo "K14 STOP.flag active"
  exit 14
fi

if [[ -d "$LOCK_DIR" ]]; then
  echo "K16 mutex active: $LOCK_DIR"
  exit 16
fi

if pgrep -f "claude_design_renderer.py" >/dev/null 2>&1; then
  echo "K16 pgrep protection active: renderer already running"
  exit 16
fi

mkdir "$LOCK_DIR"
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python3 "$SCRIPT_PATH" --config "$CONFIG_PATH" "$@"
