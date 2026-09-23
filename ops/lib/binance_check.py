"""Binance account probes the live preflight depends on — key permissions, account
identity, free balance and stop-order support.

Two layers, deliberately separated so the interesting part is unit-testable without a
network:

* :class:`BinanceClient` — a signed ``GET`` against ``api.binance.com``. It is the only
  thing here that touches the network, and nothing in the test suite constructs one.
* pure evaluators (:func:`check_restrictions`, :func:`account_uid`, :func:`free_balance`,
  :func:`supports_stop_orders`) that take the decoded JSON and return a verdict.

Every function that could carry key material returns redacted values only: the client's
errors name the endpoint and the HTTP status, never the query string (which contains the
signature), and :func:`describe_keys` reports presence and a four-character tail.

The permission requirements come from spec §8.3 check 4:

* ``enableWithdrawals`` **must** be false — a trading key that can withdraw is the one
  mistake that cannot be undone;
* ``enableSpotAndMarginTrading`` must be true, otherwise the bot cannot trade at all;
* every futures/options/margin permission must be off — Earn is spot only;
* ``ipRestrict`` is a warning, not a blocker: a home IP changes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://api.binance.com"
RESTRICTIONS_PATH = "/sapi/v1/account/apiRestrictions"
ACCOUNT_PATH = "/api/v3/account"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"

#: permissions that must be OFF for a spot trading key
FORBIDDEN_PERMISSIONS: tuple[str, ...] = (
    "enableWithdrawals",
    "enableInternalTransfer",
    "enableFutures",
    "enableMargin",
    "enableVanillaOptions",
    "permitsUniversalTransfer",
)
#: permissions that must be ON
REQUIRED_PERMISSIONS: tuple[str, ...] = ("enableSpotAndMarginTrading",)

#: the order type a stop that lives on the exchange needs
STOP_ORDER_TYPES: tuple[str, ...] = ("STOP_LOSS_LIMIT", "STOP_LOSS")


class BinanceCheckError(Exception):
    """A probe could not be completed. The message never contains key material."""


# --------------------------------------------------------------------------- keys


@dataclass(frozen=True)
class KeyPair:
    """What we know about one sleeve's exchange credentials, minus the secret."""

    sleeve: str
    key_env: str
    secret_env: str
    key: str = ""
    secret: str = ""

    @property
    def present(self) -> bool:
        return bool(self.key and self.secret)

    @property
    def last4(self) -> str:
        return self.key[-4:] if len(self.key) >= 4 else ""

    def describe(self) -> dict[str, Any]:
        return {
            "sleeve": self.sleeve,
            "key_env": self.key_env,
            "secret_env": self.secret_env,
            "present": self.present,
            "last4": self.last4,
        }


def keys_for(sleeve: str, env: Mapping[str, str] | None = None) -> KeyPair:
    """``BINANCE_KEY_A`` / ``BINANCE_SECRET_A`` for sleeve ``a``."""
    e = env if env is not None else os.environ
    up = sleeve.upper()
    key_env, secret_env = f"BINANCE_KEY_{up}", f"BINANCE_SECRET_{up}"
    return KeyPair(
        sleeve=sleeve.lower(),
        key_env=key_env,
        secret_env=secret_env,
        key=(e.get(key_env) or "").strip(),
        secret=(e.get(secret_env) or "").strip(),
    )


def describe_keys(sleeve: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return keys_for(sleeve, env).describe()


# --------------------------------------------------------------------------- client


class BinanceClient:  # pragma: no cover - the network edge; evaluators are tested
    """Minimal signed REST client. Read-only endpoints only, by construction."""

    def __init__(
        self,
        keys: KeyPair,
        *,
        base_url: str = BASE_URL,
        client: Any | None = None,
        timeout: float = 10.0,
        recv_window_ms: int = 5000,
    ) -> None:
        if not keys.present:
            raise BinanceCheckError(f"{keys.key_env}/{keys.secret_env} are not both set")
        self._keys = keys
        self.base = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout)
        self._client = client

    def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query["timestamp"] = int(time.time() * 1000)
        query["recvWindow"] = self.recv_window_ms
        qs = urllib.parse.urlencode(query)
        sig = hmac.new(self._keys.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        r = self._client.get(
            f"{self.base}{path}?{qs}&signature={sig}",
            headers={"X-MBX-APIKEY": self._keys.key},
        )
        if r.status_code != 200:
            raise BinanceCheckError(f"GET {path} -> HTTP {r.status_code}")
        return r.json()

    def _public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._client.get(f"{self.base}{path}", params=params or {})
        if r.status_code != 200:
            raise BinanceCheckError(f"GET {path} -> HTTP {r.status_code}")
        return r.json()

    def api_restrictions(self) -> dict[str, Any]:
        return dict(self._signed_get(RESTRICTIONS_PATH))

    def account(self) -> dict[str, Any]:
        return dict(self._signed_get(ACCOUNT_PATH, {"omitZeroBalances": "false"}))

    def exchange_info(self, symbols: Iterable[str]) -> dict[str, Any]:
        wanted = ",".join(f'"{s}"' for s in symbols)
        return dict(self._public_get(EXCHANGE_INFO_PATH, {"symbols": f"[{wanted}]"}))


# --------------------------------------------------------------------------- evaluators


@dataclass(frozen=True)
class RestrictionReport:
    """Verdict on one key's permissions. ``blocking`` is what refuses go-live."""

    ok: bool
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    permissions: dict[str, bool] = field(default_factory=dict)

    def detail(self) -> str:
        if self.blocking:
            return "; ".join(self.blocking)
        if self.warnings:
            return "; ".join(self.warnings)
        return "spot-only trading key, withdrawals disabled"


def check_restrictions(data: Mapping[str, Any]) -> RestrictionReport:
    """Spot-only, no-withdrawal verdict over an ``apiRestrictions`` payload."""
    perms = {k: bool(v) for k, v in data.items() if isinstance(v, bool)}
    blocking: list[str] = []
    warnings: list[str] = []
    for name in REQUIRED_PERMISSIONS:
        if name not in data:
            blocking.append(f"{name} missing from apiRestrictions")
        elif not data.get(name):
            blocking.append(f"{name} is disabled")
    for name in FORBIDDEN_PERMISSIONS:
        if data.get(name):
            blocking.append(f"{name} is ENABLED and must be off")
    if not data.get("ipRestrict"):
        warnings.append("ipRestrict is off (recommended, not required)")
    expiry = data.get("tradingAuthorityExpirationTime")
    if expiry:
        warnings.append(f"trading authority expires at {expiry}")
    return RestrictionReport(
        ok=not blocking, blocking=blocking, warnings=warnings, permissions=perms
    )


def account_uid(account: Mapping[str, Any]) -> str | None:
    """The stable account identifier one-live-sleeve-per-account compares on."""
    for key in ("uid", "accountId", "userId"):
        value = account.get(key)
        if value not in (None, "", 0):
            return str(value)
    return None


def free_balance(account: Mapping[str, Any], asset: str) -> float:
    """Free (unlocked) balance of one asset from an ``/api/v3/account`` payload."""
    for row in account.get("balances", []) or []:
        if str(row.get("asset", "")).upper() == asset.upper():
            return float(row.get("free", 0.0) or 0.0)
    return 0.0


def total_balances(
    account: Mapping[str, Any], assets: Iterable[str] | None = None
) -> dict[str, float]:
    """``{asset: free + locked}``, optionally narrowed to ``assets``, zeros dropped."""
    wanted = {a.upper() for a in assets} if assets is not None else None
    out: dict[str, float] = {}
    for row in account.get("balances", []) or []:
        name = str(row.get("asset", "")).upper()
        if wanted is not None and name not in wanted:
            continue
        total = float(row.get("free", 0.0) or 0.0) + float(row.get("locked", 0.0) or 0.0)
        if total:
            out[name] = total
    return out


def symbol_of(pair: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT``."""
    return pair.replace("/", "").replace("-", "").upper()


def supports_stop_orders(
    exchange_info: Mapping[str, Any], pairs: Iterable[str]
) -> tuple[bool, list[str]]:
    """Every pair must accept a stop order, or a live stop cannot sit on the exchange."""
    by_symbol = {
        str(s.get("symbol", "")).upper(): s for s in exchange_info.get("symbols", []) or []
    }
    missing: list[str] = []
    for pair in pairs:
        info = by_symbol.get(symbol_of(pair))
        if info is None:
            missing.append(f"{pair} (symbol unknown)")
            continue
        types = {str(t).upper() for t in info.get("orderTypes", []) or []}
        if not types & set(STOP_ORDER_TYPES):
            missing.append(f"{pair} (no {'/'.join(STOP_ORDER_TYPES)})")
    return (not missing), missing


__all__ = [
    "ACCOUNT_PATH",
    "BASE_URL",
    "EXCHANGE_INFO_PATH",
    "FORBIDDEN_PERMISSIONS",
    "REQUIRED_PERMISSIONS",
    "RESTRICTIONS_PATH",
    "STOP_ORDER_TYPES",
    "BinanceCheckError",
    "BinanceClient",
    "KeyPair",
    "RestrictionReport",
    "account_uid",
    "check_restrictions",
    "describe_keys",
    "free_balance",
    "keys_for",
    "supports_stop_orders",
    "symbol_of",
    "total_balances",
]
