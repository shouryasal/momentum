"""Read-only reader for the Binance Spot **Demo** account (``demo-api.binance.com``).

The owner's point, verbatim: *"money put in shouldnt it be from demo api if we have put in
demo api, even if we are not executing trades from it we can atleast use it for every other
info until we start testing with it"*. This module is that "every other info": balances,
what the key is allowed to do, whether anything is resting on the book, and the exchange
filters an order has to satisfy — all of it readable **before** a single order is ever
placed there, and none of it able to place one.

Three properties this file is built to keep, because each one is a way the same idea goes
wrong somewhere else in this repo:

* **Read-only by construction.** Every request is a ``GET``, and the only paths it knows
  are ``/api/v3/account``, ``/api/v3/openOrders`` and ``/api/v3/exchangeInfo``. There is no
  code path here that can sign a ``POST``, so "the console reads the demo account" can
  never quietly become "the console traded on the demo account".
* **One venue, named.** The base URL comes from
  :func:`ops.lib.exchange_endpoints.endpoints_for`, never from a constant, and a credential
  labelled for another venue is refused before the first byte goes out — the same rule
  ``ops.lib.binance_check.BinanceClient`` applies. ``docs/design/demo-mode.md`` §2a is the
  reason: a *partial* venue override leaves half the client on production.
* **Degraded is a state, not a blank.** A network that is down, a key that is missing, a
  403 — each produces a named ``state`` with a reason, and the last good snapshot is
  returned beside it marked ``stale``. The dashboard says "cannot reach demo", never
  ``$0.00`` and never an empty card. Zero is a claim about money; unknown is not.

**Secrets never leave this module.** The key and secret are read through
:mod:`ops.lib.envfile` (the one host-side reader) and are held only as locals. Nothing
returned from here contains a key, a secret, a signature or a signed URL — including the
error strings, which are composed from a status code and an exception *type*, never from
an exception message that may carry the query string that was signed.

Caching. The demo REST tier shares live's limits (``REQUEST_WEIGHT 6000/min``,
``docs/design/demo-mode.md`` §4), which is generous, but Home refetches on five SSE topics
and a 60-second timer, so an uncached read would be a round trip per dashboard paint.
Balances are cached in memory for :data:`ACCOUNT_TTL_S`; the exchange filters — reference
data that changes on the scale of months — are cached for :data:`FILTERS_TTL_S` and also
written to ``knowledge/cache/venue/`` through the existing venue cache, so a console
restart does not re-fetch them and so other code can read the venue's real filters from
disk without holding a key.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.lib import binance_check as bc
from ops.lib import envfile
from ops.lib.exchange_endpoints import (
    Venue,
    credential_env_names,
    endpoints_for,
    parse_venue,
    signed_query,
)

__all__ = [
    "ACCOUNT_TTL_S",
    "FILTERS_TTL_S",
    "IDLE_ACCOUNT_TTL_S",
    "STABLE_ASSETS",
    "DemoReadError",
    "account_snapshot",
    "balance_rows",
    "cash_and_value",
    "clear_cache",
    "filters_snapshot",
    "is_configured",
    "sizing_floor",
]

#: How long a balance read is reused while a bot is **on** demo. Short, because then the
#: balances move with every fill and they are what the money cards show.
ACCOUNT_TTL_S = 30.0
#: How long it is reused while demo is merely configured. Longer, because then the only
#: thing that moves the balance is the owner asking Binance to reset it — and this read sits
#: in the path of every Home paint, so it must not cost a round trip per refresh.
IDLE_ACCOUNT_TTL_S = 120.0
#: How long the exchange filters are reused. Long: they are reference data, not a price.
FILTERS_TTL_S = 6 * 3600.0
#: The venue cache file the filters are mirrored into, under ``knowledge/cache/venue/``.
FILTERS_CACHE_NAME = "demo_exchange_info"

#: Assets counted at one dollar when valuing an account. Anything else needs a mark, and a
#: holding with no mark makes the *total* unknown rather than smaller — see
#: :func:`cash_and_value`.
STABLE_ASSETS: frozenset[str] = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"})

ACCOUNT_PATH = "/api/v3/account"
OPEN_ORDERS_PATH = "/api/v3/openOrders"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"

#: Timeout for one demo request. Home blocks on this, so it is short on purpose: a slow
#: venue must cost the dashboard a labelled "cannot reach demo", not a spinner.
TIMEOUT_S = 4.0

#: state values, stable strings, safe to assert on
STATE_OK = "ok"
STATE_NOT_CONFIGURED = "not_configured"
STATE_UNREACHABLE = "unreachable"
STATE_REFUSED = "refused"


class DemoReadError(Exception):
    """A demo read that could not be completed. The message never carries a secret."""


# --------------------------------------------------------------------------- the cache


@dataclass
class _Entry:
    value: dict[str, Any]
    at: float


_CACHE: dict[str, _Entry] = {}


def clear_cache() -> None:
    """Drop every cached demo read. Tests call it; nothing in the app needs to."""
    _CACHE.clear()


def _cached(key: str, ttl: float, *, now: float | None = None) -> dict[str, Any] | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    age = (now if now is not None else time.monotonic()) - entry.at
    if age > ttl:
        return None
    out = dict(entry.value)
    out["cached"] = True
    out["age_s"] = round(age, 1)
    return out


def _store(key: str, value: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    _CACHE[key] = _Entry(value=value, at=now if now is not None else time.monotonic())
    out = dict(value)
    out["cached"] = False
    out["age_s"] = 0.0
    return out


def _stale(key: str) -> dict[str, Any] | None:
    """The last good snapshot, whatever its age — for the degraded branch only."""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    out = dict(entry.value)
    out["cached"] = True
    out["stale"] = True
    out["age_s"] = round(time.monotonic() - entry.at, 1)
    return out


# --------------------------------------------------------------------------- the wire


def _iso(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _credentials(venue: Venue, *, env: Mapping[str, str] | None = None,
                 env_path: Path | str | None = None) -> bc.KeyPair:
    """The key pair for ``venue``, read through the one host-side ``.env`` reader.

    ``env`` exists for tests. In the app the values come from :mod:`ops.lib.envfile`, which
    is the only reader in the repo permitted to hand a secret back, and they are held here
    as locals for the length of one request.
    """
    if env is not None:
        return bc.keys_for("a", env, venue=venue)
    names = bc.keys_for("a", {}, venue=venue)
    values = {
        names.key_env: envfile.value_of(names.key_env, path=env_path) or "",
        names.secret_env: envfile.value_of(names.secret_env, path=env_path) or "",
    }
    return bc.keys_for("a", values, venue=venue)


def is_configured(venue: Venue | str = Venue.DEMO, *, env: Mapping[str, str] | None = None,
                  env_path: Path | str | None = None) -> bool:
    """Whether a credential for this venue exists at all. Never reveals it."""
    try:
        return _credentials(parse_venue(venue), env=env, env_path=env_path).present
    except Exception:  # noqa: BLE001 - an unreadable .env is "not configured", not a crash
        return False


class _Rest:
    """The three GETs this module is allowed to make, against one named venue.

    There is deliberately no ``post``/``delete``: the class cannot express an order.
    """

    def __init__(self, keys: bc.KeyPair, venue: Venue, *, client: Any = None,
                 timeout: float = TIMEOUT_S) -> None:
        self.endpoints = endpoints_for(venue)
        self.venue = self.endpoints.venue
        if keys.venue is not self.venue:
            # The same refusal ``BinanceClient`` makes, for the same reason: a credential
            # labelled for another venue must never be signed against this host.
            raise DemoReadError(
                f"refusing to read {self.venue.value} ({self.endpoints.rest_host}) with "
                f"{keys.key_env}, which holds a {keys.venue.value} credential"
            )
        if not keys.present:
            raise DemoReadError(f"{keys.key_env}/{keys.secret_env} are not both set")
        # ``rest_base`` is scheme + host with no path; the three paths below carry their
        # own ``/api/v3``, so there is exactly one place that joins the two halves.
        self._base = self.endpoints.rest_base.rstrip("/")
        self._keys = keys
        self._owned = client is None
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout)
        self._client = client

    def close(self) -> None:
        if self._owned:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - closing must never be the thing that fails
                pass

    def _json(self, resp: Any, path: str) -> Any:
        if getattr(resp, "status_code", None) != 200:
            # Deliberately not ``resp.text``: a Binance error body is safe, but an httpx
            # exception repr is not, and one branch that can leak a signed URL is one too
            # many. The code alone is enough to act on.
            raise DemoReadError(f"GET {path} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise DemoReadError(f"GET {path} -> unreadable body ({type(e).__name__})") from None

    def _signed(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self._base}{path}?{signed_query(self._keys.secret, params or {})}"
        try:
            resp = self._client.get(url, headers={"X-MBX-APIKEY": self._keys.key})
        except Exception as e:  # noqa: BLE001 - the message may hold the signed query string
            raise DemoReadError(
                f"GET {path} on {self.endpoints.rest_host} failed ({type(e).__name__})"
            ) from None
        return self._json(resp, path)

    def _public(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            resp = self._client.get(f"{self._base}{path}", params=dict(params or {}))
        except Exception as e:  # noqa: BLE001
            raise DemoReadError(
                f"GET {path} on {self.endpoints.rest_host} failed ({type(e).__name__})"
            ) from None
        return self._json(resp, path)

    def account(self) -> dict[str, Any]:
        return dict(self._signed(ACCOUNT_PATH, {"omitZeroBalances": "false"}))

    def open_orders(self) -> list[dict[str, Any]]:
        rows = self._signed(OPEN_ORDERS_PATH)
        return [dict(r) for r in rows] if isinstance(rows, list) else []

    def exchange_info(self, symbols: Iterable[str]) -> dict[str, Any]:
        wanted = ",".join(f'"{s}"' for s in symbols)
        return dict(self._public(EXCHANGE_INFO_PATH, {"symbols": f"[{wanted}]"}))


# --------------------------------------------------------------------------- valuation


def cash_and_value(
    balances: Mapping[str, float], marks: Mapping[str, float] | None = None
) -> tuple[float, float | None, list[str]]:
    """``(cash_usdt, value_usdt, unpriced)`` for an account's balances.

    ``cash_usdt`` is every stablecoin at par — the money that is not at risk. ``value_usdt``
    is the whole account, and it is ``None`` the moment one holding cannot be marked: a
    total that silently drops an unpriced coin is a *wrong* number, which is worse on this
    screen than no number at all. The names of the coins that could not be priced come back
    so the card can say which.
    """
    marks = {str(k).upper(): float(v) for k, v in (marks or {}).items()}
    cash = 0.0
    total = 0.0
    unpriced: list[str] = []
    for raw_asset, raw_amount in balances.items():
        asset = str(raw_asset).upper()
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            unpriced.append(asset)
            continue
        if not amount:
            continue
        if asset in STABLE_ASSETS:
            cash += amount
            total += amount
            continue
        mark = marks.get(asset)
        if mark is None or mark <= 0:
            unpriced.append(asset)
            continue
        total += amount * mark
    return round(cash, 8), (None if unpriced else round(total, 8)), sorted(set(unpriced))


def balance_rows(
    balances: Mapping[str, float], marks: Mapping[str, float] | None = None
) -> list[dict[str, Any]]:
    """One row per asset held, marked — what the holdings table shows on a demo run.

    Stablecoins are marked at par and flagged ``stable`` so the table can leave them out of
    "what it holds" without losing them from "what it is worth". An asset with no mark gets
    a row with ``value_usdt: None``: the screen prints "not recorded", which is the truth,
    where a 0 would quietly shrink the account.
    """
    marks = {str(k).upper(): float(v) for k, v in (marks or {}).items()}
    rows: list[dict[str, Any]] = []
    for raw_asset, raw_amount in balances.items():
        asset = str(raw_asset).upper()
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        if not amount:
            continue
        stable = asset in STABLE_ASSETS
        mark = 1.0 if stable else marks.get(asset)
        rows.append({
            "asset": asset,
            "amount": round(amount, 8),
            "mark_usdt": mark,
            "value_usdt": round(amount * mark, 8) if mark else None,
            "stable": stable,
        })
    rows.sort(key=lambda r: (r["value_usdt"] is None, -(r["value_usdt"] or 0.0), r["asset"]))
    return rows


def sizing_floor(exchange_min_notional: float | None, gate_min_notional: float | None) -> dict[str, Any]:
    """Which minimum actually bites when an order is sized, and which one it was.

    Both are reported because they answer different questions: the exchange's
    ``NOTIONAL.minNotional`` is what gets an order *rejected*, and ``risk.min_notional_usdt``
    is what this project refuses to trade below. On BTCUSDT today they are 5 and 25, so the
    binding one is ours — but that is a fact to be read off the venue, not assumed.
    """
    venue_min = None if exchange_min_notional is None else float(exchange_min_notional)
    gate_min = None if gate_min_notional is None else float(gate_min_notional)
    candidates = [v for v in (venue_min, gate_min) if v is not None]
    effective = max(candidates) if candidates else None
    if effective is None:
        binds = None
    elif gate_min is not None and effective == gate_min and (venue_min is None or gate_min >= venue_min):
        binds = "risk.min_notional_usdt"
    else:
        binds = "exchange"
    return {
        "exchange_min_notional": venue_min,
        "gate_min_notional": gate_min,
        "effective_min_notional": effective,
        "binds": binds,
    }


# --------------------------------------------------------------------------- snapshots


def _base(venue: Venue, state: str, *, error: str | None = None) -> dict[str, Any]:
    ep = endpoints_for(venue)
    key_env, secret_env = credential_env_names(venue, "a")
    return {
        "venue": venue.value,
        "host": ep.rest_host,
        "state": state,
        "as_of_utc": _iso(),
        "cached": False,
        "stale": False,
        "age_s": 0.0,
        "error": error,
        "key_env": key_env,
        "secret_env": secret_env,
        "balance_reset": ep.balance_reset,
    }


def account_snapshot(
    venue: Venue | str = Venue.DEMO,
    *,
    marks: Mapping[str, float] | None = None,
    env: Mapping[str, str] | None = None,
    env_path: Path | str | None = None,
    client: Any = None,
    refresh: bool = False,
    ttl: float = ACCOUNT_TTL_S,
) -> dict[str, Any]:
    """Balances, permissions and resting orders on one venue. Never raises.

    The returned ``state`` is one of ``ok`` / ``not_configured`` / ``unreachable`` /
    ``refused``. On a failure the last good snapshot comes back with ``stale: true`` beside
    the reason, so a flapping network shows an old number labelled old rather than a blank
    card that reads as "you have nothing".
    """
    v = parse_venue(venue)
    cache_key = f"account:{v.value}"

    try:
        keys = _credentials(v, env=env, env_path=env_path)
    except Exception as e:  # noqa: BLE001 - an unreadable .env must not 500 the dashboard
        return _base(v, STATE_UNREACHABLE, error=f"could not read .env ({type(e).__name__})")
    if not keys.present:
        return _base(v, STATE_NOT_CONFIGURED,
                     error=f"{keys.key_env}/{keys.secret_env} are not set")

    if not refresh:
        hit = _cached(cache_key, ttl)
        if hit is not None:
            return hit

    rest: _Rest | None = None
    try:
        rest = _Rest(keys, v, client=client)
        account = rest.account()
        orders = rest.open_orders()
    except DemoReadError as e:
        stale = _stale(cache_key)
        state = STATE_REFUSED if "HTTP 4" in str(e) else STATE_UNREACHABLE
        if stale is not None:
            stale["state"] = state
            stale["error"] = str(e)
            return stale
        return _base(v, state, error=str(e))
    except Exception as e:  # noqa: BLE001 - the dashboard degrades, it does not 500
        stale = _stale(cache_key)
        message = f"demo read failed ({type(e).__name__})"
        if stale is not None:
            stale["state"] = STATE_UNREACHABLE
            stale["error"] = message
            return stale
        return _base(v, STATE_UNREACHABLE, error=message)
    finally:
        if rest is not None:
            rest.close()

    balances = bc.total_balances(account)
    cash, value, unpriced = cash_and_value(balances, marks)
    snapshot = _base(v, STATE_OK)
    snapshot.update(
        {
            "account_type": account.get("accountType"),
            "permissions": [str(p) for p in (account.get("permissions") or [])],
            "can_trade": bool(account.get("canTrade")),
            "can_withdraw": bool(account.get("canWithdraw")),
            "can_deposit": bool(account.get("canDeposit")),
            "maker_commission_bps": account.get("makerCommission"),
            "taker_commission_bps": account.get("takerCommission"),
            "balances": {k: round(float(x), 8) for k, x in sorted(balances.items())},
            "holdings": balance_rows(balances, marks),
            "cash_usdt": cash,
            "value_usdt": value,
            "unpriced": unpriced,
            "open_orders": [
                {
                    "symbol": o.get("symbol"),
                    "side": o.get("side"),
                    "type": o.get("type"),
                    "price": o.get("price"),
                    "orig_qty": o.get("origQty"),
                    "executed_qty": o.get("executedQty"),
                    "status": o.get("status"),
                    "time": o.get("time"),
                }
                for o in orders
            ],
            "open_order_count": len(orders),
        }
    )
    return _store(cache_key, snapshot)


def filters_snapshot(
    pairs: Iterable[str],
    venue: Venue | str = Venue.DEMO,
    *,
    gate_min_notional: float | None = None,
    env: Mapping[str, str] | None = None,
    env_path: Path | str | None = None,
    client: Any = None,
    root: Path | str | None = None,
    refresh: bool = False,
    ttl: float = FILTERS_TTL_S,
) -> dict[str, Any]:
    """The venue's own order-sizing rules for ``pairs``: tick, lot step, minimum notional.

    This is the half of the owner's point that has to be used *before* the first order:
    sizing read off the venue we are about to trade on is sizing that is right the first
    time, rather than right after the first ``-1013`` rejection. ``exchangeInfo`` is a
    public endpoint, so it needs no key — but it is fetched from the **demo** host anyway,
    so what the console displays is what the demo matching engine will actually enforce.
    """
    v = parse_venue(venue)
    symbols = [bc.symbol_of(p) for p in pairs]
    cache_key = f"filters:{v.value}:{','.join(symbols)}"

    if not refresh:
        hit = _cached(cache_key, ttl)
        if hit is not None:
            return hit
        disk = _filters_from_disk(root, cache_key, ttl)
        if disk is not None:
            return disk

    rest: _Rest | None = None
    try:
        # exchangeInfo is public. A key is still required to *exist* for a non-live venue,
        # because reading demo's filters while holding no demo credential would mean the
        # console is describing a venue it cannot otherwise see.
        keys = _credentials(v, env=env, env_path=env_path)
        rest = _Rest(keys, v, client=client)
        payload = rest.exchange_info(symbols)
    except DemoReadError as e:
        stale = _stale(cache_key)
        if stale is not None:
            stale["state"] = STATE_UNREACHABLE
            stale["error"] = str(e)
            return stale
        return _base(v, STATE_UNREACHABLE, error=str(e))
    except Exception as e:  # noqa: BLE001
        stale = _stale(cache_key)
        message = f"demo exchangeInfo failed ({type(e).__name__})"
        if stale is not None:
            stale["state"] = STATE_UNREACHABLE
            stale["error"] = message
            return stale
        return _base(v, STATE_UNREACHABLE, error=message)
    finally:
        if rest is not None:
            rest.close()

    from runs.features import venue as venue_features

    parsed = venue_features.parse_exchange_info(payload, symbols)
    by_pair: dict[str, Any] = {}
    for pair in pairs:
        info = dict(parsed.get(bc.symbol_of(pair), {}))
        info["symbol"] = bc.symbol_of(pair)
        info.update(sizing_floor(info.get("min_notional"), gate_min_notional))
        by_pair[str(pair)] = info

    snapshot = _base(v, STATE_OK)
    snapshot.update(
        {
            "pairs": by_pair,
            "symbols": symbols,
            "not_tradable": sorted(p for p, i in by_pair.items() if not i.get("tradable")),
            "rate_limits": payload.get("rateLimits") or [],
        }
    )
    _write_filters_to_disk(root, snapshot)
    return _store(cache_key, snapshot)


def _filters_from_disk(root: Path | str | None, cache_key: str, ttl: float) -> dict[str, Any] | None:
    """The filters the last console process fetched — reference data survives a restart."""
    try:
        from ops.lib import paths
        from runs.features import venue as venue_features

        base = Path(root) if root is not None else paths.REPO_ROOT
        payload, fetched_at = venue_features.read_cache(base, FILTERS_CACHE_NAME)
        if not isinstance(payload, dict) or fetched_at is None:
            return None
        age = (datetime.now(UTC) - fetched_at).total_seconds()
        if age > ttl or payload.get("_cache_key") != cache_key:
            return None
    except Exception:  # noqa: BLE001 - a missing or unreadable cache is simply a miss
        return None
    out = dict(payload)
    out.pop("_cache_key", None)
    out["cached"] = True
    out["age_s"] = round(age, 1)
    return out


def _write_filters_to_disk(root: Path | str | None, snapshot: Mapping[str, Any]) -> None:
    try:
        from ops.lib import paths
        from runs.features import venue as venue_features

        base = Path(root) if root is not None else paths.REPO_ROOT
        payload = dict(snapshot)
        payload["_cache_key"] = f"filters:{snapshot['venue']}:{','.join(snapshot['symbols'])}"
        json.dumps(payload)  # refuse to write anything that is not plain JSON
        venue_features.write_cache(base, FILTERS_CACHE_NAME, payload)
    except Exception:  # noqa: BLE001 - the disk mirror is an optimisation, never a failure
        return
