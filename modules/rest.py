import logging

from .config import client, state

logger = logging.getLogger(__name__)


async def rest_in_usdc():
    logger.info("Idle → resting in USDC")

    if client is None:
        return

    for pos_key, position in list(state["positions"].items()):
        raw_size = float(position.get("size", 0))
        size = abs(raw_size)
        if size <= 0.0001:
            continue

        side_raw = str(position.get("side", "")).strip().lower()
        if side_raw in ("long", "buy"):
            close_side = "SELL"
        elif side_raw in ("short", "sell"):
            close_side = "BUY"
        else:
            close_side = "SELL" if raw_size > 0 else "BUY"

        product_id = position.get("product_id") or position.get("asset") or pos_key
        try:
            client.market_order(product_id, close_side, size)
        except (AttributeError, TypeError, ValueError):
            pass

    try:
        accounts = client.get_accounts()
        for account in accounts.accounts:
            currency = account.currency
            balance = float(account.available_balance.value)
            if currency not in ("USDC", "USD") and balance > 1:
                pair = f"{currency}-USDC"
                client.market_order_sell(pair, balance)
    except (AttributeError, TypeError, ValueError):
        pass