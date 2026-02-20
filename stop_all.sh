#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

stopped=0

if pgrep -af "python main.py" >/dev/null 2>&1; then
	echo "Stopping main.py..."
	pkill -f "python main.py" || true
	stopped=1
fi

if pgrep -af "python bpytop-master/bpytop.py" >/dev/null 2>&1; then
	echo "Stopping bpytop..."
	pkill -f "python bpytop-master/bpytop.py" || true
	stopped=1
fi

if [[ "$stopped" == "0" ]]; then
	echo "No main.py or bpytop process found."
else
	echo "Stopped running TradeBot and/or bpytop processes."
fi
