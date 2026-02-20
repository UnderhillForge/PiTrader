#!/usr/bin/env python3
import argparse
import os
import shutil
import sqlite3
from datetime import datetime


def _backup(path: str, backup_dir: str) -> str | None:
    if not os.path.exists(path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = os.path.basename(path)
    dst = os.path.join(backup_dir, f"{base}.{ts}.bak")
    shutil.copy2(path, dst)
    return dst


def _exec(db_path: str, sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(sql, params)
        conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset TradeBot simulation state for a clean overnight dry-run.")
    ap.add_argument("--no-backup", action="store_true", help="Do not create timestamped DB backups.")
    ap.add_argument(
        "--backup-dir",
        default="backups",
        help="Directory to write timestamped backups into (default: backups/).",
    )
    ap.add_argument(
        "--wipe-history",
        action="store_true",
        help="Also wipe trades.db trade history (trades + trade_events). Not recommended for normal resets.",
    )
    ap.add_argument(
        "--wipe-rumors",
        action="store_true",
        help="Also wipe rumors.db rumors table.",
    )

    args = ap.parse_args()

    backups = []
    if not args.no_backup:
        for db in ("trades.db", "portfolio.db", "rumors.db"):
            saved = _backup(db, args.backup_dir)
            if saved:
                backups.append(saved)

    if os.path.exists("trades.db"):
        # Ensure tables exist, then clear only the recoverable open-trade state.
        _exec(
            "trades.db",
            "CREATE TABLE IF NOT EXISTS live_trades (id TEXT PRIMARY KEY, updated_ts TEXT, payload TEXT NOT NULL)",
        )
        _exec("trades.db", "DELETE FROM live_trades")

        if args.wipe_history:
            _exec(
                "trades.db",
                "CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, ts TEXT, asset TEXT, side TEXT, size REAL, entry REAL, exit REAL, pnl REAL, pnl_gross REAL, fee_cost REAL, funding_cost REAL, reason TEXT)",
            )
            _exec(
                "trades.db",
                "CREATE TABLE IF NOT EXISTS trade_events (event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_type TEXT NOT NULL, decision_id TEXT, trade_id TEXT, asset TEXT, payload TEXT NOT NULL)",
            )
            _exec("trades.db", "DELETE FROM trades")
            _exec("trades.db", "DELETE FROM trade_events")

    if os.path.exists("portfolio.db"):
        # Reset equity curve for a clean simulation run; keep portfolio 'state' by default.
        _exec(
            "portfolio.db",
            "CREATE TABLE IF NOT EXISTS equity_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, equity REAL NOT NULL)",
        )
        _exec("portfolio.db", "DELETE FROM equity_history")

        if args.wipe_history:
            _exec(
                "portfolio.db",
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total REAL, mode TEXT, aggr REAL, safe REAL, reason TEXT)",
            )
            _exec("portfolio.db", "DELETE FROM state")

    if args.wipe_rumors and os.path.exists("rumors.db"):
        _exec(
            "rumors.db",
            "CREATE TABLE IF NOT EXISTS rumors (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, asset TEXT, rumor TEXT, sent REAL, pump INTEGER, whale TEXT)",
        )
        _exec("rumors.db", "DELETE FROM rumors")

    print("Reset complete.")
    if backups:
        print("Backups:")
        for item in backups:
            print(f"- {item}")
    else:
        print("No backups created.")

    print("\nNotes:")
    print("- This clears recovered open trades (live_trades) and resets the equity curve (equity_history).")
    print("- SIMULATION_MODE and DRY_RUN_ORDERS are controlled via config.json/env and must already be enabled for safety.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
