import asyncio
import csv
import json
import os
import sqlite3
from datetime import datetime, timedelta, time, timezone

from . import config as cfg


HEADER = [
    "date",
    "equity_start",
    "equity_end",
    "daily_pnl_pct",
    "trades",
    "win_rate",
    "avg_rr",
    "max_dd_today",
    "health_score",
]


def _refresh_recent_returns_from_csv():
    csv_path = cfg.DAILY_SCORE_CSV_PATH
    if not os.path.exists(csv_path):
        cfg.state["recent_returns"] = []
        cfg.state["equity_momentum_7d_return_pct"] = 0.0
        return

    pct_values = []
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "daily_pnl_pct" not in reader.fieldnames:
                return
            for row in reader:
                try:
                    pct_values.append(float(row.get("daily_pnl_pct", 0.0)))
                except (TypeError, ValueError):
                    continue
    except OSError:
        return

    pct_values = pct_values[-14:]
    dec_values = [value / 100.0 for value in pct_values]
    cfg.state["recent_returns"] = dec_values
    cfg.state["equity_momentum_7d_return_pct"] = float(sum(pct_values[-7:])) if pct_values else 0.0


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _today_utc_date():
    return datetime.now().astimezone().date()


def _utc_naive_bounds_for_local_day(day_text):
    local_tz = datetime.now().astimezone().tzinfo
    local_day = datetime.fromisoformat(f"{day_text}T00:00:00").date()

    start_local = datetime.combine(local_day, time.min, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc.isoformat(), end_utc.isoformat()


def _to_date_text(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _load_written_dates(csv_path):
    if not os.path.exists(csv_path):
        return set()

    dates = set()
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames and "date" in reader.fieldnames:
                for row in reader:
                    day = str(row.get("date") or "").strip()
                    if day:
                        dates.add(day)
                return dates
    except OSError:
        return set()

    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            for idx, row in enumerate(reader):
                if idx == 0 and row and str(row[0]).strip().lower() == "date":
                    continue
                if row and str(row[0]).strip():
                    dates.add(str(row[0]).strip())
    except OSError:
        return set()
    return dates


def _ensure_csv_with_header(csv_path):
    needs_header = True
    if os.path.exists(csv_path):
        try:
            needs_header = os.path.getsize(csv_path) == 0
        except OSError:
            needs_header = True

    if not needs_header:
        return

    with open(csv_path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)


def _equity_stats_for_day(day_text):
    start_utc, end_utc = _utc_naive_bounds_for_local_day(day_text)
    rows = []
    with sqlite3.connect("portfolio.db") as conn:
        rows = conn.execute(
            "SELECT ts, equity FROM equity_history WHERE ts >= ? AND ts < ? ORDER BY ts ASC, id ASC",
            (start_utc, end_utc),
        ).fetchall()

    if not rows:
        equity_now = _safe_float(cfg.state.get("equity"), 0.0)
        return equity_now, equity_now, 0.0

    points = [_safe_float(item[1], 0.0) for item in rows]
    equity_start = points[0]
    equity_end = points[-1]

    peak = points[0]
    max_dd = 0.0
    for equity in points:
        peak = max(peak, equity)
        if peak > 0:
            dd = ((peak - equity) / peak) * 100.0
            if dd > max_dd:
                max_dd = dd

    return equity_start, equity_end, max_dd


def _trade_stats_for_day(day_text):
    start_utc, end_utc = _utc_naive_bounds_for_local_day(day_text)
    with sqlite3.connect("trades.db") as conn:
        trades_row = conn.execute(
            """
            SELECT
              COUNT(*) AS trades,
              SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM trades
            WHERE ts >= ? AND ts < ?
            """,
            (start_utc, end_utc),
        ).fetchone()

        event_rows = conn.execute(
            "SELECT payload FROM trade_events WHERE event_type='trade_opened' AND ts >= ? AND ts < ?",
            (start_utc, end_utc),
        ).fetchall()

    trades = int((trades_row or [0, 0])[0] or 0)
    wins = int((trades_row or [0, 0])[1] or 0)
    win_rate = (wins / trades * 100.0) if trades > 0 else 0.0

    rr_values = []
    for payload_raw, in event_rows:
        try:
            payload = json.loads(payload_raw)
        except (TypeError, ValueError):
            continue
        rr = payload.get("rr")
        try:
            rr_val = float(rr)
        except (TypeError, ValueError):
            continue
        rr_values.append(rr_val)

    avg_rr = (sum(rr_values) / len(rr_values)) if rr_values else 0.0
    return trades, win_rate, avg_rr


def _health_score():
    state = str(cfg.state.get("health_state", "healthy") or "healthy")
    base = {
        "healthy": 100.0,
        "degraded": 75.0,
        "recovering": 60.0,
        "outage": 25.0,
    }.get(state, 60.0)

    penalties = 0.0
    if bool(cfg.state.get("drawdown_paused", False)):
        penalties += 20.0
    if not bool(cfg.state.get("data_quality_last_ok", True)):
        penalties += 10.0
    failures = int(cfg.state.get("health_consecutive_failures", 0) or 0)
    penalties += min(20.0, failures * 2.0)

    return max(0.0, min(100.0, base - penalties))


def _build_row(day_text):
    equity_start, equity_end, max_dd_today = _equity_stats_for_day(day_text)
    trades, win_rate, avg_rr = _trade_stats_for_day(day_text)

    daily_pnl_pct = 0.0
    if equity_start > 0:
        daily_pnl_pct = ((equity_end - equity_start) / equity_start) * 100.0

    return [
        day_text,
        f"{equity_start:.6f}",
        f"{equity_end:.6f}",
        f"{daily_pnl_pct:.6f}",
        str(trades),
        f"{win_rate:.6f}",
        f"{avg_rr:.6f}",
        f"{max_dd_today:.6f}",
        f"{_health_score():.2f}",
    ]


def append_daily_score(day_text):
    csv_path = cfg.DAILY_SCORE_CSV_PATH
    _ensure_csv_with_header(csv_path)

    written_dates = _load_written_dates(csv_path)
    if day_text in written_dates:
        return False

    row = _build_row(day_text)
    with open(csv_path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(row)

    cfg.state["daily_score_last_written_day"] = day_text
    _refresh_recent_returns_from_csv()
    cfg.logger.info("Daily score appended for %s -> %s", day_text, csv_path)
    return True


async def daily_score_loop():
    _refresh_recent_returns_from_csv()
    while True:
        cfg.reload_hot_config()

        if not bool(cfg.DAILY_SCORE_ENABLED):
            await asyncio.sleep(max(10, int(cfg.DAILY_SCORE_CHECK_SEC)))
            continue

        now_day = _today_utc_date()
        now_day_text = _to_date_text(now_day)
        yesterday_text = _to_date_text(now_day - timedelta(days=1))

        if cfg.state.get("daily_score_last_seen_day") is None:
            cfg.state["daily_score_last_seen_day"] = now_day_text

        try:
            append_daily_score(yesterday_text)
        except Exception as exc:
            cfg.logger.error("Daily score append failed for %s: %s", yesterday_text, exc)

        if cfg.state.get("daily_score_last_seen_day") != now_day_text:
            try:
                append_daily_score(yesterday_text)
            except Exception as exc:
                cfg.logger.error("Daily rollover append failed for %s: %s", yesterday_text, exc)
            cfg.state["daily_score_last_seen_day"] = now_day_text

        await asyncio.sleep(max(10, int(cfg.DAILY_SCORE_CHECK_SEC)))
