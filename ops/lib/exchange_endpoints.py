"""The one place that knows which Binance venue a URL belongs to, and which credential
is allowed to reach it.

Earn trades demo before it trades live. "Demo" here is **Binance Spot Demo Mode**
(``demo-api.binance.com``), which is not the old Spot Testnet (``testnet.binance.vision``)
and not ccxt's ``set_sandbox_mode``. Three facts, each verified against the installed
libraries and the live endpoints (evidence in ``docs/design/demo-mode.md``), motivate every
line below:

1. ``ccxt.binance().set_sandbox_mode(True)`` swaps ``urls['api']`` for ``urls['test']``,
   which is **testnet**. It cannot reach demo. Sandbox mode is the wrong switch.
2. ccxt ``4.5.82`` deep-merges a constructor ``urls`` override into the production map
   rather than replacing it. Handing ccxt ``{"urls": {"api": urls["demo"]}}`` therefore
   leaves ``sapi``/``papi``/``eapi``/``fapiData``/``dapiData`` **still pointing at
   ``api.binance.com``**. A "full" override is not full. The only complete swap is ccxt's
   own ``enable_demo_trading(True)``, which assigns ``urls['api'] = urls['demo']``
   wholesale and so drops those keys entirely.
3. Demo genuinely has no ``sapi`` tier — ``demo-api.binance.com/sapi/...`` answers
   ``404`` from nginx, where production answers ``-2014 API-key format invalid``.

So this module never emits a partial override. :func:`ccxt_url_overrides` returns a value
for **every** key ccxt's binance describes, and any key the venue does not serve is routed
to :data:`BLACKHOLE_URL` — a host under the RFC 2606 reserved ``.invalid`` TLD, which
cannot resolve. Merged or assigned, the result is the same: nothing falls back to
production. That is the property that makes the dangerous mistake impossible rather than
merely discouraged.

The second half of the module is the binding rule. A credential carries the venue it was
issued for; a mode carries the venue it is allowed to reach; :func:`resolve_binding`
refuses, with a typed :class:`VenueBindingError`, to build a configuration where the two
disagree. Demo mode may only ever reach demo hosts, live mode only live hosts, and a
``TEST`` (dry-run) sleeve may carry no exchange credential at all.

:func:`verify_credential_venue` closes the remaining gap — a label is a claim, not proof.
It confirms a key authenticates against the venue it claims *and* fails against every
other one, and it returns ``cannot_verify`` (never ``confirmed``) when there is no key or
a probe could not be completed. Proving the negative is part of the check: a live key that
happens to be accepted by demo would be caught here and nowhere else.

Pure, importable, stdlib + ``httpx`` only. No freqtrade import, no ccxt import (the URL
shapes are *pinned* here and re-checked by tests, so a library upgrade that adds a URL key
fails a test instead of silently opening a hole). The only function that touches the
network is :func:`httpx_account_probe`, and nothing in the test suite calls it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal

# --------------------------------------------------------------------------- venues


class Venue(StrEnum):
    """A Binance environment. The value is what an operator writes in config or env."""

    LIVE = "live"
    DEMO = "demo"
    TESTNET = "testnet"


#: Reserved by RFC 2606: guaranteed never to resolve. Every URL key a venue does not serve
#: is routed here, so a merged-not-replaced override still cannot reach production.
BLACKHOLE_URL: Final = "https://disabled.earn.invalid"
#: The websocket counterpart of :data:`BLACKHOLE_URL`.
BLACKHOLE_WS: Final = "wss://disabled.earn.invalid/ws"

#: Every key in ``ccxt.binance().describe()["urls"]["api"]`` at ccxt 4.5.82, split by the
#: API tier it belongs to. Pinned deliberately: if a ccxt upgrade adds a key,
#: ``tests/test_ops/test_exchange_endpoints.py`` fails rather than a new key quietly
#: defaulting to production.
SPOT_URL_KEYS: Final[tuple[str, ...]] = ("public", "private", "v1")
SAPI_URL_KEYS: Final[tuple[str, ...]] = ("sapi", "sapiV2", "sapiV3", "sapiV4")
FUTURES_URL_KEYS: Final[tuple[str, ...]] = (
    "fapiPublic", "fapiPublicV2", "fapiPublicV3",
    "fapiPrivate", "fapiPrivateV2", "fapiPrivateV3", "fapiData",
    "dapiPublic", "dapiPrivate", "dapiPrivateV2", "dapiData",
    "eapiPublic", "eapiPrivate",
    "papi", "papiV2",
)
PRODUCTION_URL_KEYS: Final[tuple[str, ...]] = (
    *SPOT_URL_KEYS, *SAPI_URL_KEYS, *FUTURES_URL_KEYS,
)

#: Every key in ccxt.pro's ``urls["api"]["ws"]`` at 4.5.82, minus the nested ``ws-api``.
WS_URL_KEYS: Final[tuple[str, ...]] = (
    "spot", "margin", "future", "delivery",
    "option", "optionMarket", "optionPrivate", "papi", "stock",
)
#: Keys of the nested ``urls["api"]["ws"]["ws-api"]`` map.
WS_API_URL_KEYS: Final[tuple[str, ...]] = ("spot", "future", "delivery")

#: Production values for the keys Earn never uses but must still pin down. Only LIVE emits
#: these; demo and testnet blackhole them.
_PRODUCTION_URLS: Final[Mapping[str, str]] = {
    "public": "https://api.binance.com/api/v3",
    "private": "https://api.binance.com/api/v3",
    "v1": "https://api.binance.com/api/v1",
    "sapi": "https://api.binance.com/sapi/v1",
    "sapiV2": "https://api.binance.com/sapi/v2",
    "sapiV3": "https://api.binance.com/sapi/v3",
    "sapiV4": "https://api.binance.com/sapi/v4",
    "fapiPublic": "https://fapi.binance.com/fapi/v1",
    "fapiPublicV2": "https://fapi.binance.com/fapi/v2",
    "fapiPublicV3": "https://fapi.binance.com/fapi/v3",
    "fapiPrivate": "https://fapi.binance.com/fapi/v1",
    "fapiPrivateV2": "https://fapi.binance.com/fapi/v2",
    "fapiPrivateV3": "https://fapi.binance.com/fapi/v3",
    "fapiData": "https://fapi.binance.com/futures/data",
    "dapiPublic": "https://dapi.binance.com/dapi/v1",
    "dapiPrivate": "https://dapi.binance.com/dapi/v1",
    "dapiPrivateV2": "https://dapi.binance.com/dapi/v2",
    "dapiData": "https://dapi.binance.com/futures/data",
    "eapiPublic": "https://eapi.binance.com/eapi/v1",
    "eapiPrivate": "https://eapi.binance.com/eapi/v1",
    "papi": "https://papi.binance.com/papi/v1",
    "papiV2": "https://papi.binance.com/papi/v2",
}

_PRODUCTION_WS: Final[Mapping[str, str]] = {
    "spot": "wss://stream.binance.com:9443/ws",
    "margin": "wss://stream.binance.com:9443/ws",
    "future": "wss://fstream.binance.com/ws",
    "delivery": "wss://dstream.binance.com/ws",
    "option": "wss://fstream.binance.com/public/ws",
    "optionMarket": "wss://fstream.binance.com/market/ws",
    "optionPrivate": "wss://fstream.binance.com/private/ws",
    "papi": "wss://fstream.binance.com/pm/ws",
    "stock": "wss://nbstream.binance.com/equity/ws",
}


@dataclass(frozen=True)
class VenueEndpoints:
    """Where one venue lives, and what it can and cannot serve.

    ``rest_base`` is the scheme + host with no path; the per-tier paths are appended by
    :func:`ccxt_url_overrides` so there is exactly one place that knows ``/api/v3``.
    """

    venue: Venue
    rest_base: str
    ws_stream: str
    ws_api: str
    #: True only where Binance actually serves the ``/sapi`` tier. Demo and testnet do not:
    #: ``demo-api.binance.com/sapi/v1/account/apiRestrictions`` answers HTTP 404.
    supports_sapi: bool
    #: True where Binance serves USD-M / COIN-M futures for this venue. Earn is spot only,
    #: so this stays False everywhere and futures URLs are blackholed unconditionally.
    supports_futures: bool
    #: Where the operator mints a key for this venue.
    key_console: str
    #: How balances come back. Demo resets on request; testnet resets monthly.
    balance_reset: str

    @property
    def rest_host(self) -> str:
        return urllib.parse.urlsplit(self.rest_base).netloc

    @property
    def spot_api_v3(self) -> str:
        return f"{self.rest_base}/api/v3"


VENUES: Final[Mapping[Venue, VenueEndpoints]] = {
    Venue.LIVE: VenueEndpoints(
        venue=Venue.LIVE,
        rest_base="https://api.binance.com",
        ws_stream="wss://stream.binance.com:9443/ws",
        ws_api="wss://ws-api.binance.com:443/ws-api/v3",
        supports_sapi=True,
        supports_futures=False,
        key_console="https://www.binance.com/en/my/settings/api-management",
        balance_reset="never — this is real money",
    ),
    Venue.DEMO: VenueEndpoints(
        venue=Venue.DEMO,
        rest_base="https://demo-api.binance.com",
        ws_stream="wss://demo-stream.binance.com/ws",
        ws_api="wss://demo-ws-api.binance.com/ws-api/v3",
        supports_sapi=False,
        supports_futures=False,
        key_console="Binance account UI -> Demo Trading -> API Key Management",
        balance_reset="on operator request (not scheduled)",
    ),
    Venue.TESTNET: VenueEndpoints(
        venue=Venue.TESTNET,
        rest_base="https://testnet.binance.vision",
        ws_stream="wss://stream.testnet.binance.vision/ws",
        ws_api="wss://ws-api.testnet.binance.vision/ws-api/v3",
        supports_sapi=False,
        supports_futures=False,
        key_console="https://testnet.binance.vision",
        balance_reset="monthly, unannounced",
    ),
}

#: Hostnames that mean production. Used by :func:`assert_no_production_host` so a rendered
#: config can be swept for leaks without re-deriving the rule.
PRODUCTION_HOSTS: Final[frozenset[str]] = frozenset({
    "api.binance.com", "fapi.binance.com", "dapi.binance.com",
    "eapi.binance.com", "papi.binance.com",
    "stream.binance.com:9443", "stream.binance.com",
    "ws-api.binance.com:443", "ws-api.binance.com",
    "fstream.binance.com", "dstream.binance.com", "nbstream.binance.com",
})


def endpoints_for(venue: Venue | str) -> VenueEndpoints:
    """Look up a venue, accepting the enum or the string an operator wrote."""
    return VENUES[parse_venue(venue)]


def parse_venue(value: Venue | str) -> Venue:
    """Parse an operator-supplied venue name. Fails closed on anything unrecognised."""
    if isinstance(value, Venue):
        return value
    try:
        return Venue(str(value).strip().lower())
    except ValueError:
        known = ", ".join(v.value for v in Venue)
        raise VenueBindingError(
            f"unknown venue {value!r}. Known venues: {known}. "
            f"Set the venue explicitly next to the credential; there is no default, "
            f"because guessing wrong sends a demo key to production or worse."
        ) from None


# --------------------------------------------------------------------------- ccxt urls


def ccxt_url_overrides(venue: Venue | str) -> dict[str, str]:
    """The **complete** ``urls["api"]`` map for ``venue`` — every key ccxt knows.

    Keys the venue does not serve are set to :data:`BLACKHOLE_URL` rather than omitted.
    Omitting them is the bug: ccxt deep-merges a constructor ``urls`` override into the
    production map, so an omitted ``sapi`` stays on ``api.binance.com``.
    """
    ep = endpoints_for(venue)
    out: dict[str, str] = {}
    for key in PRODUCTION_URL_KEYS:
        if key in SPOT_URL_KEYS:
            suffix = "/api/v1" if key == "v1" else "/api/v3"
            out[key] = f"{ep.rest_base}{suffix}"
        elif key in SAPI_URL_KEYS:
            out[key] = _PRODUCTION_URLS[key] if ep.supports_sapi else BLACKHOLE_URL
        else:  # futures / options / portfolio-margin — never used by a spot-only system
            out[key] = _PRODUCTION_URLS[key] if ep.supports_futures else BLACKHOLE_URL
    return out


def ccxt_ws_overrides(venue: Venue | str) -> dict[str, Any]:
    """The complete ``urls["api"]["ws"]`` map, including the nested ``ws-api``."""
    ep = endpoints_for(venue)
    spot_ws = ep.ws_stream if ep.venue is not Venue.LIVE else _PRODUCTION_WS["spot"]
    ws: dict[str, Any] = {}
    for key in WS_URL_KEYS:
        if key in ("spot", "margin"):
            ws[key] = spot_ws
        else:
            ws[key] = _PRODUCTION_WS[key] if ep.supports_futures else BLACKHOLE_WS
    ws["ws-api"] = {
        key: (ep.ws_api if key == "spot"
              else (_PRODUCTION_WS["future"] if ep.supports_futures else BLACKHOLE_WS))
        for key in WS_API_URL_KEYS
    }
    return ws


def ccxt_config(venue: Venue | str) -> dict[str, Any]:
    """The dict to hand ccxt (or freqtrade's ``exchange.ccxt_config``) for ``venue``.

    This is the *fallback* route. The preferred route for demo is freqtrade's own
    ``exchange.demo_trading`` switch, which calls ccxt's ``enable_demo_trading(True)`` and
    replaces ``urls['api']`` wholesale — see :func:`freqtrade_exchange_patch`. Use this
    only where that switch is unavailable, and note that it works *because* every key is
    present, not because ccxt replaces the map.
    """
    urls = ccxt_url_overrides(venue)
    api: dict[str, Any] = dict(urls)
    api["ws"] = ccxt_ws_overrides(venue)
    return {"urls": {"api": api}}


def freqtrade_exchange_patch(venue: Venue | str) -> dict[str, Any]:
    """The ``exchange`` fragment freqtrade 2026.8 needs to reach ``venue``.

    For DEMO this is ``demo_trading: true`` plus the ``_ft_has_params`` override that
    unlocks it — freqtrade ships ``binance._ft_has["supports_demo_trading"] = False``, and
    ``Exchange.validate_demo_trading`` raises ``ConfigurationError`` without the override.
    ``build_ft_has()`` runs at ``exchange.py:228``, before ``_init_ccxt`` at 274, so the
    override is in force by the time the demo switch is read.
    """
    ep = endpoints_for(venue)
    if ep.venue is Venue.DEMO:
        return {
            "demo_trading": True,
            "_ft_has_params": {"supports_demo_trading": True},
        }
    if ep.venue is Venue.TESTNET:
        # freqtrade has no testnet switch for binance spot; the ccxt_config route is all
        # there is, and it is second best. Callers get it explicitly, never by accident.
        return {"demo_trading": False, "ccxt_config": ccxt_config(Venue.TESTNET)}
    return {"demo_trading": False}


def blackholed_keys(venue: Venue | str) -> tuple[str, ...]:
    """Which URL keys are disabled for ``venue`` — the audit trail for a rendered config."""
    return tuple(k for k, v in ccxt_url_overrides(venue).items() if v == BLACKHOLE_URL)


def assert_no_production_host(
    payload: Any, *, venue: Venue | str, where: str = "config"
) -> None:
    """Walk anything JSON-shaped and refuse if a production host appears for a non-live
    venue. The last line of defence before a demo config is written or handed to a bot."""
    ep = endpoints_for(venue)
    if ep.venue is Venue.LIVE:
        return
    leaks: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            host = urllib.parse.urlsplit(node).netloc
            if host and host in PRODUCTION_HOSTS:
                leaks.append(f"{path} = {node}")
        elif isinstance(node, Mapping):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload, where)
    if leaks:
        raise VenueBindingError(
            f"{where} is bound to venue {ep.venue.value} but still references production "
            f"hosts: {'; '.join(sorted(leaks))}. Every URL key must be routed to "
            f"{ep.rest_host} or to {BLACKHOLE_URL}. Rebuild it with "
            f"ops.lib.exchange_endpoints.ccxt_config({ep.venue.value!r})."
        )


#: The env-var prefix freqtrade turns into ``exchange.urls`` — and then ignores.
FREQTRADE_URL_ENV_PREFIX: Final = "FREQTRADE__EXCHANGE__URLS"

#: Every hostname this system knows about, production and not. Used by
#: :func:`assert_no_exchange_url_override` to sweep plain text (a compose file, a shell
#: script) where :func:`assert_no_production_host`'s JSON walk cannot reach.
ALL_VENUE_HOSTS: Final[frozenset[str]] = frozenset(
    {*PRODUCTION_HOSTS}
    | {urllib.parse.urlsplit(e.rest_base).netloc for e in VENUES.values()}
    | {urllib.parse.urlsplit(e.ws_stream).netloc for e in VENUES.values()}
    | {urllib.parse.urlsplit(e.ws_api).netloc for e in VENUES.values()}
)


def assert_no_exchange_url_override(text: str, *, where: str = "file") -> None:
    """Refuse any hand-written exchange URL in a compose layer or script.

    Two things are refused, for one reason each:

    * ``FREQTRADE__EXCHANGE__URLS__*`` — freqtrade 2026.8 reads ``exchange.urls`` from
      nowhere (measured: a grep of the installed package finds no config lookup, and
      ``CONF_SCHEMA``'s ``exchange`` declares neither ``properties`` nor
      ``additionalProperties``, so the key is accepted in silence). It is a line that
      looks like routing, is not, and left our testnet rehearsal pointed at production.
    * any Binance hostname at all — routing is chosen by venue in code
      (``demo_trading`` for demo, the default for live), never spelled in a YAML file
      where nothing validates it and no test reads it back.

    Comment lines (``#``) are skipped: a comment cannot route traffic, and the generated
    override names the venue's host in one so an operator reading the file knows which
    Binance the container is bound to. Everything else — keys, values, inline comments on
    a value-bearing line — is checked.
    """
    text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    if FREQTRADE_URL_ENV_PREFIX in text:
        raise VenueBindingError(
            f"{where} sets {FREQTRADE_URL_ENV_PREFIX}__*, which freqtrade 2026.8 reads "
            f"from nowhere — the config key exists, passes validation, and is never "
            f"looked up. A bot configured this way runs against "
            f"{VENUES[Venue.LIVE].rest_host}. Remove the line; route by venue instead "
            f"(exchange.demo_trading for demo, the ccxt default for live)."
        )
    found = sorted(h for h in ALL_VENUE_HOSTS if h in text)
    if found:
        raise VenueBindingError(
            f"{where} hardcodes exchange host(s) {', '.join(found)}. No compose layer may "
            f"choose a venue: the venue is bound to the mode in "
            f"ops.lib.exchange_endpoints.MODE_VENUE, and the only switch that actually "
            f"moves ccxt is exchange.demo_trading in the rendered mode overlay."
        )


# --------------------------------------------------------------------------- credentials


@dataclass(frozen=True)
class Credential:
    """One sleeve's exchange key, labelled with the venue it was issued for.

    The secret is held only long enough to sign a verification probe and is never part of
    ``repr``, ``str`` or any message this module raises.
    """

    label: str
    venue: Venue
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
            "label": self.label,
            "venue": self.venue.value,
            "present": self.present,
            "last4": self.last4,
        }

    def __repr__(self) -> str:  # never leak key material through a traceback
        return (
            f"Credential(label={self.label!r}, venue={self.venue.value!r}, "
            f"present={self.present}, last4={self.last4!r})"
        )

    __str__ = __repr__


# --------------------------------------------------------------------------- credential env

#: How a venue's credentials are named in ``.env``. The names are **disjoint per venue on
#: purpose**: a demo key is never written under a live name, so the two can coexist in one
#: ``.env`` and a mis-edit cannot silently promote a demo key to production (or the
#: reverse). ``{s}`` is the upper-cased sleeve letter where a venue is keyed per sleeve.
#:
#: LIVE is per sleeve because ``modes.live.one_live_sleeve_per_account`` means each live
#: sleeve is its own Binance account. DEMO and TESTNET are one account for the whole
#: install, so their names carry no sleeve letter — which is also what makes
#: ``BINANCE_DEMO_KEY`` impossible to mistake for ``BINANCE_KEY_A`` at a glance.
CREDENTIAL_ENV: Final[Mapping[Venue, tuple[str, str]]] = {
    Venue.LIVE: ("BINANCE_KEY_{s}", "BINANCE_SECRET_{s}"),
    Venue.DEMO: ("BINANCE_DEMO_KEY", "BINANCE_DEMO_SECRET"),
    Venue.TESTNET: ("BINANCE_TESTNET_KEY", "BINANCE_TESTNET_SECRET"),
}


def credential_env_names(venue: Venue | str, sleeve: str = "a") -> tuple[str, str]:
    """``(key_env, secret_env)`` for ``venue`` — the only place the names are spelled."""
    ep = endpoints_for(venue)
    key, secret = CREDENTIAL_ENV[ep.venue]
    s = str(sleeve).strip().upper()
    return key.format(s=s), secret.format(s=s)


def foreign_credential_env_names(
    venue: Venue | str, sleeves: Sequence[str] = ("a", "b")
) -> tuple[str, ...]:
    """Every credential env name that does **not** belong to ``venue``.

    A renderer sweeps its own output for these: a demo container that can see
    ``BINANCE_KEY_A`` is one ``.env`` typo away from trading real money, so the name must
    not appear in a demo layer at all — not even unset, not even quoted empty.
    """
    ep = endpoints_for(venue)
    mine = {n for s in sleeves for n in credential_env_names(ep.venue, s)}
    out: list[str] = []
    for other in Venue:
        if other is ep.venue:
            continue
        for s in sleeves:
            out.extend(n for n in credential_env_names(other, s) if n not in mine)
    return tuple(sorted(set(out)))


def credential_from_env(
    venue: Venue | str,
    sleeve: str = "a",
    env: Mapping[str, str] | None = None,
) -> Credential:
    """Read ``venue``'s credential for ``sleeve`` out of an environment mapping.

    Never raises on absence: an unset key yields a :class:`Credential` with
    ``present == False`` so a *render* can still describe the venue it is bound to, while
    ``resolve_binding`` / preflight are the ones that refuse to *run* without it.
    """
    import os

    e = env if env is not None else os.environ
    key_env, secret_env = credential_env_names(venue, sleeve)
    return Credential(
        label=key_env,
        venue=endpoints_for(venue).venue,
        key=(e.get(key_env) or "").strip(),
        secret=(e.get(secret_env) or "").strip(),
    )


def cross_labelled_credentials(
    env: Mapping[str, str] | None = None, sleeves: Sequence[str] = ("a", "b")
) -> tuple[str, ...]:
    """Names under which the *same* API key is filed for two different venues.

    A label is a claim (:func:`verify_credential_venue` is what proves it), but this one
    lie is detectable with no network at all and is the likeliest way the owner's live key
    ends up pointed at demo: paste the same key into ``BINANCE_DEMO_KEY`` and
    ``BINANCE_KEY_A``. Returns human-readable strings, never key material.
    """
    import os

    e = env if env is not None else os.environ
    seen: dict[str, list[tuple[Venue, str]]] = {}
    for venue in Venue:
        for sleeve in sleeves:
            key_env, _ = credential_env_names(venue, sleeve)
            value = (e.get(key_env) or "").strip()
            if not value:
                continue
            seen.setdefault(value, [])
            if (venue, key_env) not in seen[value]:
                seen[value].append((venue, key_env))
    out: list[str] = []
    for value, holders in seen.items():
        venues = {v for v, _ in holders}
        if len(venues) > 1:
            names = ", ".join(sorted(n for _, n in holders))
            tail = value[-4:] if len(value) >= 4 else ""
            out.append(
                f"key ...{tail} is filed under {names}, which claim "
                f"{', '.join(sorted(v.value for v in venues))} at once"
            )
    return tuple(sorted(out))


# --------------------------------------------------------------------------- binding


class VenueBindingError(Exception):
    """A refusal. The message names what is wrong and what the operator should do.

    Never carries key material — only the credential's label and last four characters.
    """


#: Which venue each sleeve mode is allowed to reach. ``None`` means "no exchange
#: credential at all": a dry-run sleeve talks to nobody. Anything not listed is refused,
#: so adding a mode without deciding its venue is a hard error, not a default.
MODE_VENUE: Final[Mapping[str, Venue | None]] = {
    "TEST": None,
    "DEMO_PROPOSE": Venue.DEMO,
    "DEMO_EXECUTE": Venue.DEMO,
    "LIVE_PROPOSE": Venue.LIVE,
    "LIVE_EXECUTE": Venue.LIVE,
    "TESTNET_REHEARSAL": Venue.TESTNET,
}

#: Mid-transition states. A config must never be built from one.
TRANSIENT_MODES: Final[frozenset[str]] = frozenset({"ARMING", "DISARMING"})


@dataclass(frozen=True)
class Binding:
    """A mode, the venue it may reach, and the credential bound to it. Only
    :func:`resolve_binding` constructs one, so holding a ``Binding`` *is* the proof that
    mode and venue agree."""

    mode: str
    venue: Venue | None
    endpoints: VenueEndpoints | None
    credential: Credential | None

    @property
    def needs_credential(self) -> bool:
        return self.venue is not None

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "venue": self.venue.value if self.venue else None,
            "rest_base": self.endpoints.rest_base if self.endpoints else None,
            "ws_stream": self.endpoints.ws_stream if self.endpoints else None,
            "credential": self.credential.describe() if self.credential else None,
        }


def venue_for_mode(mode: str) -> Venue | None:
    """The venue ``mode`` is allowed to reach, or ``None`` for a dry-run mode."""
    key = str(mode).strip().upper()
    if key in TRANSIENT_MODES:
        raise VenueBindingError(
            f"mode {key} is a transient state, not a destination. A config must never be "
            f"built from it. Wait for the transition to finish, or run the console's "
            f"recover step, which drops the sleeve to TEST."
        )
    if key not in MODE_VENUE:
        known = ", ".join(sorted(MODE_VENUE))
        raise VenueBindingError(
            f"unknown mode {mode!r}. Known modes: {known}. A new mode must be given a "
            f"venue in ops.lib.exchange_endpoints.MODE_VENUE before anything can run in it."
        )
    return MODE_VENUE[key]


def resolve_binding(mode: str, credential: Credential | None = None) -> Binding:
    """Bind ``mode`` to its venue, refusing every disagreement.

    Raises :class:`VenueBindingError` when the mode is unknown or transient, when a
    dry-run mode carries a usable credential, when a venue-bound mode has no credential,
    or — the case this module exists for — when the credential's venue is not the mode's.
    """
    key = str(mode).strip().upper()
    venue = venue_for_mode(key)

    if venue is None:
        if credential is not None and credential.present:
            raise VenueBindingError(
                f"mode {key} is dry-run and must reach no exchange, but credential "
                f"{credential.label} (venue={credential.venue.value}, "
                f"key ...{credential.last4}) was supplied. Remove the key from this "
                f"sleeve's environment, or move the sleeve to "
                f"{_mode_suggestion(credential.venue)}."
            )
        return Binding(mode=key, venue=None, endpoints=None, credential=None)

    if credential is None or not credential.present:
        ep = VENUES[venue]
        name = credential.label if credential else "the exchange credential"
        raise VenueBindingError(
            f"mode {key} must reach {venue.value} ({ep.rest_host}) but {name} is not set. "
            f"Mint a {venue.value} key at {ep.key_console}, enable trading, disable "
            f"withdrawals, and set both the key and the secret before switching mode."
        )

    if credential.venue is not venue:
        ep, other = VENUES[venue], VENUES[credential.venue]
        raise VenueBindingError(
            f"REFUSED: mode {key} may only reach {venue.value} ({ep.rest_host}), but "
            f"credential {credential.label} (key ...{credential.last4}) is labelled "
            f"{credential.venue.value} ({other.rest_host}). "
            f"Either label/replace the credential with a {venue.value} key from "
            f"{ep.key_console}, or move the sleeve to "
            f"{_mode_suggestion(credential.venue)}. "
            f"Refusing to build a config that points {credential.venue.value} credentials "
            f"at {ep.rest_host}."
        )

    return Binding(mode=key, venue=venue, endpoints=VENUES[venue], credential=credential)


def _mode_suggestion(venue: Venue) -> str:
    modes = sorted(m for m, v in MODE_VENUE.items() if v is venue)
    return " or ".join(modes) if modes else f"a {venue.value} mode"


# --------------------------------------------------------------------------- verification

#: What one probe against one venue concluded.
ProbeVerdict = Literal["authenticated", "rejected", "unreachable"]

#: ``(venue, credential) -> verdict``. Injected so the test suite never touches a network.
Prober = Callable[[Venue, Credential], ProbeVerdict]

VerifyStatus = Literal["confirmed", "mismatch", "cannot_verify"]


@dataclass(frozen=True)
class VenueProof:
    """The result of checking a credential against the venue it claims.

    ``confirmed`` requires both halves: it authenticated where it claimed, and it was
    *refused* everywhere else. Anything unproven is ``cannot_verify``, which is a refusal
    to pass, not a pass.
    """

    status: VerifyStatus
    label: str
    claimed: Venue
    authenticated_at: tuple[Venue, ...]
    detail: str
    probed: Mapping[str, ProbeVerdict]

    @property
    def ok(self) -> bool:
        return self.status == "confirmed"

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "label": self.label,
            "claimed": self.claimed.value,
            "authenticated_at": [v.value for v in self.authenticated_at],
            "detail": self.detail,
            "probed": dict(self.probed),
        }


def verify_credential_venue(
    credential: Credential | None,
    *,
    probe: Prober,
    venues: Sequence[Venue] | None = None,
) -> VenueProof:
    """Confirm ``credential`` authenticates at the venue it claims and nowhere else.

    Designed for ``ops/preflight.py`` to call as a blocking check. With no key present it
    returns ``cannot_verify`` — never ``confirmed`` — so an absent key can never be
    mistaken for a verified one.
    """
    if credential is None:
        return VenueProof(
            status="cannot_verify", label="(none)", claimed=Venue.LIVE,
            authenticated_at=(), probed={},
            detail="no credential supplied; cannot verify which venue it belongs to",
        )
    if not credential.present:
        return VenueProof(
            status="cannot_verify", label=credential.label, claimed=credential.venue,
            authenticated_at=(), probed={},
            detail=(
                f"{credential.label} is not set (key and secret must both be present); "
                f"cannot verify it belongs to {credential.venue.value}"
            ),
        )

    order: Iterable[Venue] = venues if venues is not None else tuple(Venue)
    results: dict[str, ProbeVerdict] = {}
    for venue in order:
        results[venue.value] = probe(venue, credential)

    claimed = results.get(credential.venue.value)
    authed = tuple(v for v in Venue if results.get(v.value) == "authenticated")
    foreign = tuple(v for v in authed if v is not credential.venue)

    if foreign:
        names = ", ".join(f"{v.value} ({VENUES[v].rest_host})" for v in foreign)
        return VenueProof(
            status="mismatch", label=credential.label, claimed=credential.venue,
            authenticated_at=authed, probed=results,
            detail=(
                f"DANGER: {credential.label} (key ...{credential.last4}) is labelled "
                f"{credential.venue.value} but authenticates against {names}. Treat this "
                f"key as compromised for its stated purpose: revoke it, and mint a "
                f"separate key per venue."
            ),
        )

    if claimed == "authenticated":
        unproven = [v for v, r in results.items() if r == "unreachable" and v != credential.venue.value]
        if unproven:
            return VenueProof(
                status="cannot_verify", label=credential.label, claimed=credential.venue,
                authenticated_at=authed, probed=results,
                detail=(
                    f"{credential.label} authenticates against {credential.venue.value}, "
                    f"but {', '.join(sorted(unproven))} could not be reached, so it is "
                    f"unproven that the key is rejected there. Retry when the network is "
                    f"available rather than proceeding."
                ),
            )
        return VenueProof(
            status="confirmed", label=credential.label, claimed=credential.venue,
            authenticated_at=authed, probed=results,
            detail=(
                f"{credential.label} (key ...{credential.last4}) authenticates against "
                f"{credential.venue.value} ({VENUES[credential.venue].rest_host}) and is "
                f"rejected by every other venue"
            ),
        )

    if claimed == "rejected":
        ep = VENUES[credential.venue]
        return VenueProof(
            status="mismatch", label=credential.label, claimed=credential.venue,
            authenticated_at=authed, probed=results,
            detail=(
                f"{credential.label} (key ...{credential.last4}) is labelled "
                f"{credential.venue.value} but {ep.rest_host} rejects it. Mint the key at "
                f"{ep.key_console}, or correct the venue label."
            ),
        )

    return VenueProof(
        status="cannot_verify", label=credential.label, claimed=credential.venue,
        authenticated_at=authed, probed=results,
        detail=(
            f"{VENUES[credential.venue].rest_host} could not be reached, so "
            f"{credential.label} could not be checked against the venue it claims"
        ),
    )


# --------------------------------------------------------------------------- probe edge


def signed_query(secret: str, params: Mapping[str, Any], *, now_ms: int | None = None,
                 recv_window_ms: int = 5000) -> str:
    """The signed query string for a Binance ``/api/v3`` GET. Pure, so it is tested."""
    query: dict[str, Any] = dict(params)
    query["timestamp"] = now_ms if now_ms is not None else int(time.time() * 1000)
    query["recvWindow"] = recv_window_ms
    qs = urllib.parse.urlencode(query)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def httpx_account_probe(  # pragma: no cover - the network edge
    venue: Venue, credential: Credential, *, timeout: float = 10.0, client: Any = None
) -> ProbeVerdict:
    """Ask one venue whether it knows this key, using a read-only ``/api/v3/account`` GET.

    ``authenticated`` on HTTP 200; ``rejected`` on the 401/403 and the Binance ``-2014`` /
    ``-2015`` key errors; ``unreachable`` for anything else, including a transport error —
    an ambiguous answer must never read as proof.
    """
    ep = VENUES[venue]
    if client is None:
        import httpx

        client = httpx.Client(timeout=timeout)
    url = f"{ep.spot_api_v3}/account?{signed_query(credential.secret, {'omitZeroBalances': 'true'})}"
    try:
        resp = client.get(url, headers={"X-MBX-APIKEY": credential.key})
    except Exception:
        return "unreachable"
    if resp.status_code == 200:
        return "authenticated"
    if resp.status_code in (401, 403):
        return "rejected"
    if resp.status_code in (400, 404):
        try:
            code = int(resp.json().get("code", 0))
        except Exception:
            return "unreachable"
        if code in (-2014, -2015, -1022, -2008):
            return "rejected"
    return "unreachable"


__all__ = [
    "ALL_VENUE_HOSTS",
    "BLACKHOLE_URL",
    "BLACKHOLE_WS",
    "CREDENTIAL_ENV",
    "FREQTRADE_URL_ENV_PREFIX",
    "FUTURES_URL_KEYS",
    "MODE_VENUE",
    "PRODUCTION_HOSTS",
    "PRODUCTION_URL_KEYS",
    "SAPI_URL_KEYS",
    "SPOT_URL_KEYS",
    "TRANSIENT_MODES",
    "VENUES",
    "WS_API_URL_KEYS",
    "WS_URL_KEYS",
    "Binding",
    "Credential",
    "ProbeVerdict",
    "Prober",
    "Venue",
    "VenueBindingError",
    "VenueEndpoints",
    "VenueProof",
    "VerifyStatus",
    "assert_no_exchange_url_override",
    "assert_no_production_host",
    "blackholed_keys",
    "ccxt_config",
    "ccxt_url_overrides",
    "ccxt_ws_overrides",
    "credential_env_names",
    "credential_from_env",
    "cross_labelled_credentials",
    "endpoints_for",
    "foreign_credential_env_names",
    "freqtrade_exchange_patch",
    "httpx_account_probe",
    "parse_venue",
    "resolve_binding",
    "signed_query",
    "venue_for_mode",
    "verify_credential_venue",
]
