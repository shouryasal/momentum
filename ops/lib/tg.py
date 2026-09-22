"""The one Telegram alert sender. Outbound HTTPS only; deliberately independent of
python-telegram-bot so alerts survive a library break.

send() dedupes by (dedupe_key, ttl) via the knowledge DB's ops_alerts table when a
connection is provided, retries 3x with backoff, and NEVER raises — an alerting
failure must not take a job down. Delivery failures are recorded for healthcheck.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

SEVERITIES = ("info", "warn", "critical")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_transport(token: str, chat_id: str, text: str) -> bool:
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=10,
    )
    return r.status_code == 200


def _deduped(conn: sqlite3.Connection, key: str, ttl_min: int, now: datetime) -> bool:
    row = conn.execute(
        "SELECT sent_at FROM ops_alerts WHERE dedupe_key=? AND delivered=1 ORDER BY sent_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return False
    sent = datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
    return now - sent < timedelta(minutes=ttl_min)


def send(
    text: str,
    severity: str = "info",
    dedupe_key: str | None = None,
    ttl_min: int = 60,
    *,
    conn: sqlite3.Connection | None = None,
    token: str | None = None,
    chat_id: str | None = None,
    transport: Callable[[str, str, str], bool] | None = None,
    now: datetime | None = None,
) -> bool:
    """Returns True when delivered (or suppressed as a duplicate), False on failure."""
    if severity not in SEVERITIES:
        severity = "warn"
    ts = now or _now()
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    transport = transport or _default_transport

    try:
        if dedupe_key and conn is not None and _deduped(conn, dedupe_key, ttl_min, ts):
            return True

        delivered = False
        if token and chat_id:
            prefix = {"info": "", "warn": "⚠️ ", "critical": "🚨 "}[severity]
            for attempt in range(3):
                try:
                    if transport(token, chat_id, prefix + text):
                        delivered = True
                        break
                except Exception:
                    pass
                time.sleep(min(2**attempt, 4))
        if conn is not None:
            conn.execute(
                "INSERT INTO ops_alerts(sent_at, severity, dedupe_key, message, delivered)"
                " VALUES (?,?,?,?,?)",
                (_iso(ts), severity, dedupe_key, text, int(delivered)),
            )
            conn.commit()
        return delivered
    except Exception:
        return False
