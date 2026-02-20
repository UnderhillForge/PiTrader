#!/usr/bin/env python3
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from twscrape import API


def _load_x_json(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to parse {path}: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _get(name: str, data: dict, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return str(value).strip()
    return str(data.get(name, default) or "").strip()


def _require(name: str, data: dict) -> str:
    value = _get(name, data)
    if not value:
        raise ValueError(f"Missing required value: {name} (env or x.json)")
    return value


def _delete_account_row(username: str) -> None:
    with sqlite3.connect("accounts.db") as conn:
        conn.execute("DELETE FROM accounts WHERE lower(username)=lower(?)", (username,))


def _config_path() -> str:
    return (sys.argv[1].strip() if len(sys.argv) > 1 else "x.json")


async def _setup_account() -> str:
    x_cfg = _load_x_json(_config_path())

    username = _require("TW_USERNAME", x_cfg).lstrip("@").strip()
    password = _require("TW_PASSWORD", x_cfg)

    email = _get("TW_EMAIL", x_cfg)
    email_password = _get("TW_EMAIL_PASSWORD", x_cfg)
    cookies = _get("TW_COOKIES", x_cfg) or None
    mfa_code = _get("TW_MFA_CODE", x_cfg) or None
    proxy = _get("TW_PROXY", x_cfg) or None

    if not cookies and (not email or not email_password):
        raise ValueError(
            "Provide either TW_COOKIES, or both TW_EMAIL and TW_EMAIL_PASSWORD."
        )

    _delete_account_row(username)

    api = API()

    await api.pool.add_account(
        username=username,
        password=password,
        email=email or "cookie@login.local",
        email_password=email_password or "cookie",
        proxy=proxy,
        cookies=cookies,
        mfa_code=mfa_code,
    )

    try:
        await api.pool.login_all(usernames=[username])
    except Exception as exc:
        print(f"login_all warning: {exc}")

    return username


def _print_status(username: str) -> int:
    with sqlite3.connect("accounts.db") as conn:
        row = conn.execute(
            "SELECT username, active, error_msg FROM accounts WHERE lower(username)=lower(?)",
            (username,),
        ).fetchone()

    if not row:
        print(f"No account row found for {username}")
        return 2

    uname, active, error_msg = row
    print(f"username={uname}")
    print(f"active={int(bool(active))}")
    print(f"error_msg={error_msg or ''}")
    return 0 if bool(active) else 1


def main() -> int:
    try:
        username = asyncio.run(_setup_account())
        return _print_status(username)
    except Exception as exc:
        print(f"setup failed: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
