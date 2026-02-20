import asyncio
from datetime import datetime

from .config import (
	AGGR_PCT,
	MIN_AGGR,
	REBALANCE_DAY,
	REBALANCE_HOUR,
	SPLIT_THRESHOLD,
	SIMULATION_MODE,
	TRADE_BALANCE,
	client,
	logger,
	state,
)
from .db import save_state
from .health import mark_exchange_failure, mark_exchange_success


def _as_float(value, default=0.0):
	try:
		if isinstance(value, dict):
			value = value.get("value", default)
		return float(value)
	except (TypeError, ValueError):
		return float(default)


def _portfolio_cash_available():
	if client is None:
		return None

	portfolio_id = None
	try:
		portfolios_resp = client.get_portfolios()
		portfolios = portfolios_resp.to_dict().get("portfolios", []) if hasattr(portfolios_resp, "to_dict") else []
		for portfolio in portfolios:
			if str(portfolio.get("type") or "").upper() == "DEFAULT":
				portfolio_id = portfolio.get("uuid") or portfolio.get("id") or portfolio.get("portfolio_id")
				break
	except Exception:
		portfolio_id = None

	if not portfolio_id:
		try:
			accounts = client.get_accounts()
			rows = accounts.to_dict().get("accounts", []) if hasattr(accounts, "to_dict") else []
			for row in rows:
				candidate = row.get("retail_portfolio_id")
				if candidate:
					portfolio_id = candidate
					break
		except Exception:
			portfolio_id = None

	if not portfolio_id:
		return None

	try:
		breakdown_resp = client.get_portfolio_breakdown(portfolio_uuid=portfolio_id)
		payload = breakdown_resp.to_dict() if hasattr(breakdown_resp, "to_dict") else {}
		spot_positions = ((payload or {}).get("breakdown") or {}).get("spot_positions") or []
	except Exception:
		return None

	usd_usdc = 0.0
	for position in spot_positions:
		asset = str(position.get("asset") or "").upper()
		if asset not in {"USD", "USDC"}:
			continue
		usd_usdc += _as_float(position.get("available_to_trade_fiat"), 0.0)
	if usd_usdc > 0:
		return usd_usdc

	cash_total = 0.0
	for position in spot_positions:
		if not bool(position.get("is_cash", False)):
			continue
		cash_total += _as_float(position.get("available_to_trade_fiat"), 0.0)
	return cash_total if cash_total > 0 else None


async def get_equity():
	if client is None:
		if state.get("mode") == "nuclear":
			state["aggr_target"] = float(state.get("equity", 0) or 0)
			state["safe_target"] = 0.0
		return state["equity"]
	try:
		accounts = client.get_accounts()
		raw_total = sum(
			float(account.available_balance.value)
			for account in accounts.accounts
			if account.currency in ("USD", "USDC")
		)
		if raw_total <= 0:
			portfolio_cash = _portfolio_cash_available()
			if portfolio_cash is not None:
				raw_total = float(portfolio_cash)
		total = min(raw_total, TRADE_BALANCE) if TRADE_BALANCE is not None else raw_total
		state["equity_raw"] = raw_total
		if SIMULATION_MODE:
			sim_pnl = float(state.get("sim_realized_pnl", 0.0) or 0.0)
			state["sim_base_equity"] = float(total)
			state["equity"] = max(0.0, float(total) + sim_pnl)
		else:
			state["equity"] = total
		if state.get("mode") == "nuclear":
			state["aggr_target"] = float(state.get("equity", total) or 0)
			state["safe_target"] = 0.0
		mark_exchange_success("portfolio_equity")
		return float(state.get("equity", total) or 0)
	except (AttributeError, TypeError, ValueError) as exc:
		mark_exchange_failure("portfolio_equity", exc)
		return state["equity"]


async def rebalance():
	total = await get_equity()
	if total < SPLIT_THRESHOLD:
		if state["mode"] != "nuclear":
			state["mode"] = "nuclear"
			state["aggr_target"] = total
			state["safe_target"] = 0
			save_state(total, "nuclear", total, 0, "below threshold")
		return

	target_aggr = max(total * AGGR_PCT, MIN_AGGR)
	target_safe = total - target_aggr

	curr_aggr = state["aggr_target"]
	drift = abs(curr_aggr - target_aggr) / total if total > 0 else 0

	if drift < 0.05 and state["mode"] == "hybrid":
		return

	if curr_aggr > target_aggr:
		logger.info("Closing ~$%.2f from aggressive", curr_aggr - target_aggr)

	state.update(
		mode="hybrid",
		aggr_target=target_aggr,
		safe_target=target_safe,
		last_rebal=datetime.utcnow(),
	)
	save_state(total, "hybrid", target_aggr, target_safe, f"drift {drift:.1%}")


async def rebalance_checker():
	while True:
		await get_equity()
		now = datetime.utcnow()
		if now.weekday() == REBALANCE_DAY and now.hour == REBALANCE_HOUR and now.minute == 0:
			if state["last_rebal"] is None or (now - state["last_rebal"]).total_seconds() > 3600:
				await rebalance()
				state["last_rebal"] = now
		await asyncio.sleep(300)
