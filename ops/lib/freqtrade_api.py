"""Thin localhost REST client for the two freqtrade bots (basic auth per request).

Used by healthcheck, nav_job and the Telegram bot. Never raises into callers'
control flow for availability probes; action calls raise FreqtradeApiError so the
caller can alert.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ops.config import EarnConfig


class FreqtradeApiError(Exception):
    pass


class BotApi:
    def __init__(self, base_url: str, password_env: str, *, username: str = "earn",
                 client: httpx.Client | None = None, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.auth = (username, os.environ.get(password_env, ""))
        self.client = client or httpx.Client(timeout=timeout)

    @classmethod
    def for_sleeve(cls, cfg: EarnConfig, sleeve: str, client: httpx.Client | None = None) -> BotApi:
        b = cfg.ops.bots[sleeve]
        return cls(b.api, b.password_env, client=client)

    def _get(self, path: str) -> Any:
        r = self.client.get(f"{self.base}/api/v1/{path}", auth=self.auth)
        if r.status_code != 200:
            raise FreqtradeApiError(f"GET {path} -> {r.status_code}")
        return r.json()

    def _post(self, path: str, payload: dict | None = None) -> Any:
        r = self.client.post(f"{self.base}/api/v1/{path}", json=payload or {}, auth=self.auth)
        if r.status_code != 200:
            raise FreqtradeApiError(f"POST {path} -> {r.status_code}")
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self.client.delete(f"{self.base}/api/v1/{path}", auth=self.auth)
        if r.status_code != 200:
            raise FreqtradeApiError(f"DELETE {path} -> {r.status_code}")
        return r.json()

    # -- probes (never raise) --------------------------------------------------

    def ping(self) -> bool:
        try:
            r = self.client.get(f"{self.base}/api/v1/ping")
            return r.status_code == 200 and r.json().get("status") == "pong"
        except Exception:
            return False

    def health(self) -> dict | None:
        try:
            return self._get("health")
        except Exception:
            return None

    # -- reads -----------------------------------------------------------------

    def balance(self) -> dict:
        return self._get("balance")

    def status(self) -> list[dict]:
        return self._get("status")

    def profit(self) -> dict:
        return self._get("profit")

    def show_config(self) -> dict:
        """The bot's effective config: ``dry_run``, ``strategy``, ``bot_name``, ``state``…

        This is what the mode transition verifies at step 9 and what the console's bot
        cards and the live preflight read, so a mismatch is caught before an order is.
        """
        return self._get("show_config")

    def locks(self) -> dict:
        """Active pair locks (``{"lock_count": N, "locks": [...]}``)."""
        return self._get("locks")

    def trades(self, limit: int = 100, offset: int = 0) -> dict:
        """Closed trades, newest first."""
        return self._get(f"trades?limit={int(limit)}&offset={int(offset)}")

    def performance(self) -> list[dict]:
        """Per-pair realised performance."""
        return self._get("performance")

    # -- actions ---------------------------------------------------------------

    def forceexit(self, tradeid: str = "all") -> dict:
        return self._post("forceexit", {"tradeid": tradeid})

    def stopentry(self) -> dict:
        """Stop opening new trades. ``stopentry`` on 2026.8, ``stopbuy`` before it.

        THE one implementation. Five callers used to carry their own
        ``getattr(api, 'stopentry', 'stopbuy')`` shim, which meant five places to be
        wrong about which name this freqtrade answers to.
        """
        try:
            return self._post("stopentry")
        except FreqtradeApiError as e:
            if " -> 404" not in str(e) and " -> 405" not in str(e):
                raise
            return self._post("stopbuy")

    def stopbuy(self) -> dict:
        """Pre-2026.8 name for :meth:`stopentry`. Kept for the older API only."""
        return self._post("stopbuy")

    def start(self) -> dict:
        """Leave the ``stopped``/``stop_entry`` state and trade normally again."""
        return self._post("start")

    def stop(self) -> dict:
        return self._post("stop")

    def delete_lock(self, lock_id: int | str) -> dict:
        """Remove one freqtrade pair lock (the Risk page's resume wizard)."""
        return self._delete(f"locks/{lock_id}")

    def cancel_open_order(self, trade_id: int) -> dict:
        return self._delete(f"trades/{trade_id}/open-order")

    def reload_config(self) -> dict:
        return self._post("reload_config")
