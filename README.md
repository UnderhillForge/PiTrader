# cbgrokbot

Autonomous Grok-powered Coinbase Advanced Trade perpetuals bot with modular async services.

## What It Runs
- Portfolio rebalance loop (nuclear/hybrid sleeve logic)
- Grok decision loop using `system.prompt`
- Market basket + price + ATR cache refresh
- Trade lifecycle monitor (partials, stop/TP checks, trailing, pump timer close)
- Rumor polling loop (stub-ready)
- Console websocket snapshot stream
- Textual touchscreen dashboard (`console_main.py`) with live portfolio panels and mini-candle market view

## Quick Setup
1. Create virtualenv and install deps:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`
2. Create `.env` with:
   - `COINBASE_API_KEY=...`
   - `COINBASE_API_SECRET=...`
   - `GROK_API_KEY=...`
   - `GROK_MODEL=grok-beta` (or preferred xAI model)
   - `PORT=8765` (optional)
   - `SYSTEM_PROMPT_PATH=system.prompt` (optional)

### Optional: Use `config.json` instead of `.env`
- Copy template:
   - `cp config.json.example config.json`
- Fill in your keys and settings.
- Precedence: environment variables override `config.json` values.
- Change file path if needed:
   - `CONFIG_JSON_PATH=/path/to/config.json python main.py`
3. Start:
   - `python main.py`

4. In another terminal (dashboard):
   - `python console_main.py`

## Safe Live Data Modes
- Data only (no AI decisions, no trade monitor):
   - `DATA_ONLY_MODE=true DRY_RUN_ORDERS=true python main.py`
- Dry-run trading (AI + lifecycle active but never places real orders):
   - `DRY_RUN_ORDERS=true python main.py`
- Simulation mode (recommended overnight test):
   - Set `"SIMULATION_MODE": true` in `config.json`
   - Start bot normally: `python main.py`
   - `SIMULATION_MODE` forces `DRY_RUN_ORDERS=true`

Use dashboard simultaneously:
- `python console_main.py`

## Databases
- `portfolio.db`: sleeve/mode/rebalance history
- `rumors.db`: rumor events
- `trades.db`:
  - `trades`: realized trade journal
  - `live_trades`: open-trade persistence for restart recovery

## Systemd (Raspberry Pi)
Service templates are in `systemd/`.

Install:
1. Copy files:
   - `sudo cp systemd/cbgrokbot.service /etc/systemd/system/`
   - `sudo cp systemd/cbgrokconsole.service /etc/systemd/system/`
2. Edit paths/user in each service if your install path differs.
3. Enable and start:
   - `sudo systemctl daemon-reload`
   - `sudo systemctl enable cbgrokbot.service cbgrokconsole.service`
   - `sudo systemctl start cbgrokbot.service cbgrokconsole.service`
4. Check logs:
   - `journalctl -u cbgrokbot.service -f`
   - `journalctl -u cbgrokconsole.service -f`

## Notes
- If Coinbase/OpenAI/dotenv packages are missing, the bot starts in degraded mode and skips dependent actions.
- `system.prompt` should stay aligned with execution constraints in `modules/trade.py`.
- Dashboard connects to `ws://127.0.0.1:8765` by default; override with `DASHBOARD_WS_URL`.
