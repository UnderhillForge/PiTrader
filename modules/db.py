import sqlite3
import json
from datetime import datetime

from .config import state

try:
	from dateutil import parser as dt_parser
except ImportError:
	dt_parser = None


def init_db():
	for db, sql in [
		(
			"trades.db",
			"CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, ts TEXT, asset TEXT, side TEXT, size REAL, entry REAL, exit REAL, pnl REAL, pnl_gross REAL, fee_cost REAL, funding_cost REAL, reason TEXT)",
		),
		(
			"trades.db",
			"CREATE TABLE IF NOT EXISTS trade_events (event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_type TEXT NOT NULL, decision_id TEXT, trade_id TEXT, asset TEXT, payload TEXT NOT NULL)",
		),
		("trades.db", "CREATE TABLE IF NOT EXISTS live_trades (id TEXT PRIMARY KEY, updated_ts TEXT, payload TEXT NOT NULL)"),
		("rumors.db", "CREATE TABLE IF NOT EXISTS rumors (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, asset TEXT, rumor TEXT, sent REAL, pump INTEGER, whale TEXT)"),
		("portfolio.db", "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total REAL, mode TEXT, aggr REAL, safe REAL, reason TEXT)"),
		("portfolio.db", "CREATE TABLE IF NOT EXISTS equity_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, equity REAL NOT NULL)")
	]:
		with sqlite3.connect(db) as conn:
			conn.execute(sql)

	with sqlite3.connect("trades.db") as conn:
		columns = {
			str(row[1]).strip().lower()
			for row in conn.execute("PRAGMA table_info(trades)").fetchall()
			if len(row) > 1
		}
		if "pnl_gross" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN pnl_gross REAL")
		if "fee_cost" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN fee_cost REAL")
		if "funding_cost" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN funding_cost REAL")


def load_state():
	with sqlite3.connect("portfolio.db") as conn:
		row = conn.execute("SELECT * FROM state ORDER BY id DESC LIMIT 1").fetchone()
	if row:
		if row[1]:
			if dt_parser is not None:
				parsed_ts = dt_parser.parse(row[1])
			else:
				parsed_ts = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
		else:
			parsed_ts = None
		state.update(
			mode=row[3],
			aggr_target=row[4] or 0,
			safe_target=row[5] or 0,
			last_rebal=parsed_ts,
		)


def save_state(total, mode, aggr, safe, reason=""):
	with sqlite3.connect("portfolio.db") as conn:
		conn.execute(
			"INSERT INTO state (ts, total, mode, aggr, safe, reason) VALUES (?,?,?,?,?,?)",
			(datetime.utcnow().isoformat(), total, mode, aggr, safe, reason),
		)


def save_equity_history_point(ts_text, equity):
	with sqlite3.connect("portfolio.db") as conn:
		conn.execute(
			"CREATE TABLE IF NOT EXISTS equity_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, equity REAL NOT NULL)"
		)
		conn.execute(
			"INSERT INTO equity_history (ts, equity) VALUES (?,?)",
			(ts_text, float(equity)),
		)


def load_equity_history_points(limit=3000):
	limit = max(1, int(limit))
	with sqlite3.connect("portfolio.db") as conn:
		conn.execute(
			"CREATE TABLE IF NOT EXISTS equity_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, equity REAL NOT NULL)"
		)
		rows = conn.execute(
			"SELECT ts, equity FROM equity_history ORDER BY id DESC LIMIT ?",
			(limit,),
		).fetchall()

	rows.reverse()
	result = []
	for ts_text, equity in rows:
		try:
			result.append({"ts": str(ts_text), "equity": float(equity)})
		except (TypeError, ValueError):
			continue
	return result


def save_live_trade(trade_id, trade_payload):
	with sqlite3.connect("trades.db") as conn:
		conn.execute(
			"INSERT OR REPLACE INTO live_trades (id, updated_ts, payload) VALUES (?,?,?)",
			(
				trade_id,
				datetime.utcnow().isoformat(),
				json.dumps(trade_payload, separators=(",", ":"), default=str),
			),
		)


def delete_live_trade(trade_id):
	with sqlite3.connect("trades.db") as conn:
		conn.execute("DELETE FROM live_trades WHERE id = ?", (trade_id,))


def load_live_trades():
	result = {}
	with sqlite3.connect("trades.db") as conn:
		rows = conn.execute("SELECT id, payload FROM live_trades").fetchall()

	for trade_id, payload in rows:
		try:
			result[trade_id] = json.loads(payload)
		except (TypeError, ValueError):
			continue
	return result


def save_trade_journal(
	trade_id,
	ts,
	asset,
	side,
	size,
	entry,
	exit_price,
	pnl,
	reason,
	pnl_gross=None,
	fee_cost=None,
	funding_cost=None,
):
	with sqlite3.connect("trades.db") as conn:
		conn.execute(
			"CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, ts TEXT, asset TEXT, side TEXT, size REAL, entry REAL, exit REAL, pnl REAL, pnl_gross REAL, fee_cost REAL, funding_cost REAL, reason TEXT)"
		)
		columns = {
			str(row[1]).strip().lower()
			for row in conn.execute("PRAGMA table_info(trades)").fetchall()
			if len(row) > 1
		}
		if "pnl_gross" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN pnl_gross REAL")
		if "fee_cost" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN fee_cost REAL")
		if "funding_cost" not in columns:
			conn.execute("ALTER TABLE trades ADD COLUMN funding_cost REAL")

		conn.execute(
			"""
			INSERT OR REPLACE INTO trades (id, ts, asset, side, size, entry, exit, pnl, pnl_gross, fee_cost, funding_cost, reason)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
			""",
			(
				trade_id,
				ts,
				asset,
				side,
				size,
				entry,
				exit_price,
				pnl,
				pnl_gross,
				fee_cost,
				funding_cost,
				reason,
			),
		)


def save_trade_event(event_id, ts, event_type, decision_id=None, trade_id=None, asset=None, payload=None):
	if payload is None:
		payload = {}
	with sqlite3.connect("trades.db") as conn:
		conn.execute(
			"CREATE TABLE IF NOT EXISTS trade_events (event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, event_type TEXT NOT NULL, decision_id TEXT, trade_id TEXT, asset TEXT, payload TEXT NOT NULL)"
		)
		conn.execute(
			"""
			INSERT INTO trade_events (event_id, ts, event_type, decision_id, trade_id, asset, payload)
			VALUES (?,?,?,?,?,?,?)
			""",
			(
				str(event_id),
				str(ts),
				str(event_type),
				None if decision_id is None else str(decision_id),
				None if trade_id is None else str(trade_id),
				None if asset is None else str(asset),
				json.dumps(payload, separators=(",", ":"), default=str),
			),
		)


def get_recent_trades_for_asset(asset, limit=12):
	limit = max(1, int(limit))
	if not asset:
		return []

	with sqlite3.connect("trades.db") as conn:
		rows = conn.execute(
			"""
			SELECT ts, asset, side, size, entry, exit, pnl, reason
			FROM trades
			WHERE asset = ?
			ORDER BY ts DESC
			LIMIT ?
			""",
			(str(asset), limit),
		).fetchall()

	result = []
	for ts, row_asset, side, size, entry, exit_price, pnl, reason in rows:
		try:
			result.append(
				{
					"ts": str(ts),
					"asset": str(row_asset),
					"side": str(side),
					"size": float(size or 0.0),
					"entry": float(entry or 0.0),
					"exit": float(exit_price or 0.0),
					"pnl": float(pnl or 0.0),
					"reason": str(reason or ""),
				}
			)
		except (TypeError, ValueError):
			continue

	return result
