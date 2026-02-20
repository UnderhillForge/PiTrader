#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .venv/bin/activate ]]; then
	echo "Missing virtual environment at .venv"
	exit 1
fi

source .venv/bin/activate

export TERM="${TERM:-xterm-256color}"
export TRADEBOT_BTOP_MODE="${TRADEBOT_BTOP_MODE:-1}"
export TRADEBOT_WS_URL="${TRADEBOT_WS_URL:-ws://127.0.0.1:8765}"

BOT_STARTED_BY_SCRIPT=0
BOT_PID=""

cleanup() {
	if [[ "$BOT_STARTED_BY_SCRIPT" == "1" && -n "$BOT_PID" ]]; then
		kill "$BOT_PID" >/dev/null 2>&1 || true
	fi
}

trap cleanup EXIT INT TERM

if pgrep -af "python main.py" >/dev/null 2>&1; then
	echo "Detected existing main.py process; reusing it."
else
	echo "Starting main.py in background..."
	python main.py >/tmp/tradebot-main.log 2>&1 &
	BOT_PID="$!"
	BOT_STARTED_BY_SCRIPT=1
	echo "main.py pid: $BOT_PID"
fi

echo "Waiting for websocket: $TRADEBOT_WS_URL"
python - <<'PY'
import asyncio
import os
import websockets

url = os.getenv("TRADEBOT_WS_URL", "ws://127.0.0.1:8765")

async def wait_ready(retries=45):
    for i in range(retries):
        try:
            async with websockets.connect(url, open_timeout=2, ping_interval=None):
                print(f"Websocket ready on attempt {i + 1}")
                return
        except Exception:
            await asyncio.sleep(1)
    raise SystemExit("Websocket did not become ready in time")

asyncio.run(wait_ready())
PY

python bpytop-master/bpytop.py
