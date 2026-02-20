import asyncio
import time
from datetime import datetime, timedelta, timezone
import requests

try:
    import ccxt
except ImportError:
    ccxt = None

from . import config as cfg
from .health import mark_exchange_failure, mark_exchange_success
from .regime import classify_regime
from .risk import compute_atr, normalize_atr_bundle


def _read_attr(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _best_price_from_levels(levels):
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if isinstance(first, dict):
        return _to_float(first.get("price"))
    if isinstance(first, (list, tuple)) and first:
        return _to_float(first[0])
    return None


def _as_level(level):
    if isinstance(level, dict):
        return _to_float(level.get("price")), _to_float(level.get("size") or level.get("quantity"))
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _to_float(level[0]), _to_float(level[1])
    return None, None


def _depth_notional(levels, max_levels=10):
    if not isinstance(levels, list) or not levels:
        return 0.0
    total = 0.0
    for level in levels[: max(1, int(max_levels))]:
        px, sz = _as_level(level)
        if px is None or sz is None:
            continue
        if px <= 0 or sz <= 0:
            continue
        total += float(px) * float(sz)
    return float(total)


def _orderbook_metrics_from_pricebook(pricebook, max_levels=10):
    if not isinstance(pricebook, dict):
        return {}
    bids = pricebook.get("bids")
    asks = pricebook.get("asks")
    bid_notional = _depth_notional(bids, max_levels=max_levels)
    ask_notional = _depth_notional(asks, max_levels=max_levels)
    denom = bid_notional + ask_notional
    imbalance = (bid_notional / denom) if denom > 0 else None
    return {
        "bid_depth_usd": round(bid_notional, 2),
        "ask_depth_usd": round(ask_notional, 2),
        "ob_imbalance": round(float(imbalance), 4) if imbalance is not None else None,
        "levels": int(max_levels),
    }


def _spot_proxy_product(product_id: str):
    text = str(product_id or "").strip().upper()
    if not text:
        return None
    if text.endswith("-PERP-INTX"):
        base = text.split("-", 1)[0]
        if base and base not in {"USD", "USDC", "USDT"}:
            return f"{base}-USD"
        return None
    return text


def _fetch_coinbase_public_trades_sync(spot_product_id: str, limit: int):
    url = f"https://api.exchange.coinbase.com/products/{spot_product_id}/trades"
    resp = requests.get(url, timeout=10, headers={"User-Agent": "TradeBot/1.0"})
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        return []
    return payload[: max(1, int(limit))]


async def _fetch_coinbase_public_trades(spot_product_id: str, limit: int):
    try:
        return await asyncio.to_thread(_fetch_coinbase_public_trades_sync, spot_product_id, limit)
    except requests.RequestException:
        return []


def _tape_metrics(trades, large_notional_usd=50_000.0):
    buy_usd = 0.0
    sell_usd = 0.0
    large_count = 0
    samples = []
    for row in trades or []:
        try:
            price = float(row.get("price") or 0)
            size = float(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        notional = price * size
        side = str(row.get("side") or "").lower()
        if side == "sell":
            sell_usd += notional
        else:
            buy_usd += notional
        if notional >= float(large_notional_usd):
            large_count += 1
            if len(samples) < 2:
                samples.append(f"{side}:${notional:,.0f}")

    delta = buy_usd - sell_usd
    denom = buy_usd + sell_usd
    delta_pct = (delta / denom) * 100.0 if denom > 0 else None
    return {
        "tape_buy_usd": round(buy_usd, 2),
        "tape_sell_usd": round(sell_usd, 2),
        "tape_delta_usd": round(delta, 2),
        "tape_delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        "tape_large_trades": int(large_count),
        "tape_samples": samples,
    }


async def update_basket():
    if cfg.client is None:
        return

    universe = str(cfg.PRODUCT_UNIVERSE or "all").lower()
    spot_ccxt_enabled = str(getattr(cfg, "SPOT_DISCOVERY_MODE", "native") or "native").lower() == "ccxt"

    active_spot = []
    if universe in {"spot", "all"} and spot_ccxt_enabled:
        await _update_spot_universe_ccxt()
        active_spot = _next_spot_active_scan_window()

    # In CCXT spot mode, we do not depend on native spot discovery at all.
    unsupported = set((cfg.state.get("unsupported_products") or {}).keys())

    if universe == "spot" and spot_ccxt_enabled:
        max_total = 40
        new_basket = [pid for pid in list(active_spot) if pid not in unsupported][:max_total]

        previous = set(cfg.state["basket"])
        current = set(new_basket)
        if new_basket != cfg.state["basket"]:
            cfg.state["basket"] = new_basket
            cfg.state["basket_ver"] += 1
            cfg.state["new_alts"] = sorted(current - previous)
            cfg.logger.info(
                "Basket updated to %d products (ver=%d, spot_total=%d, spot_active=%d)",
                len(new_basket),
                cfg.state["basket_ver"],
                int(cfg.state.get("spot_universe_count") or 0),
                len(active_spot) if active_spot else 0,
            )
        return

    try:
        response = cfg.client.get_products()
        products = _read_attr(response, "products", [])
    except (AttributeError, TypeError, ValueError):
        return

    ranked = []
    for product in products:
        product_id = _read_attr(product, "product_id", "")
        if not product_id:
            continue

        status = str(_read_attr(product, "status", "")).lower()
        if status in {"offline", "delisted", "disabled"}:
            continue

        is_perp = product_id.endswith("-PERP-INTX")
        quote = _read_attr(product, "quote_currency_id", "") or ""
        is_spot = (product_id.endswith("-USD") or product_id.endswith("-USDC")) and quote.upper() in set(cfg.SPOT_QUOTES)

        if universe == "perp" and not is_perp:
            continue
        # In ccxt mode we do NOT rely on native spot discovery; we only pull perps here.
        if universe == "spot":
            if not is_spot:
                continue
            if spot_ccxt_enabled:
                continue

        if universe == "all":
            if not (is_perp or is_spot):
                continue
            if spot_ccxt_enabled and is_spot:
                continue


        base = (product_id.split("-", 1)[0] if "-" in product_id else "").upper()
        if not any(ch.isalpha() for ch in base):
            continue

        volume = _read_attr(product, "volume_24h", 0) or _read_attr(product, "approximate_quote_24h_volume", 0)
        try:
            score = float(volume)
        except (TypeError, ValueError):
            score = 0.0
        ranked.append((score, product_id))

    ranked.sort(reverse=True)

    # Default cap stays small because the price loop polls every asset each tick.
    max_total = 40

    if universe == "all" and spot_ccxt_enabled:
        spot_slice = [pid for pid in list(active_spot) if pid not in unsupported][: max(0, int(getattr(cfg, "SPOT_ACTIVE_SCAN_SIZE", 20) or 20))]
        perp_slots = max(10, max_total - len(spot_slice))
        perps = [product_id for _, product_id in ranked if str(product_id).endswith("-PERP-INTX")]
        if not perps:
            # Some API keys/environments do not return perp products via get_products().
            # Keep any existing perps so `PRODUCT_UNIVERSE=all` still means spot + perp.
            perps = [
                product_id
                for product_id in (cfg.state.get("basket") or [])
                if str(product_id).endswith("-PERP-INTX")
            ]
        perps = [pid for pid in perps if pid not in unsupported]
        new_basket = (perps[:perp_slots] + spot_slice)[:max_total]
    else:
        if not ranked:
            return
        new_basket = [product_id for _, product_id in ranked if product_id not in unsupported][:max_total]

    if not new_basket:
        return
    previous = set(cfg.state["basket"])
    current = set(new_basket)

    if new_basket != cfg.state["basket"]:
        cfg.state["basket"] = new_basket
        cfg.state["basket_ver"] += 1
        cfg.state["new_alts"] = sorted(current - previous)
        if universe in {"spot", "all"} and spot_ccxt_enabled:
            cfg.logger.info(
                "Basket updated to %d products (ver=%d, spot_total=%d, spot_active=%d)",
                len(new_basket),
                cfg.state["basket_ver"],
                int(cfg.state.get("spot_universe_count") or 0),
                len(active_spot) if active_spot else 0,
            )
        else:
            cfg.logger.info("Basket updated to %d products (ver=%d)", len(new_basket), cfg.state["basket_ver"])


def _is_stable_base(base: str) -> bool:
    base = str(base or "").upper().strip()
    return base in {"USD", "USDC", "USDT", "DAI", "TUSD", "FDUSD", "BUSD"}


def _ccxt_volume_usd(ticker: dict) -> float:
    if not isinstance(ticker, dict):
        return 0.0
    for key in ("quoteVolume", "quote_volume", "volumeQuote"):
        val = ticker.get(key)
        try:
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            pass
    try:
        base_vol = ticker.get("baseVolume")
        last = ticker.get("last")
        if base_vol is not None and last is not None:
            return float(base_vol) * float(last)
    except (TypeError, ValueError):
        return 0.0
    return 0.0


async def _update_spot_universe_ccxt():
    if ccxt is None:
        cfg.logger.warning("SPOT_DISCOVERY_MODE=ccxt but ccxt is not installed; falling back")
        return

    last_ts = cfg.state.get("spot_discovery_last_ts")
    try:
        last_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00")) if last_ts else None
        last_age = (datetime.now(timezone.utc) - last_dt).total_seconds() if last_dt else None
    except Exception:
        last_age = None

    refresh_sec = float(getattr(cfg, "SPOT_DISCOVERY_REFRESH_SEC", 14400) or 14400)
    if last_age is not None and last_age < refresh_sec and cfg.state.get("spot_universe"):
        return

    exchange = ccxt.coinbase({"enableRateLimit": True})
    try:
        markets = await asyncio.to_thread(exchange.load_markets)
    except Exception as exc:
        cfg.logger.warning("CCXT spot discovery failed: %s", exc)
        return

    quotes = [str(q).upper().strip() for q in (getattr(cfg, "SPOT_QUOTES", None) or ["USD", "USDC"]) if str(q).strip()]
    quote_priority = {q: i for i, q in enumerate(quotes)}

    best_by_base = {}
    for symbol, market in (markets or {}).items():
        if not isinstance(market, dict):
            continue
        if not bool(market.get("active", True)):
            continue
        is_spot = bool(market.get("spot")) or str(market.get("type") or "").lower() == "spot"
        if not is_spot:
            continue
        quote = str(market.get("quote") or "").upper().strip()
        base = str(market.get("base") or "").upper().strip()
        if quote not in quote_priority:
            continue
        if not base or _is_stable_base(base) or (not any(ch.isalpha() for ch in base)):
            continue
        product_id = f"{base}-{quote}"
        existing = best_by_base.get(base)
        if existing is None or quote_priority[quote] < existing[0]:
            best_by_base[base] = (quote_priority[quote], product_id)

    spot_products = sorted({product_id for _, product_id in best_by_base.values()})
    cfg.state["spot_universe"] = spot_products
    cfg.state["spot_universe_count"] = len(spot_products)
    cfg.state["spot_discovery_last_ts"] = datetime.now(timezone.utc).isoformat()

    # Build a priority list by 24h volume using Coinbase's product metadata (more reliable than ccxt tickers).
    priority = []
    try:
        response = await asyncio.to_thread(cfg.client.get_products)
        products = _read_attr(response, "products", [])
        volume_map = {}
        for product in products:
            product_id = _read_attr(product, "product_id", "")
            if not product_id:
                continue
            try:
                volume_map[product_id] = float(
                    _read_attr(product, "volume_24h", 0) or _read_attr(product, "approximate_quote_24h_volume", 0) or 0.0
                )
            except (TypeError, ValueError):
                continue

        scored = [(float(volume_map.get(pid, 0.0) or 0.0), pid) for pid in spot_products]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        max_keep = int(getattr(cfg, "SPOT_PRIORITY_SIZE", 200) or 200)
        priority = [pid for vol, pid in scored[:max_keep] if vol > 0]
    except Exception:
        priority = []

    cfg.state["spot_priority"] = priority

    cfg.logger.info(
        "CCXT spot discovery: total_spot=%d priority=%d",
        len(spot_products),
        len(priority),
    )


def _next_spot_active_scan_window():
    universe = list(cfg.state.get("spot_priority") or cfg.state.get("spot_universe") or [])
    if not universe:
        return []

    active_size = int(getattr(cfg, "SPOT_ACTIVE_SCAN_SIZE", 80) or 80)
    active_size = max(10, active_size)
    if len(universe) <= active_size:
        return universe

    cursor = int(cfg.state.get("spot_scan_cursor") or 0)
    cursor = max(0, cursor) % len(universe)
    window = universe[cursor : cursor + active_size]
    if len(window) < active_size:
        window = window + universe[: active_size - len(window)]
    cfg.state["spot_scan_cursor"] = (cursor + active_size) % len(universe)
    return window


def _extract_candles(response):
    candles = _read_attr(response, "candles", None)
    if candles is None and isinstance(response, list):
        candles = response
    return candles or []


def _candle_start_ts(candle):
    raw = _read_attr(candle, "start")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _sorted_candles(candles):
    if not candles:
        return []
    with_ts = []
    missing = 0
    for candle in candles:
        ts = _candle_start_ts(candle)
        if ts is None:
            missing += 1
            continue
        with_ts.append((ts, candle))
    if with_ts and missing == 0:
        with_ts.sort(key=lambda pair: pair[0])
        return [c for _, c in with_ts]
    return list(candles)


def _aggregate_candles(candles, bucket_seconds):
    candles = _sorted_candles(candles)
    if not candles:
        return []

    buckets = {}
    for candle in candles:
        ts = _candle_start_ts(candle)
        if ts is None:
            continue
        bucket = ts - (ts % int(bucket_seconds))
        try:
            high = float(_read_attr(candle, "high"))
            low = float(_read_attr(candle, "low"))
            open_px = float(_read_attr(candle, "open"))
            close = float(_read_attr(candle, "close"))
        except (TypeError, ValueError):
            continue

        row = buckets.get(bucket)
        if row is None:
            buckets[bucket] = {
                "start": bucket,
                "open": open_px,
                "high": high,
                "low": low,
                "close": close,
                "_last_ts": ts,
            }
            continue

        row["high"] = max(float(row.get("high") or high), high)
        row["low"] = min(float(row.get("low") or low), low)
        if ts > int(row.get("_last_ts") or ts):
            row["close"] = close
            row["_last_ts"] = ts

    rows = list(buckets.values())
    rows.sort(key=lambda item: int(item.get("start") or 0))
    for row in rows:
        row.pop("_last_ts", None)
    return rows


def _ema_from_candles(candles, period):
    candles = _sorted_candles(candles)
    closes = []
    for candle in candles:
        try:
            closes.append(float(_read_attr(candle, "close")))
        except (TypeError, ValueError):
            continue
    if len(closes) < int(period):
        return None

    period = int(period)
    alpha = 2.0 / (period + 1.0)
    sma = sum(closes[:period]) / float(period)
    ema = sma
    for close in closes[period:]:
        ema = (alpha * close) + ((1.0 - alpha) * ema)
    return float(ema)


def _fetch_candles(product_id, granularity):
    if cfg.client is None:
        return []

    now = datetime.now(timezone.utc)
    if granularity == "ONE_HOUR":
        start = int((now - timedelta(hours=140)).timestamp())
    elif granularity == "SIX_HOUR":
        start = int((now - timedelta(hours=900)).timestamp())
    else:
        start = int((now - timedelta(hours=140)).timestamp())
    end = int(now.timestamp())

    attempts = [
        lambda: cfg.client.get_candles(product_id=product_id, start=start, end=end, granularity=granularity),
        lambda: cfg.client.get_candles(product_id, start, end, granularity),
    ]
    for call in attempts:
        try:
            response = call()
            candles = _extract_candles(response)
            if candles:
                return candles
        except Exception:
            continue
    return []


async def refresh_atr_cache():
    unsupported = set((cfg.state.get("unsupported_products") or {}).keys())

    for product_id in list(cfg.state.get("basket") or []):
        if product_id in unsupported:
            continue

        # Use 1h candles as the single source of truth and aggregate for higher timeframes.
        # This avoids relying on exchange-provided SIX_HOUR candles which can be unavailable for many spot products.
        one_hour = await asyncio.to_thread(_fetch_candles, product_id, "ONE_HOUR")
        six_hour = _aggregate_candles(one_hour, bucket_seconds=6 * 3600)

        atr_1h = compute_atr(one_hour, period=14)
        atr_6h = compute_atr(six_hour, period=14)
        atr_1h, atr_6h = normalize_atr_bundle(atr_1h, atr_6h)

        if atr_1h is None and atr_6h is None:
            continue
        cfg.state["vol_cache"][product_id] = {
            "atr_1h": atr_1h,
            "atr_6h": atr_6h,
        }

        ema_1h = _ema_from_candles(one_hour, period=20)
        four_hour = _aggregate_candles(one_hour, bucket_seconds=4 * 3600)
        ema_4h = _ema_from_candles(four_hour, period=20)

        ref_price = cfg.state.get("price", {}).get(product_id)
        if ref_price is None:
            try:
                ref_price = float(_read_attr((_sorted_candles(one_hour) or [None])[-1], "close")) if one_hour else None
            except (TypeError, ValueError):
                ref_price = None
        ref_price = _to_float(ref_price)

        above_1h = None
        above_4h = None
        dist_1h_pct = None
        dist_4h_pct = None
        if ref_price is not None and ref_price > 0:
            if ema_1h is not None and ema_1h > 0:
                above_1h = bool(ref_price > float(ema_1h))
                dist_1h_pct = ((float(ref_price) - float(ema_1h)) / float(ema_1h)) * 100.0
            if ema_4h is not None and ema_4h > 0:
                above_4h = bool(ref_price > float(ema_4h))
                dist_4h_pct = ((float(ref_price) - float(ema_4h)) / float(ema_4h)) * 100.0

        alignment = "unknown"
        if above_1h is True and above_4h is True:
            alignment = "bullish"
        elif above_1h is False and above_4h is False:
            alignment = "bearish"
        elif above_1h is not None and above_4h is not None:
            alignment = "mixed"

        cfg.state.setdefault("mtf_cache", {})[product_id] = {
            "price": ref_price,
            "ema20_1h": round(float(ema_1h), 8) if ema_1h is not None else None,
            "ema20_4h": round(float(ema_4h), 8) if ema_4h is not None else None,
            "ema20_1h_dist_pct": round(float(dist_1h_pct), 4) if dist_1h_pct is not None else None,
            "ema20_4h_dist_pct": round(float(dist_4h_pct), 4) if dist_4h_pct is not None else None,
            "above_ema20_1h": above_1h,
            "above_ema20_4h": above_4h,
            "alignment": alignment,
            "ts": datetime.now(timezone.utc).isoformat(),
        }


async def start_websockets():
    last_basket_refresh = 0.0
    last_atr_refresh = 0.0
    last_micro_refresh = 0.0
    micro_cursor = 0
    while True:
        cfg.reload_hot_config()

        if cfg.client is None:
            await asyncio.sleep(10)
            continue

        now = time.monotonic()

        if now - last_basket_refresh >= float(cfg.BASKET_REFRESH_SEC):
            await update_basket()
            last_basket_refresh = now
        if now - last_atr_refresh >= float(cfg.ATR_REFRESH_SEC):
            await refresh_atr_cache()
            last_atr_refresh = now

        cycle_price_updates = 0
        to_drop = []
        unsupported_products = cfg.state.setdefault("unsupported_products", {})

        for product_id in list(cfg.state["basket"]):
            if product_id in unsupported_products:
                continue
            try:
                product = await asyncio.to_thread(cfg.client.get_product, product_id)
                price_raw = _read_attr(product, "price")
                if price_raw is None:
                    price_raw = _read_attr(product, "pricebook", {}).get("mid_market") if isinstance(_read_attr(product, "pricebook", {}), dict) else None
                if price_raw is not None:
                    price_value = float(price_raw)
                    cfg.state["price"][product_id] = price_value
                    cfg.state["price_ts"][product_id] = datetime.now(timezone.utc).isoformat()
                    history = cfg.state["price_history"].setdefault(product_id, [])
                    history.append(price_value)
                    if len(history) > 240:
                        del history[:-240]

                    pricebook = _read_attr(product, "pricebook", {})
                    best_bid = _to_float(_read_attr(product, "best_bid"))
                    best_ask = _to_float(_read_attr(product, "best_ask"))
                    if isinstance(pricebook, dict):
                        if best_bid is None:
                            best_bid = _best_price_from_levels(pricebook.get("bids"))
                        if best_ask is None:
                            best_ask = _best_price_from_levels(pricebook.get("asks"))

                    mid_price = None
                    if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
                        mid_price = (best_bid + best_ask) / 2.0
                    elif price_value > 0:
                        mid_price = price_value

                    spread_pct = None
                    if mid_price and best_bid is not None and best_ask is not None and mid_price > 0:
                        spread_pct = ((best_ask - best_bid) / mid_price) * 100.0

                    volume_24h = _to_float(_read_attr(product, "volume_24h"), 0.0) or 0.0
                    volume_1m = volume_24h / 1440.0 if volume_24h > 0 else None
                    cfg.state.setdefault("exec_liq_cache", {})[product_id] = {
                        "bid": best_bid,
                        "ask": best_ask,
                        "mid": mid_price,
                        "spread_pct": spread_pct,
                        "volume_24h": volume_24h,
                        "volume_1m": volume_1m,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }

                    # Order-book microstructure (no extra HTTP): imbalance from available pricebook levels.
                    ob_metrics = _orderbook_metrics_from_pricebook(pricebook, max_levels=10)
                    if ob_metrics:
                        micro_bucket = cfg.state.setdefault("micro_cache", {}).setdefault(product_id, {})
                        micro_bucket.update(ob_metrics)
                        micro_bucket["ts"] = datetime.now(timezone.utc).isoformat()
                    cycle_price_updates += 1
            except Exception as exc:
                mark_exchange_failure("market_price_poll", exc)
                msg = str(exc or "")
                msg_l = msg.lower()
                # Auto-drop permanently unsupported products to avoid log spam and broken price loops.
                if (
                    "not supported" in msg_l
                    or "productid is invalid" in msg_l
                    or "invalid_argument" in msg_l
                    or "not_found" in msg_l
                    or "not found" in msg_l
                ):
                    unsupported_products[product_id] = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "reason": msg[:240],
                    }
                    to_drop.append(product_id)
                    cfg.logger.warning("Dropping unsupported product from basket: %s (%s)", product_id, msg[:160])
                continue

        if to_drop:
            existing = list(cfg.state.get("basket") or [])
            filtered = [pid for pid in existing if pid not in set(to_drop)]
            if filtered and filtered != existing:
                cfg.state["basket"] = filtered
                cfg.state["basket_ver"] += 1
                cfg.logger.warning("Basket pruned to %d products (ver=%d) due to unsupported products", len(filtered), cfg.state["basket_ver"])

        if cycle_price_updates > 0:
            mark_exchange_success("market_price_poll")
            classify_regime()
        else:
            mark_exchange_failure("market_price_poll", "no fresh prices")

        # Tape microstructure (lightweight): refresh one asset per interval using Coinbase public trades on a spot proxy.
        micro_refresh_sec = 20.0
        if now - last_micro_refresh >= micro_refresh_sec and cfg.state.get("basket"):
            assets = list(cfg.state.get("basket") or [])
            if assets:
                micro_cursor = int(micro_cursor) % len(assets)
                asset = assets[micro_cursor]
                micro_cursor += 1

                spot_product = _spot_proxy_product(asset)
                if spot_product:
                    trades = await _fetch_coinbase_public_trades(spot_product, limit=60)
                    if trades:
                        micro_bucket = cfg.state.setdefault("micro_cache", {}).setdefault(asset, {})
                        micro_bucket.update(_tape_metrics(trades, large_notional_usd=50_000.0))
                        micro_bucket["tape_source"] = "coinbase_exchange_spot_proxy"
                        micro_bucket["tape_product"] = spot_product
                        micro_bucket["ts"] = datetime.now(timezone.utc).isoformat()
            last_micro_refresh = now

        await asyncio.sleep(cfg.PRICE_POLL_SEC)