# TradeBot Algorithm (Current Implementation)

This document describes the **actual trading algorithm** implemented in the current codebase (`main.py` + `modules/*`).

## 1) Runtime Architecture

On startup (`main.py`):
1. Initialize databases and load persisted portfolio mode/state.
2. Recover persisted open trades from `trades.db`.
3. Start concurrent loops:
   - `rebalance_checker()`
   - `start_websockets()` (market prices + basket + ATR cache)
   - `fetch_loop()` (rumor/news scoring)
   - `serve_console_ws()` (dashboard/papertrader snapshot stream)
   - `grok_loop()` (decision engine) unless `DATA_ONLY_MODE`
   - `monitor_trades_loop()` (position lifecycle/risk exits) unless `DATA_ONLY_MODE`

If `SIMULATION_MODE=true`, order placement is dry-run and realized PnL is applied to simulated equity.

---

## 2) Market Universe and Data Collection

### 2.1 Product universe selection
`update_basket()` ranks products by 24h volume and keeps top 40, filtered by config:
- `PRODUCT_UNIVERSE = perp | spot | all`
- Spot quote filter via `SPOT_QUOTES` (default USD, USDC)
- Excludes offline/delisted/disabled assets

State updates:
- `state["basket"]` (top 40)
- `state["basket_ver"]`
- `state["new_alts"]` (newly added assets)

### 2.2 Price stream/cache
`start_websockets()` polls every `PRICE_POLL_SEC`:
- Fetches current product price for each basket asset
- Updates `state["price"][asset]`
- Appends into `state["price_history"][asset]` (max 240 points)

### 2.3 Volatility (ATR) cache
On `ATR_REFRESH_SEC` cadence:
- Fetch ONE_HOUR candles and SIX_HOUR candles
- Compute ATR(14) for each timeframe
- Normalize missing side:
  - if only 1h exists: `atr_6h = atr_1h * sqrt(6)`
  - if only 6h exists: `atr_1h = atr_6h / sqrt(6)`
- Store in `state["vol_cache"][asset] = {atr_1h, atr_6h}`

---

## 3) Rumor + Volatility Signal Layer

`fetch_loop()` (via `modules/rumors.py`) collects from configured source:
- X/Twitter (`twscrape`) or
- RSS/news feeds

For each rumor text:
- Match basket assets via cashtags/hashtags/symbol presence
- Score sentiment with keyword rules (bullish/bearish/whale)
- Mark pump-like mentions (`pump`, `breakout`)
- De-duplicate and persist into `rumors.db`

Also computes volatility spikes:
- `vol_spike = atr_1h / atr_6h`
- Flag if `vol_spike >= 1.35`
- Keep top spike list in `state["spike_list"]`

Produces `state["rumors_summary"]` and `state["rumor_items"]` used by decision prompt.

---

## 4) Equity, Mode, and Sleeve Allocation

### 4.1 Equity source
`get_equity()`:
- Reads USD/USDC available balances from Coinbase (with portfolio-breakdown fallback)
- Optionally caps by `TRADE_BALANCE`
- In simulation mode:
  - `equity = sim_base_equity + sim_realized_pnl`

### 4.2 Mode logic
- If `equity < SPLIT_THRESHOLD`: force **nuclear** mode
  - `aggr_target = equity`, `safe_target = 0`
- Else rebalance weekly (`REBALANCE_DAY`, `REBALANCE_HOUR`):
  - target aggressive sleeve = `max(equity * AGGR_PCT, MIN_AGGR)`
  - safe sleeve = remainder
  - rebalance only when drift is meaningful

---

## 5) Decision Engine (Grok)

Each decision cycle (`grok_loop()`):
1. Reload hot config, check parked flag.
2. Build prompt from:
   - equity/mode/sleeves/rebalance state
   - current positions
   - price cache
   - spike list
   - rumor summary
3. Ask Grok model (`GROK_MODEL`) for strict JSON decision.
4. Parse and normalize fields:
   - `decision`: `hold | open_long | open_short | close`
   - `asset`, `sleeve`, `pump_score`
   - optional `contracts`, `leverage`, `stop`, `take_profit`, trailing config
5. Route:
   - `hold` -> `rest_in_usdc()`
   - `open_long/open_short/close` -> `execute(decision)`

If parse/API fails, default behavior is defensive hold (`rest_in_usdc()`).

---

## 6) Trade Entry Rules (`execute`)

### 6.1 Hard gates
- Asset must be valid (`-PERP-INTX`, `-USD`, `-USDC`)
- `open_short` only allowed on perpetual products
- Readiness gate: no opening trades before `READINESS_HOURS` since startup
- Safe sleeve restrictions:
  - only BTC/ETH/SOL safe assets
  - no pump trades in safe sleeve (`pump_score >= 60` rejected)

### 6.2 Price, leverage, ATR-derived protection
- Entry price from decision payload or live `state["price"]`
- Leverage clamped to `[1, MAX_LEV]`
- If stop/TP missing, derive from ATR:
  - Pump trade (`pump_score >= 60`):
    - stop multiple = 1.8 ATR
    - take-profit multiple = 2.7 ATR
  - Non-pump trade:
    - stop multiple = 2.5 ATR
    - take-profit multiple = 3.8 ATR

### 6.3 R:R minimum
- `min_rr = 2.0` for safe sleeve
- `min_rr = 1.5` for aggressive sleeve
- Trade rejected if computed R:R is below threshold

### 6.4 Position sizing
If explicit `contracts` not provided:
- risk notional = sleeve_budget × risk_pct
- sleeve budget:
  - hybrid mode: `safe_target` or `aggr_target`
  - otherwise: full equity
- risk_pct:
  - safe sleeve: `RISK_SAFE`
  - aggressive sleeve: `RISK_NUCLEAR`
- size = `notional / price`

### 6.5 Entry execution and persistence
- Live mode: place market order via Coinbase
- Simulation/dry-run: log simulated open
- Persist open trade to `state["trades"]` + `live_trades` table
- For pump trades with hold timer, schedule timed auto-close

---

## 7) Trade Lifecycle and Exits (`monitor_trades_loop`)

Every 5 seconds, each open trade is evaluated:

1. **Trailing stop update** when activation move threshold reached
2. **Partial at 1.5R**: close 50%
3. **Partial at 3.0R**: close 30%
4. **Stop loss**: full close
5. **Take profit**: full close
6. **Pump timer expiry**: full close

On any close:
- Realized PnL computed from entry vs exit and side
- In simulation mode, realized PnL updates `sim_realized_pnl` and `equity`
- Trade written to `trades` journal table
- Trade removed from `live_trades`

Recovery logic restores open trades and timer intent after restart.

---

## 8) Resting Behavior

`rest_in_usdc()` behavior:
- Attempts to close open positions
- Attempts to convert non-USD/USDC balances to USDC
- Used whenever decision is `hold` or fallback path is triggered

---

## 9) Persistence and Evaluation Data

- `trades.db`:
  - `live_trades` (open positions)
  - `trades` (closed trade journal)
- `portfolio.db`:
  - mode/sleeve/rebalance state history
- `rumors.db`:
  - rumor items and metadata
- `paper.log` (from `papertrader.py`):
  - equity deltas, decision timeline, trade open/close events, session summaries

---

## 10) Practical Summary

The strategy is a **prompt-driven execution engine** with strict guardrails:
- market/rumor context -> Grok decision
- deterministic eligibility + risk validation
- ATR-based stop/TP defaults
- sleeve-based sizing and minimum R:R checks
- automated lifecycle management (partials, trailing, stop/TP, timer close)
- simulation equity accounting for performance review
