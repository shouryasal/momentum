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

    # -- actions ---------------------------------------------------------------

    def forceexit(self, tradeid: str = "all") -> dict:
        return self._post("forceexit", {"tradeid": tradeid})

    def stopbuy(self) -> dict:
        return self._post("stopbuy")

    def cancel_open_order(self, trade_id: int) -> dict:
        return self._delete(f"trades/{trade_id}/open-order")

    def reload_config(self) -> dict:
        return self._post("reload_config")
