import asyncio
import json
import uuid
from pathlib import Path
import os
from datetime import datetime
import requests

from . import config as cfg
from .data_quality import evaluate_pre_grok_data_quality
from .db import get_recent_trades_for_asset, save_trade_event
from .drawdown import drawdown_entries_blocked
from .rest import rest_in_usdc
from .trade import execute


FEAR_GREED_URL = "https://api.alternative.me/fng/"
BTC_DOM_URL = "https://api.coingecko.com/api/v3/global"
COINBASE_TICKER_URL = "https://api.coinbase.com/api/v3/brokerage/products/{product_id}/ticker"
COINBASE_PRODUCT_URL = "https://api.coinbase.com/api/v3/brokerage/products/{product_id}"


def _load_system_prompt():
    try:
        return Path(cfg.SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")
    except OSError:
        return (
            "You are GrokTrader. Respond only with JSON: "
            '{"decision":"hold","asset":null,"reason":"prompt file missing"}'
        )


def _build_prompt():
    template = _load_system_prompt()
    spikes = cfg.state.get("spike_list") or []
    rumors_summary = cfg.state.get("rumors_summary") or "none"
    replacements = {
        "${total_equity:.2f}": f"${float(cfg.state.get('equity', 0)):.2f}",
        "{mode}": str(cfg.state.get("mode", "nuclear")),
        "{aggressive_sleeve:.2f}": f"{float(cfg.state.get('aggr_target', 0)):.2f}",
        "{aggressive_pct:.0f}": "10",
        "{safe_sleeve:.2f}": f"{float(cfg.state.get('safe_target', 0)):.2f}",
        "{last_rebalance or \"never\"}": str(cfg.state.get("last_rebal") or "never"),
        "{json.dumps(positions_summary, indent=None)}": json.dumps(cfg.state.get("positions", {}), separators=(",", ":")),
        "{json.dumps(price_cache_summary, indent=None)}": json.dumps(cfg.state.get("price", {}), separators=(",", ":")),
        "{spike_list or \"none\"}": json.dumps(spikes, separators=(",", ":")) if spikes else "none",
        "{rumors_summary or \"none\"}": rumors_summary,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _funding_rate_from_product_payload(payload):
    if not isinstance(payload, dict):
        return None

    for key in (
        "funding_rate",
        "current_funding_rate",
    ):
        try:
            val = payload.get(key)
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            pass

    nested_paths = [
        ("product", "future_product_details", "perpetual_details", "funding_rate"),
        ("product", "future_product_details", "current_funding_rate"),
        ("future_product_details", "perpetual_details", "funding_rate"),
        ("future_product_details", "current_funding_rate"),
    ]
    for path in nested_paths:
        node = payload
        ok = True
        for step in path:
            if not isinstance(node, dict) or step not in node:
                ok = False
                break
            node = node.get(step)
        if not ok:
            continue
        try:
            return float(node)
        except (TypeError, ValueError):
            continue
    return None


def _funding_rate_from_ticker_payload(payload):
    if not isinstance(payload, dict):
        return None
    try:
        value = payload.get("funding_rate")
        if value is not None:
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _http_get_json(url, timeout_sec):
    response = requests.get(url, timeout=timeout_sec)
    response.raise_for_status()
    return response.json()


async def _fetch_json_url(url, timeout_sec):
    return await asyncio.to_thread(_http_get_json, url, timeout_sec)


async def fetch_free_regime_signals(asset=None):
    timeout_sec = int(cfg.GROK_FREE_SIGNALS_TIMEOUT_SEC or 6)
    signals = {
        "fear_greed": 50,
        "fear_greed_class": "neutral",
        "btc_dominance": 55.0,
        "funding_rate": None,
        "funding_rate_pct": None,
        "funding_source": "unavailable",
    }

    try:
        data = await _fetch_json_url(FEAR_GREED_URL, timeout_sec)
        row = ((data or {}).get("data") or [])[0]
        signals["fear_greed"] = int(row.get("value", 50))
        signals["fear_greed_class"] = str(row.get("value_classification", "neutral")).lower()
    except Exception:
        pass

    try:
        data = await _fetch_json_url(BTC_DOM_URL, timeout_sec)
        dominance = (((data or {}).get("data") or {}).get("market_cap_percentage") or {}).get("btc")
        if dominance is not None:
            signals["btc_dominance"] = round(float(dominance), 2)
    except Exception:
        pass

    if asset:
        funding_rate = None
        try:
            data = await _fetch_json_url(COINBASE_TICKER_URL.format(product_id=asset), timeout_sec)
            funding_rate = _funding_rate_from_ticker_payload(data or {})
            if funding_rate is not None:
                signals["funding_source"] = "coinbase_ticker"
        except Exception:
            pass

        if funding_rate is None:
            try:
                data = await _fetch_json_url(COINBASE_PRODUCT_URL.format(product_id=asset), timeout_sec)
                funding_rate = _funding_rate_from_product_payload(data or {})
                if funding_rate is not None:
                    signals["funding_source"] = "coinbase_product"
            except Exception:
                pass

        if funding_rate is not None:
            signals["funding_rate"] = funding_rate
            signals["funding_rate_pct"] = float(funding_rate) * 100.0

    return signals


def _funding_filter_block_reason(decision, signals):
    if not bool(cfg.GROK_FUNDING_FILTER_ENABLED):
        return None
    if decision not in ("open_long", "open_short"):
        return None

    funding_pct = signals.get("funding_rate_pct")
    try:
        funding_pct = float(funding_pct)
    except (TypeError, ValueError):
        return None

    long_block = float(cfg.GROK_FUNDING_BLOCK_LONG_PCT or 0.08)
    short_block = float(cfg.GROK_FUNDING_BLOCK_SHORT_PCT or -0.08)

    if decision == "open_long" and funding_pct > long_block:
        return f"long_blocked_funding>{long_block:.4f}% (current={funding_pct:.4f}%)"
    if decision == "open_short" and funding_pct < short_block:
        return f"short_blocked_funding<{short_block:.4f}% (current={funding_pct:.4f}%)"
    return None


def _extract_json_payload(raw_text):
    text = (raw_text or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty response", text, 0)
    if text.startswith("{") and text.endswith("}"):
        return json.loads(text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    return json.loads(text[start : end + 1])


def _normalize_decision(payload):
    decision = {
        "decision": str(payload.get("decision", "hold")).strip().lower(),
        "asset": payload.get("asset"),
        "leverage": payload.get("leverage"),
        "contracts": payload.get("contracts"),
        "stop": payload.get("stop"),
        "take_profit": payload.get("take_profit"),
        "trailing_activation_pct": payload.get("trailing_activation_pct"),
        "trailing_pct": payload.get("trailing_pct"),
        "expected_hold_min": payload.get("expected_hold_min"),
        "pump_score": payload.get("pump_score", 0),
        "sleeve": str(payload.get("sleeve", "any")).strip().lower(),
        "reason": str(payload.get("reason", "")).strip()[:240],
    }
    if decision["sleeve"] not in {"aggressive", "safe", "any"}:
        decision["sleeve"] = "any"
    return decision


def _normalize_critique(payload):
    flaws = payload.get("major_flaws")
    if not isinstance(flaws, list):
        flaws = []

    revised = payload.get("revised_decision")
    if not isinstance(revised, dict):
        revised = None

    try:
        conviction = int(payload.get("conviction", 0))
    except (TypeError, ValueError):
        conviction = 0

    return {
        "approve": bool(payload.get("approve", False)),
        "conviction": max(0, min(100, conviction)),
        "major_flaws": [str(item)[:120] for item in flaws if str(item).strip()],
        "notes": str(payload.get("notes", "")).strip()[:240],
        "revised_decision": revised,
    }


def _asset_context(asset):
    if not asset:
        return {}
    vol = (cfg.state.get("vol_cache", {}).get(asset) or {})
    mtf = (cfg.state.get("mtf_cache", {}).get(asset) or {})
    micro = (cfg.state.get("micro_cache", {}).get(asset) or {})
    atr_1h = _safe_float(vol.get("atr_1h"), 0.0)
    atr_6h = _safe_float(vol.get("atr_6h"), 0.0)
    price = _safe_float((cfg.state.get("price") or {}).get(asset), 0.0)
    vol_spike = (atr_1h / atr_6h) if atr_6h > 0 else None
    return {
        "asset": asset,
        "price": price,
        "atr_1h": atr_1h,
        "atr_6h": atr_6h,
        "vol_spike": round(vol_spike, 4) if vol_spike is not None else None,
        "regime": cfg.state.get("regime", "chop"),
        "ema20_1h": mtf.get("ema20_1h"),
        "ema20_4h": mtf.get("ema20_4h"),
        "ema20_1h_dist_pct": mtf.get("ema20_1h_dist_pct"),
        "ema20_4h_dist_pct": mtf.get("ema20_4h_dist_pct"),
        "above_ema20_1h": mtf.get("above_ema20_1h"),
        "above_ema20_4h": mtf.get("above_ema20_4h"),
        "mtf_alignment": mtf.get("alignment"),
        "ob_imbalance": micro.get("ob_imbalance"),
        "bid_depth_usd": micro.get("bid_depth_usd"),
        "ask_depth_usd": micro.get("ask_depth_usd"),
        "tape_delta_usd": micro.get("tape_delta_usd"),
        "tape_delta_pct": micro.get("tape_delta_pct"),
        "tape_large_trades": micro.get("tape_large_trades"),
        "tape_samples": micro.get("tape_samples"),
    }


def _equity_momentum_profile():
    returns = cfg.state.get("recent_returns") or []
    try:
        returns = [float(item) for item in returns]
    except (TypeError, ValueError):
        returns = []

    returns = returns[-14:]
    seven_day_return_pct = float(sum(returns[-7:]) * 100.0) if returns else 0.0
    cfg.state["equity_momentum_7d_return_pct"] = round(seven_day_return_pct, 4)

    profile = {
        "recent_returns": [round(item, 6) for item in returns],
        "seven_day_return_pct": round(seven_day_return_pct, 4),
        "risk_pct_override": None,
        "min_conviction": int(cfg.GROK_MIN_CRITIQUE_CONVICTION or 78),
        "rule": "normal",
    }

    if seven_day_return_pct <= -8.0:
        profile["risk_pct_override"] = 0.04
        profile["min_conviction"] = max(profile["min_conviction"], 85)
        profile["rule"] = "defensive"
    elif seven_day_return_pct >= 15.0:
        profile["risk_pct_override"] = 0.15
        profile["min_conviction"] = max(profile["min_conviction"], 78)
        profile["rule"] = "offensive"

    return profile


def _whale_flow_for_asset(asset):
    flow = (cfg.state.get("whale_flow") or {}).get(asset or "") or {}
    if not isinstance(flow, dict):
        return {"bias": "none", "conviction_adj": 0}
    try:
        adj = int(flow.get("conviction_adj", 0))
    except (TypeError, ValueError):
        adj = 0
    return {
        "bias": str(flow.get("bias") or "none"),
        "conviction_adj": adj,
        "accumulation": int(flow.get("accumulation", 0) or 0),
        "distribution": int(flow.get("distribution", 0) or 0),
        "samples": flow.get("samples", [])[:2],
    }


def _recent_trade_memory(assets, limit_per_asset):
    memory = {}
    for asset in assets:
        rows = get_recent_trades_for_asset(asset, limit=limit_per_asset)
        if not rows:
            continue
        compact = []
        for row in rows:
            compact.append(
                {
                    "ts": row.get("ts"),
                    "pnl": round(_safe_float(row.get("pnl"), 0.0), 4),
                    "side": row.get("side"),
                    "reason": row.get("reason", "")[:80],
                }
            )
        memory[asset] = compact
    return memory


async def _grok_json_call(prompt, temperature=0.2):
    response = await cfg.grok.chat.completions.create(
        model=cfg.GROK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    raw_content = response.choices[0].message.content
    return _extract_json_payload(raw_content)


async def grok_decision():
    cfg.reload_hot_config()
    cfg.state["parked"] = os.path.exists(cfg.PARK_FLAG)
    if cfg.state["parked"]:
        cfg.logger.info("Decision cycle skipped: bot is parked")
        return

    if drawdown_entries_blocked():
        cfg.logger.warning(
            "Decision cycle skipped: drawdown pause active (%s)",
            cfg.state.get("drawdown_pause_reason") or "limit breached",
        )
        await rest_in_usdc()
        return

    gate = evaluate_pre_grok_data_quality()
    cfg.state["data_quality_last_check_ts"] = datetime.utcnow().isoformat()
    if not gate.get("ok", False):
        payload = {
            "reason": gate.get("reason"),
            "details": gate.get("details", {}),
            "action": "hold_rest_usdc",
        }
        cfg.state["data_quality_last_ok"] = False
        cfg.state["data_quality_last_reason"] = json.dumps(payload, separators=(",", ":"))[:240]
        cfg.state["last_decision"] = "hold"
        cfg.state["last_decision_asset"] = None
        cfg.state["last_decision_reason"] = f"data_quality_gate:{gate.get('reason')}"[:180]
        cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
        cfg.logger.warning("Data-quality gate rejected decision cycle: %s", json.dumps(payload, separators=(",", ":")))
        await rest_in_usdc()
        return

    cfg.state["data_quality_last_ok"] = True
    cfg.state["data_quality_last_reason"] = "ok"

    if cfg.grok is None:
        cfg.logger.warning("Grok client unavailable; skipping decision cycle")
        return

    prompt_base = _build_prompt()
    cfg.logger.info("Decision cycle started (model=%s)", cfg.GROK_MODEL)

    try:
        momentum = _equity_momentum_profile()
        focus_assets = list(cfg.state.get("basket", []))[:8]
        recent_memory = _recent_trade_memory(focus_assets, int(cfg.GROK_CONTEXT_RECENT_TRADES or 12))
        proposal_signals = await fetch_free_regime_signals(None)
        mtf_focus = {asset: (cfg.state.get("mtf_cache", {}).get(asset) or {}) for asset in focus_assets}
        micro_focus = {asset: (cfg.state.get("micro_cache", {}).get(asset) or {}) for asset in focus_assets}

        proposal_prompt = (
            f"{prompt_base}\n\n"
            "Use the context below and output STRICT JSON only (no markdown).\n"
            "Schema: {\"decision\":\"open_long|open_short|close|hold\",\"asset\":string|null,\"leverage\":int,\"contracts\":number|null,\"stop\":number|null,\"take_profit\":number|null,\"trailing_activation_pct\":number|null,\"trailing_pct\":number|null,\"expected_hold_min\":int|null,\"pump_score\":int,\"sleeve\":\"aggressive|safe|any\",\"reason\":string}.\n"
            f"Regime signals: {json.dumps(proposal_signals, separators=(',', ':'))}\n"
            f"Equity curve momentum (last 14 days): {json.dumps(momentum.get('recent_returns', []), separators=(',', ':'))}\n"
            "Momentum rules:\n"
            "- If 7-day return < -8%, force max risk 4% and only highest conviction (>=85).\n"
            "- If 7-day return > +15%, allow up to 15% risk only on exceptional setups.\n"
            "- Otherwise keep normal risk rules.\n"
            f"Current momentum profile: {json.dumps(momentum, separators=(',', ':'))}\n"
            f"Whale flow last 4h: {str(cfg.state.get('whale_summary') or 'none')[:700]}\n"
            "Whale rules: if whale accumulation on chosen asset, conviction +20. If distribution/large sell, conviction -30 or reject.\n"
            "Pyramiding policy: you may allow pyramiding up to 2 adds only if price reaches >=1.5R, conviction remains >=80, and total exposure after add stays <=18% equity.\n"
            f"Multi-timeframe (1h/4h) EMA20 alignment for top assets: {json.dumps(mtf_focus, separators=(',', ':'))}\n"
            f"Order book + tape microstructure (top assets): {json.dumps(micro_focus, separators=(',', ':'))}\n"
            "Microstructure rules: if microstructure fields are null/missing, treat as unavailable (do NOT reject solely for missing micro data). If microstructure strongly conflicts with direction, lower conviction heavily or choose hold.\n"
            "MTF tolerance: if 1h is above EMA20 but 4h is within ~0.25% below EMA20 (see ema20_4h_dist_pct), you may proceed only with reduced conviction and leverage <= 3.\n"
            f"Recent trade memory (top assets): {json.dumps(recent_memory, separators=(',', ':'))}\n"
            f"Live prices: {json.dumps(cfg.state.get('price', {}), separators=(',', ':'))}\n"
        )

        decision = _normalize_decision(await _grok_json_call(proposal_prompt, temperature=0.25))
        asset = decision.get("asset")
        asset_signals = await fetch_free_regime_signals(asset)
        cfg.state["last_regime_signals"] = asset_signals

        critique = {
            "approve": True,
            "conviction": 100,
            "major_flaws": [],
            "notes": "critique_disabled",
            "revised_decision": None,
        }

        if bool(cfg.GROK_SELF_CRITIQUE_ENABLED):
            exact_memory = get_recent_trades_for_asset(asset, limit=int(cfg.GROK_CONTEXT_RECENT_TRADES or 12)) if asset else []
            asset_ctx = _asset_context(asset)

            critique_prompt = (
                "You are the senior risk manager. Critique this proposed trade ruthlessly.\n"
                "Output STRICT JSON only (no markdown).\n"
                "Schema: {\"approve\":bool,\"conviction\":0-100,\"major_flaws\":[string],\"notes\":string,\"revised_decision\":object|null}.\n"
                "Set approve=true only if edge is clear, risk is justified, and no major flaws remain.\n"
                f"Minimum required conviction: {int(momentum.get('min_conviction', cfg.GROK_MIN_CRITIQUE_CONVICTION or 78))}\n"
                f"Momentum profile: {json.dumps(momentum, separators=(',', ':'))}\n"
                f"Funding rate for {asset or 'asset'}: {asset_signals.get('funding_rate_pct')}%\n"
                f"Funding rules: never open long if funding > +{float(cfg.GROK_FUNDING_BLOCK_LONG_PCT or 0.08):.4f}%; never open short if funding < {float(cfg.GROK_FUNDING_BLOCK_SHORT_PCT or -0.08):.4f}%; strong bias against fighting funding direction.\n"
                f"Whale flow last 4h: {str(cfg.state.get('whale_summary') or 'none')[:700]}\n"
                "Whale rules: accumulation on this asset => conviction +20; distribution/large sell => conviction -30 or reject.\n"
                "Pyramiding policy check: adds are valid only if >=1.5R, conviction>=80, max 2 adds, and exposure after add <=18% equity.\n"
                "Microstructure rules: if microstructure fields are null/missing, treat as unavailable (do NOT reject solely for missing micro data). Conflicting microstructure should reduce conviction materially.\n"
                "MTF tolerance: allow only limited exceptions when 4h is within ~0.25% below EMA20, and require leverage <= 3.\n"
                f"Proposal: {json.dumps(decision, separators=(',', ':'))}\n"
                f"Asset context: {json.dumps(asset_ctx, separators=(',', ':'))}\n"
                f"Exact recent trades on this asset: {json.dumps(exact_memory, separators=(',', ':'))}\n"
                f"Regime signals: {json.dumps(asset_signals, separators=(',', ':'))}\n"
                f"Rumors summary: {str(cfg.state.get('rumors_summary') or 'none')[:600]}\n"
            )
            critique = _normalize_critique(await _grok_json_call(critique_prompt, temperature=0.15))

            whale_asset = decision.get("asset")
            whale_info = _whale_flow_for_asset(whale_asset)
            adjusted_conviction = int(critique.get("conviction", 0)) + int(whale_info.get("conviction_adj", 0))
            critique["conviction_raw"] = int(critique.get("conviction", 0))
            critique["whale_conviction_adj"] = int(whale_info.get("conviction_adj", 0))
            critique["conviction"] = max(0, min(100, adjusted_conviction))
            critique["whale_bias"] = whale_info.get("bias")
            if whale_info.get("samples"):
                critique["whale_samples"] = whale_info.get("samples")

            cfg.state["last_critique"] = critique

            has_major_flaws = len(critique.get("major_flaws", [])) > 0
            conviction_ok = int(critique.get("conviction", 0)) >= int(momentum.get("min_conviction", cfg.GROK_MIN_CRITIQUE_CONVICTION or 78))
            whale_reject = decision.get("decision") in ("open_long", "open_short") and critique.get("whale_bias") == "distribution"
            if not critique.get("approve", False) or has_major_flaws or not conviction_ok:
                cfg.logger.warning(
                    "Decision rejected by critique: approve=%s conviction=%s flaws=%s",
                    critique.get("approve"),
                    critique.get("conviction"),
                    ", ".join(critique.get("major_flaws", []))[:180],
                )
                await rest_in_usdc()
                cfg.state["last_decision"] = "hold"
                cfg.state["last_decision_asset"] = asset
                cfg.state["last_decision_reason"] = (
                    f"critique_reject:{int(critique.get('conviction', 0))}:{';'.join(critique.get('major_flaws', []))}"
                )[:180]
                cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
                return
            if whale_reject:
                cfg.logger.warning("Decision rejected by whale flow: distribution bias on %s", decision.get("asset"))
                await rest_in_usdc()
                cfg.state["last_decision"] = "hold"
                cfg.state["last_decision_asset"] = decision.get("asset")
                cfg.state["last_decision_reason"] = "whale_flow_distribution_reject"
                cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
                return

            revised = critique.get("revised_decision")
            if isinstance(revised, dict) and revised:
                decision = _normalize_decision(revised)

        if decision.get("decision") in ("open_long", "open_short"):
            decision["conviction"] = int(critique.get("conviction", 0))
            risk_override = momentum.get("risk_pct_override")
            if risk_override is not None:
                if momentum.get("rule") == "offensive" and int(critique.get("conviction", 0)) < 90:
                    risk_override = None
                if risk_override is not None:
                    decision["risk_pct_override"] = float(risk_override)

        funding_block = _funding_filter_block_reason(decision.get("decision"), asset_signals)
        if funding_block:
            cfg.logger.warning("Decision rejected by funding filter: %s", funding_block)
            await rest_in_usdc()
            cfg.state["last_decision"] = "hold"
            cfg.state["last_decision_asset"] = asset
            cfg.state["last_decision_reason"] = f"funding_filter:{funding_block}"[:180]
            cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
            save_trade_event(
                event_id=str(uuid.uuid4()),
                ts=cfg.state["last_decision_ts"],
                event_type="decision_rejected_funding",
                decision_id=None,
                trade_id=None,
                asset=asset,
                payload={
                    "decision": decision.get("decision"),
                    "funding_rate_pct": asset_signals.get("funding_rate_pct"),
                    "reason": funding_block,
                },
            )
            return

        decision_id = str(uuid.uuid4())
        decision["decision_id"] = decision_id
        cfg.state["last_decision"] = str(decision.get("decision") or "n/a")
        cfg.state["last_decision_asset"] = decision.get("asset")
        cfg.state["last_decision_reason"] = (
            f"{str(decision.get('reason') or '')[:120]} | conv={int(critique.get('conviction', 100))}"
        )[:180]
        cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
        cfg.logger.info(
            "Decision received: decision=%s asset=%s sleeve=%s pump_score=%s",
            decision.get("decision"),
            decision.get("asset"),
            decision.get("sleeve"),
            decision.get("pump_score"),
        )
        save_trade_event(
            event_id=str(uuid.uuid4()),
            ts=cfg.state["last_decision_ts"],
            event_type="decision_received",
            decision_id=decision_id,
            trade_id=None,
            asset=decision.get("asset"),
            payload={
                "decision": decision.get("decision"),
                "asset": decision.get("asset"),
                "sleeve": decision.get("sleeve"),
                "pump_score": decision.get("pump_score"),
                "reason": decision.get("reason"),
                "critique_conviction": critique.get("conviction"),
                "critique_major_flaws": critique.get("major_flaws"),
                "equity_momentum_7d_return_pct": momentum.get("seven_day_return_pct"),
                "risk_pct_override": decision.get("risk_pct_override"),
                "funding_rate_pct": asset_signals.get("funding_rate_pct"),
                "whale_summary": str(cfg.state.get("whale_summary") or "none")[:700],
                "whale_bias": critique.get("whale_bias"),
                "whale_conviction_adj": critique.get("whale_conviction_adj"),
            },
        )
        if decision.get("decision") == "hold":
            await rest_in_usdc()
        elif decision.get("decision") in ("open_long", "open_short", "close"):
            await execute(decision)
        else:
            await rest_in_usdc()
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        cfg.state["last_decision"] = "error"
        cfg.state["last_decision_asset"] = None
        cfg.state["last_decision_reason"] = str(exc)[:180]
        cfg.state["last_decision_ts"] = datetime.utcnow().isoformat()
        cfg.logger.error("Grok error: %s", exc)
        await rest_in_usdc()


async def decision_loop():
    while True:
        await grok_decision()
        await asyncio.sleep(cfg.DECISION_INTERVAL_SEC if not cfg.state["parked"] else cfg.PARKED_DECISION_INTERVAL_SEC)


async def grok_loop():
    await decision_loop()