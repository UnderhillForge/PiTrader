# TradeBot Runtime API Guide

This document describes every practical way to read runtime data from `main.py` while the bot is running, including live streaming, persisted storage, and control surfaces.

## 1) Runtime surfaces at a glance

When `main.py` is running, data is available through:

1. **Console WebSocket stream** (primary live API)
2. **SQLite databases** (persistent records)
3. **Control/config files** (interactive runtime behavior)
4. **Event replay utility** (timeline reconstruction)
5. **Daily score CSV** (`daily_score.csv`)

There is currently **no HTTP REST API** in this codebase.

---

## 2) Live endpoint: Console WebSocket

- **Protocol**: WebSocket
- **Default URL**: `ws://127.0.0.1:8765`
- **Bind host**: `0.0.0.0`
- **Update frequency**: one JSON snapshot per second
- **Source**: `modules/console_ws.py`

### Quick test

```bash
python3 - <<'PY'
import asyncio, json, websockets

async def main():
    async with websockets.connect('ws://127.0.0.1:8765', ping_interval=None) as ws:
        snap = json.loads(await ws.recv())
        print(json.dumps(snap, indent=2)[:2000])

asyncio.run(main())
PY
```

### Snapshot fields

The snapshot currently includes:

- **Timing / readiness**
  - `ts`, `started_at`, `readiness_hours`, `ready_to_trade`

- **Health state machine**
  - `health_state`
  - `health_last_transition_ts`
  - `health_last_success_ts`
  - `health_last_failure_ts`
  - `health_last_failure_reason`
  - `health_outage_since_ts`
  - `health_outage_flattened`

- **Data quality gate state**
  - `data_quality_last_ok`
  - `data_quality_last_reason`
  - `data_quality_last_check_ts`

- **Drawdown guard state**
  - `drawdown_paused`
  - `drawdown_pause_reason`
  - `drawdown_pause_ts`
  - `drawdown_daily_dd_pct`
  - `drawdown_weekly_dd_pct`
  - `drawdown_ath_dd_pct`
  - `drawdown_daily_peak`
  - `drawdown_weekly_peak`
  - `drawdown_ath_peak`

- **Portfolio / mode**
  - `equity`, `equity_raw`
  - `sim_base_equity`, `sim_realized_pnl`
  - `mode`, `aggr_target`, `safe_target`
  - `parked`

- **Universe / prices / volatility**
  - `basket_size`, `price_count`, `new_alts`
  - `top_prices` (asset, price, atr_1h, atr_6h, spread_pct, volume_1m)
  - `top_histories`
  - `focus_asset`, `focus_price_history`

- **Trades**
  - `open_trades_count`
  - `open_trades` (id, asset, side, entry, remaining_size, stop, take_profit, rr, sleeve, pump_score, vol_spike, execution_path, guard_spread_pct, guard_size_to_vol1m_pct)

- **Decision quality hard gate**
  - `decision_gate_last_ok`
  - `decision_gate_last_reason`
  - `decision_gate_last_ts`
  - `decision_gate_last_metrics`

- **Execution path telemetry**
  - `execution_last_ok`
  - `execution_last_path` (`post_only` / `ioc` / `market` / `dry_run`)
  - `execution_last_reason`
  - `execution_last_ts`

Execution note: before `market` fallback, runtime guard now enforces:
- spread <= `EXEC_MARKET_GUARD_MAX_SPREAD_PCT` (default 0.35%)
- order size <= `EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT` of estimated 1-minute volume (default 0.5%)

If guard fails, market order is rejected, logged, and a limit-IOC retry is attempted when `EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED=true`.

- **Rumors / decisions**
  - `rumors_summary`, `rumor_headlines`
  - `whale_summary`, `whale_flow`
  - `last_decision`, `last_decision_asset`, `last_decision_reason`, `last_decision_ts`
  - `recent_returns`, `equity_momentum_7d_return_pct`

---

## 3) Persistent data locations (SQLite)

## `trades.db`

### Table: `live_trades`
Open-trade state persisted for recovery.

- `id` (trade id)
- `updated_ts`
- `payload` (JSON object)

### Table: `trades`
Closed trade journal (final settlement values).

- `id`
- `ts`
- `asset`
- `side`
- `size`
- `entry`
- `exit`
- `pnl` (net)
- `pnl_gross`
- `fee_cost`
- `funding_cost`
- `reason`

### Table: `trade_events`
Immutable event-sourced timeline.

- `event_id` (UUID)
- `ts`
- `event_type`
- `decision_id`
- `trade_id`
- `asset`
- `payload` (JSON)

Current emitted event types include:

- `decision_received`
- `trade_open_requested`
- `trade_open_rejected_quality`
- `trade_opened`
- `trade_close_requested`
- `partial_close`
- `stop_hit`
- `tp_hit`
- `timer_exit`
- `close_settled`

## `portfolio.db`

### Table: `state`
Rebalance mode/targets snapshots.

- `ts`, `total`, `mode`, `aggr`, `safe`, `reason`

### Table: `equity_history`
Equity time series used by drawdown guard.

- `ts`, `equity`

## `rumors.db`

### Table: `rumors`
Fetched rumor/news records.

- `ts`, `asset`, `rumor`, `sent`, `pump`, `whale`

---

## 4) Event replay API (CLI)

Use `replay_events.py` to reconstruct causality by `decision_id` or `trade_id`.

### Examples

```bash
python3 replay_events.py --limit 200 --show-payload
python3 replay_events.py --decision-id <decision_uuid> --show-payload
python3 replay_events.py --trade-id <trade_uuid> --show-payload
python3 replay_events.py --decision-id <decision_uuid> --json
```

This is the easiest way to answer postmortem questions like:
- Why was a trade opened?
- Was it partially closed before stop/TP?
- What final net/gross/fee/funding settlement occurred?

---

## 5) Interactive runtime controls

## Hot-reload config

- Config file: `config.json`
- Reload behavior: `cfg.reload_hot_config()` is called repeatedly in runtime loops.
- Hot-safe keys include intervals, health thresholds, data-quality gates, drawdown thresholds, and sim realism knobs.

## Park/unpark control

- `PARK_FLAG` path is defined in `config.json`.
- If file exists, decision loop is skipped (`parked=True`).
- Health and drawdown safety actions can auto-create this flag.

## Single-instance lock

- `main.py` lock name: `tradebot-main`
- Lock files live under: `/tmp/tradebot-locks`

---

## 6) Recommended ways to get “all data” while running

If you want full observability with minimal code:

1. **Subscribe to WebSocket** for current state every second.
2. **Tail `trade_events`** for immutable decision/trade lifecycle.
3. **Read `trades`** for settled PnL attribution (gross/fees/funding/net).
4. **Read `equity_history`** for drawdown analytics and period postmortems.
5. **Use `replay_events.py`** for human-readable incident timelines.

---

## 9) Daily score file

- **Path**: `daily_score.csv` (configurable via `DAILY_SCORE_CSV_PATH`)
- **Cadence**: one append per local day rollover (midnight local time), summarizing the prior local day
- **Columns**:
  - `date`
  - `equity_start`
  - `equity_end`
  - `daily_pnl_pct`
  - `trades`
  - `win_rate`
  - `avg_rr`
  - `max_dd_today`
  - `health_score`

Source notes:
- Equity and drawdown metrics come from `portfolio.db` `equity_history`.
- Trade count / win rate come from `trades.db` `trades`.
- `avg_rr` comes from `trade_events` (`trade_opened` payload `rr`).

## 7) Known limits

- No built-in authenticated remote API (only local WS + SQLite).
- Snapshot is state-oriented; deep history is in SQLite, not WS.
- `trade_events` is append-only by design (immutability); corrections should be new events, not updates.

---

## Equity Curve Momentum rules

Decisioning now includes an equity-curve momentum profile from the last 14 daily returns (`recent_returns`), with 7-day aggregate in `equity_momentum_7d_return_pct`.

- If 7-day return < -8%:
  - enforce max risk override 4%
  - require critique conviction >= 85
- If 7-day return > +15%:
  - allow risk override up to 15% only for high-conviction setups (>=90)
- Otherwise:
  - use normal risk rules

---

## 8) SQL cookbook (copy/paste)

Run with:

```bash
sqlite3 trades.db
```

### Top winners / losers (net)

```sql
SELECT id, ts, asset, side, pnl, reason
FROM trades
ORDER BY pnl DESC
LIMIT 20;
```

```sql
SELECT id, ts, asset, side, pnl, reason
FROM trades
ORDER BY pnl ASC
LIMIT 20;
```

### Net vs gross vs cost attribution (all-time)

```sql
SELECT
  ROUND(COALESCE(SUM(pnl), 0), 4) AS net_pnl,
  ROUND(COALESCE(SUM(pnl_gross), 0), 4) AS gross_pnl,
  ROUND(COALESCE(SUM(fee_cost), 0), 4) AS total_fees,
  ROUND(COALESCE(SUM(funding_cost), 0), 4) AS total_funding,
  COUNT(*) AS closed_trades
FROM trades;
```

### Win rate + avg winner/loser + profit factor

```sql
SELECT
  COUNT(*) AS trades,
  ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS win_rate_pct,
  ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 4) AS avg_win,
  ROUND(AVG(CASE WHEN pnl < 0 THEN pnl END), 4) AS avg_loss,
  ROUND(
    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0)
    / NULLIF(ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)), 0),
    4
  ) AS profit_factor
FROM trades;
```

### Monthly performance summary

```sql
SELECT
  substr(ts, 1, 7) AS month,
  COUNT(*) AS trades,
  ROUND(SUM(pnl), 4) AS net_pnl,
  ROUND(SUM(pnl_gross), 4) AS gross_pnl,
  ROUND(SUM(fee_cost), 4) AS fees,
  ROUND(SUM(funding_cost), 4) AS funding,
  ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 2) AS win_rate_pct
FROM trades
GROUP BY substr(ts, 1, 7)
ORDER BY month DESC;
```

### Replay one decision timeline (event journal)

Replace `DECISION_ID_HERE`:

```sql
SELECT
  ts,
  event_type,
  decision_id,
  trade_id,
  asset,
  payload
FROM trade_events
WHERE decision_id = 'DECISION_ID_HERE'
ORDER BY ts ASC, event_id ASC;
```

### Replay one trade timeline (event journal)

Replace `TRADE_ID_HERE`:

```sql
SELECT
  ts,
  event_type,
  decision_id,
  trade_id,
  asset,
  payload
FROM trade_events
WHERE trade_id = 'TRADE_ID_HERE'
ORDER BY ts ASC, event_id ASC;
```

### Join settlement to open event (entry context + final outcome)

```sql
SELECT
  s.id AS trade_id,
  s.ts AS settled_ts,
  s.asset,
  s.pnl AS net_pnl,
  s.pnl_gross,
  s.fee_cost,
  s.funding_cost,
  json_extract(o.payload, '$.entry') AS opened_entry,
  json_extract(o.payload, '$.size') AS opened_size,
  json_extract(o.payload, '$.entry_slippage_pct') AS entry_slippage_pct,
  json_extract(o.payload, '$.open_fee') AS open_fee
FROM trades s
LEFT JOIN trade_events o
  ON o.trade_id = s.id
 AND o.event_type = 'trade_opened'
ORDER BY s.ts DESC
LIMIT 100;
```

### Daily equity and drawdown sketch (from equity_history)

Run in `portfolio.db`:

```bash
sqlite3 portfolio.db
```

```sql
WITH day_points AS (
  SELECT
    substr(ts, 1, 10) AS day,
    MIN(id) AS first_id,
    MAX(id) AS last_id
  FROM equity_history
  GROUP BY substr(ts, 1, 10)
),
daily AS (
  SELECT
    d.day,
    e_open.equity AS open_equity,
    e_close.equity AS close_equity,
    MAX(e_all.equity) AS peak_equity,
    MIN(e_all.equity) AS trough_equity
  FROM day_points d
  JOIN equity_history e_open ON e_open.id = d.first_id
  JOIN equity_history e_close ON e_close.id = d.last_id
  JOIN equity_history e_all ON substr(e_all.ts, 1, 10) = d.day
  GROUP BY d.day, e_open.equity, e_close.equity
)
SELECT
  day,
  ROUND(open_equity, 4) AS open_equity,
  ROUND(close_equity, 4) AS close_equity,
  ROUND(close_equity - open_equity, 4) AS day_pnl,
  ROUND(100.0 * (close_equity - open_equity) / NULLIF(open_equity, 0), 2) AS day_return_pct,
  ROUND(100.0 * (peak_equity - trough_equity) / NULLIF(peak_equity, 0), 2) AS intraday_drawdown_pct
FROM daily
ORDER BY day DESC;
```
