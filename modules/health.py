import asyncio
from datetime import datetime

from . import config as cfg


def _utcnow_iso():
    return datetime.utcnow().isoformat()


def _seconds_since(ts_text):
    if not ts_text:
        return None
    try:
        when = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return max(0.0, (datetime.utcnow() - when).total_seconds())


def get_health_state():
    return str(cfg.state.get("health_state", "healthy") or "healthy")


def _set_health_state(new_state, reason):
    current = get_health_state()
    if current == new_state:
        return

    cfg.state["health_state"] = new_state
    cfg.state["health_last_transition_ts"] = _utcnow_iso()

    if new_state == "outage":
        cfg.state["health_outage_since_ts"] = cfg.state["health_last_transition_ts"]
        cfg.state["health_outage_flattened"] = False
        cfg.state["health_recovering_since_ts"] = None
    elif new_state == "recovering":
        cfg.state["health_recovering_since_ts"] = cfg.state["health_last_transition_ts"]
    else:
        cfg.state["health_outage_since_ts"] = None
        cfg.state["health_recovering_since_ts"] = None

    cfg.logger.warning("Health transition: %s -> %s (%s)", current, new_state, reason)


def mark_exchange_failure(source, exc=None):
    failure_count = int(cfg.state.get("health_consecutive_failures", 0) or 0) + 1
    cfg.state["health_consecutive_failures"] = failure_count
    cfg.state["health_consecutive_successes"] = 0
    cfg.state["health_last_failure_ts"] = _utcnow_iso()

    message = str(exc)[:180] if exc else "failure"
    reason = f"{source}: {message}"
    cfg.state["health_last_failure_reason"] = reason

    state = get_health_state()
    if failure_count >= int(cfg.HEALTH_OUTAGE_FAILURES):
        _set_health_state("outage", reason)
        return

    if state == "healthy" and failure_count >= int(cfg.HEALTH_DEGRADED_FAILURES):
        _set_health_state("degraded", reason)
        return

    if state == "recovering":
        _set_health_state("outage", reason)


def mark_exchange_success(source):
    cfg.state["health_last_success_ts"] = _utcnow_iso()
    cfg.state["health_consecutive_failures"] = 0
    success_count = int(cfg.state.get("health_consecutive_successes", 0) or 0) + 1
    cfg.state["health_consecutive_successes"] = success_count

    state = get_health_state()
    if state == "healthy":
        return

    if state == "outage":
        _set_health_state("recovering", f"{source}: connectivity restored")
        return

    if success_count >= int(cfg.HEALTH_RECOVER_SUCCESS_STREAK):
        _set_health_state("healthy", f"{source}: stable for {success_count} checks")


def entries_blocked():
    state = get_health_state()
    if state == "outage":
        return True
    if state == "recovering" and bool(cfg.HEALTH_BLOCK_RECOVERING):
        return True
    return False


async def _flatten_if_prolonged_outage():
    if get_health_state() != "outage":
        return
    if bool(cfg.state.get("health_outage_flattened", False)):
        return

    outage_age = _seconds_since(cfg.state.get("health_outage_since_ts"))
    if outage_age is None or outage_age < float(cfg.HEALTH_OUTAGE_FLATTEN_SEC):
        return

    from .rest import rest_in_usdc
    from .trade import force_close_all_open_trades

    await rest_in_usdc()
    force_close_all_open_trades("outage_flatten")
    cfg.state["health_outage_flattened"] = True
    cfg.state["parked"] = True

    try:
        with open(cfg.PARK_FLAG, "a", encoding="utf-8"):
            pass
    except OSError:
        pass

    cfg.logger.error(
        "Outage safety action: flattened and parked after %.0fs outage",
        float(outage_age),
    )


async def health_watchdog_loop():
    while True:
        cfg.reload_hot_config()
        try:
            await _flatten_if_prolonged_outage()
        except Exception as exc:
            cfg.logger.error("Health watchdog error: %s", exc)
        await asyncio.sleep(5)
