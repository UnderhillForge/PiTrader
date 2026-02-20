Update/refine my existing Coinbase spot trading scanner bot to handle ALL available spot pairs on Coinbase Advanced Trade. Use CCXT 'coinbase' exchange class.
Key changes & requirements:

Dynamic pair discovery: At startup and every 4 hours (or on restart), call exchange.load_markets() and filter for active spot markets. Only include pairs where:
quote currency is 'USD' or 'USDC' (ignore rare others like EUR if present).
active: True
spot: True (or type == 'spot')
Exclude any stablecoin-only or tiny-volume junk if possible (e.g., min 24h volume filter optional later).
Log the total number of monitored pairs (expect 500+).

SMT logic adaptation for many pairs:
Use major anchors for bias & divergence detection: BTC-USD as primary anchor, ETH-USD as secondary.
For SMT divergence: Compare the anchor (BTC or ETH) against alt pairs on the same timeframe.
Example: For an alt like SOL-USD, check SMT vs BTC-USD or ETH-USD (e.g., alt makes new HH while BTC fails = potential bullish divergence if HTF bias aligns).
To scale: Group alts by correlation family if possible (e.g., L1s: SOL, AVAX, ADA; memes/low-caps separate), but start simple: scan all alts against BTC first, then ETH if no signal.
Only trigger setups on alts when SMT divergence appears AND aligns with BTC/ETH HTF bias (don't trade isolated alts without market structure confirmation from majors).

Limit active scanning: To avoid rate-limit hell with 500+ pairs, prioritize:
Top 50–100 by 24h volume (fetch via exchange.fetch_tickers() or cache).
Or focus on pairs correlated to BTC/ETH (e.g., any pair where base is in a predefined high-corr list: ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOGE', 'SHIB', 'LINK', etc.] — make this list expandable).
Full scan every hour; frequent checks only on high-priority pairs.


Keep core strategy:
HTF bias on BTC-USD (and optionally ETH-USD) using 4h/1d: clear bullish (HH/HL) or bearish (LH/LL) structure over last 5–10 swings.
LTF (15m/5m): Look for SMT divergence between anchor and alt pair.
Retrace + tap into OB/FVG/mitigation block on the alt's LTF chart.
Entry on tap/rejection.
Targets: Next resting liquidity (equal highs/lows, prev daily/4h high/low, swing liq) at ≥1:2 RR, prefer 1:3.
Strict confluence only → aim for high win-rate setups.

Scanning frequency (24/7 crypto market, US/Eastern time via pytz):
Baseline: Every 15 minutes.
High-frequency during kill zones:
London open/overlap: 02:00 – 05:00 ET → every 5 min
NY open/AM session: 07:00 – 12:00 ET → every 5 min

Lower frequency outside: every 30 min if you want to save API calls.
Log current ET time + which zone on each cycle.

Other specs (same as before):
Libraries: ccxt, pandas, numpy, pandas_ta (or ta-lib if available), pytz, schedule/APScheduler, python-dotenv.
.env for COINBASE_API_KEY, COINBASE_API_SECRET.
Console + file logging with clear setup alerts, e.g.:text[2026-02-20 09:42 ET] LONG SETUP SOL-USD (SMT vs BTC)
HTF Bias (BTC): Bullish | SMT: Bullish div | Entry: 142.5 | SL: 138 | TP1: 151 (1:2) | TP2: 155 (1:3)
Liquidity: Prev 4h high
Error handling, rate-limit respect (CCXT handles most), graceful reconnect.
Optional: Telegram/email alerts (commented).
Modular, commented code; main.py with run instructions.


Generate the full updated script in one file. Make it efficient for many pairs (cache OHLCV/markets, batch fetches where possible).