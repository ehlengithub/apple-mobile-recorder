#!/usr/bin/env bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
  echo '本脚本目前只在 macOS 验证，其他系统请按 README 分别准备依赖。' >&2
  exit 2
fi
if ! command -v brew >/dev/null; then
  echo '请先从 https://brew.sh 安装 Homebrew，然后重新运行本脚本。' >&2
  exit 2
fi
for package in uv arduino-cli uxplay; do
  if ! command -v "$package" >/dev/null; then brew install "$package"; fi
done
if [[ ! -x "$APP_ROOT/server/.venv/bin/python" ]]; then
  uv venv --python 3.12 "$APP_ROOT/server/.venv"
fi
uv sync --project "$APP_ROOT/server" --extra dev --frozen
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
if [[ ! -f "$APP_ROOT/server/.env" ]]; then cp "$APP_ROOT/server/.env.example" "$APP_ROOT/server/.env"; fi
echo '环境准备完成。现在双击「启动工作台.command」。此脚本未烧录或控制任何设备。'
