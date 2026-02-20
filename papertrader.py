#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import websockets
from modules.single_instance import SingleInstanceError, SingleInstanceLock


@dataclass
class SessionStats:
    start_ts: datetime
    end_ts: datetime
    start_equity: Optional[float] = None
    last_equity: Optional[float] = None
    max_equity: Optional[float] = None
    min_equity: Optional[float] = None
    realized_pnl_start: float = 0.0
    realized_pnl_last: float = 0.0
    equity_updates: int = 0
    closed_trades: int = 0
    winners: int = 0
    losers: int = 0
    journal_gross_pnl: float = 0.0
    journal_fee_cost: float = 0.0
    journal_funding_cost: float = 0.0
    journal_net_pnl: float = 0.0


class PaperTraderLogger:
    def __init__(self, log_path: str, ws_url: str, interval_sec: float, heartbeat_sec: int):
        self.log_path = log_path
        self.ws_url = ws_url
        self.interval_sec = max(1.0, float(interval_sec))
        self.heartbeat_sec = max(10, int(heartbeat_sec))

        self.seen_closed_trade_ids: Set[str] = set()
        self.prev_open_ids: Set[str] = set()
        self.last_decision_key: str = ""
        self.last_heartbeat: Optional[datetime] = None
        self._ws = None
        self._ws_fail_streak: int = 0
        self._ws_connected_once: bool = False

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _fmt(self, value: Optional[float], digits: int = 2) -> str:
        if value is None:
            return "n/a"
        return f"{value:.{digits}f}"

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _write_line(self, message: str) -> None:
        ts = self._now().isoformat()
        line = f"[{ts}] {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def _close_ws(self) -> None:
        if self._ws is None:
            return
        try:
            await asyncio.wait_for(self._ws.close(), timeout=1.0)
        except Exception:
            pass
        self._ws = None

    def _warn_ws_issue(self, message: str) -> None:
        if self._ws_fail_streak == 1 or self._ws_fail_streak % 12 == 0:
            self._write_line(f"WARN {message}; retrying (streak={self._ws_fail_streak})")

    async def _ensure_ws(self) -> bool:
        if self._ws is not None:
            return True
        try:
            self._ws = await websockets.connect(self.ws_url, open_timeout=2.0, ping_interval=None)
            if not self._ws_connected_once:
                self._write_line(f"WS_CONNECTED url={self.ws_url}")
                self._ws_connected_once = True
            self._ws_fail_streak = 0
            return True
        except Exception:
            self._ws = None
            self._ws_fail_streak += 1
            self._warn_ws_issue("snapshot unavailable")
            return False

    async def _fetch_snapshot(self) -> Optional[dict]:
        if not await self._ensure_ws():
            return None
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=max(3.0, self.interval_sec * 3.0))
            data = json.loads(raw)
            if isinstance(data, dict):
                self._ws_fail_streak = 0
                return data
        except Exception:
            await self._close_ws()
            self._ws_fail_streak += 1
            self._warn_ws_issue("snapshot unavailable")
            return None
        return None

    def _existing_closed_trade_ids(self) -> Set[str]:
        if not os.path.exists("trades.db"):
            return set()
        try:
            with sqlite3.connect("trades.db") as conn:
                rows = conn.execute("SELECT id FROM trades").fetchall()
            return {str(row[0]) for row in rows if row and row[0] is not None}
        except sqlite3.Error:
            return set()

    def _load_new_closed_trades(self) -> List[Dict]:
        if not os.path.exists("trades.db"):
            return []
        try:
            with sqlite3.connect("trades.db") as conn:
                try:
                    rows = conn.execute(
                        "SELECT id, ts, asset, side, size, entry, exit, pnl, pnl_gross, fee_cost, funding_cost, reason "
                        "FROM trades ORDER BY ts ASC, id ASC"
                    ).fetchall()
                except sqlite3.Error:
                    rows = conn.execute(
                        "SELECT id, ts, asset, side, size, entry, exit, pnl, reason FROM trades ORDER BY ts ASC, id ASC"
                    ).fetchall()
        except sqlite3.Error:
            return []

        new_rows: List[Dict] = []
        for row in rows:
            trade_id = str(row[0])
            if trade_id in self.seen_closed_trade_ids:
                continue
            self.seen_closed_trade_ids.add(trade_id)
            if len(row) >= 12:
                new_rows.append(
                    {
                        "id": trade_id,
                        "ts": row[1],
                        "asset": row[2],
                        "side": row[3],
                        "size": self._safe_float(row[4]),
                        "entry": self._safe_float(row[5]),
                        "exit": self._safe_float(row[6]),
                        "pnl": self._safe_float(row[7]),
                        "pnl_gross": self._safe_float(row[8]),
                        "fee_cost": self._safe_float(row[9]),
                        "funding_cost": self._safe_float(row[10]),
                        "reason": row[11],
                    }
                )
            else:
                new_rows.append(
                    {
                        "id": trade_id,
                        "ts": row[1],
                        "asset": row[2],
                        "side": row[3],
                        "size": self._safe_float(row[4]),
                        "entry": self._safe_float(row[5]),
                        "exit": self._safe_float(row[6]),
                        "pnl": self._safe_float(row[7]),
                        "pnl_gross": self._safe_float(row[7]),
                        "fee_cost": 0.0,
                        "funding_cost": 0.0,
                        "reason": row[8],
                    }
                )
        return new_rows

    def _update_equity_stats(self, stats: SessionStats, equity: Optional[float]) -> None:
        if equity is None:
            return
        if stats.start_equity is None:
            stats.start_equity = equity
            stats.max_equity = equity
            stats.min_equity = equity
            stats.last_equity = equity
            return

        if stats.last_equity is None:
            stats.last_equity = equity

        if abs(equity - float(stats.last_equity)) > 1e-9:
            stats.equity_updates += 1

        stats.last_equity = equity
        stats.max_equity = max(float(stats.max_equity), equity) if stats.max_equity is not None else equity
        stats.min_equity = min(float(stats.min_equity), equity) if stats.min_equity is not None else equity

    def _log_start(self, stats: SessionStats, snapshot: dict) -> None:
        equity = snapshot.get("equity")
        realized = self._safe_float(snapshot.get("sim_realized_pnl"), 0.0)
        mode = snapshot.get("mode")
        ready = snapshot.get("ready_to_trade")
        open_count = int(snapshot.get("open_trades_count") or 0)

        stats.realized_pnl_start = realized
        stats.realized_pnl_last = realized

        self._write_line(
            "SESSION_START "
            f"duration_h={(stats.end_ts - stats.start_ts).total_seconds() / 3600:.2f} "
            f"equity={self._fmt(self._safe_float(equity, 0.0))} "
            f"sim_realized_pnl={self._fmt(realized)} mode={mode} ready={ready} open_trades={open_count}"
        )

    def _log_snapshot_changes(self, stats: SessionStats, snapshot: dict) -> None:
        equity = snapshot.get("equity")
        equity_val = self._safe_float(equity) if equity is not None else None
        prev_equity = stats.last_equity
        self._update_equity_stats(stats, equity_val)

        if prev_equity is not None and equity_val is not None and abs(equity_val - prev_equity) > 1e-9:
            delta = equity_val - prev_equity
            rel = 0.0 if prev_equity == 0 else (delta / prev_equity) * 100.0
            base = stats.start_equity if stats.start_equity is not None else equity_val
            since_start = equity_val - base
            self._write_line(
                "EQUITY_UPDATE "
                f"equity={self._fmt(equity_val)} delta={delta:+.2f} ({rel:+.2f}%) "
                f"since_start={since_start:+.2f}"
            )

        realized_now = self._safe_float(snapshot.get("sim_realized_pnl"), stats.realized_pnl_last)
        if abs(realized_now - stats.realized_pnl_last) > 1e-9:
            self._write_line(
                "SIM_PNL_UPDATE "
                f"sim_realized_pnl={self._fmt(realized_now)} delta={realized_now - stats.realized_pnl_last:+.2f}"
            )
            stats.realized_pnl_last = realized_now

        open_trades = snapshot.get("open_trades") or []
        open_ids = {str(item.get("id")) for item in open_trades if item.get("id") is not None}
        opened = sorted(open_ids - self.prev_open_ids)
        closed = sorted(self.prev_open_ids - open_ids)

        by_id = {str(item.get("id")): item for item in open_trades if item.get("id") is not None}
        for trade_id in opened:
            item = by_id.get(trade_id, {})
            self._write_line(
                "TRADE_OPEN "
                f"id={trade_id} asset={item.get('asset')} side={item.get('side')} "
                f"entry={self._fmt(self._safe_float(item.get('entry')))} "
                f"rr={self._fmt(self._safe_float(item.get('rr')), 3)}"
            )
        for trade_id in closed:
            self._write_line(f"TRADE_CLOSED_LIVE id={trade_id}")

        self.prev_open_ids = open_ids

        decision_ts = snapshot.get("last_decision_ts") or ""
        decision = snapshot.get("last_decision") or ""
        decision_asset = snapshot.get("last_decision_asset") or ""
        decision_reason = snapshot.get("last_decision_reason") or ""
        decision_key = f"{decision_ts}|{decision}|{decision_asset}|{decision_reason}"
        if decision and decision_key != self.last_decision_key:
            self._write_line(
                "DECISION "
                f"ts={decision_ts or 'n/a'} decision={decision} asset={decision_asset or '-'} reason={decision_reason or '-'}"
            )
            self.last_decision_key = decision_key

    def _log_closed_trades_from_db(self, stats: SessionStats) -> None:
        new_rows = self._load_new_closed_trades()
        for trade in new_rows:
            pnl_net = self._safe_float(trade.get("pnl"), 0.0)
            pnl_gross = self._safe_float(trade.get("pnl_gross"), pnl_net)
            fee_cost = self._safe_float(trade.get("fee_cost"), 0.0)
            funding_cost = self._safe_float(trade.get("funding_cost"), 0.0)
            stats.closed_trades += 1
            stats.journal_gross_pnl += pnl_gross
            stats.journal_fee_cost += fee_cost
            stats.journal_funding_cost += funding_cost
            stats.journal_net_pnl += pnl_net

            if pnl_net > 0:
                stats.winners += 1
            elif pnl_net < 0:
                stats.losers += 1
            self._write_line(
                "TRADE_JOURNAL "
                f"id={trade.get('id')} ts={trade.get('ts')} asset={trade.get('asset')} side={trade.get('side')} "
                f"size={self._fmt(self._safe_float(trade.get('size')), 6)} entry={self._fmt(self._safe_float(trade.get('entry')))} "
                f"exit={self._fmt(self._safe_float(trade.get('exit')))} gross={pnl_gross:+.2f} fee={fee_cost:+.2f} "
                f"funding={funding_cost:+.2f} net={pnl_net:+.2f} reason={trade.get('reason')}"
            )

    def _log_heartbeat_if_due(self, stats: SessionStats, snapshot: dict) -> None:
        now = self._now()
        if self.last_heartbeat and (now - self.last_heartbeat).total_seconds() < self.heartbeat_sec:
            return

        open_count = int(snapshot.get("open_trades_count") or 0)
        equity = self._safe_float(snapshot.get("equity"), stats.last_equity or 0.0)
        realized = self._safe_float(snapshot.get("sim_realized_pnl"), stats.realized_pnl_last)
        base = stats.start_equity if stats.start_equity is not None else equity
        since_start = equity - base

        self._write_line(
            "HEARTBEAT "
            f"equity={self._fmt(equity)} since_start={since_start:+.2f} sim_realized_pnl={self._fmt(realized)} "
            f"open_trades={open_count} closed_trades={stats.closed_trades} winners={stats.winners} losers={stats.losers} "
            f"gross={stats.journal_gross_pnl:+.2f} fee={stats.journal_fee_cost:+.2f} "
            f"funding={stats.journal_funding_cost:+.2f} net={stats.journal_net_pnl:+.2f}"
        )
        self.last_heartbeat = now

    def _log_end(self, stats: SessionStats) -> None:
        start = stats.start_equity if stats.start_equity is not None else 0.0
        end = stats.last_equity if stats.last_equity is not None else start
        net = end - start
        roi = 0.0 if start == 0 else (net / start) * 100.0

        self._write_line(
            "SESSION_END "
            f"start_equity={self._fmt(start)} end_equity={self._fmt(end)} net={net:+.2f} roi={roi:+.2f}% "
            f"max_equity={self._fmt(stats.max_equity)} min_equity={self._fmt(stats.min_equity)} "
            f"sim_realized_pnl_delta={(stats.realized_pnl_last - stats.realized_pnl_start):+.2f} "
            f"closed_trades={stats.closed_trades} winners={stats.winners} losers={stats.losers} "
            f"equity_updates={stats.equity_updates} journal_gross={stats.journal_gross_pnl:+.2f} "
            f"journal_fee={stats.journal_fee_cost:+.2f} journal_funding={stats.journal_funding_cost:+.2f} "
            f"journal_net={stats.journal_net_pnl:+.2f}"
        )

    async def run(self, duration_hours: float) -> None:
        start_ts = self._now()
        end_ts = start_ts + timedelta(hours=max(0.001, float(duration_hours)))
        stats = SessionStats(start_ts=start_ts, end_ts=end_ts)

        self.seen_closed_trade_ids = self._existing_closed_trade_ids()
        self._write_line(
            f"PAPERTRADER_INIT ws_url={self.ws_url} log={self.log_path} duration_h={duration_hours:.2f} baseline_closed_trades={len(self.seen_closed_trade_ids)}"
        )

        while self._now() < end_ts:
            snapshot = await self._fetch_snapshot()
            if snapshot is None:
                await asyncio.sleep(self.interval_sec)
                continue

            if stats.start_equity is None:
                self._log_start(stats, snapshot)

            self._log_snapshot_changes(stats, snapshot)
            self._log_closed_trades_from_db(stats)
            self._log_heartbeat_if_due(stats, snapshot)

            await asyncio.sleep(self.interval_sec)

        await self._close_ws()
        self._log_end(stats)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper trading session logger for simulation performance evaluation")
    parser.add_argument("--duration-hours", type=float, default=18.0, help="How long to run the logger (default: 18)")
    parser.add_argument("--interval-sec", type=float, default=5.0, help="Polling interval in seconds (default: 5)")
    parser.add_argument("--heartbeat-sec", type=int, default=300, help="Heartbeat interval in seconds (default: 300)")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765", help="Console websocket URL")
    parser.add_argument("--log", default="paper.log", help="Output log file path")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = PaperTraderLogger(
        log_path=args.log,
        ws_url=args.ws_url,
        interval_sec=args.interval_sec,
        heartbeat_sec=args.heartbeat_sec,
    )
    try:
        with SingleInstanceLock("tradebot-papertrader"):
            asyncio.run(runner.run(duration_hours=args.duration_hours))
    except SingleInstanceError as exc:
        runner._write_line(f"STARTUP_BLOCKED {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        runner._write_line("SESSION_ABORTED keyboard_interrupt")


if __name__ == "__main__":
    main()
