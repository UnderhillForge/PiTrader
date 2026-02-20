import os
import logging
import json
import time
from datetime import datetime
from threading import Lock
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

try:
    from coinbase_advanced_py import AdvancedTradeClient
except ImportError:
    try:
        from coinbase.rest import RESTClient as AdvancedTradeClient
    except ImportError:
        AdvancedTradeClient = None

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

load_dotenv()


def _load_json_config():
    path = os.getenv("CONFIG_JSON_PATH", "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


_JSON_CONFIG = _load_json_config()
_HOT_RELOAD_LOCK = Lock()


def _normalize_tz_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.strip().lower().replace("_", "-")
    if key in {"us-east", "useast", "us/east", "eastern", "est", "edt", "america-new-york"}:
        return "America/New_York"
    if key in {"utc", "gmt", "z"}:
        return "UTC"
    return text


_LOG_TIMEZONE = _normalize_tz_name(os.getenv("LOG_TIMEZONE") or _JSON_CONFIG.get("LOG_TIMEZONE") or "")
if _LOG_TIMEZONE:
    os.environ["TZ"] = _LOG_TIMEZONE
    try:
        time.tzset()
    except (AttributeError, OSError):
        pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _cfg(name, default=None):
    env_value = os.getenv(name)
    if env_value is not None and env_value != "":
        return env_value
    return _JSON_CONFIG.get(name, default)


def _cfg_bool(name, default=False):
    value = _cfg(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cfg_int(name, default):
    value = _cfg(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _cfg_float(name, default):
    value = _cfg(name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cfg_optional_money(name):
    value = _cfg(name, "")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None

    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("$", "").replace(",", "").strip()
    try:
        parsed = float(normalized)
        return parsed if parsed > 0 else None
    except ValueError:
        return None

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
API_KEY = _cfg("COINBASE_API_KEY")
API_SECRET = _cfg("COINBASE_API_SECRET")
GROK_KEY = _cfg("GROK_API_KEY")
GROK_MODEL = _cfg("GROK_MODEL", "grok-beta")
GROK_SELF_CRITIQUE_ENABLED = _cfg_bool("GROK_SELF_CRITIQUE_ENABLED", True)
GROK_MIN_CRITIQUE_CONVICTION = max(0, min(100, _cfg_int("GROK_MIN_CRITIQUE_CONVICTION", 78)))
GROK_CONTEXT_RECENT_TRADES = max(3, _cfg_int("GROK_CONTEXT_RECENT_TRADES", 12))
GROK_FREE_SIGNALS_TIMEOUT_SEC = max(2, _cfg_int("GROK_FREE_SIGNALS_TIMEOUT_SEC", 6))
GROK_FUNDING_FILTER_ENABLED = _cfg_bool("GROK_FUNDING_FILTER_ENABLED", True)
GROK_FUNDING_BLOCK_LONG_PCT = _cfg_float("GROK_FUNDING_BLOCK_LONG_PCT", 0.08)
GROK_FUNDING_BLOCK_SHORT_PCT = _cfg_float("GROK_FUNDING_BLOCK_SHORT_PCT", -0.08)

if isinstance(API_SECRET, str):
    secret_candidate = API_SECRET.strip()
    if os.path.isfile(secret_candidate):
        try:
            with open(secret_candidate, "r", encoding="utf-8") as pem_file:
                API_SECRET = pem_file.read().replace("\\n", "\n").strip()
        except OSError:
            API_SECRET = secret_candidate
    else:
        API_SECRET = secret_candidate.replace("\\n", "\n").strip()

RISK_NUCLEAR = _cfg_float("RISK_NUCLEAR", 0.12)
RISK_SAFE = _cfg_float("RISK_SAFE", 0.015)
SPLIT_THRESHOLD = _cfg_float("SPLIT_THRESHOLD", 10000)
AGGR_PCT = _cfg_float("AGGR_PCT", 0.10)
MIN_AGGR = _cfg_float("MIN_AGGR", 1000)
REBALANCE_DAY = _cfg_int("REBALANCE_DAY", 6)  # Sunday
REBALANCE_HOUR = _cfg_int("REBALANCE_HOUR", 0)

MAX_LEV = _cfg_int("MAX_LEV", 10)
PORT = _cfg_int("PORT", 8765)
PARK_FLAG = _cfg("PARK_FLAG", "parked.flag")
SYSTEM_PROMPT_PATH = _cfg("SYSTEM_PROMPT_PATH", "system.prompt")
DATA_ONLY_MODE = _cfg_bool("DATA_ONLY_MODE", False)
DRY_RUN_ORDERS = _cfg_bool("DRY_RUN_ORDERS", True)
SIMULATION_MODE = _cfg_bool("SIMULATION_MODE", False)
if SIMULATION_MODE:
    DRY_RUN_ORDERS = True
TRADE_BALANCE = _cfg_optional_money("TRADE_BALANCE")
RUMOR_POLL_SEC = _cfg_int("RUMOR_POLL_SEC", 900)
RUMOR_MAX_POSTS = _cfg_int("RUMOR_MAX_POSTS", 30)
RUMOR_LOOKBACK_HOURS = _cfg_int("RUMOR_LOOKBACK_HOURS", 12)
DECISION_INTERVAL_SEC = max(30, _cfg_int("DECISION_INTERVAL_SEC", 300))
PARKED_DECISION_INTERVAL_SEC = max(10, _cfg_int("PARKED_DECISION_INTERVAL_SEC", 60))
PRICE_POLL_SEC = max(1, _cfg_int("PRICE_POLL_SEC", 5))
ATR_REFRESH_SEC = max(10, _cfg_int("ATR_REFRESH_SEC", 60))
BASKET_REFRESH_SEC = max(30, _cfg_int("BASKET_REFRESH_SEC", 600))
RUMOR_SOURCE = str(_cfg("RUMOR_SOURCE", "auto")).strip().lower()
PRODUCT_UNIVERSE = str(_cfg("PRODUCT_UNIVERSE", "all")).strip().lower()
SPOT_QUOTES = [q.strip().upper() for q in str(_cfg("SPOT_QUOTES", "USD,USDC")).split(",") if q.strip()]

# Spot universe discovery/scanning (used when PRODUCT_UNIVERSE includes spot)
SPOT_DISCOVERY_MODE = str(_cfg("SPOT_DISCOVERY_MODE", "native")).strip().lower()  # native|ccxt
SPOT_DISCOVERY_REFRESH_SEC = max(600, _cfg_int("SPOT_DISCOVERY_REFRESH_SEC", 14400))
SPOT_PRIORITY_SIZE = max(20, _cfg_int("SPOT_PRIORITY_SIZE", 200))
SPOT_ACTIVE_SCAN_SIZE = max(10, _cfg_int("SPOT_ACTIVE_SCAN_SIZE", 80))
READINESS_HOURS = _cfg_float("READINESS_HOURS", 12.0)
HEALTH_DEGRADED_FAILURES = max(1, _cfg_int("HEALTH_DEGRADED_FAILURES", 2))
HEALTH_OUTAGE_FAILURES = max(HEALTH_DEGRADED_FAILURES + 1, _cfg_int("HEALTH_OUTAGE_FAILURES", 5))
HEALTH_RECOVER_SUCCESS_STREAK = max(1, _cfg_int("HEALTH_RECOVER_SUCCESS_STREAK", 2))
HEALTH_OUTAGE_FLATTEN_SEC = max(30, _cfg_int("HEALTH_OUTAGE_FLATTEN_SEC", 300))
HEALTH_BLOCK_RECOVERING = _cfg_bool("HEALTH_BLOCK_RECOVERING", True)
DATAQ_MAX_PRICE_AGE_SEC = max(5, _cfg_int("DATAQ_MAX_PRICE_AGE_SEC", 20))
DATAQ_MIN_BASKET_SIZE = max(1, _cfg_int("DATAQ_MIN_BASKET_SIZE", 10))
DATAQ_MIN_FRESH_PRICE_RATIO = max(0.0, min(1.0, _cfg_float("DATAQ_MIN_FRESH_PRICE_RATIO", 0.60)))
DATAQ_MIN_ATR_COVERAGE_RATIO = max(0.0, min(1.0, _cfg_float("DATAQ_MIN_ATR_COVERAGE_RATIO", 0.50)))
DD_DAILY_LIMIT_PCT = max(0.1, _cfg_float("DD_DAILY_LIMIT_PCT", 5.0))
DD_WEEKLY_LIMIT_PCT = max(0.1, _cfg_float("DD_WEEKLY_LIMIT_PCT", 17.5))
DD_ATH_TRAILING_LIMIT_PCT = max(0.1, _cfg_float("DD_ATH_TRAILING_LIMIT_PCT", 30.0))
DD_CHECK_SEC = max(10, _cfg_int("DD_CHECK_SEC", 60))
DD_AUTO_FLATTEN = _cfg_bool("DD_AUTO_FLATTEN", True)
DD_AUTO_PARK = _cfg_bool("DD_AUTO_PARK", True)
SIM_TAKER_FEE_RATE = max(0.0, _cfg_float("SIM_TAKER_FEE_RATE", 0.0006))
SIM_FUNDING_RATE_PER_8H = max(0.0, _cfg_float("SIM_FUNDING_RATE_PER_8H", 0.0003))
SIM_SLIPPAGE_MIN_PCT = max(0.0, _cfg_float("SIM_SLIPPAGE_MIN_PCT", 0.10))
SIM_SLIPPAGE_MAX_PCT = max(SIM_SLIPPAGE_MIN_PCT, _cfg_float("SIM_SLIPPAGE_MAX_PCT", 0.50))
SIM_SLIPPAGE_ATR_MULT = max(0.0, _cfg_float("SIM_SLIPPAGE_ATR_MULT", 0.50))
REGIME_CLASSIFIER_ASSET = str(_cfg("REGIME_CLASSIFIER_ASSET", "BTC-PERP-INTX")).strip() or "BTC-PERP-INTX"
REGIME_LOOKBACK_POINTS = max(20, _cfg_int("REGIME_LOOKBACK_POINTS", 60))
REGIME_TREND_RET_PCT = max(0.05, _cfg_float("REGIME_TREND_RET_PCT", 0.8))
REGIME_HIGH_VOL_ATR_PCT = max(0.05, _cfg_float("REGIME_HIGH_VOL_ATR_PCT", 2.5))
REGIME_TREND_NOISE_RATIO = max(0.5, _cfg_float("REGIME_TREND_NOISE_RATIO", 1.5))
REGIME_RISK_MULT_TREND = max(0.1, _cfg_float("REGIME_RISK_MULT_TREND", 1.0))
REGIME_RISK_MULT_CHOP = max(0.1, _cfg_float("REGIME_RISK_MULT_CHOP", 0.7))
REGIME_RISK_MULT_HIGH_VOL = max(0.1, _cfg_float("REGIME_RISK_MULT_HIGH_VOL", 0.5))
REGIME_LEV_CAP_TREND = max(1, _cfg_int("REGIME_LEV_CAP_TREND", 8))
REGIME_LEV_CAP_CHOP = max(1, _cfg_int("REGIME_LEV_CAP_CHOP", 5))
REGIME_LEV_CAP_HIGH_VOL = max(1, _cfg_int("REGIME_LEV_CAP_HIGH_VOL", 3))
REGIME_MIN_RR_ADD_TREND = max(0.0, _cfg_float("REGIME_MIN_RR_ADD_TREND", 0.0))
REGIME_MIN_RR_ADD_CHOP = max(0.0, _cfg_float("REGIME_MIN_RR_ADD_CHOP", 0.2))
REGIME_MIN_RR_ADD_HIGH_VOL = max(0.0, _cfg_float("REGIME_MIN_RR_ADD_HIGH_VOL", 0.3))
DECISION_MIN_PUMP_SCORE = max(0, _cfg_int("DECISION_MIN_PUMP_SCORE", 15))
DECISION_MIN_VOL_SPIKE = max(0.0, _cfg_float("DECISION_MIN_VOL_SPIKE", 1.0))
DECISION_MIN_RR_AGGRESSIVE = max(0.1, _cfg_float("DECISION_MIN_RR_AGGRESSIVE", 1.5))
DECISION_MIN_RR_SAFE = max(0.1, _cfg_float("DECISION_MIN_RR_SAFE", 2.0))
EXEC_POST_ONLY_ENABLED = _cfg_bool("EXEC_POST_ONLY_ENABLED", True)
EXEC_POST_ONLY_OFFSET_PCT = max(0.0, _cfg_float("EXEC_POST_ONLY_OFFSET_PCT", 0.02))
EXEC_IOC_FALLBACK_ENABLED = _cfg_bool("EXEC_IOC_FALLBACK_ENABLED", True)
EXEC_IOC_SLIPPAGE_PCT = max(0.0, _cfg_float("EXEC_IOC_SLIPPAGE_PCT", 0.05))
EXEC_MARKET_FALLBACK_ENABLED = _cfg_bool("EXEC_MARKET_FALLBACK_ENABLED", True)
EXEC_MARKET_GUARD_MAX_SPREAD_PCT = max(0.01, _cfg_float("EXEC_MARKET_GUARD_MAX_SPREAD_PCT", 0.35))
EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT = max(0.01, _cfg_float("EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT", 0.5))
EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED = _cfg_bool("EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED", True)
EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT = max(0.0, _cfg_float("EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT", 0.08))
PYRAMID_ENABLED = _cfg_bool("PYRAMID_ENABLED", True)
PYRAMID_RR_TRIGGER = max(0.5, _cfg_float("PYRAMID_RR_TRIGGER", 1.5))
PYRAMID_ADD_FRACTION = max(0.01, min(1.0, _cfg_float("PYRAMID_ADD_FRACTION", 0.30)))
PYRAMID_MAX_ADDS = max(0, _cfg_int("PYRAMID_MAX_ADDS", 2))
PYRAMID_MIN_CONVICTION = max(0, min(100, _cfg_int("PYRAMID_MIN_CONVICTION", 80)))
PYRAMID_MAX_EXPOSURE_PCT = max(0.01, min(1.0, _cfg_float("PYRAMID_MAX_EXPOSURE_PCT", 0.18)))
DAILY_SCORE_ENABLED = _cfg_bool("DAILY_SCORE_ENABLED", True)
DAILY_SCORE_CSV_PATH = str(_cfg("DAILY_SCORE_CSV_PATH", "daily_score.csv")).strip() or "daily_score.csv"
DAILY_SCORE_CHECK_SEC = max(10, _cfg_int("DAILY_SCORE_CHECK_SEC", 30))

HOT_RELOAD_SAFE_KEYS = [
    "PARK_FLAG",
    "READINESS_HOURS",
    "RUMOR_SOURCE",
    "RUMOR_POLL_SEC",
    "RUMOR_MAX_POSTS",
    "RUMOR_LOOKBACK_HOURS",
    "DECISION_INTERVAL_SEC",
    "PARKED_DECISION_INTERVAL_SEC",
    "PRICE_POLL_SEC",
    "ATR_REFRESH_SEC",
    "BASKET_REFRESH_SEC",
    "PRODUCT_UNIVERSE",
    "SPOT_QUOTES",
    "SPOT_DISCOVERY_MODE",
    "SPOT_DISCOVERY_REFRESH_SEC",
    "SPOT_PRIORITY_SIZE",
    "SPOT_ACTIVE_SCAN_SIZE",
    "HEALTH_DEGRADED_FAILURES",
    "HEALTH_OUTAGE_FAILURES",
    "HEALTH_RECOVER_SUCCESS_STREAK",
    "HEALTH_OUTAGE_FLATTEN_SEC",
    "HEALTH_BLOCK_RECOVERING",
    "DATAQ_MAX_PRICE_AGE_SEC",
    "DATAQ_MIN_BASKET_SIZE",
    "DATAQ_MIN_FRESH_PRICE_RATIO",
    "DATAQ_MIN_ATR_COVERAGE_RATIO",
    "DD_DAILY_LIMIT_PCT",
    "DD_WEEKLY_LIMIT_PCT",
    "DD_ATH_TRAILING_LIMIT_PCT",
    "DD_CHECK_SEC",
    "DD_AUTO_FLATTEN",
    "DD_AUTO_PARK",
    "SIM_TAKER_FEE_RATE",
    "SIM_FUNDING_RATE_PER_8H",
    "SIM_SLIPPAGE_MIN_PCT",
    "SIM_SLIPPAGE_MAX_PCT",
    "SIM_SLIPPAGE_ATR_MULT",
    "REGIME_CLASSIFIER_ASSET",
    "REGIME_LOOKBACK_POINTS",
    "REGIME_TREND_RET_PCT",
    "REGIME_HIGH_VOL_ATR_PCT",
    "REGIME_TREND_NOISE_RATIO",
    "REGIME_RISK_MULT_TREND",
    "REGIME_RISK_MULT_CHOP",
    "REGIME_RISK_MULT_HIGH_VOL",
    "REGIME_LEV_CAP_TREND",
    "REGIME_LEV_CAP_CHOP",
    "REGIME_LEV_CAP_HIGH_VOL",
    "REGIME_MIN_RR_ADD_TREND",
    "REGIME_MIN_RR_ADD_CHOP",
    "REGIME_MIN_RR_ADD_HIGH_VOL",
    "DECISION_MIN_PUMP_SCORE",
    "DECISION_MIN_VOL_SPIKE",
    "DECISION_MIN_RR_AGGRESSIVE",
    "DECISION_MIN_RR_SAFE",
    "EXEC_POST_ONLY_ENABLED",
    "EXEC_POST_ONLY_OFFSET_PCT",
    "EXEC_IOC_FALLBACK_ENABLED",
    "EXEC_IOC_SLIPPAGE_PCT",
    "EXEC_MARKET_FALLBACK_ENABLED",
    "EXEC_MARKET_GUARD_MAX_SPREAD_PCT",
    "EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT",
    "EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED",
    "EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT",
    "PYRAMID_ENABLED",
    "PYRAMID_RR_TRIGGER",
    "PYRAMID_ADD_FRACTION",
    "PYRAMID_MAX_ADDS",
    "PYRAMID_MIN_CONVICTION",
    "PYRAMID_MAX_EXPOSURE_PCT",
    "DAILY_SCORE_ENABLED",
    "DAILY_SCORE_CSV_PATH",
    "DAILY_SCORE_CHECK_SEC",
    "GROK_SELF_CRITIQUE_ENABLED",
    "GROK_MIN_CRITIQUE_CONVICTION",
    "GROK_CONTEXT_RECENT_TRADES",
    "GROK_FREE_SIGNALS_TIMEOUT_SEC",
    "GROK_FUNDING_FILTER_ENABLED",
    "GROK_FUNDING_BLOCK_LONG_PCT",
    "GROK_FUNDING_BLOCK_SHORT_PCT",
]


def _parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _parse_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _env_or_cfg(name, cfg_data, default):
    env_value = os.getenv(name)
    if env_value is not None and env_value != "":
        return env_value
    return cfg_data.get(name, default)


def reload_hot_config():
    cfg_data = _load_json_config()

    global PARK_FLAG
    global READINESS_HOURS
    global RUMOR_SOURCE
    global RUMOR_POLL_SEC
    global RUMOR_MAX_POSTS
    global RUMOR_LOOKBACK_HOURS
    global DECISION_INTERVAL_SEC
    global PARKED_DECISION_INTERVAL_SEC
    global PRICE_POLL_SEC
    global ATR_REFRESH_SEC
    global BASKET_REFRESH_SEC
    global PRODUCT_UNIVERSE
    global SPOT_QUOTES
    global SPOT_DISCOVERY_MODE
    global SPOT_DISCOVERY_REFRESH_SEC
    global SPOT_PRIORITY_SIZE
    global SPOT_ACTIVE_SCAN_SIZE
    global HEALTH_DEGRADED_FAILURES
    global HEALTH_OUTAGE_FAILURES
    global HEALTH_RECOVER_SUCCESS_STREAK
    global HEALTH_OUTAGE_FLATTEN_SEC
    global HEALTH_BLOCK_RECOVERING
    global DATAQ_MAX_PRICE_AGE_SEC
    global DATAQ_MIN_BASKET_SIZE
    global DATAQ_MIN_FRESH_PRICE_RATIO
    global DATAQ_MIN_ATR_COVERAGE_RATIO
    global DD_DAILY_LIMIT_PCT
    global DD_WEEKLY_LIMIT_PCT
    global DD_ATH_TRAILING_LIMIT_PCT
    global DD_CHECK_SEC
    global DD_AUTO_FLATTEN
    global DD_AUTO_PARK
    global SIM_TAKER_FEE_RATE
    global SIM_FUNDING_RATE_PER_8H
    global SIM_SLIPPAGE_MIN_PCT
    global SIM_SLIPPAGE_MAX_PCT
    global SIM_SLIPPAGE_ATR_MULT
    global REGIME_CLASSIFIER_ASSET
    global REGIME_LOOKBACK_POINTS
    global REGIME_TREND_RET_PCT
    global REGIME_HIGH_VOL_ATR_PCT
    global REGIME_TREND_NOISE_RATIO
    global REGIME_RISK_MULT_TREND
    global REGIME_RISK_MULT_CHOP
    global REGIME_RISK_MULT_HIGH_VOL
    global REGIME_LEV_CAP_TREND
    global REGIME_LEV_CAP_CHOP
    global REGIME_LEV_CAP_HIGH_VOL
    global REGIME_MIN_RR_ADD_TREND
    global REGIME_MIN_RR_ADD_CHOP
    global REGIME_MIN_RR_ADD_HIGH_VOL
    global DECISION_MIN_PUMP_SCORE
    global DECISION_MIN_VOL_SPIKE
    global DECISION_MIN_RR_AGGRESSIVE
    global DECISION_MIN_RR_SAFE
    global EXEC_POST_ONLY_ENABLED
    global EXEC_POST_ONLY_OFFSET_PCT
    global EXEC_IOC_FALLBACK_ENABLED
    global EXEC_IOC_SLIPPAGE_PCT
    global EXEC_MARKET_FALLBACK_ENABLED
    global EXEC_MARKET_GUARD_MAX_SPREAD_PCT
    global EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT
    global EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED
    global EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT
    global PYRAMID_ENABLED
    global PYRAMID_RR_TRIGGER
    global PYRAMID_ADD_FRACTION
    global PYRAMID_MAX_ADDS
    global PYRAMID_MIN_CONVICTION
    global PYRAMID_MAX_EXPOSURE_PCT
    global DAILY_SCORE_ENABLED
    global DAILY_SCORE_CSV_PATH
    global DAILY_SCORE_CHECK_SEC
    global GROK_SELF_CRITIQUE_ENABLED
    global GROK_MIN_CRITIQUE_CONVICTION
    global GROK_CONTEXT_RECENT_TRADES
    global GROK_FREE_SIGNALS_TIMEOUT_SEC
    global GROK_FUNDING_FILTER_ENABLED
    global GROK_FUNDING_BLOCK_LONG_PCT
    global GROK_FUNDING_BLOCK_SHORT_PCT

    with _HOT_RELOAD_LOCK:
        PARK_FLAG = str(_env_or_cfg("PARK_FLAG", cfg_data, PARK_FLAG)).strip() or "parked.flag"
        READINESS_HOURS = _parse_float(_env_or_cfg("READINESS_HOURS", cfg_data, READINESS_HOURS), READINESS_HOURS)

        RUMOR_SOURCE = str(_env_or_cfg("RUMOR_SOURCE", cfg_data, RUMOR_SOURCE)).strip().lower()
        RUMOR_POLL_SEC = max(60, _parse_int(_env_or_cfg("RUMOR_POLL_SEC", cfg_data, RUMOR_POLL_SEC), RUMOR_POLL_SEC))
        RUMOR_MAX_POSTS = max(1, _parse_int(_env_or_cfg("RUMOR_MAX_POSTS", cfg_data, RUMOR_MAX_POSTS), RUMOR_MAX_POSTS))
        RUMOR_LOOKBACK_HOURS = max(1, _parse_int(_env_or_cfg("RUMOR_LOOKBACK_HOURS", cfg_data, RUMOR_LOOKBACK_HOURS), RUMOR_LOOKBACK_HOURS))

        DECISION_INTERVAL_SEC = max(30, _parse_int(_env_or_cfg("DECISION_INTERVAL_SEC", cfg_data, DECISION_INTERVAL_SEC), DECISION_INTERVAL_SEC))
        PARKED_DECISION_INTERVAL_SEC = max(10, _parse_int(_env_or_cfg("PARKED_DECISION_INTERVAL_SEC", cfg_data, PARKED_DECISION_INTERVAL_SEC), PARKED_DECISION_INTERVAL_SEC))
        PRICE_POLL_SEC = max(1, _parse_int(_env_or_cfg("PRICE_POLL_SEC", cfg_data, PRICE_POLL_SEC), PRICE_POLL_SEC))
        ATR_REFRESH_SEC = max(10, _parse_int(_env_or_cfg("ATR_REFRESH_SEC", cfg_data, ATR_REFRESH_SEC), ATR_REFRESH_SEC))
        BASKET_REFRESH_SEC = max(30, _parse_int(_env_or_cfg("BASKET_REFRESH_SEC", cfg_data, BASKET_REFRESH_SEC), BASKET_REFRESH_SEC))

        PRODUCT_UNIVERSE = str(_env_or_cfg("PRODUCT_UNIVERSE", cfg_data, PRODUCT_UNIVERSE)).strip().lower() or "all"
        spot_quotes_raw = str(_env_or_cfg("SPOT_QUOTES", cfg_data, ",".join(SPOT_QUOTES)))
        SPOT_QUOTES = [q.strip().upper() for q in spot_quotes_raw.split(",") if q.strip()]

        SPOT_DISCOVERY_MODE = str(_env_or_cfg("SPOT_DISCOVERY_MODE", cfg_data, SPOT_DISCOVERY_MODE)).strip().lower() or SPOT_DISCOVERY_MODE
        SPOT_DISCOVERY_REFRESH_SEC = max(
            600,
            _parse_int(
                _env_or_cfg("SPOT_DISCOVERY_REFRESH_SEC", cfg_data, SPOT_DISCOVERY_REFRESH_SEC),
                SPOT_DISCOVERY_REFRESH_SEC,
            ),
        )
        SPOT_PRIORITY_SIZE = max(
            20,
            _parse_int(_env_or_cfg("SPOT_PRIORITY_SIZE", cfg_data, SPOT_PRIORITY_SIZE), SPOT_PRIORITY_SIZE),
        )
        SPOT_ACTIVE_SCAN_SIZE = max(
            10,
            _parse_int(_env_or_cfg("SPOT_ACTIVE_SCAN_SIZE", cfg_data, SPOT_ACTIVE_SCAN_SIZE), SPOT_ACTIVE_SCAN_SIZE),
        )
        HEALTH_DEGRADED_FAILURES = max(1, _parse_int(_env_or_cfg("HEALTH_DEGRADED_FAILURES", cfg_data, HEALTH_DEGRADED_FAILURES), HEALTH_DEGRADED_FAILURES))
        HEALTH_OUTAGE_FAILURES = max(
            HEALTH_DEGRADED_FAILURES + 1,
            _parse_int(_env_or_cfg("HEALTH_OUTAGE_FAILURES", cfg_data, HEALTH_OUTAGE_FAILURES), HEALTH_OUTAGE_FAILURES),
        )
        HEALTH_RECOVER_SUCCESS_STREAK = max(
            1,
            _parse_int(
                _env_or_cfg("HEALTH_RECOVER_SUCCESS_STREAK", cfg_data, HEALTH_RECOVER_SUCCESS_STREAK),
                HEALTH_RECOVER_SUCCESS_STREAK,
            ),
        )
        HEALTH_OUTAGE_FLATTEN_SEC = max(
            30,
            _parse_int(_env_or_cfg("HEALTH_OUTAGE_FLATTEN_SEC", cfg_data, HEALTH_OUTAGE_FLATTEN_SEC), HEALTH_OUTAGE_FLATTEN_SEC),
        )
        HEALTH_BLOCK_RECOVERING = str(
            _env_or_cfg("HEALTH_BLOCK_RECOVERING", cfg_data, "1" if HEALTH_BLOCK_RECOVERING else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        DATAQ_MAX_PRICE_AGE_SEC = max(
            5,
            _parse_int(_env_or_cfg("DATAQ_MAX_PRICE_AGE_SEC", cfg_data, DATAQ_MAX_PRICE_AGE_SEC), DATAQ_MAX_PRICE_AGE_SEC),
        )
        DATAQ_MIN_BASKET_SIZE = max(
            1,
            _parse_int(_env_or_cfg("DATAQ_MIN_BASKET_SIZE", cfg_data, DATAQ_MIN_BASKET_SIZE), DATAQ_MIN_BASKET_SIZE),
        )
        DATAQ_MIN_FRESH_PRICE_RATIO = max(
            0.0,
            min(
                1.0,
                _parse_float(
                    _env_or_cfg("DATAQ_MIN_FRESH_PRICE_RATIO", cfg_data, DATAQ_MIN_FRESH_PRICE_RATIO),
                    DATAQ_MIN_FRESH_PRICE_RATIO,
                ),
            ),
        )
        DATAQ_MIN_ATR_COVERAGE_RATIO = max(
            0.0,
            min(
                1.0,
                _parse_float(
                    _env_or_cfg("DATAQ_MIN_ATR_COVERAGE_RATIO", cfg_data, DATAQ_MIN_ATR_COVERAGE_RATIO),
                    DATAQ_MIN_ATR_COVERAGE_RATIO,
                ),
            ),
        )
        DD_DAILY_LIMIT_PCT = max(
            0.1,
            _parse_float(_env_or_cfg("DD_DAILY_LIMIT_PCT", cfg_data, DD_DAILY_LIMIT_PCT), DD_DAILY_LIMIT_PCT),
        )
        DD_WEEKLY_LIMIT_PCT = max(
            0.1,
            _parse_float(_env_or_cfg("DD_WEEKLY_LIMIT_PCT", cfg_data, DD_WEEKLY_LIMIT_PCT), DD_WEEKLY_LIMIT_PCT),
        )
        DD_ATH_TRAILING_LIMIT_PCT = max(
            0.1,
            _parse_float(
                _env_or_cfg("DD_ATH_TRAILING_LIMIT_PCT", cfg_data, DD_ATH_TRAILING_LIMIT_PCT),
                DD_ATH_TRAILING_LIMIT_PCT,
            ),
        )
        DD_CHECK_SEC = max(
            10,
            _parse_int(_env_or_cfg("DD_CHECK_SEC", cfg_data, DD_CHECK_SEC), DD_CHECK_SEC),
        )
        DD_AUTO_FLATTEN = str(
            _env_or_cfg("DD_AUTO_FLATTEN", cfg_data, "1" if DD_AUTO_FLATTEN else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        DD_AUTO_PARK = str(
            _env_or_cfg("DD_AUTO_PARK", cfg_data, "1" if DD_AUTO_PARK else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        SIM_TAKER_FEE_RATE = max(
            0.0,
            _parse_float(_env_or_cfg("SIM_TAKER_FEE_RATE", cfg_data, SIM_TAKER_FEE_RATE), SIM_TAKER_FEE_RATE),
        )
        SIM_FUNDING_RATE_PER_8H = max(
            0.0,
            _parse_float(
                _env_or_cfg("SIM_FUNDING_RATE_PER_8H", cfg_data, SIM_FUNDING_RATE_PER_8H),
                SIM_FUNDING_RATE_PER_8H,
            ),
        )
        SIM_SLIPPAGE_MIN_PCT = max(
            0.0,
            _parse_float(_env_or_cfg("SIM_SLIPPAGE_MIN_PCT", cfg_data, SIM_SLIPPAGE_MIN_PCT), SIM_SLIPPAGE_MIN_PCT),
        )
        SIM_SLIPPAGE_MAX_PCT = max(
            SIM_SLIPPAGE_MIN_PCT,
            _parse_float(_env_or_cfg("SIM_SLIPPAGE_MAX_PCT", cfg_data, SIM_SLIPPAGE_MAX_PCT), SIM_SLIPPAGE_MAX_PCT),
        )
        SIM_SLIPPAGE_ATR_MULT = max(
            0.0,
            _parse_float(_env_or_cfg("SIM_SLIPPAGE_ATR_MULT", cfg_data, SIM_SLIPPAGE_ATR_MULT), SIM_SLIPPAGE_ATR_MULT),
        )
        REGIME_CLASSIFIER_ASSET = str(_env_or_cfg("REGIME_CLASSIFIER_ASSET", cfg_data, REGIME_CLASSIFIER_ASSET)).strip() or "BTC-PERP-INTX"
        REGIME_LOOKBACK_POINTS = max(
            20,
            _parse_int(_env_or_cfg("REGIME_LOOKBACK_POINTS", cfg_data, REGIME_LOOKBACK_POINTS), REGIME_LOOKBACK_POINTS),
        )
        REGIME_TREND_RET_PCT = max(
            0.05,
            _parse_float(_env_or_cfg("REGIME_TREND_RET_PCT", cfg_data, REGIME_TREND_RET_PCT), REGIME_TREND_RET_PCT),
        )
        REGIME_HIGH_VOL_ATR_PCT = max(
            0.05,
            _parse_float(_env_or_cfg("REGIME_HIGH_VOL_ATR_PCT", cfg_data, REGIME_HIGH_VOL_ATR_PCT), REGIME_HIGH_VOL_ATR_PCT),
        )
        REGIME_TREND_NOISE_RATIO = max(
            0.5,
            _parse_float(_env_or_cfg("REGIME_TREND_NOISE_RATIO", cfg_data, REGIME_TREND_NOISE_RATIO), REGIME_TREND_NOISE_RATIO),
        )
        REGIME_RISK_MULT_TREND = max(
            0.1,
            _parse_float(_env_or_cfg("REGIME_RISK_MULT_TREND", cfg_data, REGIME_RISK_MULT_TREND), REGIME_RISK_MULT_TREND),
        )
        REGIME_RISK_MULT_CHOP = max(
            0.1,
            _parse_float(_env_or_cfg("REGIME_RISK_MULT_CHOP", cfg_data, REGIME_RISK_MULT_CHOP), REGIME_RISK_MULT_CHOP),
        )
        REGIME_RISK_MULT_HIGH_VOL = max(
            0.1,
            _parse_float(_env_or_cfg("REGIME_RISK_MULT_HIGH_VOL", cfg_data, REGIME_RISK_MULT_HIGH_VOL), REGIME_RISK_MULT_HIGH_VOL),
        )
        REGIME_LEV_CAP_TREND = max(
            1,
            _parse_int(_env_or_cfg("REGIME_LEV_CAP_TREND", cfg_data, REGIME_LEV_CAP_TREND), REGIME_LEV_CAP_TREND),
        )
        REGIME_LEV_CAP_CHOP = max(
            1,
            _parse_int(_env_or_cfg("REGIME_LEV_CAP_CHOP", cfg_data, REGIME_LEV_CAP_CHOP), REGIME_LEV_CAP_CHOP),
        )
        REGIME_LEV_CAP_HIGH_VOL = max(
            1,
            _parse_int(_env_or_cfg("REGIME_LEV_CAP_HIGH_VOL", cfg_data, REGIME_LEV_CAP_HIGH_VOL), REGIME_LEV_CAP_HIGH_VOL),
        )
        REGIME_MIN_RR_ADD_TREND = max(
            0.0,
            _parse_float(_env_or_cfg("REGIME_MIN_RR_ADD_TREND", cfg_data, REGIME_MIN_RR_ADD_TREND), REGIME_MIN_RR_ADD_TREND),
        )
        REGIME_MIN_RR_ADD_CHOP = max(
            0.0,
            _parse_float(_env_or_cfg("REGIME_MIN_RR_ADD_CHOP", cfg_data, REGIME_MIN_RR_ADD_CHOP), REGIME_MIN_RR_ADD_CHOP),
        )
        REGIME_MIN_RR_ADD_HIGH_VOL = max(
            0.0,
            _parse_float(_env_or_cfg("REGIME_MIN_RR_ADD_HIGH_VOL", cfg_data, REGIME_MIN_RR_ADD_HIGH_VOL), REGIME_MIN_RR_ADD_HIGH_VOL),
        )
        DECISION_MIN_PUMP_SCORE = max(
            0,
            _parse_int(_env_or_cfg("DECISION_MIN_PUMP_SCORE", cfg_data, DECISION_MIN_PUMP_SCORE), DECISION_MIN_PUMP_SCORE),
        )
        DECISION_MIN_VOL_SPIKE = max(
            0.0,
            _parse_float(_env_or_cfg("DECISION_MIN_VOL_SPIKE", cfg_data, DECISION_MIN_VOL_SPIKE), DECISION_MIN_VOL_SPIKE),
        )
        DECISION_MIN_RR_AGGRESSIVE = max(
            0.1,
            _parse_float(
                _env_or_cfg("DECISION_MIN_RR_AGGRESSIVE", cfg_data, DECISION_MIN_RR_AGGRESSIVE),
                DECISION_MIN_RR_AGGRESSIVE,
            ),
        )
        DECISION_MIN_RR_SAFE = max(
            0.1,
            _parse_float(_env_or_cfg("DECISION_MIN_RR_SAFE", cfg_data, DECISION_MIN_RR_SAFE), DECISION_MIN_RR_SAFE),
        )
        EXEC_POST_ONLY_ENABLED = str(
            _env_or_cfg("EXEC_POST_ONLY_ENABLED", cfg_data, "1" if EXEC_POST_ONLY_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        EXEC_POST_ONLY_OFFSET_PCT = max(
            0.0,
            _parse_float(
                _env_or_cfg("EXEC_POST_ONLY_OFFSET_PCT", cfg_data, EXEC_POST_ONLY_OFFSET_PCT),
                EXEC_POST_ONLY_OFFSET_PCT,
            ),
        )
        EXEC_IOC_FALLBACK_ENABLED = str(
            _env_or_cfg("EXEC_IOC_FALLBACK_ENABLED", cfg_data, "1" if EXEC_IOC_FALLBACK_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        EXEC_IOC_SLIPPAGE_PCT = max(
            0.0,
            _parse_float(_env_or_cfg("EXEC_IOC_SLIPPAGE_PCT", cfg_data, EXEC_IOC_SLIPPAGE_PCT), EXEC_IOC_SLIPPAGE_PCT),
        )
        EXEC_MARKET_FALLBACK_ENABLED = str(
            _env_or_cfg("EXEC_MARKET_FALLBACK_ENABLED", cfg_data, "1" if EXEC_MARKET_FALLBACK_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        EXEC_MARKET_GUARD_MAX_SPREAD_PCT = max(
            0.01,
            _parse_float(
                _env_or_cfg("EXEC_MARKET_GUARD_MAX_SPREAD_PCT", cfg_data, EXEC_MARKET_GUARD_MAX_SPREAD_PCT),
                EXEC_MARKET_GUARD_MAX_SPREAD_PCT,
            ),
        )
        EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT = max(
            0.01,
            _parse_float(
                _env_or_cfg(
                    "EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT",
                    cfg_data,
                    EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT,
                ),
                EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT,
            ),
        )
        EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED = str(
            _env_or_cfg(
                "EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED",
                cfg_data,
                "1" if EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED else "0",
            )
        ).strip().lower() in {"1", "true", "yes", "on"}
        EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT = max(
            0.0,
            _parse_float(
                _env_or_cfg(
                    "EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT",
                    cfg_data,
                    EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT,
                ),
                EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT,
            ),
        )
        PYRAMID_ENABLED = str(
            _env_or_cfg("PYRAMID_ENABLED", cfg_data, "1" if PYRAMID_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        PYRAMID_RR_TRIGGER = max(
            0.5,
            _parse_float(_env_or_cfg("PYRAMID_RR_TRIGGER", cfg_data, PYRAMID_RR_TRIGGER), PYRAMID_RR_TRIGGER),
        )
        PYRAMID_ADD_FRACTION = max(
            0.01,
            min(
                1.0,
                _parse_float(_env_or_cfg("PYRAMID_ADD_FRACTION", cfg_data, PYRAMID_ADD_FRACTION), PYRAMID_ADD_FRACTION),
            ),
        )
        PYRAMID_MAX_ADDS = max(
            0,
            _parse_int(_env_or_cfg("PYRAMID_MAX_ADDS", cfg_data, PYRAMID_MAX_ADDS), PYRAMID_MAX_ADDS),
        )
        PYRAMID_MIN_CONVICTION = max(
            0,
            min(
                100,
                _parse_int(
                    _env_or_cfg("PYRAMID_MIN_CONVICTION", cfg_data, PYRAMID_MIN_CONVICTION),
                    PYRAMID_MIN_CONVICTION,
                ),
            ),
        )
        PYRAMID_MAX_EXPOSURE_PCT = max(
            0.01,
            min(
                1.0,
                _parse_float(
                    _env_or_cfg("PYRAMID_MAX_EXPOSURE_PCT", cfg_data, PYRAMID_MAX_EXPOSURE_PCT),
                    PYRAMID_MAX_EXPOSURE_PCT,
                ),
            ),
        )
        DAILY_SCORE_ENABLED = str(
            _env_or_cfg("DAILY_SCORE_ENABLED", cfg_data, "1" if DAILY_SCORE_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        DAILY_SCORE_CSV_PATH = str(
            _env_or_cfg("DAILY_SCORE_CSV_PATH", cfg_data, DAILY_SCORE_CSV_PATH)
        ).strip() or "daily_score.csv"
        DAILY_SCORE_CHECK_SEC = max(
            10,
            _parse_int(_env_or_cfg("DAILY_SCORE_CHECK_SEC", cfg_data, DAILY_SCORE_CHECK_SEC), DAILY_SCORE_CHECK_SEC),
        )
        GROK_SELF_CRITIQUE_ENABLED = str(
            _env_or_cfg("GROK_SELF_CRITIQUE_ENABLED", cfg_data, "1" if GROK_SELF_CRITIQUE_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        GROK_MIN_CRITIQUE_CONVICTION = max(
            0,
            min(
                100,
                _parse_int(
                    _env_or_cfg("GROK_MIN_CRITIQUE_CONVICTION", cfg_data, GROK_MIN_CRITIQUE_CONVICTION),
                    GROK_MIN_CRITIQUE_CONVICTION,
                ),
            ),
        )
        GROK_CONTEXT_RECENT_TRADES = max(
            3,
            _parse_int(_env_or_cfg("GROK_CONTEXT_RECENT_TRADES", cfg_data, GROK_CONTEXT_RECENT_TRADES), GROK_CONTEXT_RECENT_TRADES),
        )
        GROK_FREE_SIGNALS_TIMEOUT_SEC = max(
            2,
            _parse_int(
                _env_or_cfg("GROK_FREE_SIGNALS_TIMEOUT_SEC", cfg_data, GROK_FREE_SIGNALS_TIMEOUT_SEC),
                GROK_FREE_SIGNALS_TIMEOUT_SEC,
            ),
        )
        GROK_FUNDING_FILTER_ENABLED = str(
            _env_or_cfg("GROK_FUNDING_FILTER_ENABLED", cfg_data, "1" if GROK_FUNDING_FILTER_ENABLED else "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        GROK_FUNDING_BLOCK_LONG_PCT = _parse_float(
            _env_or_cfg("GROK_FUNDING_BLOCK_LONG_PCT", cfg_data, GROK_FUNDING_BLOCK_LONG_PCT),
            GROK_FUNDING_BLOCK_LONG_PCT,
        )
        GROK_FUNDING_BLOCK_SHORT_PCT = _parse_float(
            _env_or_cfg("GROK_FUNDING_BLOCK_SHORT_PCT", cfg_data, GROK_FUNDING_BLOCK_SHORT_PCT),
            GROK_FUNDING_BLOCK_SHORT_PCT,
        )

    return {
        "PARK_FLAG": PARK_FLAG,
        "READINESS_HOURS": READINESS_HOURS,
        "RUMOR_SOURCE": RUMOR_SOURCE,
        "RUMOR_POLL_SEC": RUMOR_POLL_SEC,
        "RUMOR_MAX_POSTS": RUMOR_MAX_POSTS,
        "RUMOR_LOOKBACK_HOURS": RUMOR_LOOKBACK_HOURS,
        "DECISION_INTERVAL_SEC": DECISION_INTERVAL_SEC,
        "PARKED_DECISION_INTERVAL_SEC": PARKED_DECISION_INTERVAL_SEC,
        "PRICE_POLL_SEC": PRICE_POLL_SEC,
        "ATR_REFRESH_SEC": ATR_REFRESH_SEC,
        "BASKET_REFRESH_SEC": BASKET_REFRESH_SEC,
        "PRODUCT_UNIVERSE": PRODUCT_UNIVERSE,
        "SPOT_QUOTES": SPOT_QUOTES,
        "SPOT_DISCOVERY_MODE": SPOT_DISCOVERY_MODE,
        "SPOT_DISCOVERY_REFRESH_SEC": SPOT_DISCOVERY_REFRESH_SEC,
        "SPOT_PRIORITY_SIZE": SPOT_PRIORITY_SIZE,
        "SPOT_ACTIVE_SCAN_SIZE": SPOT_ACTIVE_SCAN_SIZE,
        "HEALTH_DEGRADED_FAILURES": HEALTH_DEGRADED_FAILURES,
        "HEALTH_OUTAGE_FAILURES": HEALTH_OUTAGE_FAILURES,
        "HEALTH_RECOVER_SUCCESS_STREAK": HEALTH_RECOVER_SUCCESS_STREAK,
        "HEALTH_OUTAGE_FLATTEN_SEC": HEALTH_OUTAGE_FLATTEN_SEC,
        "HEALTH_BLOCK_RECOVERING": HEALTH_BLOCK_RECOVERING,
        "DATAQ_MAX_PRICE_AGE_SEC": DATAQ_MAX_PRICE_AGE_SEC,
        "DATAQ_MIN_BASKET_SIZE": DATAQ_MIN_BASKET_SIZE,
        "DATAQ_MIN_FRESH_PRICE_RATIO": DATAQ_MIN_FRESH_PRICE_RATIO,
        "DATAQ_MIN_ATR_COVERAGE_RATIO": DATAQ_MIN_ATR_COVERAGE_RATIO,
        "DD_DAILY_LIMIT_PCT": DD_DAILY_LIMIT_PCT,
        "DD_WEEKLY_LIMIT_PCT": DD_WEEKLY_LIMIT_PCT,
        "DD_ATH_TRAILING_LIMIT_PCT": DD_ATH_TRAILING_LIMIT_PCT,
        "DD_CHECK_SEC": DD_CHECK_SEC,
        "DD_AUTO_FLATTEN": DD_AUTO_FLATTEN,
        "DD_AUTO_PARK": DD_AUTO_PARK,
        "SIM_TAKER_FEE_RATE": SIM_TAKER_FEE_RATE,
        "SIM_FUNDING_RATE_PER_8H": SIM_FUNDING_RATE_PER_8H,
        "SIM_SLIPPAGE_MIN_PCT": SIM_SLIPPAGE_MIN_PCT,
        "SIM_SLIPPAGE_MAX_PCT": SIM_SLIPPAGE_MAX_PCT,
        "SIM_SLIPPAGE_ATR_MULT": SIM_SLIPPAGE_ATR_MULT,
        "REGIME_CLASSIFIER_ASSET": REGIME_CLASSIFIER_ASSET,
        "REGIME_LOOKBACK_POINTS": REGIME_LOOKBACK_POINTS,
        "REGIME_TREND_RET_PCT": REGIME_TREND_RET_PCT,
        "REGIME_HIGH_VOL_ATR_PCT": REGIME_HIGH_VOL_ATR_PCT,
        "REGIME_TREND_NOISE_RATIO": REGIME_TREND_NOISE_RATIO,
        "REGIME_RISK_MULT_TREND": REGIME_RISK_MULT_TREND,
        "REGIME_RISK_MULT_CHOP": REGIME_RISK_MULT_CHOP,
        "REGIME_RISK_MULT_HIGH_VOL": REGIME_RISK_MULT_HIGH_VOL,
        "REGIME_LEV_CAP_TREND": REGIME_LEV_CAP_TREND,
        "REGIME_LEV_CAP_CHOP": REGIME_LEV_CAP_CHOP,
        "REGIME_LEV_CAP_HIGH_VOL": REGIME_LEV_CAP_HIGH_VOL,
        "REGIME_MIN_RR_ADD_TREND": REGIME_MIN_RR_ADD_TREND,
        "REGIME_MIN_RR_ADD_CHOP": REGIME_MIN_RR_ADD_CHOP,
        "REGIME_MIN_RR_ADD_HIGH_VOL": REGIME_MIN_RR_ADD_HIGH_VOL,
        "DECISION_MIN_PUMP_SCORE": DECISION_MIN_PUMP_SCORE,
        "DECISION_MIN_VOL_SPIKE": DECISION_MIN_VOL_SPIKE,
        "DECISION_MIN_RR_AGGRESSIVE": DECISION_MIN_RR_AGGRESSIVE,
        "DECISION_MIN_RR_SAFE": DECISION_MIN_RR_SAFE,
        "EXEC_POST_ONLY_ENABLED": EXEC_POST_ONLY_ENABLED,
        "EXEC_POST_ONLY_OFFSET_PCT": EXEC_POST_ONLY_OFFSET_PCT,
        "EXEC_IOC_FALLBACK_ENABLED": EXEC_IOC_FALLBACK_ENABLED,
        "EXEC_IOC_SLIPPAGE_PCT": EXEC_IOC_SLIPPAGE_PCT,
        "EXEC_MARKET_FALLBACK_ENABLED": EXEC_MARKET_FALLBACK_ENABLED,
        "EXEC_MARKET_GUARD_MAX_SPREAD_PCT": EXEC_MARKET_GUARD_MAX_SPREAD_PCT,
        "EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT": EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT,
        "EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED": EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED,
        "EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT": EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT,
        "PYRAMID_ENABLED": PYRAMID_ENABLED,
        "PYRAMID_RR_TRIGGER": PYRAMID_RR_TRIGGER,
        "PYRAMID_ADD_FRACTION": PYRAMID_ADD_FRACTION,
        "PYRAMID_MAX_ADDS": PYRAMID_MAX_ADDS,
        "PYRAMID_MIN_CONVICTION": PYRAMID_MIN_CONVICTION,
        "PYRAMID_MAX_EXPOSURE_PCT": PYRAMID_MAX_EXPOSURE_PCT,
        "DAILY_SCORE_ENABLED": DAILY_SCORE_ENABLED,
        "DAILY_SCORE_CSV_PATH": DAILY_SCORE_CSV_PATH,
        "DAILY_SCORE_CHECK_SEC": DAILY_SCORE_CHECK_SEC,
        "GROK_SELF_CRITIQUE_ENABLED": GROK_SELF_CRITIQUE_ENABLED,
        "GROK_MIN_CRITIQUE_CONVICTION": GROK_MIN_CRITIQUE_CONVICTION,
        "GROK_CONTEXT_RECENT_TRADES": GROK_CONTEXT_RECENT_TRADES,
        "GROK_FREE_SIGNALS_TIMEOUT_SEC": GROK_FREE_SIGNALS_TIMEOUT_SEC,
        "GROK_FUNDING_FILTER_ENABLED": GROK_FUNDING_FILTER_ENABLED,
        "GROK_FUNDING_BLOCK_LONG_PCT": GROK_FUNDING_BLOCK_LONG_PCT,
        "GROK_FUNDING_BLOCK_SHORT_PCT": GROK_FUNDING_BLOCK_SHORT_PCT,
    }

SAFE_ASSETS = {"BTC-PERP-INTX", "ETH-PERP-INTX", "SOL-PERP-INTX"}

if AdvancedTradeClient is not None and API_KEY and API_SECRET:
    client = AdvancedTradeClient(API_KEY, API_SECRET)
else:
    client = None

if AsyncOpenAI is not None and GROK_KEY:
    grok = AsyncOpenAI(api_key=GROK_KEY, base_url="https://api.x.ai/v1")
else:
    grok = None

state = {
    "started_at": datetime.utcnow().isoformat(),
    "equity": float(TRADE_BALANCE) if TRADE_BALANCE is not None else 250.0,
    "sim_base_equity": float(TRADE_BALANCE) if TRADE_BALANCE is not None else 250.0,
    "sim_realized_pnl": 0.0,
    "positions": {},
    "price": {},
    "price_ts": {},
    "price_history": {},
    "vol_cache": {},
    "mtf_cache": {},
    "micro_cache": {},
    "exec_liq_cache": {},
    "spot_universe": [],
    "spot_universe_count": 0,
    "spot_priority": [],
    "spot_scan_cursor": 0,
    "spot_discovery_last_ts": None,
    "new_alts": [],
    "rumor_items": [],
    "rumors_summary": "none",
    "whale_summary": "none",
    "whale_flow": {},
    "spike_list": [],
    "basket": ["BTC-PERP-INTX", "ETH-PERP-INTX", "SOL-PERP-INTX", "DOGE-PERP-INTX", "PEPE-PERP-INTX"],
    "basket_ver": 0,
    "parked": os.path.exists(PARK_FLAG),
    "mode": "nuclear",
    "aggr_target": float(TRADE_BALANCE) if TRADE_BALANCE is not None else 250.0,
    "safe_target": 0.0,
    "last_rebal": None,
    "trades": {},  # order_id → info
    "timers": {},
    "ready_to_trade": False,
    "last_decision": "n/a",
    "last_decision_asset": None,
    "last_decision_reason": "",
    "last_decision_ts": None,
    "health_state": "healthy",
    "health_last_transition_ts": datetime.utcnow().isoformat(),
    "health_consecutive_failures": 0,
    "health_consecutive_successes": 0,
    "health_last_success_ts": None,
    "health_last_failure_ts": None,
    "health_last_failure_reason": "",
    "health_outage_since_ts": None,
    "health_recovering_since_ts": None,
    "health_outage_flattened": False,
    "data_quality_last_ok": True,
    "data_quality_last_reason": "",
    "data_quality_last_check_ts": None,
    "equity_history": [],
    "drawdown_paused": False,
    "drawdown_pause_reason": "",
    "drawdown_pause_ts": None,
    "drawdown_daily_date": None,
    "drawdown_daily_peak": 0.0,
    "drawdown_weekly_peak": 0.0,
    "drawdown_ath_peak": 0.0,
    "drawdown_daily_dd_pct": 0.0,
    "drawdown_weekly_dd_pct": 0.0,
    "drawdown_ath_dd_pct": 0.0,
    "regime": "chop",
    "regime_asset": "BTC-PERP-INTX",
    "regime_last_ts": None,
    "regime_metrics": {},
    "decision_gate_last_ok": True,
    "decision_gate_last_reason": "",
    "decision_gate_last_ts": None,
    "decision_gate_last_metrics": {},
    "execution_last_ok": True,
    "execution_last_path": "",
    "execution_last_reason": "",
    "execution_last_ts": None,
    "daily_score_last_seen_day": None,
    "daily_score_last_written_day": None,
    "last_regime_signals": {},
    "last_critique": {},
    "recent_returns": [],
    "equity_momentum_7d_return_pct": 0.0,
}