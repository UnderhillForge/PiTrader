import asyncio
import sys
from modules.db import init_db, load_state
from modules.config import DATA_ONLY_MODE, SIMULATION_MODE, logger
from modules.portfolio import rebalance_checker
from modules.grok import grok_loop
from modules.market import start_websockets
from modules.rumors import fetch_loop
from modules.console_ws import serve as serve_console_ws
from modules.health import health_watchdog_loop
from modules.drawdown import drawdown_guard_loop
from modules.daily_score import daily_score_loop
from modules.trade import monitor_trades_loop, recover_open_trades
from modules.single_instance import SingleInstanceError, SingleInstanceLock

async def main():
    init_db()
    load_state()
    recover_open_trades()

    # Start core loops
    tasks = [
        asyncio.create_task(rebalance_checker()),
        asyncio.create_task(start_websockets()),
        asyncio.create_task(fetch_loop()),
        asyncio.create_task(serve_console_ws()),
        asyncio.create_task(health_watchdog_loop()),
        asyncio.create_task(drawdown_guard_loop()),
        asyncio.create_task(daily_score_loop()),
    ]

    if DATA_ONLY_MODE:
        logger.info("Running in DATA_ONLY_MODE: Grok decisioning and trade monitor are disabled")
    else:
        tasks.append(asyncio.create_task(grok_loop()))
        tasks.append(asyncio.create_task(monitor_trades_loop()))

    if SIMULATION_MODE:
        logger.info("Running in SIMULATION_MODE: real orders are disabled (dry-run execution)")

    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        with SingleInstanceLock("tradebot-main"):
            asyncio.run(main())
    except SingleInstanceError as exc:
        logger.error("Startup blocked: %s", exc)
        sys.exit(1)