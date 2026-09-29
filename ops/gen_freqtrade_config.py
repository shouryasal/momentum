"""Render every generated config from config/earn.yaml.

Two tiers of output, deliberately separated:

**Committed and deterministic** — a pure function of ``earn.yaml``, drift-checked by
``--check`` and reviewed in git:

  config/freqtrade-a.json, config/freqtrade-b.json   the bots' base configs (always dry_run)
  config/riskgate.json                               the gate's in-container config (stdlib json)
  config/params-sleeve-{a,b}.json                    tier-1 sleeve params (seeded once, then
                                                     owned by runs/apply_changes.py)

**Machine-local and mode-dependent** — under ``var/runtime/`` (gitignored, mounted
read-only into the containers), rendered from the signed ``var/state/mode.json``:

  var/runtime/freqtrade-<s>.mode.json   the overlay passed as a second ``--config``
  var/runtime/runtime-<s>.json          what the strategy reads through ``$EARN_RUNTIME``
  var/runtime/compose.override.yml      exchange credentials, by env reference only

That split is what lets live-ness stay out of git while a config save can never silently
drop a live bot to dry-run: the committed file always says ``dry_run: true`` and only a
**verified** mode state can render an overlay that says otherwise. An unverified mode file
(missing, tampered, or no ``EARN_CONSOLE_SECRET``) renders TEST overlays and refuses to
render a live one.

Deliberate choices (documented for reviewers):
  - NO "order_types" in the committed bot JSON: a config-file order_types dict overwrites
    the strategy's wholesale (freqtrade-documented), which would clobber the gate's
    force_exit/emergency_exit settings. order_types live in the strategy class, and the
    live overlay only adds ``stoploss_on_exchange``.
  - NO "telegram" block AT ALL: freqtrade's own Telegram stays off (one alerting pipeline
    — ops/lib/tg.py + the ops bot), and in freqtrade's CONF_SCHEMA "off" is expressed by
    OMITTING the block. Writing ``{"enabled": false}`` is not a disabled block, it is an
    INVALID one: the schema requires ``[enabled, token, chat_id]`` whenever the key
    exists, so every bot died at startup with ``Invalid configuration. Reason: 'token' is
    a required property`` and restarted forever. :data:`SCHEMA_REQUIRED_WHEN_PRESENT` and
    :func:`audit_optional_blocks` make that class of bug a render-time refusal, and
    tests/test_ops/test_freqtrade_config_schema.py validates every rendered config
    against freqtrade's OWN validator.
  - api_server binds 0.0.0.0 INSIDE the container; localhost-only exposure is enforced
    by the compose host binding (127.0.0.1:808x). Credentials come from env overrides
    (FREQTRADE__API_SERVER__*), never from this JSON. ``jwt_secret_key`` is therefore
    omitted rather than written as ``""``: the schema demands >= 32 characters, so the
    empty placeholder was a second startup crash waiting behind the telegram one. Absent,
    it is filled by freqtrade's own schema default and then overridden by
    ``FREQTRADE__API_SERVER__JWT_SECRET_KEY`` from ops/docker-compose.yml.
  - No exchange key or secret is ever written to any JSON file. The compose override
    references ``${BINANCE_KEY_A}`` (live) or ``${BINANCE_DEMO_KEY}`` (demo); docker
    interpolates them. Which names appear is decided by the sleeve's *mode*, in code.
  - VENUE IS BOUND TO MODE, IN CODE. ``ops.lib.exchange_endpoints.MODE_VENUE`` is the only
    table that says which Binance a mode may reach — there is deliberately no
    ``exchange.venue`` key in earn.yaml, because a second source of truth is a source of
    disagreement. A demo sleeve renders ``exchange.demo_trading: true`` plus the
    ``_ft_has_params`` unlock (freqtrade ships ``binance.supports_demo_trading = False``),
    ``dry_run: false``, and a compose override that references the demo credential names
    and **no live name at all**. The reverse is equally refused. Both refusals are
    :class:`RenderError` at render time, so the bad config cannot be produced, let alone
    started.
  - NO url override is ever emitted. ``FREQTRADE__EXCHANGE__URLS__*`` is read by nothing in
    freqtrade 2026.8 (measured — docs/design/demo-mode.md §3a), and a partial
    ``ccxt_config.urls`` deep-merges, leaving sapi/papi/fapi on production. Demo routing
    comes from ``demo_trading``, which makes ccxt assign the whole demo URL map.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal

from ops import universe
from ops.config import (
    DEFAULT_CONFIG,
    REPO_ROOT,
    EarnConfig,
    load_config,
    seed_for,
    startup_candles,
    stoploss_on_exchange,
    trading_for,
)
from ops.lib import mode_state as ms
from ops.lib import paths
from ops.lib.exchange_endpoints import (
    Venue,
    VenueBindingError,
    assert_no_exchange_url_override,
    assert_no_production_host,
    blackholed_keys,
    credential_env_names,
    credential_from_env,
    cross_labelled_credentials,
    endpoints_for,
    foreign_credential_env_names,
    freqtrade_exchange_patch,
    resolve_binding,
    venue_for_mode,
)

CONFIG_DIR = REPO_ROOT / "config"

#: The committed render is a pure function of earn.yaml, so it pins the baseline phase.
#: Anything mode-dependent belongs in var/runtime/.
COMMITTED_PHASE = "paper"

RUNTIME_VERSION = 1

CONTAINER_PATHS = {
    "journal_db": "/freqtrade/journal/journal.db",
    "knowledge_db": "/freqtrade/knowledge/earn.db",
    "flags_file": "/freqtrade/knowledge/flags.json",
    "kill_file": "/freqtrade/killdir/KILL",
    "proposals_dir": "/freqtrade/proposals",
    "config_dir": "/freqtrade/earn-config",
    "data_dir": "/freqtrade/user_data/data",
}

#: where the approved-proposal files live inside the container (propose mode)
CONTAINER_APPROVALS = "/freqtrade/proposals/approved"

Sleeve = Literal["a", "b"]

#: Freqtrade config blocks that are OPTIONAL as a whole but, once the key exists, oblige
#: the whole set below. The values are the keys ``CONF_SCHEMA`` marks required **and does
#: not default** for us — i.e. what this generator must write whenever it renders the
#: block. Pinned against the installed package by
#: ``tests/test_ops/test_freqtrade_config_schema.py::test_required_when_present_matches_schema``
#: so a freqtrade upgrade that adds a required key fails a test instead of a bot.
SCHEMA_REQUIRED_WHEN_PRESENT: dict[str, tuple[str, ...]] = {
    # jwt_secret_key is required but schema-defaulted, so it is deliberately absent here.
    "api_server": ("enabled", "listen_ip_address", "listen_port", "username", "password"),
    "telegram": ("enabled", "token", "chat_id"),
    "webhook": (),
    "order_types": ("entry", "exit", "stoploss", "stoploss_on_exchange"),
    # price_side is required but defaulted ("same"), so an incomplete pricing block is
    # freqtrade's problem to fill, not ours.
    "entry_pricing": (),
    "exit_pricing": (),
    "unfilledtimeout": (),
    "internals": (),
}


class RenderError(Exception):
    """Refusal to render — always fail closed, never render live by accident."""


def audit_optional_blocks(conf: dict[str, Any], *, where: str) -> dict[str, Any]:
    """Refuse to render an optional block that freqtrade would reject.

    The invariant is *present implies complete*: a block we render carries every key the
    schema requires of it, and a block we cannot complete — a disabled telegram has no
    token — is left out altogether. That is the exact shape of the bug that put both bots
    in a restart loop, so it is checked here at render time rather than discovered at
    ``docker compose up``.
    """
    for name, required in SCHEMA_REQUIRED_WHEN_PRESENT.items():
        block = conf.get(name)
        if block is None:
            continue
        missing = sorted(k for k in required if k not in block)
        if missing:
            hint = ""
            if isinstance(block, dict) and block.get("enabled") is False:
                hint = (
                    " — a DISABLED block must be omitted entirely; freqtrade has no "
                    "'enabled: false means ignore the rest' rule"
                )
            raise RenderError(
                f"{where}: config block '{name}' is missing {missing}, which freqtrade's "
                f"CONF_SCHEMA requires whenever the key exists{hint}"
            )
    return conf


def _source_sha(config_path: Path) -> str:
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- universe

#: Point-in-time universe snapshots written by the resolver (``ops/universe.py``, U1).
#: The most recent one is the whitelist the bots run AND the whitelist the backtests
#: replay — freqtrade's dynamic pairlists are all ``SupportsBacktesting.NO``, so a live
#: bot that picked its own universe could never be backtested (wide-universe.md §5.3).
UNIVERSE_DIR = REPO_ROOT / "knowledge" / "universe"

#: The one tier growth-audit.md §1.5's exclusion filter applies to (see satellite_exclusions).
SATELLITE_TIER = "satellite"


def latest_snapshot(root: Path | None = None) -> dict[str, Any] | None:
    """The newest ``knowledge/universe/<date>.json``, or ``None`` before the first refresh.

    Unreadable or malformed is the same as absent: the render then falls back to
    ``universe.core`` and the whole wide-universe machinery stays dormant, which is a
    two-asset bot and not a wrong one. A snapshot that cannot be parsed must never
    silently become a *different* universe.

    The format is owned by ``ops/universe.py`` and pinned in ``docs/contracts.md``;
    this function only locates the file.
    """
    path = universe.latest_snapshot_path(root or UNIVERSE_DIR)
    if path is None:
        return None
    try:
        snap = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("pairs"), dict):
        return None
    snap.setdefault("date", path.stem)
    return snap


def snapshot_whitelist(snap: dict[str, Any] | None, cfg: EarnConfig) -> list[str]:
    """The pair whitelist: the snapshot's TRADEABLE tier, or ``universe.pairs``.

    ``exit_only`` names stay on the whitelist on purpose. Dropping a pair freqtrade holds
    a position in is not an exit, it is an orphan; the gate refuses new entries for those
    names (``exit_only:<asset>``) while the sleeve winds the position down through the
    normal band (wide-universe.md §1.5).

    Ordering is the snapshot's own: core first in config order, then major, then
    satellites by score rank. A rendered config is a committed, drift-checked artefact,
    so the order has to be a function of the snapshot and not of dict iteration.
    """
    if not snap:
        return list(cfg.universe.pairs)
    pairs = snap.get("pairs")
    if isinstance(pairs, dict) and pairs:
        return [
            p for p, e in _ordered_snapshot_pairs(snap)
            if str(e.get("tier")) in universe.TRADEABLE_TIERS
        ]
    # Tolerated legacy shape: a flat {asset: tier} map with a separate exit_only list.
    quote = cfg.universe.quote
    tiers = snap.get("tiers") or {}
    tradeable = {str(a) for a, t in tiers.items() if str(t) in universe.TRADEABLE_TIERS}
    tradeable |= {str(a) for a in (snap.get("exit_only") or []) if str(a) in tiers}
    return [f"{a}/{quote}" for a in sorted(tradeable)]


def _ordered_snapshot_pairs(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    core = list((snap.get("rules") or {}).get("core") or ())
    quote = str(snap.get("quote") or "USDT")
    core_pairs = [f"{a}/{quote}" for a in core]
    tier_order = {t: i for i, t in enumerate(universe.WATCHED_TIERS)}

    def key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, int, str]:
        pair, entry = item
        pos = core_pairs.index(pair) if pair in core_pairs else 10_000
        return (pos, tier_order.get(str(entry.get("tier")), 99),
                int(entry.get("rank") or 10_000), pair)

    return sorted((snap.get("pairs") or {}).items(), key=key)


def satellite_exclusions(snap: dict[str, Any] | None, cfg: EarnConfig) -> dict[str, str]:
    """growth-audit.md §1.5's exclusion filter, applied to the snapshot's SATELLITE tier.

    ``universe.satellite_eligibility`` carries the three measured thresholds — 60d realised
    vol ceiling (``max_ann_vol``), listing-age floor (``min_listing_age_days``) and the
    median 90d quote-volume floor. MEMBERSHIP (``universe.tiers.satellite``, what the weekly
    resolver puts in the tier) is deliberately untouched, so the committed snapshot stays
    the artefact it was resolved as; ELIGIBILITY is enforced HERE, when the snapshot is
    rendered into the gate's config, so a satellite that fails it is rendered
    ``exit_only``: the gate refuses every entry for it and a held position is wound down
    through the normal path rather than orphaned (wide-universe.md §1.5).

    Returns ``{base_asset: reason}`` for every satellite the filter removes. Core and
    major names are never touched — the filter is a satellite-eligibility rule, and the
    audit measured it as one. A leg whose metric the snapshot does not carry is not
    evaluated: the resolver's ``Metrics.to_dict`` always writes all three, so an absent key
    is a fixture or a legacy shape, not a measurement of anything; a metric that is present
    and fails excludes. Legacy flat-shape snapshots carry no metrics and are left alone.
    """
    if not snap or not isinstance(snap.get("pairs"), dict):
        return {}
    rule = cfg.universe.satellite_eligibility
    out: dict[str, str] = {}
    for pair, entry in sorted(snap["pairs"].items()):
        if not isinstance(entry, dict) or str(entry.get("tier")) != SATELLITE_TIER:
            continue
        base = str(entry.get("base") or str(pair).split("/")[0])
        m = entry.get("metrics") or {}
        reasons: list[str] = []
        vol, age, adv = (m.get("ann_vol_short"), m.get("listing_age_days"),
                         m.get("median_quote_volume"))
        if rule.max_ann_vol is not None and vol is not None:
            if float(vol) > rule.max_ann_vol + 1e-12:
                reasons.append(f"vol60:{float(vol):.2f}>{rule.max_ann_vol:.2f}")
        if rule.min_listing_age_days and age is not None:
            if int(age) < rule.min_listing_age_days:
                reasons.append(f"age:{int(age)}d<{rule.min_listing_age_days}d")
        if rule.min_median_quote_volume_usdt and adv is not None:
            if float(adv) < rule.min_median_quote_volume_usdt:
                reasons.append(
                    f"adv:{float(adv) / 1e6:.1f}M<{rule.min_median_quote_volume_usdt / 1e6:.0f}M")
        if reasons:
            out[base] = ",".join(reasons)
    return out


def gate_universe_block(snap: dict[str, Any],
                        exclusions: dict[str, str] | None = None) -> dict[str, Any]:
    """The slice of a resolver snapshot the deterministic gate actually enforces.

    The full snapshot carries 107 pairs of metrics, funnel counts and provenance; the gate
    needs five maps keyed by BASE ASSET and nothing else. Extracting here rather than
    shipping the whole file keeps ``strategies/riskgate.py`` stdlib-only and small, and
    keeps the committed ``riskgate.json`` diff readable when the weekly refresh moves a
    name between tiers — which is the diff a human is actually asked to review.

    ``exit_only`` is the union of the resolver's own flag and any pair carrying a
    ``delisting_at``: a delisting notice must block entries from the moment it is in the
    snapshot, not from the moment somebody re-runs the resolver (wide-universe.md §2.6).

    ``step_notional`` is ``step_size x price`` at snapshot time — the USDT value of one
    ``LOT_SIZE`` step, which is the form the gate can check against an order notional
    without knowing the live price. It is re-derived every refresh, so a name whose price
    has moved an order of magnitude gets a fresh floor within the week.

    ``exclusions`` (from :func:`satellite_exclusions`) are added to ``exit_only`` and
    recorded under ``excluded`` with their reasons, so the rendered file says WHY a name
    the snapshot calls tradeable is not enterable.
    """
    pairs = snap.get("pairs")
    excluded = dict(exclusions or {})
    if not isinstance(pairs, dict):
        # Legacy flat shape ({asset: tier} + exit_only list), used by fixtures.
        return {
            "date": str(snap.get("date") or ""),
            "sha256": str(snap.get("sha256") or ""),
            "tiers": dict(snap.get("tiers") or {}),
            "caps": dict(snap.get("caps") or {}),
            "scores": dict(snap.get("scores") or {}),
            "exit_only": sorted(str(a) for a in (snap.get("exit_only") or [])),
            "filters": dict(snap.get("filters") or {}),
        }
    tiers: dict[str, str] = {}
    caps: dict[str, float] = {}
    scores: dict[str, float] = {}
    exit_only: set[str] = set()
    filters: dict[str, dict[str, float]] = {}
    for pair, entry in sorted(pairs.items()):
        if not isinstance(entry, dict):
            continue
        base = str(entry.get("base") or str(pair).split("/")[0])
        tiers[base] = str(entry.get("tier") or "")
        if entry.get("cap") is not None:
            caps[base] = float(entry["cap"])
        if entry.get("score") is not None:
            scores[base] = float(entry["score"])
        if entry.get("exit_only") or entry.get("delisting_at"):
            exit_only.add(base)
        price = float((entry.get("metrics") or {}).get("price") or 0.0)
        step = float(entry.get("step_size") or 0.0)
        spec = {"min_notional": float(entry.get("min_notional") or 0.0)}
        if step > 0 and price > 0:
            spec["step_notional"] = round(step * price, 8)
        filters[base] = spec
    exit_only |= set(excluded)
    block = {
        "date": str(snap.get("date") or ""),
        "sha256": str(snap.get("sha256") or ""),
        "tiers": tiers,
        "caps": caps,
        "scores": scores,
        "exit_only": sorted(exit_only),
        "filters": filters,
    }
    if excluded:
        block["excluded"] = dict(sorted(excluded.items()))
    return block


def default_run_id(sleeve: str) -> str:
    """Run id used when no mode transition has minted one yet (fresh checkout)."""
    return f"test-{sleeve}-000"


# --------------------------------------------------------------------------- committed


def build_bot_config(cfg: EarnConfig, sleeve: Sleeve,
                     snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The committed bot config: always dry-run, always the test seed."""
    sl = getattr(cfg.sleeves, sleeve)
    seed = float(cfg.modes.test.seed_usdt.get(sleeve, 0.0))
    port = 8080  # in-container port is always 8080; the host binding differs per bot
    whitelist = snapshot_whitelist(
        latest_snapshot() if snapshot is None else snapshot, cfg)
    conf: dict[str, Any] = {
        "strategy": sl.strategy,
        "dry_run": True,
        "dry_run_wallet": seed,
        "stake_currency": cfg.universe.quote,
        "stake_amount": "unlimited",
        "tradable_balance_ratio": 0.99,
        # NOT len(pairs): under a wide universe that silently becomes the whole whitelist.
        # Concurrency is a RISK limit and comes from the risk block (wide-universe.md §5.3).
        "max_open_trades": cfg.risk.max_open_positions,
        "timeframe": cfg.trading.timeframe,
        "exchange": {
            "name": cfg.exchange.name,
            "key": "",
            "secret": "",
            "pair_whitelist": whitelist,
            "ccxt_config": {},
            "ccxt_async_config": {},
        },
        # Always StaticPairList. Every dynamic pairlist in the installed freqtrade 2026.8
        # is SupportsBacktesting.NO, so a bot that selected its own universe could not be
        # backtested and the change-control protocol would go blind exactly where the new
        # risk lives. The resolver is ours; this whitelist is its snapshot.
        "pairlists": [{"method": "StaticPairList"}],
        "entry_pricing": {
            "price_side": "same",
            "use_order_book": True,
            "order_book_top": 1,
            "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
        },
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "unfilledtimeout": {
            "entry": cfg.execution.entry_unfilled_timeout_min,
            "exit": cfg.execution.exit_unfilled_timeout_min,
            "exit_timeout_count": cfg.execution.exit_timeout_count,
            "unit": "minutes",
        },
        # NO "telegram" key: see the module docstring — a disabled block is an omitted one.
        "api_server": {
            "enabled": True,
            "listen_ip_address": "0.0.0.0",
            "listen_port": port,
            "verbosity": "error",
            "enable_openapi": False,
            "username": "earn",
            "password": "",  # env-injected: FREQTRADE__API_SERVER__PASSWORD
            # no "jwt_secret_key": "" would fail the schema's minLength 32. The real value
            # arrives as FREQTRADE__API_SERVER__JWT_SECRET_KEY from ops/docker-compose.yml.
            "CORS_origins": [],
        },
        "db_url": "sqlite:////freqtrade/user_data/tradesv3.sqlite",
        "bot_name": f"earn-{sleeve}",
        "initial_state": "running",
        "internals": {"process_throttle_secs": 5, "heartbeat_interval": 60},
        "user_data_dir": "/freqtrade/user_data",
    }
    return audit_optional_blocks(conf, where=f"freqtrade-{sleeve}.json")


def build_riskgate_json(cfg: EarnConfig, config_path: Path = DEFAULT_CONFIG,
                        snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The gate's in-container config. ``phase`` is the committed baseline; the live value
    reaches the gate through ``var/runtime/runtime-<s>.json``.

    The universe snapshot is BAKED IN rather than mounted: the gate is stdlib-only inside
    a container that does not mount ``knowledge/universe/``, and a committed copy means
    the tiers the gate enforced are in git next to the limits it enforced them with. The
    weekly refresh (U1) therefore re-runs this generator; a snapshot that has not been
    rendered is a snapshot the bots are not trading.
    """
    snap = latest_snapshot() if snapshot is None else snapshot
    universe = cfg.universe.model_dump()
    universe["pairs"] = snapshot_whitelist(snap, cfg)
    if snap:
        universe["snapshot"] = gate_universe_block(snap, satellite_exclusions(snap, cfg))
    return {
        "generated_from": "config/earn.yaml",
        "source_sha256": _source_sha(config_path),
        # Which profile overlay produced this render (``""`` = the shipped configuration).
        # Stamped rather than inferred: ``source_sha256`` is the sha of earn.yaml, which a
        # profile does not change, so without this a rendered config could not say whether
        # a profile was in force. The gate ignores the key; it is provenance for the human,
        # the journal and the post-mortem.
        "profile": cfg.profiles.active or "",
        "phase": COMMITTED_PHASE,
        "risk": cfg.risk.model_dump(),
        "universe": universe,
        "proposal": cfg.proposal.model_dump(by_alias=True),
        "execution": cfg.execution.model_dump(),
        "sleeve_b": cfg.sleeve_b.model_dump(),
        "trading": {
            "timeframe": cfg.trading.timeframe,
            "startup_candles": startup_candles(cfg),
            "plan_bounds": cfg.trading.plan_bounds.model_dump(),
            "sleeves": {s: trading_for(cfg, s).model_dump(by_alias=True) for s in ("a", "b")},
        },
        "bounds": {k: v.model_dump() for k, v in cfg.bounds.items()},
        "container_paths": CONTAINER_PATHS,
    }


def build_params(cfg: EarnConfig, sleeve: Sleeve) -> dict[str, Any]:
    if sleeve == "a":
        params = cfg.sleeve_a.model_dump()
    else:
        params = cfg.sleeve_b.model_dump()
    params["rebalance_band"] = cfg.execution.rebalance_band
    return {"sleeve": sleeve, "params": params}


# --------------------------------------------------------------------------- var/runtime


def assert_profile_not_live(cfg: EarnConfig, state: ms.ModeState) -> None:
    """Refuse to render a runtime for REAL MONEY while a profile overlay is active.

    A profile changes cadence and entry/exit logic — deliberately, and without the years of
    walk-forward the shipped strategy carries. DEMO is exactly what one is for and is
    allowed; LIVE is not, and this is a render-time refusal rather than a note in a document
    so the file a live container would read cannot be produced at all.

    ``sl.is_live`` is real money only: demo places real orders with free money and is
    deliberately not in ``LIVE_MODES``.
    """
    name = cfg.profiles.active
    if not name:
        return
    live = [s for s in paths.SLEEVES if state.sleeve(s).is_live]
    if live:
        raise RenderError(
            f"profiles.active is {name!r} and sleeve(s) {', '.join(live)} are LIVE. A "
            f"profile is a test configuration: it may run in TEST and on DEMO, never "
            f"against real money. Set profiles.active to null and regenerate, or take the "
            f"sleeve out of live first."
        )


def venue_of(sl: ms.SleeveState) -> Venue | None:
    """The Binance venue this sleeve's mode may reach, or ``None`` for a dry-run sleeve.

    The binding table lives in ``ops.lib.exchange_endpoints`` and nowhere else, so adding a
    mode without deciding its venue is a hard error rather than a silent default.

    A *transient* mode (``ARMING``/``DISARMING``) is deliberately mapped to ``None`` here
    rather than propagated as the refusal ``venue_for_mode`` raises: mid-transition the
    sleeve is being taken down, and the right runtime for it is the credential-free
    dry-run one. ``ops.modes`` never renders from a transient state (it writes the final
    state at step 7 and renders at step 8); this is the belt-and-braces path used by
    ``recover()``, and rendering "no exchange at all" is the safe answer there.
    """
    if sl.state in ms.TRANSIENT_MODES:
        return None
    try:
        return venue_for_mode(sl.state)
    except VenueBindingError as e:
        raise RenderError(str(e)) from e


def _state_for(sleeve: str, state: ms.ModeState) -> ms.SleeveState:
    sl = state.sleeve(sleeve)
    if sl.is_live and not state.verified:  # unreachable via load(), belt and braces
        raise RenderError(
            f"sleeve {sleeve}: refusing to render a live runtime from an unverified mode "
            f"state ({state.reason})"
        )
    # Demo is not live money, so it is deliberately not in ``LIVE_MODES`` — but it does
    # place real orders against a real exchange with a real key, so it gets the same
    # "never from an unverified mode file" rule.
    if venue_of(sl) is not None and not state.verified:
        raise RenderError(
            f"sleeve {sleeve}: mode {sl.state} reaches an exchange, and the mode state is "
            f"unverified ({state.reason}); refusing to render it"
        )
    return sl


def build_mode_overlay(
    cfg: EarnConfig, sleeve: Sleeve, state: ms.ModeState
) -> dict[str, Any]:
    """``var/runtime/freqtrade-<s>.mode.json`` — the second ``--config`` freqtrade reads.

    Three shapes, chosen by the sleeve's mode and nothing else:

    ``TEST``
        ``dry_run: true``, a paper wallet, no exchange block. Talks to nobody.
    ``DEMO_*``
        ``dry_run: false`` (freqtrade refuses ``demo_trading`` together with ``dry_run`` —
        ``config_validation.py:417``), exchange-side stops, and
        ``exchange: {demo_trading: true, _ft_has_params: {supports_demo_trading: true}}``.
        The ``_ft_has_params`` half is not optional: freqtrade ships
        ``binance._ft_has["supports_demo_trading"] = False`` and
        ``validate_demo_trading`` raises ``ConfigurationError`` without the override.
    ``LIVE_*``
        ``dry_run: false``, exchange-side stops, and no exchange switch at all —
        production is ccxt's default and the only venue that needs no unlocking.

    No ``urls`` and no ``ccxt_config`` is written in any of the three. See the module
    docstring: freqtrade ignores ``exchange.urls`` entirely, and a ``ccxt_config.urls``
    override deep-merges, so it can only ever make the demo map *less* complete than the
    wholesale swap ``demo_trading`` triggers inside ccxt.
    """
    sl = _state_for(sleeve, state)
    venue = venue_of(sl)
    # A sleeve that reaches an exchange is never dry-run, whether the money is real (LIVE)
    # or free (DEMO) — so the switch here is the VENUE, not ``sl.is_live``. ``is_live``
    # keeps its narrow meaning (real money) for the paths that gate on that, and demo is
    # deliberately not in ``LIVE_MODES``.
    trades = venue is not None
    run_id = sl.run_id or default_run_id(sleeve)
    seed = seed_for(cfg, sleeve, state=state)
    label = venue.value if venue is not None else "test"
    overlay: dict[str, Any] = {
        "dry_run": not trades,
        "available_capital": seed,
        "db_url": paths.ft_run_db(run_id, sleeve),
        "bot_name": f"earn-{sleeve}-{label}",
    }
    if trades:
        overlay["order_types"] = {
            "entry": trading_for(cfg, sleeve).order_types.entry,
            "exit": trading_for(cfg, sleeve).order_types.exit,
            "emergency_exit": "market",
            "force_entry": "market",
            "force_exit": "market",
            "stoploss": "market",
            # Demo's orderTypes and filters are byte-identical to live (demo-mode.md §4a),
            # incl. STOP_LOSS_LIMIT and OCO, so the live rule applies unchanged.
            "stoploss_on_exchange": stoploss_on_exchange(cfg, sleeve, live=True),
        }
        if venue is not Venue.LIVE:
            # LIVE deliberately gets no exchange block: production is ccxt's default and
            # the only venue that needs no switch, and an overlay key that says
            # "demo_trading: false" is one careless edit away from saying the opposite.
            overlay["exchange"] = freqtrade_exchange_patch(venue)
    else:
        overlay["dry_run_wallet"] = seed
    where = f"freqtrade-{sleeve}.mode.json"
    if venue is not None:
        # Last line of defence before the file is written: no production host may appear
        # anywhere in a non-live overlay, at any depth, under any key.
        assert_no_production_host(overlay, venue=venue, where=where)
        _assert_venue_switch(overlay, venue=venue, where=where)
    return audit_optional_blocks(overlay, where=where)


def _assert_venue_switch(overlay: dict[str, Any], *, venue: Venue, where: str) -> None:
    """Refuse an overlay whose exchange block does not match the venue it claims.

    ``freqtrade_exchange_patch`` is the only producer of that block, so this is a
    self-check rather than input validation — but it is the assertion that would fire if a
    future edit dropped the ``_ft_has_params`` unlock and left ``demo_trading: true``
    behind, which freqtrade rejects at boot with ``Demo trading is not supported for
    Binance``: a crash loop instead of a refusal, in the one mode where a crash loop is
    indistinguishable from "the exchange is down".
    """
    exchange = overlay.get("exchange") or {}
    if "urls" in exchange:
        raise RenderError(
            f"{where}: exchange.urls is read by NOTHING in freqtrade 2026.8 (measured — "
            f"docs/design/demo-mode.md §3a). Writing it looks like routing and is not; "
            f"the bot would run against {endpoints_for(Venue.LIVE).rest_host}."
        )
    if venue is Venue.DEMO:
        if "ccxt_config" in exchange:
            raise RenderError(
                f"{where}: a demo overlay must carry no ccxt_config url map. ccxt "
                f"deep-merges it into the production map, leaving sapi/papi/fapi on "
                f"{endpoints_for(Venue.LIVE).rest_host}; exchange.demo_trading makes ccxt "
                f"ASSIGN the whole demo map instead."
            )
        unlocked = (exchange.get("_ft_has_params") or {}).get("supports_demo_trading")
        if not exchange.get("demo_trading") or not unlocked:
            raise RenderError(
                f"{where}: venue is demo but the overlay does not carry "
                f"exchange.demo_trading + _ft_has_params.supports_demo_trading. Without "
                f"BOTH, freqtrade either refuses to start (binance ships "
                f"supports_demo_trading: False) or starts pointed at "
                f"{endpoints_for(Venue.LIVE).rest_host}."
            )
        if overlay.get("dry_run"):
            raise RenderError(
                f"{where}: demo_trading and dry_run cannot be combined — freqtrade's "
                f"config_validation.py refuses it. A demo sleeve places real orders."
            )
    elif exchange.get("demo_trading"):
        raise RenderError(
            f"{where}: venue is {venue.value} but the overlay says demo_trading: true"
        )


def build_sleeve_runtime(
    cfg: EarnConfig,
    sleeve: Sleeve,
    state: ms.ModeState,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """``var/runtime/runtime-<s>.json`` — what the strategy reads through ``$EARN_RUNTIME``."""
    sl = _state_for(sleeve, state)
    live = sl.is_live
    submode = sl.submode or (cfg.modes.live.submode_default if live else None)
    require_approval = (
        sl.requires_approval
        or (not live and cfg.modes.test.simulate_approval)
    )
    return {
        "version": RUNTIME_VERSION,
        "sleeve": sleeve,
        "mode": "live" if live else "test",
        "state": sl.state,
        "submode": submode,
        "run_id": sl.run_id or default_run_id(sleeve),
        "seed_usdt": seed_for(cfg, sleeve, state=state),
        "require_approval": require_approval,
        "approval_dir": CONTAINER_APPROVALS,
        "generated_at": state.set_at,
        "config_sha": _source_sha(config_path),
    }


def build_compose_override(
    cfg: EarnConfig, state: ms.ModeState, *, env: dict[str, str] | None = None
) -> str:
    """``var/runtime/compose.override.yml`` — exchange credentials by env reference only.

    This is where a credential is bound to a venue, and it is the reason a demo key cannot
    reach a live container or the reverse: the env NAMES are chosen from the sleeve's mode
    (``BINANCE_KEY_A`` for live, ``BINANCE_DEMO_KEY`` for demo — deliberately disjoint), and
    :func:`_assert_credentials_bound` then re-reads each rendered service block and refuses
    it if any other venue's name appears in it. A TEST sleeve gets empty strings, so a
    dry-run container never sees a key at all.

    The check is per SERVICE, not per file, because docker injects only the variables listed
    under a service: sleeve A may be live while sleeve B is on demo, and neither container
    can see the other's credential.

    ``EARN_VENUE`` is stamped on every service so ``docker inspect`` and the console can
    both answer "which Binance is this container talking to?" without parsing a JSON file.

    ``env`` is the environment the *render* can see (the process env by default). It is
    never written out — it is read only to catch the one mislabelling that needs no
    network to detect: the same API key filed under two venues' names.
    """
    project = cfg.runtime.docker.compose_project
    #: sleeve -> (mode name, venue it may reach). Fail closed exactly as
    #: build_mode_overlay does: an unverified mode file means TEST for every sleeve, and
    #: TEST means no credentials, whatever the file claims.
    venues: dict[str, tuple[str, Venue | None]] = {}
    for sleeve in paths.SLEEVES:
        sl = state.sleeve(sleeve)
        mode = sl.state if state.verified else "TEST"
        venues[sleeve] = (mode, venue_of(sl) if state.verified else None)

    lines = [
        "# GENERATED by ops.gen_freqtrade_config — do not edit.",
        "# Exchange credentials are env REFERENCES; docker compose interpolates them from",
        "# the repo .env. No key or secret is ever written into a file here.",
        "#",
        "# The venue is bound to the MODE, in code (ops.lib.exchange_endpoints.MODE_VENUE).",
        "# Each venue has its own credential names, and a service only ever sees its own:",
        "#   live -> BINANCE_KEY_<S> / BINANCE_SECRET_<S>   demo -> BINANCE_DEMO_<KEY|SECRET>",
        "# No exchange URL is set here, in any form. freqtrade 2026.8 reads exchange.urls",
        "# from nowhere, so such a line looks like routing and is a silent no-op; demo",
        "# routing comes from exchange.demo_trading in freqtrade-<s>.mode.json.",
        f"name: {project}",
        "services:",
    ]
    blocks: dict[str, list[str]] = {}
    for sleeve in paths.SLEEVES:
        service = cfg.ops.bots[sleeve].service  # type: ignore[index]
        mode, venue = venues[sleeve]
        block = [
            f"  {service}:",
            "    environment:",
            f'      EARN_VENUE: "{venue.value if venue else "none"}"',
        ]
        if venue is None:
            block.append(f"      # {sleeve}: {mode} — reaches no exchange")
            block.append('      FREQTRADE__EXCHANGE__KEY: ""')
            block.append('      FREQTRADE__EXCHANGE__SECRET: ""')
        else:
            ep = endpoints_for(venue)
            key_env, secret_env = credential_env_names(venue, sleeve)
            block.append(
                f"      # {sleeve}: {mode} -> {venue.value} ({ep.rest_host}); "
                f"{len(blackholed_keys(venue))} url tiers blackholed"
            )
            block.append(f"      FREQTRADE__EXCHANGE__KEY: ${{{key_env}}}")
            block.append(f"      FREQTRADE__EXCHANGE__SECRET: ${{{secret_env}}}")
        blocks[sleeve] = block
        lines.extend(block)
    text = "\n".join(lines) + "\n"
    _assert_credentials_bound(
        blocks, venues, env=env, where="compose.override.yml"
    )
    return text


def _assert_credentials_bound(
    blocks: dict[str, list[str]],
    venues: dict[str, tuple[str, Venue | None]],
    *,
    env: dict[str, str] | None,
    where: str,
) -> None:
    """Refuse a rendered compose layer that pairs a sleeve with the wrong venue's key.

    Four refusals, all at render time, so the bad file is never written:

    1. any exchange URL or ``FREQTRADE__EXCHANGE__URLS__*`` line in the rendered layer —
       the silent no-op that left the old testnet rehearsal pointed at production;
    2. another venue's credential name appearing in that service's block — a demo container
       that can *see* ``BINANCE_KEY_A`` is one ``.env`` typo away from trading real money;
    3. the same API key filed under two venues' names in ``.env``
       (:func:`cross_labelled_credentials`) — the one mislabelling detectable with no
       network, and the exact shape of "the owner pasted the live key into the demo slot";
    4. a mode whose venue, re-derived through :func:`resolve_binding` from the credential
       actually present, is not the venue this layer was rendered for. Only
       ``resolve_binding`` constructs a ``Binding``, so holding one is the proof that mode
       and venue agree; this is the assertion that would fire if the two tables ever drift
       apart. Absence of a key is not a refusal — an unset key is preflight's business.
    """
    bound = [v for _, v in venues.values() if v is not None]
    try:
        assert_no_exchange_url_override(
            "\n".join(line for b in blocks.values() for line in b), where=where
        )
    except VenueBindingError as e:
        raise RenderError(str(e)) from e
    for sleeve, (mode, venue) in venues.items():
        if venue is None:
            continue
        block = "\n".join(blocks[sleeve])
        for foreign in foreign_credential_env_names(venue):
            if foreign in block:
                raise RenderError(
                    f"{where}: sleeve {sleeve} is in {mode}, which may only reach "
                    f"{venue.value} ({endpoints_for(venue).rest_host}), but its service "
                    f"block references {foreign} — a credential for another venue. A "
                    f"container must never be able to see a key it is not allowed to use. "
                    f"Refusing to write it."
                )
    if not bound:
        return
    crossed = cross_labelled_credentials(env)
    if crossed:
        raise RenderError(
            f"{where}: the same exchange key is filed under two venues' names in .env — "
            + "; ".join(crossed)
            + ". A venue label is a promise about which Binance a key reaches; two labels "
            "on one key means one of them is a lie. Mint a separate key per venue "
            "(demo: Binance account UI -> Demo Trading -> API Key Management) and refile "
            "them. Refusing to render until they are distinct."
        )
    for sleeve, (mode, venue) in venues.items():
        if venue is None:
            continue
        cred = credential_from_env(venue, sleeve, env)
        if not cred.present:
            continue  # an unset key is preflight's refusal, not the renderer's
        try:
            binding = resolve_binding(mode, cred)
        except VenueBindingError as e:
            raise RenderError(f"{where}: {e}") from e
        if binding.venue is not venue:
            raise RenderError(
                f"{where}: sleeve {sleeve} rendered for {venue.value} but mode {mode} "
                f"binds to {binding.venue.value if binding.venue else 'no venue'}"
            )


def render_runtime(
    cfg: EarnConfig,
    *,
    state: ms.ModeState | None = None,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[Path, str]:
    """The ``var/runtime/`` files as {path: content}, without writing them."""
    state = state if state is not None else ms.load()
    assert_profile_not_live(cfg, state)
    target = runtime_dir or paths.runtime_dir()
    out: dict[Path, str] = {}
    for sleeve in paths.SLEEVES:
        out[target / f"freqtrade-{sleeve}.mode.json"] = _dump(
            build_mode_overlay(cfg, sleeve, state)  # type: ignore[arg-type]
        )
        out[target / f"runtime-{sleeve}.json"] = _dump(
            build_sleeve_runtime(cfg, sleeve, state, config_path)  # type: ignore[arg-type]
        )
    out[target / "compose.override.yml"] = build_compose_override(cfg, state, env=env)
    return out


def write_runtime(
    cfg: EarnConfig,
    *,
    state: ms.ModeState | None = None,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> list[Path]:
    """Render and write ``var/runtime/`` (0700 directory, 0600 files)."""
    if runtime_dir is None:
        paths.ensure_var_layout()
    else:
        paths.ensure_dir(runtime_dir)
    written: list[Path] = []
    for path, content in render_runtime(
        cfg, state=state, config_path=config_path, runtime_dir=runtime_dir, env=env
    ).items():
        paths.write_private(path, content)
        written.append(path)
    return written


# --------------------------------------------------------------------------- CLI


def _dump(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2, sort_keys=True) + "\n"


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check = "--check" in argv
    skip_runtime = "--no-runtime" in argv or check
    cfg = load_config()
    profile = cfg.profiles.active
    print(f"profile: {profile or 'none (shipped configuration)'}"
          + (f"  [{cfg.trading.timeframe}, sleeves "
             f"{cfg.sleeves.a.strategy}/{cfg.sleeves.b.strategy}]" if profile else ""))

    regenerated = {
        CONFIG_DIR / "freqtrade-a.json": _dump(build_bot_config(cfg, "a")),
        CONFIG_DIR / "freqtrade-b.json": _dump(build_bot_config(cfg, "b")),
        CONFIG_DIR / "riskgate.json": _dump(build_riskgate_json(cfg)),
    }
    seeded_once = {
        CONFIG_DIR / "params-sleeve-a.json": _dump(build_params(cfg, "a")),
        CONFIG_DIR / "params-sleeve-b.json": _dump(build_params(cfg, "b")),
    }

    drift: list[str] = []
    for path, content in regenerated.items():
        if check:
            if not path.exists() or path.read_text() != content:
                drift.append(_rel(path))
        else:
            path.write_text(content)
            print(f"wrote {_rel(path)}")
    for path, content in seeded_once.items():
        if not path.exists():
            if check:
                drift.append(f"{_rel(path)} (missing)")
            else:
                path.write_text(content)
                print(f"seeded {_rel(path)}")

    if not skip_runtime:
        state = ms.load()
        try:
            for path in write_runtime(cfg, state=state):
                print(f"wrote {_rel(path)}")
        except RenderError as e:
            print(f"runtime render refused: {e}", file=sys.stderr)
            return 2
        print(f"mode state: {ms.describe(state)}")

    if check:
        if drift:
            print("DRIFT — regenerate with `python -m ops.gen_freqtrade_config`:", file=sys.stderr)
            for d in drift:
                print(f"  {d}", file=sys.stderr)
            return 1
        print("generated configs match earn.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
