#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT/server"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先运行 scripts/setup-macos.sh'
  exit 2
fi
exec .venv/bin/python -m hidagent.workbench serve
