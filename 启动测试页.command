#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
echo '在iPhone/iPad Safari中打开 http://电脑局域网IP:8992/ ，再从工作台录制九个测试点。'
exec "$APP_ROOT/server/.venv/bin/python" -m http.server 8992 --bind 0.0.0.0 --directory "$APP_ROOT/examples/targets"
