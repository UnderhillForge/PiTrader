from datetime import datetime

from . import config as cfg


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _select_asset():
    configured = str(cfg.REGIME_CLASSIFIER_ASSET or "").strip()
    if configured and configured in cfg.state.get("price_history", {}):
        return configured
    basket = list(cfg.state.get("basket", []))
    if basket:
        return basket[0]
    return configured or "BTC-PERP-INTX"


def classify_regime():
    asset = _select_asset()
    history = (cfg.state.get("price_history", {}).get(asset) or [])[-int(cfg.REGIME_LOOKBACK_POINTS):]

    if len(history) < 20:
        cfg.state["regime"] = "chop"
        cfg.state["regime_asset"] = asset
        cfg.state["regime_last_ts"] = datetime.utcnow().isoformat()
        cfg.state["regime_metrics"] = {"reason": "insufficient_history", "samples": len(history)}
        return cfg.state["regime"]

    first = _safe_float(history[0], 0.0)
    last = _safe_float(history[-1], 0.0)
    if first <= 0 or last <= 0:
        cfg.state["regime"] = "chop"
        cfg.state["regime_asset"] = asset
        cfg.state["regime_last_ts"] = datetime.utcnow().isoformat()
        cfg.state["regime_metrics"] = {"reason": "invalid_prices", "samples": len(history)}
        return cfg.state["regime"]

    total_return_pct = ((last - first) / first) * 100.0

    step_returns = []
    for idx in range(1, len(history)):
        prev = _safe_float(history[idx - 1], 0.0)
        cur = _safe_float(history[idx], 0.0)
        if prev <= 0 or cur <= 0:
            continue
        step_returns.append(abs((cur - prev) / prev) * 100.0)

    noise_pct = (sum(step_returns) / len(step_returns)) if step_returns else 0.0

    atr_6h = (cfg.state.get("vol_cache", {}).get(asset) or {}).get("atr_6h")
    atr_pct = (_safe_float(atr_6h, 0.0) / last) * 100.0 if last > 0 else 0.0

    if atr_pct >= float(cfg.REGIME_HIGH_VOL_ATR_PCT):
        regime = "high_vol"
    else:
        trend_threshold = float(cfg.REGIME_TREND_RET_PCT)
        noise_ratio = float(cfg.REGIME_TREND_NOISE_RATIO)
        trend_signal = abs(total_return_pct) >= trend_threshold and abs(total_return_pct) >= (noise_pct * noise_ratio)
        regime = "trend" if trend_signal else "chop"

    cfg.state["regime"] = regime
    cfg.state["regime_asset"] = asset
    cfg.state["regime_last_ts"] = datetime.utcnow().isoformat()
    cfg.state["regime_metrics"] = {
        "total_return_pct": round(total_return_pct, 4),
        "noise_pct": round(noise_pct, 4),
        "atr_pct": round(atr_pct, 4),
        "samples": len(history),
    }
    return regime


def get_regime_profile():
    regime = str(cfg.state.get("regime") or "chop")
    if regime == "trend":
        return {
            "regime": "trend",
            "risk_mult": float(cfg.REGIME_RISK_MULT_TREND),
            "leverage_cap": int(cfg.REGIME_LEV_CAP_TREND),
            "min_rr_add": float(cfg.REGIME_MIN_RR_ADD_TREND),
        }
    if regime == "high_vol":
        return {
            "regime": "high_vol",
            "risk_mult": float(cfg.REGIME_RISK_MULT_HIGH_VOL),
            "leverage_cap": int(cfg.REGIME_LEV_CAP_HIGH_VOL),
            "min_rr_add": float(cfg.REGIME_MIN_RR_ADD_HIGH_VOL),
        }
    return {
        "regime": "chop",
        "risk_mult": float(cfg.REGIME_RISK_MULT_CHOP),
        "leverage_cap": int(cfg.REGIME_LEV_CAP_CHOP),
        "min_rr_add": float(cfg.REGIME_MIN_RR_ADD_CHOP),
    }
