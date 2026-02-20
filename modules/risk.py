from math import sqrt


def _read(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def compute_atr(candles, period=14):
    if not candles:
        return None

    parsed = []
    for candle in candles:
        try:
            high = float(_read(candle, "high"))
            low = float(_read(candle, "low"))
            close = float(_read(candle, "close"))
            parsed.append((high, low, close))
        except (TypeError, ValueError):
            continue

    if len(parsed) < period + 1:
        return None

    trs = []
    prev_close = parsed[0][2]
    for high, low, close in parsed[1:]:
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def normalize_atr_bundle(atr_1h, atr_6h):
    if atr_6h is None and atr_1h is not None:
        atr_6h = atr_1h * sqrt(6)
    if atr_1h is None and atr_6h is not None:
        atr_1h = atr_6h / sqrt(6)
    return atr_1h, atr_6h


def derive_stop_take_profit(decision, entry, atr, pump_score):
    if atr is None:
        return None, None

    if pump_score >= 60:
        stop_mult = 1.8
        tp_mult = 2.7
    else:
        stop_mult = 2.5
        tp_mult = 3.8

    if decision == "open_long":
        stop = entry - (stop_mult * atr)
        take_profit = entry + (tp_mult * atr)
    else:
        stop = entry + (stop_mult * atr)
        take_profit = entry - (tp_mult * atr)

    return stop, take_profit
