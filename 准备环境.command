#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$APP_ROOT/scripts/setup-macos.sh"
