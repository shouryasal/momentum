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

**Venue.** Nothing here is pinned to production any more. :class:`BinanceClient` takes a
``venue`` and asks ``ops.lib.exchange_endpoints`` for the host, so a demo preflight probes
``demo-api.binance.com`` and a live preflight probes ``api.binance.com`` — with no shared
default that a caller could forget to override. :func:`keys_for` picks the credential env
names the same way, from the venue, which is why a demo key lives under
``BINANCE_DEMO_KEY`` and not under ``BINANCE_KEY_A``.

One thing genuinely changes between venues: **demo has no ``sapi`` tier at all.**
``demo-api.binance.com/sapi/v1/account/apiRestrictions`` answers an nginx ``404`` where
production answers a Binance error code, so key permissions cannot be read back on demo.
:func:`restrictions_unavailable` is the honest verdict for that case — a warning naming the
reason, never a silent pass. Treating the 404 as "no restrictions found, therefore no
forbidden restrictions" would turn the single most important check (withdrawals disabled)
into a rubber stamp. On demo the operator disables withdrawals in the UI and
``docs/design/demo-mode.md`` §7 records it as a manual step.
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

from ops.lib.exchange_endpoints import (
    Venue,
    credential_env_names,
    endpoints_for,
)

#: Kept for callers that still import it, and derived rather than spelled: it is the LIVE
#: venue's host and nothing else. New code passes ``venue=`` instead — a module-level
#: default pointed at production is exactly how a demo probe ends up on the real exchange.
BASE_URL = endpoints_for(Venue.LIVE).rest_base
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
    #: Which Binance these credentials are for. A KeyPair always knows, so a probe can
    #: never be built against a venue the key was not issued for.
    venue: Venue = Venue.LIVE

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
            "venue": self.venue.value,
            "present": self.present,
            "last4": self.last4,
        }


def keys_for(
    sleeve: str,
    env: Mapping[str, str] | None = None,
    *,
    venue: Venue | str = Venue.LIVE,
) -> KeyPair:
    """The credentials for ``sleeve`` at ``venue``.

    ``live`` -> ``BINANCE_KEY_A``/``BINANCE_SECRET_A``;
    ``demo`` -> ``BINANCE_DEMO_KEY``/``BINANCE_DEMO_SECRET`` (one demo account, no sleeve
    letter — which is also what makes the two impossible to confuse by eye).
    """
    e = env if env is not None else os.environ
    v = endpoints_for(venue).venue
    key_env, secret_env = credential_env_names(v, sleeve)
    return KeyPair(
        sleeve=sleeve.lower(),
        key_env=key_env,
        secret_env=secret_env,
        key=(e.get(key_env) or "").strip(),
        secret=(e.get(secret_env) or "").strip(),
        venue=v,
    )


def describe_keys(
    sleeve: str, env: Mapping[str, str] | None = None, *, venue: Venue | str = Venue.LIVE
) -> dict[str, Any]:
    return keys_for(sleeve, env, venue=venue).describe()


# --------------------------------------------------------------------------- client


class BinanceClient:  # pragma: no cover - the network edge; evaluators are tested
    """Minimal signed REST client. Read-only endpoints only, by construction."""

    def __init__(
        self,
        keys: KeyPair,
        *,
        venue: Venue | str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        timeout: float = 10.0,
        recv_window_ms: int = 5000,
    ) -> None:
        if not keys.present:
            raise BinanceCheckError(f"{keys.key_env}/{keys.secret_env} are not both set")
        # The venue comes from the credentials unless a caller overrides it deliberately;
        # there is no production default to forget.
        self.endpoints = endpoints_for(venue if venue is not None else keys.venue)
        self.venue = self.endpoints.venue
        if self.venue is not keys.venue:
            raise BinanceCheckError(
                f"refusing to probe {self.venue.value} ({self.endpoints.rest_host}) with "
                f"{keys.key_env}, which holds a {keys.venue.value} credential"
            )
        self._keys = keys
        self.base = (base_url or self.endpoints.rest_base).rstrip("/")
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

    @property
    def supports_restrictions(self) -> bool:
        """Whether :meth:`api_restrictions` can be asked at all on this venue."""
        return self.endpoints.supports_sapi

    def api_restrictions(self) -> dict[str, Any]:
        if not self.supports_restrictions:
            raise BinanceCheckError(
                f"{self.endpoints.rest_host} does not serve the sapi tier, so key "
                f"permissions cannot be read there"
            )
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


def restrictions_unavailable(venue: Venue | str) -> RestrictionReport:
    """The verdict when a venue cannot be asked about key permissions at all.

    Demo serves no ``sapi`` tier (``/sapi/v1/account/apiRestrictions`` -> nginx 404), so
    there is no payload to evaluate. This returns ``ok=True`` with an explicit WARNING and
    an EMPTY ``permissions`` map — it must never be mistaken for a checked key:

    * ``ok`` is True because a missing endpoint is not a failing key, and blocking here
      would make demo unreachable for a reason that is not about the key at all;
    * the warning names the venue and the reason, so the preflight report says
      "not checked, here is why" instead of "passed";
    * ``permissions`` stays empty, so any consumer that reasons about
      ``report.permissions["enableWithdrawals"]`` raises rather than reading False as
      "withdrawals are off".
    """
    ep = endpoints_for(venue)
    return RestrictionReport(
        ok=True,
        blocking=[],
        warnings=[
            f"key permissions NOT verified: {ep.rest_host} serves no sapi tier, so "
            f"{RESTRICTIONS_PATH} returns 404 and withdrawals-disabled cannot be read "
            f"back. Confirm it by hand at {ep.key_console} (demo-mode.md §7 step 2)."
        ],
        permissions={},
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
    "restrictions_unavailable",
    "supports_stop_orders",
    "symbol_of",
    "total_balances",
]
