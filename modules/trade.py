import uuid
import asyncio
from datetime import datetime

from . import config as cfg
from .config import DRY_RUN_ORDERS, MAX_LEV, RISK_NUCLEAR, RISK_SAFE, SAFE_ASSETS, SIMULATION_MODE, client, logger, state
from .drawdown import drawdown_entries_blocked
from .db import delete_live_trade, load_live_trades, save_live_trade, save_trade_event, save_trade_journal
from .health import entries_blocked, get_health_state, mark_exchange_failure, mark_exchange_success
from .regime import get_regime_profile
from .risk import derive_stop_take_profit


def _infer_close_side(position, raw_size):
    side_raw = str(position.get("side", "")).strip().lower()
    if side_raw in ("long", "buy"):
        return "SELL"
    if side_raw in ("short", "sell"):
        return "BUY"
    return "SELL" if raw_size > 0 else "BUY"


def _close_side_from_open(open_side):
    return "SELL" if open_side == "BUY" else "BUY"


def _format_base_size(value):
    try:
        size = abs(float(value))
    except (TypeError, ValueError):
        size = 0.0
    return f"{size:.8f}"


def _format_limit_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        price = 0.0
    return f"{max(price, 0.0):.8f}"


def _exec_limit_price(reference_price, side, offset_pct, maker):
    try:
        ref = float(reference_price)
    except (TypeError, ValueError):
        ref = 0.0
    if ref <= 0:
        return 0.0

    offset = max(0.0, float(offset_pct or 0.0)) / 100.0
    if maker:
        return ref * (1.0 - offset) if side == "BUY" else ref * (1.0 + offset)
    return ref * (1.0 + offset) if side == "BUY" else ref * (1.0 - offset)


def _record_execution_result(ok, path, reason=""):
    state["execution_last_ok"] = bool(ok)
    state["execution_last_path"] = str(path or "")
    state["execution_last_reason"] = str(reason or "")[:180]
    state["execution_last_ts"] = datetime.utcnow().isoformat()


def _evaluate_market_guard(asset, size):
    cache = (state.get("exec_liq_cache", {}) or {}).get(asset) or {}
    spread_pct = cache.get("spread_pct")
    mid = cache.get("mid")
    volume_1m = cache.get("volume_1m")

    try:
        spread_pct = float(spread_pct)
    except (TypeError, ValueError):
        spread_pct = None
    try:
        mid = float(mid)
    except (TypeError, ValueError):
        mid = None
    try:
        volume_1m = float(volume_1m)
    except (TypeError, ValueError):
        volume_1m = None

    try:
        order_size = abs(float(size))
    except (TypeError, ValueError):
        order_size = 0.0

    max_spread = float(cfg.EXEC_MARKET_GUARD_MAX_SPREAD_PCT or 0.35)
    max_size_to_vol_pct = float(cfg.EXEC_MARKET_GUARD_MAX_SIZE_TO_VOL1M_PCT or 0.5)

    reasons = []
    if spread_pct is None or mid is None or mid <= 0:
        reasons.append("spread_unavailable")
    elif spread_pct > max_spread:
        reasons.append(f"spread>{max_spread:.2f}%")

    size_to_vol_pct = None
    if volume_1m is None or volume_1m <= 0:
        reasons.append("vol1m_unavailable")
    else:
        size_to_vol_pct = (order_size / max(volume_1m, 1e-12)) * 100.0
        if size_to_vol_pct > max_size_to_vol_pct:
            reasons.append(f"size_to_vol1m>{max_size_to_vol_pct:.2f}%")

    return {
        "ok": len(reasons) == 0,
        "spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
        "max_spread_pct": max_spread,
        "size_to_vol1m_pct": round(size_to_vol_pct, 6) if size_to_vol_pct is not None else None,
        "max_size_to_vol1m_pct": max_size_to_vol_pct,
        "vol1m": round(volume_1m, 8) if volume_1m is not None else None,
        "reasons": reasons,
    }


def _parse_iso_ts(ts_text):
    if not ts_text:
        return None
    try:
        return datetime.fromisoformat(str(ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _age_seconds(ts_text):
    parsed = _parse_iso_ts(ts_text)
    if parsed is None:
        return None
    return max(0.0, (datetime.utcnow() - parsed).total_seconds())


def _execution_micro_gate(asset, side, size, price):
    cache = (state.get("exec_liq_cache", {}) or {}).get(asset) or {}
    micro = (state.get("micro_cache", {}) or {}).get(asset) or {}

    spread_pct = cache.get("spread_pct")
    try:
        spread_pct = float(spread_pct) if spread_pct is not None else None
    except (TypeError, ValueError):
        spread_pct = None

    try:
        notional = abs(float(size)) * float(price)
    except (TypeError, ValueError):
        notional = 0.0

    hard_reasons = []
    soft_penalty = 0
    soft_notes = []

    # Hard safety: extreme spread (execution risk), and extremely thin visible depth.
    try:
        base_spread_cap = float(getattr(cfg, "EXEC_MARKET_GUARD_MAX_SPREAD_PCT", 0.35) or 0.35)
    except (TypeError, ValueError):
        base_spread_cap = 0.35
    hard_spread_cap = max(0.80, base_spread_cap * 2.5)
    if spread_pct is not None and spread_pct > hard_spread_cap:
        hard_reasons.append(f"spread>{hard_spread_cap:.2f}%")

    bid_depth = micro.get("bid_depth_usd")
    ask_depth = micro.get("ask_depth_usd")
    try:
        bid_depth = float(bid_depth) if bid_depth is not None else None
    except (TypeError, ValueError):
        bid_depth = None
    try:
        ask_depth = float(ask_depth) if ask_depth is not None else None
    except (TypeError, ValueError):
        ask_depth = None

    total_depth = None
    if bid_depth is not None or ask_depth is not None:
        total_depth = float(bid_depth or 0.0) + float(ask_depth or 0.0)

    # Require at least ~1.2x the intended notional within top-of-book levels, with a small absolute floor.
    min_depth = max(10_000.0, float(notional) * 1.2)
    if total_depth is not None and total_depth > 0 and total_depth < min_depth:
        hard_reasons.append(f"depth<{min_depth:.0f}")

    # Soft edge-quality: book/tape conflict => conviction penalty (do not hard-block).
    ob_imbalance = micro.get("ob_imbalance")
    try:
        ob_imbalance = float(ob_imbalance) if ob_imbalance is not None else None
    except (TypeError, ValueError):
        ob_imbalance = None

    tape_delta_pct = micro.get("tape_delta_pct")
    try:
        tape_delta_pct = float(tape_delta_pct) if tape_delta_pct is not None else None
    except (TypeError, ValueError):
        tape_delta_pct = None

    side = str(side or "").upper()
    if side == "BUY":
        if ob_imbalance is not None:
            if ob_imbalance < 0.45:
                soft_penalty += 20
                soft_notes.append("ob_imbalance_strongly_bearish")
            elif ob_imbalance < 0.50:
                soft_penalty += 10
                soft_notes.append("ob_imbalance_bearish")
        if tape_delta_pct is not None:
            if tape_delta_pct < -10.0:
                soft_penalty += 20
                soft_notes.append("tape_strong_sell")
            elif tape_delta_pct < 0.0:
                soft_penalty += 10
                soft_notes.append("tape_sell")
    elif side == "SELL":
        if ob_imbalance is not None:
            if ob_imbalance > 0.55:
                soft_penalty += 20
                soft_notes.append("ob_imbalance_strongly_bullish")
            elif ob_imbalance > 0.50:
                soft_penalty += 10
                soft_notes.append("ob_imbalance_bullish")
        if tape_delta_pct is not None:
            if tape_delta_pct > 10.0:
                soft_penalty += 20
                soft_notes.append("tape_strong_buy")
            elif tape_delta_pct > 0.0:
                soft_penalty += 10
                soft_notes.append("tape_buy")

    soft_penalty = int(max(0, min(40, soft_penalty)))

    return {
        "ok": len(hard_reasons) == 0,
        "hard_reasons": hard_reasons,
        "soft_penalty_conviction": soft_penalty,
        "soft_notes": soft_notes[:6],
        "metrics": {
            "spread_pct": round(spread_pct, 6) if spread_pct is not None else None,
            "hard_spread_cap_pct": hard_spread_cap,
            "bid_depth_usd": round(bid_depth, 2) if bid_depth is not None else None,
            "ask_depth_usd": round(ask_depth, 2) if ask_depth is not None else None,
            "total_depth_usd": round(total_depth, 2) if total_depth is not None else None,
            "min_depth_usd": round(min_depth, 2),
            "ob_imbalance": round(ob_imbalance, 4) if ob_imbalance is not None else None,
            "tape_delta_pct": round(tape_delta_pct, 2) if tape_delta_pct is not None else None,
            "tape_delta_usd": micro.get("tape_delta_usd"),
            "tape_large_trades": micro.get("tape_large_trades"),
            "tape_source": micro.get("tape_source"),
            "cache_ts": micro.get("ts"),
            "cache_age_sec": round(_age_seconds(micro.get("ts")) or 0.0, 3) if micro.get("ts") else None,
        },
    }


def _submit_live_order(asset, side, size, reference_price=None, leverage=None):
    if client is None:
        return {"ok": False, "path": "none", "reason": "client_unavailable"}

    base_size = _format_base_size(size)
    errors = []
    leverage_text = None
    if leverage is not None:
        try:
            leverage_text = str(int(leverage))
        except (TypeError, ValueError):
            leverage_text = None

    if cfg.EXEC_POST_ONLY_ENABLED:
        post_only_px = _exec_limit_price(reference_price, side, cfg.EXEC_POST_ONLY_OFFSET_PCT, maker=True)
        if post_only_px > 0:
            try:
                kwargs = {}
                if leverage_text is not None:
                    kwargs["leverage"] = leverage_text
                client.limit_order_gtc(
                    client_order_id=str(uuid.uuid4()),
                    product_id=asset,
                    side=side,
                    base_size=base_size,
                    limit_price=_format_limit_price(post_only_px),
                    post_only=True,
                    **kwargs,
                )
                return {
                    "ok": True,
                    "path": "post_only",
                    "reason": "ok",
                    "limit_price": post_only_px,
                }
            except Exception as exc:
                errors.append(f"post_only:{exc}")

    if cfg.EXEC_IOC_FALLBACK_ENABLED:
        ioc_px = _exec_limit_price(reference_price, side, cfg.EXEC_IOC_SLIPPAGE_PCT, maker=False)
        if ioc_px > 0:
            try:
                kwargs = {}
                if leverage_text is not None:
                    kwargs["leverage"] = leverage_text
                client.limit_order_ioc(
                    client_order_id=str(uuid.uuid4()),
                    product_id=asset,
                    side=side,
                    base_size=base_size,
                    limit_price=_format_limit_price(ioc_px),
                    **kwargs,
                )
                return {
                    "ok": True,
                    "path": "ioc",
                    "reason": "ok",
                    "limit_price": ioc_px,
                }
            except Exception as exc:
                errors.append(f"ioc:{exc}")

    if cfg.EXEC_MARKET_FALLBACK_ENABLED:
        guard = _evaluate_market_guard(asset, size)
        if not guard.get("ok"):
            reason = ", ".join(guard.get("reasons") or ["market_guard_failed"])
            logger.warning(
                "Market fallback rejected for %s (%s). spread=%.4f%% size/vol1m=%.4f%%",
                asset,
                reason,
                float(guard.get("spread_pct") or 0.0),
                float(guard.get("size_to_vol1m_pct") or 0.0),
            )
            errors.append(f"market_guard:{reason}")

            if cfg.EXEC_MARKET_GUARD_LIMIT_RETRY_ENABLED:
                retry_px = _exec_limit_price(
                    reference_price,
                    side,
                    cfg.EXEC_MARKET_GUARD_RETRY_IOC_SLIPPAGE_PCT,
                    maker=False,
                )
                if retry_px > 0:
                    try:
                        kwargs = {}
                        if leverage_text is not None:
                            kwargs["leverage"] = leverage_text
                        client.limit_order_ioc(
                            client_order_id=str(uuid.uuid4()),
                            product_id=asset,
                            side=side,
                            base_size=base_size,
                            limit_price=_format_limit_price(retry_px),
                            **kwargs,
                        )
                        return {
                            "ok": True,
                            "path": "limit_retry_ioc",
                            "reason": f"market_guard_reject:{reason}",
                            "limit_price": retry_px,
                        }
                    except Exception as exc:
                        errors.append(f"limit_retry_ioc:{exc}")
            reason_detail = "; ".join(errors)[-400:] if errors else "market_guard_rejected"
            return {"ok": False, "path": "rejected", "reason": reason_detail}

        try:
            kwargs = {}
            if leverage_text is not None:
                kwargs["leverage"] = leverage_text
            client.market_order(
                client_order_id=str(uuid.uuid4()),
                product_id=asset,
                side=side,
                base_size=base_size,
                **kwargs,
            )
            return {"ok": True, "path": "market", "reason": "ok"}
        except TypeError:
            try:
                client.market_order(asset, side, float(size))
                return {"ok": True, "path": "market", "reason": "ok"}
            except Exception as exc:
                errors.append(f"market:{exc}")
        except Exception as exc:
            errors.append(f"market:{exc}")

    reason = "; ".join(errors)[-400:] if errors else "order_path_exhausted"
    return {"ok": False, "path": "failed", "reason": reason}


def _sanitize_leverage(value):
    try:
        lev = int(value)
    except (TypeError, ValueError):
        lev = 6
    return max(1, min(MAX_LEV, lev))


def _sanitize_risk_pct(value):
    try:
        risk = float(value)
    except (TypeError, ValueError):
        return None
    if risk <= 0:
        return None
    return min(1.0, risk)


def _sanitize_conviction(value):
    try:
        conviction = int(value)
    except (TypeError, ValueError):
        conviction = 0
    return max(0, min(100, conviction))


def _is_valid_asset(asset):
    if not isinstance(asset, str):
        return False
    return asset.endswith("-PERP-INTX") or asset.endswith("-USD") or asset.endswith("-USDC")


def _is_perp(asset):
    return isinstance(asset, str) and asset.endswith("-PERP-INTX")


def _asset_base(asset):
    if not isinstance(asset, str) or "-" not in asset:
        return ""
    return asset.split("-", 1)[0].upper()


def _emit_trade_event(event_type, decision_id=None, trade_id=None, asset=None, payload=None):
    try:
        save_trade_event(
            event_id=str(uuid.uuid4()),
            ts=datetime.utcnow().isoformat(),
            event_type=event_type,
            decision_id=decision_id,
            trade_id=trade_id,
            asset=asset,
            payload=payload or {},
        )
    except Exception as exc:
        logger.debug("Trade event emit failed (%s): %s", event_type, exc)


def _calc_rr(decision, entry, stop, take_profit):
    if stop is None or take_profit is None:
        return None
    try:
        entry = float(entry)
        stop = float(stop)
        take_profit = float(take_profit)
    except (TypeError, ValueError):
        return None

    if decision == "open_long":
        risk = entry - stop
        reward = take_profit - entry
    else:
        risk = stop - entry
        reward = entry - take_profit

    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def _calc_rr_now(side, entry, stop, current_price):
    try:
        entry = float(entry)
        stop = float(stop)
        current_price = float(current_price)
    except (TypeError, ValueError):
        return None

    if side == "BUY":
        risk = entry - stop
        reward = current_price - entry
    else:
        risk = stop - entry
        reward = entry - current_price

    if risk <= 0:
        return None
    return reward / risk


def _vol_spike_ratio(asset):
    metrics = (state.get("vol_cache", {}).get(asset) or {})
    try:
        atr_1h = float(metrics.get("atr_1h") or 0.0)
        atr_6h = float(metrics.get("atr_6h") or 0.0)
    except (TypeError, ValueError):
        return None
    if atr_1h <= 0 or atr_6h <= 0:
        return None
    return atr_1h / max(atr_6h, 1e-12)


def _evaluate_entry_gate(decision, asset, sleeve, pump_score, entry, stop, take_profit, regime_profile):
    rr = _calc_rr(decision, entry, stop, take_profit)
    min_rr_base = float(cfg.DECISION_MIN_RR_SAFE if sleeve == "safe" else cfg.DECISION_MIN_RR_AGGRESSIVE)
    min_rr = min_rr_base + float(regime_profile.get("min_rr_add", 0.0))
    vol_spike = _vol_spike_ratio(asset)
    min_pump = int(cfg.DECISION_MIN_PUMP_SCORE or 0)
    min_vol_spike = float(cfg.DECISION_MIN_VOL_SPIKE or 0.0)

    reasons = []
    if int(pump_score or 0) < min_pump:
        reasons.append(f"pump_score<{min_pump}")
    if vol_spike is None:
        reasons.append("vol_spike_unavailable")
    elif vol_spike < min_vol_spike:
        reasons.append(f"vol_spike<{min_vol_spike:.2f}")
    if rr is None:
        reasons.append("rr_unavailable")
    elif rr < min_rr:
        reasons.append(f"rr<{min_rr:.2f}")

    return {
        "ok": len(reasons) == 0,
        "rr": rr,
        "min_rr": min_rr,
        "pump_score": int(pump_score or 0),
        "min_pump_score": min_pump,
        "vol_spike": round(vol_spike, 4) if vol_spike is not None else None,
        "min_vol_spike": min_vol_spike,
        "regime": regime_profile.get("regime", "chop"),
        "reasons": reasons,
    }


def _hours_since_started():
    started_at = state.get("started_at")
    if not started_at:
        return 0.0
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return 0.0
    return max(0.0, (datetime.utcnow() - started).total_seconds() / 3600.0)


def _readiness_ready():
    required_hours = float(cfg.READINESS_HOURS or 0)
    if required_hours <= 0:
        state["ready_to_trade"] = True
        return True
    ready = _hours_since_started() >= required_hours
    state["ready_to_trade"] = ready
    return ready


def _is_stop_hit(side, stop, current_price):
    if stop is None:
        return False
    if side == "BUY":
        return current_price <= stop
    return current_price >= stop


def _is_take_profit_hit(side, take_profit, current_price):
    if take_profit is None:
        return False
    if side == "BUY":
        return current_price >= take_profit
    return current_price <= take_profit


def _update_trailing_stop(trade, current_price):
    trailing_pct = trade.get("trailing_pct")
    if trailing_pct is None:
        return
    trailing_ratio = float(trailing_pct) / 100.0
    if trailing_ratio <= 0:
        return

    side = trade["side"]
    best_price = trade.get("best_price")
    if best_price is None:
        best_price = current_price

    if side == "BUY":
        best_price = max(best_price, current_price)
        trail_stop = best_price * (1.0 - trailing_ratio)
        stop = trade.get("stop")
        trade["stop"] = max(stop, trail_stop) if stop is not None else trail_stop
    else:
        best_price = min(best_price, current_price)
        trail_stop = best_price * (1.0 + trailing_ratio)
        stop = trade.get("stop")
        trade["stop"] = min(stop, trail_stop) if stop is not None else trail_stop
    trade["best_price"] = best_price


async def _auto_close_after(trade_id, asset, open_side, size, hold_minutes):
    try:
        await asyncio.sleep(max(1, int(hold_minutes)) * 60)
        trade = state["trades"].get(trade_id)
        if trade and trade.get("status") == "open":
            remaining = float(trade.get("remaining_size", 0) or 0)
            if remaining > 0:
                _close_trade_full(trade_id, "pump_timer")
                logger.info("Auto-closed pump trade %s on %s after %s min", trade_id, asset, hold_minutes)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.error("Auto-close failed for %s: %s", asset, exc)
    finally:
        state["timers"].pop(trade_id, None)


def _close_size(asset, open_side, size):
    if DRY_RUN_ORDERS:
        close_side = _close_side_from_open(open_side)
        logger.info("DRY_RUN_ORDERS: simulated close %s %s %.8f", close_side, asset, size)
        _record_execution_result(True, "dry_run", "simulated")
        return True
    if client is None:
        _record_execution_result(False, "none", "client_unavailable")
        return False
    if size <= 0:
        _record_execution_result(False, "none", "invalid_size")
        return False
    close_side = _close_side_from_open(open_side)
    reference_price = state.get("price", {}).get(asset)
    result = _submit_live_order(
        asset=asset,
        side=close_side,
        size=size,
        reference_price=reference_price,
        leverage=None,
    )
    _record_execution_result(result.get("ok"), result.get("path"), result.get("reason"))
    if result.get("ok"):
        logger.info("Close execution path for %s: %s", asset, result.get("path"))
        return True
    logger.warning("Close order failed for %s: %s", asset, result.get("reason"))
    return False


def _estimate_realized_pnl(open_side, entry, exit_price, size):
    try:
        entry = float(entry)
        exit_price = float(exit_price)
        size = float(size)
    except (TypeError, ValueError):
        return 0.0
    if open_side == "BUY":
        return (exit_price - entry) * size
    return (entry - exit_price) * size


def _clamp(value, low, high):
    return max(low, min(high, value))


def _sim_slippage_pct(asset, reference_price, pump_score):
    min_pct = float(cfg.SIM_SLIPPAGE_MIN_PCT or 0.0)
    max_pct = float(cfg.SIM_SLIPPAGE_MAX_PCT or min_pct)
    if max_pct < min_pct:
        max_pct = min_pct

    atr_key = "atr_1h" if int(pump_score or 0) >= 60 else "atr_6h"
    atr_value = (state.get("vol_cache", {}).get(asset) or {}).get(atr_key)

    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0.0

    if reference_price > 0 and atr_value is not None:
        try:
            atr_pct = (float(atr_value) / reference_price) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            atr_pct = 0.0
        scaled = min_pct + (atr_pct * float(cfg.SIM_SLIPPAGE_ATR_MULT or 0.0))
    else:
        scaled = (min_pct + max_pct) / 2.0

    return _clamp(float(scaled), min_pct, max_pct)


def _apply_slippage_to_price(reference_price, side, slippage_pct):
    try:
        px = float(reference_price)
        slip = max(0.0, float(slippage_pct)) / 100.0
    except (TypeError, ValueError):
        return float(reference_price or 0.0)

    if side == "BUY":
        return px * (1.0 + slip)
    return px * (1.0 - slip)


def _estimate_taker_fee(fill_price, size):
    try:
        notional = abs(float(fill_price) * float(size))
    except (TypeError, ValueError):
        return 0.0
    return notional * float(cfg.SIM_TAKER_FEE_RATE or 0.0)


def _estimate_funding_drag(trade, mark_price, size):
    if not SIMULATION_MODE:
        return 0.0
    rate_per_8h = float(cfg.SIM_FUNDING_RATE_PER_8H or 0.0)
    if rate_per_8h <= 0:
        return 0.0

    now = datetime.utcnow()
    last_ts_text = trade.get("funding_last_ts") or trade.get("ts")
    try:
        last_ts = datetime.fromisoformat(str(last_ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        trade["funding_last_ts"] = now.isoformat()
        return 0.0

    elapsed_hours = max(0.0, (now - last_ts).total_seconds() / 3600.0)
    if elapsed_hours <= 0:
        return 0.0

    try:
        notional = abs(float(size) * float(mark_price))
    except (TypeError, ValueError):
        notional = 0.0
    if notional <= 0:
        trade["funding_last_ts"] = now.isoformat()
        return 0.0

    cost = notional * rate_per_8h * (elapsed_hours / 8.0)
    trade["funding_last_ts"] = now.isoformat()
    return max(0.0, cost)


def _apply_sim_realized_pnl(delta):
    if not SIMULATION_MODE:
        return
    try:
        change = float(delta)
    except (TypeError, ValueError):
        return

    state["sim_realized_pnl"] = float(state.get("sim_realized_pnl", 0.0) or 0.0) + change
    base_equity = float(state.get("sim_base_equity", state.get("equity", 0.0)) or 0.0)
    state["equity"] = max(0.0, base_equity + float(state["sim_realized_pnl"]))
    if state.get("mode") == "nuclear":
        state["aggr_target"] = float(state["equity"])
        state["safe_target"] = 0.0


def _close_trade_partial(trade_id, fraction, reason):
    trade = state["trades"].get(trade_id)
    if not trade or trade.get("status") != "open":
        return False

    close_size = round(trade["initial_size"] * fraction, 8)
    close_size = min(close_size, trade["remaining_size"])
    if close_size <= 0:
        return False
    if not _close_size(trade["asset"], trade["side"], close_size):
        return False

    market_price = state["price"].get(trade["asset"], trade.get("entry"))
    close_side = _close_side_from_open(trade.get("side"))
    slippage_pct = _sim_slippage_pct(trade.get("asset"), market_price, trade.get("pump_score", 0)) if SIMULATION_MODE else 0.0
    exec_price = _apply_slippage_to_price(market_price, close_side, slippage_pct) if SIMULATION_MODE else float(market_price)

    gross_realized = _estimate_realized_pnl(trade["side"], trade.get("entry"), exec_price, close_size)
    fee_cost = _estimate_taker_fee(exec_price, close_size) if SIMULATION_MODE else 0.0
    funding_cost = _estimate_funding_drag(trade, market_price, close_size)
    net_realized = gross_realized - fee_cost - funding_cost

    trade["realized_gross_pnl"] = float(trade.get("realized_gross_pnl", 0.0)) + gross_realized
    trade["realized_fees"] = float(trade.get("realized_fees", 0.0)) + fee_cost
    trade["realized_funding"] = float(trade.get("realized_funding", 0.0)) + funding_cost
    trade["realized_pnl"] = float(trade.get("realized_pnl", 0.0)) + net_realized
    _apply_sim_realized_pnl(net_realized)

    trade["remaining_size"] = round(trade["remaining_size"] - close_size, 8)
    save_live_trade(trade_id, trade)
    logger.info(
        "Partial close %s %.8f (%s) gross=%.4f net=%.4f fee=%.4f funding=%.4f slip=%.3f%%",
        trade["asset"],
        close_size,
        reason,
        gross_realized,
        net_realized,
        fee_cost,
        funding_cost,
        slippage_pct,
    )
    _emit_trade_event(
        event_type="partial_close",
        decision_id=trade.get("decision_id"),
        trade_id=trade_id,
        asset=trade.get("asset"),
        payload={
            "reason": reason,
            "close_size": close_size,
            "remaining_size": trade.get("remaining_size"),
            "gross_pnl": gross_realized,
            "fee_cost": fee_cost,
            "funding_cost": funding_cost,
            "net_pnl": net_realized,
        },
    )
    return True


def _try_pyramid_add(trade_id, trade, current_price, rr_now, trigger):
    if not bool(getattr(cfg, "PYRAMID_ENABLED", True)):
        return False
    add_count = int(trade.get("pyramid_add_count", 0) or 0)
    max_adds = max(0, int(getattr(cfg, "PYRAMID_MAX_ADDS", 2) or 2))
    if add_count >= max_adds:
        return False

    base_trigger = float(getattr(cfg, "PYRAMID_RR_TRIGGER", 1.5) or 1.5)
    required_rr = base_trigger + (0.5 * add_count)
    if rr_now is None or float(rr_now) < required_rr:
        return False

    conviction = _sanitize_conviction(trade.get("conviction"))
    min_conviction = _sanitize_conviction(getattr(cfg, "PYRAMID_MIN_CONVICTION", 80))
    if conviction < min_conviction:
        logger.info("Pyramid skipped %s: conviction %d < %d", trade.get("asset"), conviction, min_conviction)
        return False

    try:
        price = float(current_price)
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False

    base_size = float(trade.get("pyramid_base_size") or trade.get("initial_size") or trade.get("size") or 0.0)
    add_fraction = max(0.0, float(getattr(cfg, "PYRAMID_ADD_FRACTION", 0.30) or 0.30))
    add_size = round(base_size * add_fraction, 8)
    if add_size <= 0:
        return False

    try:
        equity = float(state.get("equity") or 0.0)
    except (TypeError, ValueError):
        equity = 0.0
    max_exposure_pct = max(0.01, float(getattr(cfg, "PYRAMID_MAX_EXPOSURE_PCT", 0.18) or 0.18))
    max_notional = equity * max_exposure_pct
    current_notional = max(0.0, float(trade.get("remaining_size") or 0.0) * price)
    add_notional = add_size * price
    if max_notional > 0 and (current_notional + add_notional) > max_notional:
        logger.info(
            "Pyramid skipped %s: exposure cap %.4f%% exceeded (current=%.2f add=%.2f cap=%.2f)",
            trade.get("asset"),
            max_exposure_pct * 100.0,
            current_notional,
            add_notional,
            max_notional,
        )
        return False

    side = str(trade.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return False

    if DRY_RUN_ORDERS:
        logger.info("DRY_RUN_ORDERS: simulated pyramid add %s %s %.8f", side, trade.get("asset"), add_size)
        _record_execution_result(True, "dry_run", "simulated_pyramid")
        execution_path = "dry_run"
    elif client is None:
        logger.warning("Pyramid skipped: Coinbase client unavailable")
        _record_execution_result(False, "none", "client_unavailable")
        return False
    else:
        try:
            result = _submit_live_order(
                asset=trade.get("asset"),
                side=side,
                size=add_size,
                reference_price=price,
                leverage=trade.get("leverage") if _is_perp(trade.get("asset")) else None,
            )
            if not result.get("ok"):
                raise RuntimeError(result.get("reason") or "pyramid_order_failed")
            mark_exchange_success("order_open")
            _record_execution_result(True, result.get("path"), result.get("reason"))
            execution_path = str(result.get("path") or "unknown")
        except Exception as exc:
            mark_exchange_failure("order_open", exc)
            _record_execution_result(False, "failed", str(exc))
            logger.warning("Pyramid add failed for %s: %s", trade.get("asset"), exc)
            return False

    trade["size"] = round(float(trade.get("size") or 0.0) + add_size, 8)
    trade["remaining_size"] = round(float(trade.get("remaining_size") or 0.0) + add_size, 8)
    trade["pyramid_add_count"] = add_count + 1
    trade["pyramid_last_rr"] = float(rr_now)
    trade["pyramid_last_ts"] = datetime.utcnow().isoformat()

    exposure_after_notional = current_notional + add_notional
    exposure_after_pct = (exposure_after_notional / max(equity, 1e-12)) * 100.0 if equity > 0 else None
    save_live_trade(trade_id, trade)
    _emit_trade_event(
        event_type="trade_pyramided",
        decision_id=trade.get("decision_id"),
        trade_id=trade_id,
        asset=trade.get("asset"),
        payload={
            "add_size": add_size,
            "add_count": trade.get("pyramid_add_count"),
            "rr_now": float(rr_now),
            "conviction": conviction,
            "trigger": str(trigger or "rr").strip()[:60],
            "execution_path": execution_path,
            "exposure_after_pct": round(exposure_after_pct, 6) if exposure_after_pct is not None else None,
            "max_exposure_pct": max_exposure_pct * 100.0,
        },
    )
    logger.info(
        "Pyramided %s add=%.8f count=%d rr=%.2f conv=%d exposure=%.2f%%",
        trade.get("asset"),
        add_size,
        int(trade.get("pyramid_add_count") or 0),
        float(rr_now),
        conviction,
        float(exposure_after_pct or 0.0),
    )
    return True


def _close_trade_full(trade_id, reason, decision_id=None):
    trade = state["trades"].get(trade_id)
    if not trade or trade.get("status") != "open":
        return

    if decision_id is None:
        decision_id = trade.get("decision_id")

    remaining = trade.get("remaining_size", 0)
    market_price = state["price"].get(trade.get("asset"), trade.get("entry"))
    net_realized = 0.0
    journal_exit_price = market_price
    if remaining > 0 and _close_size(trade["asset"], trade["side"], remaining):
        close_side = _close_side_from_open(trade.get("side"))
        slippage_pct = _sim_slippage_pct(trade.get("asset"), market_price, trade.get("pump_score", 0)) if SIMULATION_MODE else 0.0
        exec_price = _apply_slippage_to_price(market_price, close_side, slippage_pct) if SIMULATION_MODE else float(market_price)
        journal_exit_price = exec_price

        gross_realized = _estimate_realized_pnl(trade["side"], trade.get("entry"), exec_price, remaining)
        fee_cost = _estimate_taker_fee(exec_price, remaining) if SIMULATION_MODE else 0.0
        funding_cost = _estimate_funding_drag(trade, market_price, remaining)
        net_realized = gross_realized - fee_cost - funding_cost

        trade["realized_gross_pnl"] = float(trade.get("realized_gross_pnl", 0.0)) + gross_realized
        trade["realized_fees"] = float(trade.get("realized_fees", 0.0)) + fee_cost
        trade["realized_funding"] = float(trade.get("realized_funding", 0.0)) + funding_cost
        trade["realized_pnl"] = float(trade.get("realized_pnl", 0.0)) + net_realized
        trade["remaining_size"] = 0.0
        _apply_sim_realized_pnl(net_realized)

        logger.info(
            "Final close %s %.8f (%s) gross=%.4f net=%.4f fee=%.4f funding=%.4f slip=%.3f%%",
            trade.get("asset"),
            remaining,
            reason,
            gross_realized,
            net_realized,
            fee_cost,
            funding_cost,
            slippage_pct,
        )

    trade["status"] = "closed"
    trade["closed_ts"] = datetime.utcnow().isoformat()
    trade["close_reason"] = reason

    reason_event_map = {
        "stop": "stop_hit",
        "take_profit": "tp_hit",
        "pump_timer": "timer_exit",
        "pump_timer_recovery": "timer_exit",
    }
    reason_event_type = reason_event_map.get(reason)
    if reason_event_type is not None:
        _emit_trade_event(
            event_type=reason_event_type,
            decision_id=decision_id,
            trade_id=trade_id,
            asset=trade.get("asset"),
            payload={
                "reason": reason,
                "net_pnl": float(trade.get("realized_pnl", 0.0)),
            },
        )

    save_trade_journal(
        trade_id=trade_id,
        ts=trade.get("closed_ts"),
        asset=trade.get("asset"),
        side=trade.get("side"),
        size=trade.get("initial_size", trade.get("size", 0)),
        entry=trade.get("entry"),
        exit_price=journal_exit_price,
        pnl=float(trade.get("realized_pnl", 0.0)),
        pnl_gross=float(trade.get("realized_gross_pnl", 0.0)),
        fee_cost=float(trade.get("realized_fees", 0.0)),
        funding_cost=float(trade.get("realized_funding", 0.0)),
        reason=reason,
    )

    _emit_trade_event(
        event_type="close_settled",
        decision_id=decision_id,
        trade_id=trade_id,
        asset=trade.get("asset"),
        payload={
            "reason": reason,
            "entry": trade.get("entry"),
            "exit": journal_exit_price,
            "size": trade.get("initial_size", trade.get("size", 0)),
            "gross_pnl": float(trade.get("realized_gross_pnl", 0.0)),
            "fee_cost": float(trade.get("realized_fees", 0.0)),
            "funding_cost": float(trade.get("realized_funding", 0.0)),
            "net_pnl": float(trade.get("realized_pnl", 0.0)),
        },
    )
    delete_live_trade(trade_id)
    logger.info("Closed trade %s (%s)", trade_id, reason)


def _process_open_trade(trade_id, trade):
    asset = trade.get("asset")
    current_price = state["price"].get(asset)
    if current_price is None:
        return
    current_price = float(current_price)

    if SIMULATION_MODE:
        funding_cost = _estimate_funding_drag(trade, current_price, trade.get("remaining_size", 0))
        if funding_cost > 0:
            trade["realized_funding"] = float(trade.get("realized_funding", 0.0)) + funding_cost
            trade["realized_pnl"] = float(trade.get("realized_pnl", 0.0)) - funding_cost
            _apply_sim_realized_pnl(-funding_cost)

    side = trade.get("side")
    entry = trade.get("entry")
    rr_now = _calc_rr_now(side, entry, trade.get("stop"), current_price)

    if trade.get("pump_score", 0) >= 60:
        if trade.get("expected_hold_min"):
            pass

    activation_pct = trade.get("trailing_activation_pct")
    if activation_pct is not None and entry:
        try:
            activation_ratio = float(activation_pct) / 100.0
            entry = float(entry)
            if side == "BUY":
                move = (current_price - entry) / entry
            else:
                move = (entry - current_price) / entry
            if move >= activation_ratio:
                _update_trailing_stop(trade, current_price)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    pyramiding_triggered = False
    if rr_now is not None and not trade.get("partial_15") and rr_now >= 1.5:
        if _close_trade_partial(trade_id, 0.50, "1.5R"):
            trade["partial_15"] = True
            pyramiding_triggered = True
    if rr_now is not None and not trade.get("partial_30") and rr_now >= 3.0:
        if _close_trade_partial(trade_id, 0.30, "3.0R"):
            trade["partial_30"] = True
            pyramiding_triggered = True

    if rr_now is not None and rr_now >= float(getattr(cfg, "PYRAMID_RR_TRIGGER", 1.5) or 1.5):
        _try_pyramid_add(
            trade_id,
            trade,
            current_price,
            rr_now,
            trigger="partial_close" if pyramiding_triggered else "rr_cross",
        )

    if _is_stop_hit(side, trade.get("stop"), current_price):
        _close_trade_full(trade_id, "stop", decision_id=trade.get("decision_id"))
        return

    if _is_take_profit_hit(side, trade.get("take_profit"), current_price):
        _close_trade_full(trade_id, "take_profit", decision_id=trade.get("decision_id"))
        return

    if trade.get("remaining_size", 0) <= 0:
        trade["status"] = "closed"
        trade["closed_ts"] = datetime.utcnow().isoformat()
        trade["close_reason"] = "fully_partialed"
        save_trade_journal(
            trade_id=trade_id,
            ts=trade.get("closed_ts"),
            asset=trade.get("asset"),
            side=trade.get("side"),
            size=trade.get("initial_size", trade.get("size", 0)),
            entry=trade.get("entry"),
            exit_price=state["price"].get(trade.get("asset"), trade.get("entry")),
            pnl=float(trade.get("realized_pnl", 0.0)),
            pnl_gross=float(trade.get("realized_gross_pnl", 0.0)),
            fee_cost=float(trade.get("realized_fees", 0.0)),
            funding_cost=float(trade.get("realized_funding", 0.0)),
            reason="fully_partialed",
        )
        _emit_trade_event(
            event_type="close_settled",
            decision_id=trade.get("decision_id"),
            trade_id=trade_id,
            asset=trade.get("asset"),
            payload={
                "reason": "fully_partialed",
                "entry": trade.get("entry"),
                "exit": state["price"].get(trade.get("asset"), trade.get("entry")),
                "size": trade.get("initial_size", trade.get("size", 0)),
                "gross_pnl": float(trade.get("realized_gross_pnl", 0.0)),
                "fee_cost": float(trade.get("realized_fees", 0.0)),
                "funding_cost": float(trade.get("realized_funding", 0.0)),
                "net_pnl": float(trade.get("realized_pnl", 0.0)),
            },
        )
        delete_live_trade(trade_id)
        return

    save_live_trade(trade_id, trade)


def _restore_timer_if_needed(trade_id, trade):
    if trade.get("status") != "open":
        return
    if int(trade.get("pump_score", 0) or 0) < 60:
        return
    hold_minutes = trade.get("expected_hold_min")
    if hold_minutes is None:
        return

    try:
        hold_minutes = int(hold_minutes)
    except (TypeError, ValueError):
        return
    hold_minutes = max(5, min(90, hold_minutes))

    ts_text = trade.get("ts")
    if not ts_text:
        return

    try:
        opened = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00"))
    except ValueError:
        return

    elapsed_seconds = (datetime.utcnow() - opened.replace(tzinfo=None)).total_seconds()
    remaining_seconds = (hold_minutes * 60) - max(0, int(elapsed_seconds))
    if remaining_seconds <= 0:
        _close_trade_full(trade_id, "pump_timer_recovery")
        return

    remaining_minutes = max(1, int((remaining_seconds + 59) // 60))
    task = asyncio.create_task(
        _auto_close_after(
            trade_id,
            trade.get("asset"),
            trade.get("side"),
            trade.get("remaining_size", 0),
            remaining_minutes,
        )
    )
    state["timers"][trade_id] = task


def recover_open_trades():
    restored = load_live_trades()
    for trade_id, trade in restored.items():
        if not isinstance(trade, dict):
            continue
        if trade.get("status") != "open":
            delete_live_trade(trade_id)
            continue
        state["trades"][trade_id] = trade
        _restore_timer_if_needed(trade_id, trade)
    if restored:
        logger.info("Recovered %d persisted open trades", len(state["trades"]))


async def monitor_trades_loop():
    while True:
        for trade_id, trade in list(state["trades"].items()):
            if trade.get("status") != "open":
                continue
            try:
                _process_open_trade(trade_id, trade)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.error("Trade monitor error for %s: %s", trade_id, exc)
        await asyncio.sleep(5)


def force_close_all_open_trades(reason="outage_flatten"):
    closed = 0
    for trade_id, trade in list(state["trades"].items()):
        if trade.get("status") != "open":
            continue
        _close_trade_full(trade_id, reason)
        closed += 1
    if closed > 0:
        logger.warning("Forced close of %d open trade(s) due to %s", closed, reason)


async def execute(dec):
    decision = str(dec.get("decision", "")).strip().lower()
    asset = dec.get("asset")
    decision_id = dec.get("decision_id")

    if decision not in {"open_long", "open_short", "close"}:
        return
    if not asset:
        logger.warning("Trade skipped: missing asset in decision %s", dec)
        return
    if not _is_valid_asset(asset):
        logger.warning("Trade skipped: invalid asset format %s", asset)
        return

    try:
        if decision == "close":
            _emit_trade_event(
                event_type="trade_close_requested",
                decision_id=decision_id,
                trade_id=None,
                asset=asset,
                payload={"decision": decision, "asset": asset},
            )
            for trade_id, trade in list(state["trades"].items()):
                if trade.get("status") == "open" and trade.get("asset") == asset:
                    _close_trade_full(trade_id, "manual_close", decision_id=decision_id)

            position = state["positions"].get(asset, {})
            raw_size = float(position.get("size", 0))
            size = abs(raw_size)
            if size <= 0:
                logger.info("No open size to close for %s", asset)
                return
            side = _infer_close_side(position, raw_size)
            if DRY_RUN_ORDERS:
                logger.info("DRY_RUN_ORDERS: simulated close %s %s %.8f", side, asset, size)
                _record_execution_result(True, "dry_run", "simulated")
                return
            if client is None:
                logger.warning("Close skipped: Coinbase client unavailable")
                _record_execution_result(False, "none", "client_unavailable")
                return
            try:
                result = _submit_live_order(
                    asset=asset,
                    side=side,
                    size=size,
                    reference_price=state.get("price", {}).get(asset),
                    leverage=None,
                )
                if not result.get("ok"):
                    raise RuntimeError(result.get("reason") or "close_order_failed")
                mark_exchange_success("order_close")
                _record_execution_result(True, result.get("path"), result.get("reason"))
            except Exception as exc:
                mark_exchange_failure("order_close", exc)
                _record_execution_result(False, "failed", str(exc))
                logger.warning("Close failed for %s: %s", asset, exc)
                return
            logger.info("Closed %s size %.8f via %s", asset, size, state.get("execution_last_path") or "unknown")
            return

        if decision in {"open_long", "open_short"} and entries_blocked():
            logger.warning("Trade skipped: health gate active (state=%s)", get_health_state())
            return

        if decision in {"open_long", "open_short"} and drawdown_entries_blocked():
            logger.warning(
                "Trade skipped: drawdown pause active (%s)",
                state.get("drawdown_pause_reason") or "limit breached",
            )
            return

        if decision in {"open_long", "open_short"} and not _readiness_ready():
            logger.info(
                "Trade skipped: readiness gate active (%.2fh / %.2fh)",
                _hours_since_started(),
                float(cfg.READINESS_HOURS or 0),
            )
            return

        if decision == "open_short" and not _is_perp(asset):
            logger.info("Trade skipped: short not supported for non-perpetual product %s", asset)
            return

        price = dec.get("price") or state["price"].get(asset)
        if price is None:
            logger.warning("Trade skipped: missing price for %s", asset)
            return
        price = float(price)

        sleeve = str(dec.get("sleeve", "any")).lower()
        if sleeve not in {"aggressive", "safe", "any"}:
            sleeve = "any"
        if state.get("mode") == "hybrid" and sleeve == "any":
            sleeve = "aggressive"

        pump_score = int(dec.get("pump_score", 0) or 0)
        if sleeve == "safe":
            base = _asset_base(asset)
            safe_base_ok = base in {"BTC", "ETH", "SOL"}
            if not (asset in SAFE_ASSETS or (safe_base_ok and (asset.endswith("-USD") or asset.endswith("-USDC")))):
                logger.warning("Trade skipped: safe sleeve asset not allowed (%s)", asset)
                return
            if pump_score >= 60:
                logger.warning("Trade skipped: pump trades are not allowed in safe sleeve")
                return

        regime_profile = get_regime_profile()
        regime = regime_profile["regime"]

        leverage = _sanitize_leverage(dec.get("leverage"))

        # Extra live safety for minimal-money testing: cap leverage until equity is meaningfully funded.
        if not SIMULATION_MODE:
            try:
                equity_now = float(state.get("equity") or 0.0)
            except (TypeError, ValueError):
                equity_now = 0.0
            if equity_now > 0 and equity_now < 100.0:
                leverage = min(leverage, 3)
        leverage_cap = int(regime_profile.get("leverage_cap", leverage))
        if leverage > leverage_cap:
            logger.info("Leverage adjusted by regime (%s): %dx -> %dx", regime, leverage, leverage_cap)
            leverage = leverage_cap
        atr_key = "atr_1h" if pump_score >= 60 else "atr_6h"
        atr_value = (state.get("vol_cache", {}).get(asset) or {}).get(atr_key)
        if dec.get("stop") is None or dec.get("take_profit") is None:
            calc_stop, calc_take_profit = derive_stop_take_profit(decision, price, atr_value, pump_score)
            if dec.get("stop") is None:
                dec["stop"] = calc_stop
            if dec.get("take_profit") is None:
                dec["take_profit"] = calc_take_profit

        if dec.get("trailing_activation_pct") is None:
            dec["trailing_activation_pct"] = 10.0
        if dec.get("trailing_pct") is None:
            dec["trailing_pct"] = 4.0

        gate_metrics = _evaluate_entry_gate(
            decision=decision,
            asset=asset,
            sleeve=sleeve,
            pump_score=pump_score,
            entry=price,
            stop=dec.get("stop"),
            take_profit=dec.get("take_profit"),
            regime_profile=regime_profile,
        )
        rr = gate_metrics.get("rr")
        min_rr = float(gate_metrics.get("min_rr") or 0.0)
        state["decision_gate_last_ok"] = bool(gate_metrics.get("ok"))
        state["decision_gate_last_reason"] = "ok" if gate_metrics.get("ok") else ";".join(gate_metrics.get("reasons", []))
        state["decision_gate_last_ts"] = datetime.utcnow().isoformat()
        state["decision_gate_last_metrics"] = gate_metrics

        if not gate_metrics.get("ok"):
            logger.warning(
                "Trade skipped: quality gate failed (%s)",
                ", ".join(gate_metrics.get("reasons", [])) or "unknown",
            )
            _emit_trade_event(
                event_type="trade_open_rejected_quality",
                decision_id=decision_id,
                trade_id=None,
                asset=asset,
                payload={
                    "decision": decision,
                    "asset": asset,
                    "sleeve": sleeve,
                    "gate": gate_metrics,
                },
            )
            return

        risk_mult = float(regime_profile.get("risk_mult", 1.0))
        if abs(risk_mult - 1.0) > 1e-9:
            logger.info("Risk adjusted by regime (%s): %.2fx", regime, risk_mult)

        requested_contracts = dec.get("contracts")
        risk_pct_override = _sanitize_risk_pct(dec.get("risk_pct_override"))
        if requested_contracts is not None:
            try:
                size = abs(float(requested_contracts)) * risk_mult
            except (TypeError, ValueError):
                size = 0.0

            if risk_pct_override is not None and price > 0:
                if state.get("mode") == "hybrid":
                    budget = float(state.get("safe_target", 0) if sleeve == "safe" else state.get("aggr_target", 0))
                else:
                    budget = float(state.get("equity", 0))
                max_notional = budget * float(risk_pct_override)
                max_size = round(max_notional / price, 8) if max_notional > 0 else 0.0
                if max_size > 0 and size > max_size:
                    logger.info("Risk override capped size: %.8f -> %.8f (risk_pct=%.4f)", size, max_size, risk_pct_override)
                    size = max_size
        else:
            risk_pct = risk_pct_override if risk_pct_override is not None else (RISK_SAFE if sleeve == "safe" else RISK_NUCLEAR)
            if state.get("mode") == "hybrid":
                budget = float(state.get("safe_target", 0) if sleeve == "safe" else state.get("aggr_target", 0))
            else:
                budget = float(state.get("equity", 0))
            notional = budget * risk_pct * risk_mult
            size = round(notional / price, 8)

        if size <= 0:
            logger.warning("Trade skipped: non-positive size for %s", asset)
            return

        side = "BUY" if decision == "open_long" else "SELL"

        exec_gate = _execution_micro_gate(asset=asset, side=side, size=size, price=price)
        state["execution_gate_last_ok"] = bool(exec_gate.get("ok"))
        state["execution_gate_last_reason"] = "ok" if exec_gate.get("ok") else ";".join(exec_gate.get("hard_reasons") or [])
        state["execution_gate_last_ts"] = datetime.utcnow().isoformat()
        state["execution_gate_last_metrics"] = exec_gate

        if not exec_gate.get("ok"):
            logger.warning(
                "Trade skipped: execution safety gate failed (%s)",
                ", ".join(exec_gate.get("hard_reasons") or ["unknown"]),
            )
            _emit_trade_event(
                event_type="trade_open_rejected_exec_safety",
                decision_id=decision_id,
                trade_id=None,
                asset=asset,
                payload={
                    "decision": decision,
                    "asset": asset,
                    "side": side,
                    "size": size,
                    "price": price,
                    "sleeve": sleeve,
                    "exec_gate": exec_gate,
                },
            )
            return

        soft_penalty = int(exec_gate.get("soft_penalty_conviction") or 0)
        if soft_penalty > 0 and dec.get("conviction") is not None:
            try:
                dec["conviction_raw_execgate"] = int(dec.get("conviction") or 0)
            except (TypeError, ValueError):
                dec["conviction_raw_execgate"] = 0
            dec["conviction"] = max(0, int(dec["conviction_raw_execgate"]) - soft_penalty)

        _emit_trade_event(
            event_type="trade_open_requested",
            decision_id=decision_id,
            trade_id=None,
            asset=asset,
            payload={
                "decision": decision,
                "asset": asset,
                "side": side,
                "size": size,
                "price": price,
                "sleeve": sleeve,
                "pump_score": pump_score,
                "regime": regime,
                "risk_mult": risk_mult,
                "leverage_cap": leverage_cap,
                "min_rr": min_rr,
                "vol_spike": gate_metrics.get("vol_spike"),
                "min_vol_spike": gate_metrics.get("min_vol_spike"),
                "min_pump_score": gate_metrics.get("min_pump_score"),
                "exec_gate": exec_gate,
                "soft_penalty_conviction": soft_penalty,
                "conviction_after_execgate": dec.get("conviction"),
                "guard_spread_pct": (state.get("exec_liq_cache", {}).get(asset) or {}).get("spread_pct"),
                "guard_size_to_vol1m_pct": (
                    (size / max(float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0), 1e-12)) * 100.0
                    if float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0) > 0
                    else None
                ),
            },
        )
        if DRY_RUN_ORDERS:
            logger.info("DRY_RUN_ORDERS: simulated open %s %s %.8f", side, asset, size)
            _record_execution_result(True, "dry_run", "simulated")
            execution_path = "dry_run"
        elif client is None:
            logger.warning("Trade skipped: Coinbase client unavailable")
            _record_execution_result(False, "none", "client_unavailable")
            return
        else:
            try:
                result = _submit_live_order(
                    asset=asset,
                    side=side,
                    size=size,
                    reference_price=price,
                    leverage=leverage if _is_perp(asset) else None,
                )
                if not result.get("ok"):
                    raise RuntimeError(result.get("reason") or "open_order_failed")
                mark_exchange_success("order_open")
                _record_execution_result(True, result.get("path"), result.get("reason"))
                execution_path = str(result.get("path") or "unknown")
            except Exception as exc:
                mark_exchange_failure("order_open", exc)
                _record_execution_result(False, "failed", str(exc))
                logger.warning("Trade skipped: order placement failed for %s (%s)", asset, exc)
                return

        entry_reference_price = float(price)
        entry_slippage_pct = _sim_slippage_pct(asset, entry_reference_price, pump_score) if SIMULATION_MODE else 0.0
        entry_price = _apply_slippage_to_price(entry_reference_price, side, entry_slippage_pct) if SIMULATION_MODE else entry_reference_price
        open_fee = _estimate_taker_fee(entry_price, size) if SIMULATION_MODE else 0.0
        if SIMULATION_MODE and open_fee > 0:
            _apply_sim_realized_pnl(-open_fee)

        trade_id = str(dec.get("id") or uuid.uuid4())
        state["trades"][trade_id] = {
            "ts": datetime.utcnow().isoformat(),
            "asset": asset,
            "side": side,
            "size": size,
            "initial_size": size,
            "remaining_size": size,
            "status": "open",
            "entry": entry_price,
            "entry_ref_price": entry_reference_price,
            "entry_slippage_pct": entry_slippage_pct,
            "best_price": entry_price,
            "leverage": leverage,
            "stop": dec.get("stop"),
            "take_profit": dec.get("take_profit"),
            "trailing_activation_pct": dec.get("trailing_activation_pct"),
            "trailing_pct": dec.get("trailing_pct"),
            "expected_hold_min": dec.get("expected_hold_min"),
            "funding_last_ts": datetime.utcnow().isoformat(),
            "pump_score": pump_score,
            "decision_id": decision_id,
            "conviction": _sanitize_conviction(dec.get("conviction")),
            "sleeve": sleeve,
            "regime": regime,
            "rr": rr,
            "vol_spike": gate_metrics.get("vol_spike"),
            "execution_path": execution_path,
            "exec_gate_soft_penalty": soft_penalty,
            "guard_spread_pct": (state.get("exec_liq_cache", {}).get(asset) or {}).get("spread_pct"),
            "guard_size_to_vol1m_pct": (
                (size / max(float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0), 1e-12)) * 100.0
                if float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0) > 0
                else None
            ),
            "partial_15": False,
            "partial_30": False,
            "pyramid_base_size": size,
            "pyramid_add_count": 0,
            "realized_pnl": -open_fee,
            "realized_gross_pnl": 0.0,
            "realized_fees": open_fee,
            "realized_funding": 0.0,
            "reason": dec.get("reason", "grok"),
        }
        save_live_trade(trade_id, state["trades"][trade_id])
        _emit_trade_event(
            event_type="trade_opened",
            decision_id=decision_id,
            trade_id=trade_id,
            asset=asset,
            payload={
                "side": side,
                "size": size,
                "entry": entry_price,
                "entry_ref_price": entry_reference_price,
                "entry_slippage_pct": entry_slippage_pct,
                "open_fee": open_fee,
                "stop": dec.get("stop"),
                "take_profit": dec.get("take_profit"),
                "regime": regime,
                "conviction": _sanitize_conviction(dec.get("conviction")),
                "risk_mult": risk_mult,
                "leverage": leverage,
                "rr": rr,
                "vol_spike": gate_metrics.get("vol_spike"),
                "execution_path": execution_path,
                "exec_gate": exec_gate,
                "soft_penalty_conviction": soft_penalty,
                "guard_spread_pct": (state.get("exec_liq_cache", {}).get(asset) or {}).get("spread_pct"),
                "guard_size_to_vol1m_pct": (
                    (size / max(float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0), 1e-12)) * 100.0
                    if float((state.get("exec_liq_cache", {}).get(asset) or {}).get("volume_1m") or 0.0) > 0
                    else None
                ),
            },
        )
        logger.info(
            "Opened %s %s size %.8f (lev=%dx, sleeve=%s, path=%s, entry=%.6f, slip=%.3f%%, open_fee=%.4f)",
            side,
            asset,
            size,
            leverage,
            sleeve,
            execution_path,
            entry_price,
            entry_slippage_pct,
            open_fee,
        )

        hold_minutes = dec.get("expected_hold_min")
        if pump_score >= 60 and hold_minutes is not None:
            try:
                hold_minutes = int(hold_minutes)
            except (TypeError, ValueError):
                hold_minutes = None
            if hold_minutes is not None:
                hold_minutes = max(5, min(90, hold_minutes))
                timer_task = asyncio.create_task(_auto_close_after(trade_id, asset, side, size, hold_minutes))
                state["timers"][trade_id] = timer_task
    except (AttributeError, TypeError, ValueError) as exc:
        logger.error("Trade execution failed for %s: %s", asset, exc)