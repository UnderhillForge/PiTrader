#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
source .venv/bin/activate
export TERM="${TERM:-xterm-256color}"
export TRADEBOT_BTOP_MODE="${TRADEBOT_BTOP_MODE:-1}"
export TRADEBOT_WS_URL="${TRADEBOT_WS_URL:-ws://127.0.0.1:8765}"
exec python bpytop-master/bpytop.py
