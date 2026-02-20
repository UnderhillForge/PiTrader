#!/usr/bin/env python3
import argparse
import json
import sqlite3
from collections import defaultdict


def _load_events(db_path, decision_id=None, trade_id=None, limit=200):
    where = []
    params = []
    if decision_id:
        where.append("decision_id = ?")
        params.append(str(decision_id))
    if trade_id:
        where.append("trade_id = ?")
        params.append(str(trade_id))

    sql = (
        "SELECT event_id, ts, event_type, decision_id, trade_id, asset, payload "
        "FROM trade_events"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, event_id ASC LIMIT ?"
    params.append(max(1, int(limit)))

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS trade_events (event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_type TEXT NOT NULL, decision_id TEXT, trade_id TEXT, asset TEXT, payload TEXT NOT NULL)"
        )
        rows = conn.execute(sql, tuple(params)).fetchall()

    events = []
    for row in rows:
        payload = row[6]
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            payload = {"raw": str(payload)}
        events.append(
            {
                "event_id": row[0],
                "ts": row[1],
                "event_type": row[2],
                "decision_id": row[3],
                "trade_id": row[4],
                "asset": row[5],
                "payload": payload,
            }
        )
    return events


def _print_timeline(events, show_payload=False):
    if not events:
        print("No events found for the selected filter.")
        return

    print(f"events={len(events)}")
    by_trade = defaultdict(int)
    for event in events:
        by_trade[str(event.get("trade_id") or "-")] += 1

    if len(by_trade) > 1:
        print("trades:")
        for trade_key, count in sorted(by_trade.items(), key=lambda item: item[0]):
            print(f"  {trade_key}: {count} events")

    for event in events:
        header = (
            f"{event.get('ts')} | {event.get('event_type')}"
            f" | decision_id={event.get('decision_id') or '-'}"
            f" | trade_id={event.get('trade_id') or '-'}"
            f" | asset={event.get('asset') or '-'}"
        )
        print(header)
        payload = event.get("payload") or {}
        if show_payload and payload:
            print("  payload:", json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _build_parser():
    parser = argparse.ArgumentParser(description="Replay event-sourced trade journal timelines.")
    parser.add_argument("--db", default="trades.db", help="Path to SQLite database (default: trades.db)")
    parser.add_argument("--decision-id", default=None, help="Filter by decision_id")
    parser.add_argument("--trade-id", default=None, help="Filter by trade_id")
    parser.add_argument("--limit", type=int, default=200, help="Max events to load (default: 200)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of timeline")
    parser.add_argument("--show-payload", action="store_true", help="Include payload details in timeline output")
    return parser


def main():
    args = _build_parser().parse_args()
    events = _load_events(
        db_path=args.db,
        decision_id=args.decision_id,
        trade_id=args.trade_id,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(events, indent=2, sort_keys=True, default=str))
        return

    _print_timeline(events, show_payload=bool(args.show_payload))


if __name__ == "__main__":
    main()
