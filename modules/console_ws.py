import asyncio
import json
from datetime import datetime

from . import config as cfg

try:
    import websockets
except ImportError:
    websockets = None


def _snapshot():
    cfg.reload_hot_config()
    open_trades = []
    for trade_id, trade in cfg.state.get("trades", {}).items():
        if trade.get("status") != "open":
            continue
        open_trades.append(
            {
                "id": trade_id,
                "asset": trade.get("asset"),
                "side": trade.get("side"),
                "entry": trade.get("entry"),
                "remaining_size": trade.get("remaining_size"),
                "stop": trade.get("stop"),
                "take_profit": trade.get("take_profit"),
                "rr": trade.get("rr"),
                "sleeve": trade.get("sleeve"),
                "pump_score": trade.get("pump_score"),
                "vol_spike": trade.get("vol_spike"),
                "execution_path": trade.get("execution_path"),
                "guard_spread_pct": trade.get("guard_spread_pct"),
                "guard_size_to_vol1m_pct": trade.get("guard_size_to_vol1m_pct"),
            }
        )

    basket = list(cfg.state.get("basket", []))
    top_assets = basket[:8]
    top_prices = []
    top_histories = {}
    for asset in top_assets:
        liq = (cfg.state.get("exec_liq_cache", {}) or {}).get(asset) or {}
        top_prices.append(
            {
                "asset": asset,
                "price": cfg.state.get("price", {}).get(asset),
                "atr_1h": (cfg.state.get("vol_cache", {}).get(asset) or {}).get("atr_1h"),
                "atr_6h": (cfg.state.get("vol_cache", {}).get(asset) or {}).get("atr_6h"),
                "spread_pct": liq.get("spread_pct"),
                "volume_1m": liq.get("volume_1m"),
            }
        )
        top_histories[asset] = (cfg.state.get("price_history", {}).get(asset) or [])[-120:]

    focus_asset = top_assets[0] if top_assets else None
    rumor_items = cfg.state.get("rumor_items", [])[:8]
    rumor_headlines = []
    for item in rumor_items:
        rumor_headlines.append(
            {
                "asset": item.get("asset"),
                "sent": item.get("sent"),
                "pump": item.get("pump"),
                "rumor": item.get("rumor"),
            }
        )

    started_at_raw = cfg.state.get("started_at")
    ready_to_trade = bool(cfg.state.get("ready_to_trade", False))
    if started_at_raw:
        try:
            started = datetime.fromisoformat(str(started_at_raw).replace("Z", "+00:00")).replace(tzinfo=None)
            elapsed_h = max(0.0, (datetime.utcnow() - started).total_seconds() / 3600.0)
            ready_to_trade = elapsed_h >= float(cfg.READINESS_HOURS or 0)
        except ValueError:
            pass

    return {
        "ts": datetime.utcnow().isoformat(),
        "started_at": cfg.state.get("started_at"),
        "readiness_hours": cfg.READINESS_HOURS,
        "ready_to_trade": ready_to_trade,
        "health_state": cfg.state.get("health_state", "healthy"),
        "health_last_transition_ts": cfg.state.get("health_last_transition_ts"),
        "health_last_success_ts": cfg.state.get("health_last_success_ts"),
        "health_last_failure_ts": cfg.state.get("health_last_failure_ts"),
        "health_last_failure_reason": cfg.state.get("health_last_failure_reason", ""),
        "health_outage_since_ts": cfg.state.get("health_outage_since_ts"),
        "health_outage_flattened": cfg.state.get("health_outage_flattened", False),
        "data_quality_last_ok": cfg.state.get("data_quality_last_ok", True),
        "data_quality_last_reason": cfg.state.get("data_quality_last_reason", ""),
        "data_quality_last_check_ts": cfg.state.get("data_quality_last_check_ts"),
        "drawdown_paused": cfg.state.get("drawdown_paused", False),
        "drawdown_pause_reason": cfg.state.get("drawdown_pause_reason", ""),
        "drawdown_pause_ts": cfg.state.get("drawdown_pause_ts"),
        "drawdown_daily_dd_pct": cfg.state.get("drawdown_daily_dd_pct", 0.0),
        "drawdown_weekly_dd_pct": cfg.state.get("drawdown_weekly_dd_pct", 0.0),
        "drawdown_ath_dd_pct": cfg.state.get("drawdown_ath_dd_pct", 0.0),
        "drawdown_daily_peak": cfg.state.get("drawdown_daily_peak", 0.0),
        "drawdown_weekly_peak": cfg.state.get("drawdown_weekly_peak", 0.0),
        "drawdown_ath_peak": cfg.state.get("drawdown_ath_peak", 0.0),
        "regime": cfg.state.get("regime", "chop"),
        "regime_asset": cfg.state.get("regime_asset"),
        "regime_last_ts": cfg.state.get("regime_last_ts"),
        "regime_metrics": cfg.state.get("regime_metrics", {}),
        "decision_gate_last_ok": cfg.state.get("decision_gate_last_ok", True),
        "decision_gate_last_reason": cfg.state.get("decision_gate_last_reason", ""),
        "decision_gate_last_ts": cfg.state.get("decision_gate_last_ts"),
        "decision_gate_last_metrics": cfg.state.get("decision_gate_last_metrics", {}),
        "execution_last_ok": cfg.state.get("execution_last_ok", True),
        "execution_last_path": cfg.state.get("execution_last_path", ""),
        "execution_last_reason": cfg.state.get("execution_last_reason", ""),
        "execution_last_ts": cfg.state.get("execution_last_ts"),
        "equity": cfg.state.get("equity"),
        "equity_raw": cfg.state.get("equity_raw"),
        "sim_base_equity": cfg.state.get("sim_base_equity"),
        "sim_realized_pnl": cfg.state.get("sim_realized_pnl"),
        "mode": cfg.state.get("mode"),
        "aggr_target": cfg.state.get("aggr_target"),
        "safe_target": cfg.state.get("safe_target"),
        "parked": cfg.state.get("parked"),
        "new_alts": cfg.state.get("new_alts", [])[:8],
        "basket_size": len(cfg.state.get("basket", [])),
        "price_count": len(cfg.state.get("price", {})),
        "open_trades_count": len(open_trades),
        "top_prices": top_prices,
        "top_histories": top_histories,
        "open_trades": open_trades,
        "rumors_summary": cfg.state.get("rumors_summary", "none"),
        "whale_summary": cfg.state.get("whale_summary", "none"),
        "whale_flow": cfg.state.get("whale_flow", {}),
        "rumor_headlines": rumor_headlines,
        "last_decision": cfg.state.get("last_decision"),
        "last_decision_asset": cfg.state.get("last_decision_asset"),
        "last_decision_reason": cfg.state.get("last_decision_reason"),
        "last_decision_ts": cfg.state.get("last_decision_ts"),
        "recent_returns": cfg.state.get("recent_returns", []),
        "equity_momentum_7d_return_pct": cfg.state.get("equity_momentum_7d_return_pct", 0.0),
        "focus_asset": focus_asset,
        "focus_price_history": cfg.state.get("price_history", {}).get(focus_asset, [])[-120:] if focus_asset else [],
    }


async def serve(host="0.0.0.0", port=None):
    if port is None:
        port = cfg.PORT
    if websockets is None:
        cfg.logger.warning("Console WS disabled: install `websockets` to enable dashboard stream")
        while True:
            await asyncio.sleep(3600)

    async def handler(websocket):
        try:
            while True:
                await websocket.send(json.dumps(_snapshot()))
                await asyncio.sleep(1)
        except websockets.exceptions.ConnectionClosed:
            return
        except Exception as exc:
            cfg.logger.debug("Console WS handler ended: %s", exc)

    async with websockets.serve(handler, host, port):
        cfg.logger.info("Console WS listening on ws://%s:%s", host, port)
        await asyncio.Future()