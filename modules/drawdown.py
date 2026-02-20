import asyncio
from datetime import datetime, timedelta

from . import config as cfg
from .db import load_equity_history_points, save_equity_history_point


def _utcnow_iso():
    return datetime.utcnow().isoformat()


def _to_dt(ts_text):
    try:
        return datetime.fromisoformat(str(ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _pct_drawdown(current, peak):
    try:
        current = float(current)
        peak = float(peak)
    except (TypeError, ValueError):
        return 0.0
    if peak <= 0:
        return 0.0
    return max(0.0, ((peak - current) / peak) * 100.0)


def drawdown_entries_blocked():
    return bool(cfg.state.get("drawdown_paused", False))


def _ensure_equity_history_loaded():
    if cfg.state.get("equity_history"):
        return
    cfg.state["equity_history"] = load_equity_history_points(limit=5000)


def _append_equity_point(equity):
    ts_text = _utcnow_iso()
    point = {"ts": ts_text, "equity": float(equity)}
    history = cfg.state.setdefault("equity_history", [])
    history.append(point)

    cutoff = datetime.utcnow() - timedelta(days=8)
    filtered = []
    for item in history:
        dt_value = _to_dt(item.get("ts"))
        if dt_value is None or dt_value >= cutoff:
            filtered.append(item)
    cfg.state["equity_history"] = filtered[-5000:]
    save_equity_history_point(ts_text, equity)


def evaluate_drawdown_status(equity):
    _ensure_equity_history_loaded()
    _append_equity_point(equity)

    now = datetime.utcnow()
    day_key = now.date().isoformat()

    daily_peak = float(cfg.state.get("drawdown_daily_peak", 0.0) or 0.0)
    if cfg.state.get("drawdown_daily_date") != day_key:
        daily_peak = float(equity)
    else:
        daily_peak = max(daily_peak, float(equity))

    history = cfg.state.get("equity_history", [])
    weekly_cutoff = now - timedelta(days=7)
    weekly_peak = float(equity)
    ath_peak = float(cfg.state.get("drawdown_ath_peak", 0.0) or 0.0)
    ath_peak = max(ath_peak, float(equity))

    for item in history:
        dt_value = _to_dt(item.get("ts"))
        if dt_value is None:
            continue
        eq = float(item.get("equity", 0.0) or 0.0)
        if dt_value >= weekly_cutoff:
            weekly_peak = max(weekly_peak, eq)
        ath_peak = max(ath_peak, eq)

    daily_dd = _pct_drawdown(equity, daily_peak)
    weekly_dd = _pct_drawdown(equity, weekly_peak)
    ath_dd = _pct_drawdown(equity, ath_peak)

    cfg.state["drawdown_daily_date"] = day_key
    cfg.state["drawdown_daily_peak"] = daily_peak
    cfg.state["drawdown_weekly_peak"] = weekly_peak
    cfg.state["drawdown_ath_peak"] = ath_peak
    cfg.state["drawdown_daily_dd_pct"] = daily_dd
    cfg.state["drawdown_weekly_dd_pct"] = weekly_dd
    cfg.state["drawdown_ath_dd_pct"] = ath_dd

    breached_reason = None
    if daily_dd >= float(cfg.DD_DAILY_LIMIT_PCT):
        breached_reason = "daily"
    elif weekly_dd >= float(cfg.DD_WEEKLY_LIMIT_PCT):
        breached_reason = "weekly"
    elif ath_dd >= float(cfg.DD_ATH_TRAILING_LIMIT_PCT):
        breached_reason = "ath"

    return {
        "breached": breached_reason is not None,
        "reason": breached_reason,
        "daily_dd_pct": daily_dd,
        "weekly_dd_pct": weekly_dd,
        "ath_dd_pct": ath_dd,
        "daily_peak": daily_peak,
        "weekly_peak": weekly_peak,
        "ath_peak": ath_peak,
        "equity": float(equity),
    }


async def _enforce_drawdown_pause(status):
    if bool(cfg.state.get("drawdown_paused", False)):
        return

    reason = str(status.get("reason") or "unknown")
    cfg.state["drawdown_paused"] = True
    cfg.state["drawdown_pause_ts"] = _utcnow_iso()
    cfg.state["drawdown_pause_reason"] = (
        f"{reason}: daily={status.get('daily_dd_pct', 0.0):.2f}% "
        f"weekly={status.get('weekly_dd_pct', 0.0):.2f}% ath={status.get('ath_dd_pct', 0.0):.2f}%"
    )

    cfg.logger.error("Drawdown gate triggered: %s", cfg.state["drawdown_pause_reason"])

    if bool(cfg.DD_AUTO_FLATTEN):
        from .rest import rest_in_usdc
        from .trade import force_close_all_open_trades

        await rest_in_usdc()
        force_close_all_open_trades(f"drawdown_{reason}")

    if bool(cfg.DD_AUTO_PARK):
        cfg.state["parked"] = True
        try:
            with open(cfg.PARK_FLAG, "a", encoding="utf-8"):
                pass
        except OSError:
            pass


async def drawdown_guard_loop():
    while True:
        cfg.reload_hot_config()
        try:
            equity = float(cfg.state.get("equity", 0.0) or 0.0)
            status = evaluate_drawdown_status(equity)
            if status.get("breached"):
                await _enforce_drawdown_pause(status)
        except Exception as exc:
            cfg.logger.error("Drawdown guard error: %s", exc)
        await asyncio.sleep(int(cfg.DD_CHECK_SEC))
