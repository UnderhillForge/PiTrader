from datetime import datetime

from . import config as cfg


def _seconds_since(ts_text):
    if not ts_text:
        return None
    try:
        when = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return max(0.0, (datetime.utcnow() - when).total_seconds())


def evaluate_pre_grok_data_quality():
    cfg.reload_hot_config()

    basket = list(cfg.state.get("basket", []))
    basket_size = len(basket)
    min_basket_size = int(cfg.DATAQ_MIN_BASKET_SIZE)

    if basket_size < min_basket_size:
        return {
            "ok": False,
            "reason": "basket_too_small",
            "details": {
                "basket_size": basket_size,
                "required_min": min_basket_size,
            },
        }

    checked = basket[: max(1, min(40, basket_size))]
    max_age = int(cfg.DATAQ_MAX_PRICE_AGE_SEC)

    fresh_prices = 0
    valid_prices = 0
    atr_coverage = 0
    stale_assets = []
    invalid_assets = []
    atr_missing_assets = []

    for asset in checked:
        raw_price = cfg.state.get("price", {}).get(asset)
        try:
            price = float(raw_price)
            is_valid = price > 0
        except (TypeError, ValueError):
            is_valid = False

        if is_valid:
            valid_prices += 1
        else:
            invalid_assets.append(asset)

        age_sec = _seconds_since(cfg.state.get("price_ts", {}).get(asset))
        if age_sec is not None and age_sec <= max_age and is_valid:
            fresh_prices += 1
        else:
            stale_assets.append(asset)

        atr_bundle = (cfg.state.get("vol_cache", {}).get(asset) or {})
        atr_1h = atr_bundle.get("atr_1h")
        atr_6h = atr_bundle.get("atr_6h")
        if atr_1h is not None or atr_6h is not None:
            atr_coverage += 1
        else:
            atr_missing_assets.append(asset)

    total = len(checked)
    fresh_ratio = (fresh_prices / total) if total > 0 else 0.0
    atr_ratio = (atr_coverage / total) if total > 0 else 0.0

    min_fresh_ratio = float(cfg.DATAQ_MIN_FRESH_PRICE_RATIO)
    min_atr_ratio = float(cfg.DATAQ_MIN_ATR_COVERAGE_RATIO)

    if valid_prices < total:
        return {
            "ok": False,
            "reason": "invalid_or_missing_prices",
            "details": {
                "valid_prices": valid_prices,
                "total_assets": total,
                "sample_assets": invalid_assets[:5],
            },
        }

    if fresh_ratio < min_fresh_ratio:
        return {
            "ok": False,
            "reason": "stale_price_data",
            "details": {
                "fresh_ratio": round(fresh_ratio, 4),
                "required_min_ratio": min_fresh_ratio,
                "max_age_sec": max_age,
                "sample_assets": stale_assets[:5],
            },
        }

    if atr_ratio < min_atr_ratio:
        return {
            "ok": False,
            "reason": "atr_coverage_low",
            "details": {
                "atr_ratio": round(atr_ratio, 4),
                "required_min_ratio": min_atr_ratio,
                "sample_assets": atr_missing_assets[:5],
            },
        }

    return {
        "ok": True,
        "reason": "ok",
        "details": {
            "basket_size": basket_size,
            "checked_assets": total,
            "fresh_ratio": round(fresh_ratio, 4),
            "atr_ratio": round(atr_ratio, 4),
            "max_price_age_sec": max_age,
        },
    }
