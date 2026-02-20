import asyncio
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import re

import requests

from . import config as cfg

try:
    from twscrape import API as TwAPI
except ImportError:
    TwAPI = None


NEWS_FEEDS = [
    "https://thedefiant.io/feed/",
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
    "https://cryptoslate.com/feed/",
    "https://cryptonews.com/news/feed/",
    "https://smartliquidity.info/feed/",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "https://time.com/nextadvisor/feed/",
    "https://benjaminion.xyz/newineth2/rss_feed.xml",
]

WHALE_HANDLES = ["whale_alert", "lookonchain", "unusual_whales"]


def _twscrape_enabled():
    raw = str(os.getenv("RUMOR_DISABLE_TWSCRAPE", "")).strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def _twscrape_timeout_sec():
    raw = str(os.getenv("RUMOR_X_TIMEOUT_SEC", "20")).strip()
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 20.0
    return max(3.0, min(120.0, val))


def _x_cooldown_minutes():
    raw = str(os.getenv("RUMOR_X_COOLDOWN_MIN", "60")).strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 60
    return max(1, min(24 * 60, val))


def _x_cooldown_active():
    until_text = str(cfg.state.get("x_cooldown_until") or "").strip()
    if not until_text:
        return False
    try:
        until_dt = datetime.fromisoformat(until_text)
    except ValueError:
        return False
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)
    if until_dt <= datetime.now(timezone.utc):
        return False
    return True


def _activate_x_cooldown(reason):
    minutes = _x_cooldown_minutes()
    until_dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    cfg.state["x_cooldown_until"] = until_dt.isoformat()
    cfg.state["x_cooldown_reason"] = str(reason or "x_fetch_error")[:220]
    until_local = until_dt.astimezone()
    cfg.logger.warning(
        "X cooldown active for %dm until %s (%s)",
        minutes,
        until_local.isoformat(timespec="seconds"),
        cfg.state["x_cooldown_reason"],
    )


def _public_whale_notional_usd_threshold():
    raw = str(os.getenv("RUMOR_PUBLIC_WHALE_NOTIONAL_USD", "250000")).strip()
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 250000.0
    return max(10000.0, min(10000000.0, val))


def _public_whale_trade_limit():
    raw = str(os.getenv("RUMOR_PUBLIC_WHALE_TRADE_LIMIT", "200")).strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 200
    return max(20, min(1000, val))


async def _search_with_timeout(api, query, limit):
    timeout = _twscrape_timeout_sec()
    tweets = []

    async def _collect():
        async for tweet in api.search(query, limit=limit):
            tweets.append(tweet)

    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        _activate_x_cooldown(f"timeout:{query[:40]}")
        cfg.logger.warning("twscrape timeout (%.1fs) for query: %s", timeout, query[:80])
    except Exception as exc:
        _activate_x_cooldown(f"error:{exc}")
        cfg.logger.debug("twscrape query failed: %s", exc)
    return tweets


def _basket_bases():
    bases = set()
    for product_id in cfg.state.get("basket", []):
        if not isinstance(product_id, str):
            continue
        base = product_id.split("-", 1)[0].upper()
        if base:
            bases.add(base)
    return sorted(bases)


def _build_query(bases):
    if not bases:
        return None
    cashtags = [f"${base}" for base in bases[:20]]
    symbols = [f"#{base}" for base in bases[:20]]
    market_terms = ["breakout", "listing", "whale", "pump", "narrative", "short squeeze", "long"]
    assets_block = " OR ".join(cashtags + symbols)
    terms_block = " OR ".join(f'"{term}"' for term in market_terms)
    return f"({assets_block}) ({terms_block}) -is:retweet lang:en"


def _build_whale_query(bases):
    if not bases:
        return None
    assets = [f"${base}" for base in bases[:20]] + [f"#{base}" for base in bases[:20]]
    handle_block = " OR ".join(f"from:{name}" for name in WHALE_HANDLES)
    asset_block = " OR ".join(assets)
    return f"({handle_block}) ({asset_block}) -is:retweet lang:en"


def _binance_symbol_for_base(base):
    clean = str(base or "").upper().strip()
    if not clean:
        return None
    blocked = {"USDC", "USDT", "FDUSD", "BUSD", "DAI", "TUSD", "USD"}
    if clean in blocked:
        return None
    return f"{clean}USDT"


async def _fetch_binance_agg_trades(symbol, limit):
    url = "https://api.binance.com/api/v3/aggTrades"

    def _request():
        response = requests.get(url, params={"symbol": symbol, "limit": int(limit)}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    try:
        return await asyncio.to_thread(_request)
    except requests.RequestException as exc:
        cfg.logger.debug("Public whale fetch failed for %s: %s", symbol, exc)
        return []


def _coinbase_product_for_base(base):
    clean = str(base or "").upper().strip()
    if not clean:
        return None
    blocked = {"USDC", "USDT", "FDUSD", "BUSD", "DAI", "TUSD", "USD"}
    if clean in blocked:
        return None
    return f"{clean}-USD"


async def _fetch_coinbase_trades(product_id, limit):
    url = f"https://api.exchange.coinbase.com/products/{product_id}/trades"

    def _request():
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "TradeBot/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return payload[: int(limit)]

    try:
        return await asyncio.to_thread(_request)
    except requests.RequestException as exc:
        cfg.logger.debug("Coinbase public whale fetch failed for %s: %s", product_id, exc)
        return []


def _merge_whale_flows(*flows):
    merged_counts = {}
    for flow in flows:
        for asset, stats in (flow or {}).items():
            bucket = merged_counts.setdefault(asset, {"accumulation": 0, "distribution": 0, "samples": []})
            bucket["accumulation"] += int((stats or {}).get("accumulation", 0) or 0)
            bucket["distribution"] += int((stats or {}).get("distribution", 0) or 0)
            for sample in (stats or {}).get("samples", []):
                if sample and sample not in bucket["samples"] and len(bucket["samples"]) < 3:
                    bucket["samples"].append(sample)

    if not merged_counts:
        return "none", {}

    summary_parts = []
    merged_flow = {}
    ordered = sorted(
        merged_counts.items(),
        key=lambda pair: (pair[1]["accumulation"] + pair[1]["distribution"]),
        reverse=True,
    )[:8]

    for asset, stats in ordered:
        acc = int(stats.get("accumulation", 0))
        dist = int(stats.get("distribution", 0))
        if acc >= dist + 1:
            bias = "accumulation"
            conviction_adj = 20
        elif dist >= acc + 1:
            bias = "distribution"
            conviction_adj = -30
        else:
            bias = "mixed"
            conviction_adj = 0

        merged_flow[asset] = {
            "bias": bias,
            "conviction_adj": conviction_adj,
            "accumulation": acc,
            "distribution": dist,
            "samples": stats.get("samples", []),
        }
        summary_parts.append(f"{asset}:bias={bias},acc={acc},dist={dist}")

    return " | ".join(summary_parts), merged_flow


def _extract_text(tweet):
    if tweet is None:
        return ""
    for key in ("rawContent", "full_text", "text", "content"):
        value = getattr(tweet, key, None)
        if value:
            return str(value)
    return ""


def _extract_news_items_from_feed(url, cutoff, bases, max_items):
    items = []
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except (requests.RequestException, ET.ParseError):
        return items

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        content = f"{title} {description}".strip()
        if not content:
            continue

        pub_date_text = item.findtext("pubDate") or item.findtext("published") or ""
        try:
            created_at = parsedate_to_datetime(pub_date_text).replace(tzinfo=None) if pub_date_text else datetime.utcnow()
        except (TypeError, ValueError):
            created_at = datetime.utcnow()

        if created_at < cutoff:
            continue

        matched_assets = _match_assets(content, bases)
        if not matched_assets:
            continue

        sent, pump, whale_hit = _score_text(content)
        whale = "newsfeed" if whale_hit else "none"
        snippet = content.replace("\n", " ")[:300]

        for asset in matched_assets[:2]:
            items.append(
                {
                    "ts": created_at.isoformat(),
                    "asset": f"{asset}-PERP-INTX",
                    "rumor": snippet,
                    "sent": float(sent),
                    "pump": int(pump),
                    "whale": whale,
                }
            )
            if len(items) >= max_items:
                return items

    return items


def _extract_created_at(tweet):
    value = getattr(tweet, "date", None) or getattr(tweet, "created_at", None)
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _extract_author(tweet):
    user = getattr(tweet, "user", None)
    if user is not None:
        username = getattr(user, "username", None) or getattr(user, "screen_name", None)
        if username:
            return f"@{username}"
    for key in ("username", "author", "author_username"):
        value = getattr(tweet, key, None)
        if value:
            text = str(value)
            return text if text.startswith("@") else f"@{text}"
    return "unknown"


def _match_assets(text, bases):
    upper = text.upper()
    matched = []
    for base in bases:
        if f"${base}" in upper or f"#{base}" in upper or f" {base} " in f" {upper} ":
            matched.append(base)
    return matched


def _score_text(text):
    upper = text.upper()
    bullish = ["BREAKOUT", "BULL", "LONG", "PUMP", "LISTING", "ACCUMULATION", "UPONLY"]
    bearish = ["DUMP", "SHORT", "RUG", "LIQUIDATION", "SELL", "CAPITULATION"]
    whale = ["WHALE", "SMART MONEY", "INSIDER", "ORDER FLOW"]

    score = 0
    for token in bullish:
        if token in upper:
            score += 1
    for token in bearish:
        if token in upper:
            score -= 1
    whale_hit = any(token in upper for token in whale)
    pump = 1 if ("PUMP" in upper or "BREAKOUT" in upper) else 0
    return score, pump, whale_hit


def _whale_direction(text):
    upper = str(text or "").upper()
    accumulation_terms = ["BUY", "BOUGHT", "ACCUMULAT", "INFLOW", "WITHDRAW", "ADDED", "LONG"]
    distribution_terms = ["SELL", "SOLD", "DISTRIBUT", "OUTFLOW", "DEPOSIT", "UNLOAD", "DUMP", "SHORT"]

    acc = sum(1 for token in accumulation_terms if token in upper)
    dist = sum(1 for token in distribution_terms if token in upper)
    if acc > dist:
        return "accumulation"
    if dist > acc:
        return "distribution"
    return "mixed"


async def _fetch_whale_flow_summary_from_x():
    if _x_cooldown_active():
        return "none", {}
    if TwAPI is None:
        return "none", {}

    bases = _basket_bases()
    query = _build_whale_query(bases)
    if not query:
        return "none", {}

    api = TwAPI()
    cutoff = datetime.utcnow() - timedelta(hours=4)
    per_asset = {}

    tweets = await _search_with_timeout(api, query, limit=max(10, cfg.RUMOR_MAX_POSTS * 2))
    for tweet in tweets:
        text = _extract_text(tweet)
        if not text:
            continue

        created_at = _extract_created_at(tweet)
        if created_at < cutoff:
            continue

        matched_assets = _match_assets(text, bases)
        if not matched_assets:
            continue

        direction = _whale_direction(text)
        author = _extract_author(tweet)
        snippet = text.strip().replace("\n", " ")[:220]
        for base in matched_assets[:2]:
            asset = f"{base}-PERP-INTX"
            bucket = per_asset.setdefault(asset, {"accumulation": 0, "distribution": 0, "mixed": 0, "samples": []})
            bucket[direction] += 1
            if len(bucket["samples"]) < 2:
                bucket["samples"].append(f"{author}:{snippet}")

    if not per_asset:
        return "none", {}

    summary_parts = []
    flow = {}
    for asset, stats in sorted(per_asset.items(), key=lambda pair: (pair[1]["accumulation"] + pair[1]["distribution"]), reverse=True)[:8]:
        acc = int(stats.get("accumulation", 0))
        dist = int(stats.get("distribution", 0))
        if acc >= dist + 1:
            bias = "accumulation"
            conviction_adj = 20
        elif dist >= acc + 1:
            bias = "distribution"
            conviction_adj = -30
        else:
            bias = "mixed"
            conviction_adj = 0

        flow[asset] = {
            "bias": bias,
            "conviction_adj": conviction_adj,
            "accumulation": acc,
            "distribution": dist,
            "samples": stats.get("samples", []),
        }
        summary_parts.append(f"{asset}:bias={bias},acc={acc},dist={dist}")

    return " | ".join(summary_parts), flow


async def _fetch_whale_flow_summary_from_public_markets():
    bases = _basket_bases()
    if not bases:
        return "none", {}

    threshold = _public_whale_notional_usd_threshold()
    limit = _public_whale_trade_limit()
    per_asset = {}

    for base in bases[:20]:
        asset = f"{base}-PERP-INTX"
        bucket = per_asset.setdefault(asset, {"accumulation": 0, "distribution": 0, "samples": []})

        symbol = _binance_symbol_for_base(base)
        if symbol:
            trades = await _fetch_binance_agg_trades(symbol, limit=limit)
            for trade in trades:
                try:
                    price = float(trade.get("p") or 0)
                    qty = float(trade.get("q") or 0)
                    notional_usd = price * qty
                except (TypeError, ValueError):
                    continue
                if notional_usd < threshold:
                    continue

                is_buyer_maker = bool(trade.get("m"))
                if is_buyer_maker:
                    bucket["distribution"] += 1
                    direction = "distribution"
                else:
                    bucket["accumulation"] += 1
                    direction = "accumulation"

                if len(bucket["samples"]) < 2:
                    bucket["samples"].append(f"binance:{direction}:${notional_usd:,.0f}")

        cb_product = _coinbase_product_for_base(base)
        if cb_product:
            cb_trades = await _fetch_coinbase_trades(cb_product, limit=min(100, limit))
            for trade in cb_trades:
                try:
                    price = float(trade.get("price") or 0)
                    qty = float(trade.get("size") or 0)
                    notional_usd = price * qty
                except (TypeError, ValueError):
                    continue
                if notional_usd < threshold:
                    continue

                side = str(trade.get("side") or "").lower()
                if side == "sell":
                    bucket["distribution"] += 1
                    direction = "distribution"
                else:
                    bucket["accumulation"] += 1
                    direction = "accumulation"

                if len(bucket["samples"]) < 2:
                    bucket["samples"].append(f"coinbase:{direction}:${notional_usd:,.0f}")

        if bucket["accumulation"] == 0 and bucket["distribution"] == 0:
            per_asset.pop(asset, None)

    flow = {}
    for asset, stats in per_asset.items():
        acc = int(stats.get("accumulation", 0))
        dist = int(stats.get("distribution", 0))
        if acc == 0 and dist == 0:
            continue
        flow[asset] = {
            "bias": "mixed",
            "conviction_adj": 0,
            "accumulation": acc,
            "distribution": dist,
            "samples": stats.get("samples", []),
        }

    if not flow:
        return "none", {}
    return _merge_whale_flows(flow)


def _save_rumor(ts, asset, rumor, sent, pump, whale):
    with sqlite3.connect("rumors.db") as conn:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        existing = conn.execute(
            "SELECT 1 FROM rumors WHERE asset = ? AND rumor = ? AND ts >= ? LIMIT 1",
            (asset, rumor, cutoff),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO rumors (ts, asset, rumor, sent, pump, whale) VALUES (?,?,?,?,?,?)",
            (ts, asset, rumor, sent, pump, whale),
        )
    return True


def _compute_spike_list():
    spikes = []
    for asset, metrics in (cfg.state.get("vol_cache") or {}).items():
        try:
            atr_1h = float((metrics or {}).get("atr_1h") or 0)
            atr_6h = float((metrics or {}).get("atr_6h") or 0)
        except (TypeError, ValueError):
            continue
        if atr_1h <= 0 or atr_6h <= 0:
            continue
        ratio = atr_1h / max(atr_6h, 1e-12)
        if ratio >= 1.35:
            spikes.append({"asset": asset, "vol_spike": round(ratio, 3)})
    spikes.sort(key=lambda item: item["vol_spike"], reverse=True)
    cfg.state["spike_list"] = spikes[:8]


def _summarize_items(items):
    if not items:
        return "none"

    by_asset = {}
    for item in items:
        bucket = by_asset.setdefault(item["asset"], {"count": 0, "sent": 0.0, "pump": 0, "whale": 0})
        bucket["count"] += 1
        bucket["sent"] += float(item["sent"])
        bucket["pump"] += int(item["pump"])
        bucket["whale"] += 1 if item["whale"] != "none" else 0

    ordered = sorted(by_asset.items(), key=lambda pair: pair[1]["count"], reverse=True)
    parts = []
    for asset, stats in ordered[:8]:
        avg_sent = stats["sent"] / max(stats["count"], 1)
        parts.append(
            f"{asset}:mentions={stats['count']},sent={avg_sent:+.2f},pump={stats['pump']},whale={stats['whale']}"
        )
    return " | ".join(parts)


def _normalize_rumor_text(text):
    if not text:
        return ""
    normalized = re.sub(r"https?://\S+", "", str(text).lower())
    normalized = re.sub(r"[^a-z0-9\s$#-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _dedupe_items(items):
    deduped = []
    seen = set()
    for item in items:
        asset = str(item.get("asset") or "")
        rumor = _normalize_rumor_text(item.get("rumor"))[:180]
        if not rumor:
            continue
        key = (asset, rumor)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def _fetch_items_from_x():
    if _x_cooldown_active():
        return []
    if not _twscrape_enabled():
        return []
    if TwAPI is None:
        return []

    bases = _basket_bases()
    query = _build_query(bases)
    if not query:
        return []

    api = TwAPI()
    cutoff = datetime.utcnow() - timedelta(hours=max(1, cfg.RUMOR_LOOKBACK_HOURS))
    items = []

    tweets = await _search_with_timeout(api, query, limit=max(5, cfg.RUMOR_MAX_POSTS))
    for tweet in tweets:
        text = _extract_text(tweet)
        if not text:
            continue

        created_at = _extract_created_at(tweet)
        if created_at < cutoff:
            continue

        matched_assets = _match_assets(text, bases)
        if not matched_assets:
            continue

        sent, pump, whale_hit = _score_text(text)
        author = _extract_author(tweet)
        whale = author if whale_hit else "none"
        snippet = text.strip().replace("\n", " ")[:300]

        for asset in matched_assets[:2]:
            item = {
                "ts": created_at.isoformat(),
                "asset": f"{asset}-PERP-INTX",
                "rumor": snippet,
                "sent": float(sent),
                "pump": int(pump),
                "whale": whale,
            }
            items.append(item)
            if len(items) >= cfg.RUMOR_MAX_POSTS:
                return items
    return items


async def _fetch_items_from_news():
    bases = _basket_bases()
    if not bases:
        return []

    cutoff = datetime.utcnow() - timedelta(hours=max(1, cfg.RUMOR_LOOKBACK_HOURS))
    max_items = max(5, cfg.RUMOR_MAX_POSTS)
    all_items = []

    for feed_url in NEWS_FEEDS:
        found = _extract_news_items_from_feed(feed_url, cutoff, bases, max_items=max_items - len(all_items))
        if found:
            all_items.extend(found)
        if len(all_items) >= max_items:
            break

    all_items.sort(key=lambda item: item.get("ts", ""), reverse=True)
    return _dedupe_items(all_items)[:max_items]


async def _fetch_items():
    source = (cfg.RUMOR_SOURCE or "auto").lower()
    tw_enabled = _twscrape_enabled()
    if source == "news":
        return await _fetch_items_from_news()
    if source == "x":
        if not tw_enabled:
            return []
        return await _fetch_items_from_x()

    if tw_enabled and TwAPI is not None:
        items = await _fetch_items_from_x()
        if items:
            return _dedupe_items(items)
    return _dedupe_items(await _fetch_items_from_news())


async def fetch_loop():
    while True:
        try:
            cfg.reload_hot_config()
            tw_enabled = _twscrape_enabled()
            rumor_items = await _fetch_items()
            if rumor_items:
                inserted_count = 0
                for item in rumor_items:
                    inserted = _save_rumor(
                        item["ts"],
                        item["asset"],
                        item["rumor"],
                        item["sent"],
                        item["pump"],
                        item["whale"],
                    )
                    if inserted:
                        inserted_count += 1

                cfg.state["rumor_items"] = rumor_items[:50]
                cfg.state["rumors_summary"] = _summarize_items(rumor_items)
                cfg.logger.info(
                    "Rumors updated: %d items (%d new, %s source)",
                    len(rumor_items),
                    inserted_count,
                    cfg.RUMOR_SOURCE,
                )
            else:
                cfg.state["rumors_summary"] = "none"

            local_summary = "none"
            local_flow = {}
            try:
                local_summary, local_flow = await _fetch_whale_flow_summary_from_public_markets()
            except Exception as local_exc:
                cfg.logger.debug("Public whale flow fetch error: %s", local_exc)

            x_summary = "none"
            x_flow = {}
            if tw_enabled:
                try:
                    x_summary, x_flow = await _fetch_whale_flow_summary_from_x()
                except Exception as whale_exc:
                    cfg.logger.debug("Whale flow fetch error: %s", whale_exc)

            whale_summary, whale_flow = _merge_whale_flows(local_flow, x_flow)
            if whale_summary == "none":
                if local_summary != "none":
                    whale_summary = local_summary
                elif (not tw_enabled) and x_summary == "none":
                    whale_summary = "disabled"
            cfg.state["whale_summary"] = whale_summary
            cfg.state["whale_flow"] = whale_flow

            _compute_spike_list()
        except Exception as exc:
            cfg.logger.warning("Rumor fetch loop error: %s", exc)

        await asyncio.sleep(max(60, cfg.RUMOR_POLL_SEC))