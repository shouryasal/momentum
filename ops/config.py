"""EarnConfig: the typed loader for config/earn.yaml (``config_version: 2``).

Every host-side job loads configuration through :func:`load_config` and nothing else.
``extra="forbid"`` everywhere: an unknown key in earn.yaml is a hard error, so a typo can
never silently disable a limit.

Two things make this file bigger than a plain schema:

* **Every leaf is annotated for the console.** Each field carries ``description`` plus the
  ``x-*`` keys the UI's ``SchemaForm`` consumes verbatim — ``x-tier`` (who may change it),
  ``x-group`` (where it sits in the page), and where they apply ``x-unit``, ``x-widget``,
  ``x-effects`` (what a save has to restart or regenerate), ``x-protected`` (step-up plus a
  typed confirmation) and ``x-help-md``. A foundation test walks the generated JSON Schema
  and fails if any property lacks ``description``, ``x-tier`` or ``x-group``, so a new key
  appears in the UI with no frontend change — or the build goes red. A second test walks
  ``config/earn.yaml`` itself and demands that *every leaf in the file* resolves to such a
  node, with no exceptions list: a subtree typed loosely enough to have no schema below it
  (``trading.sleeves`` once was ``dict[str, dict[str, Any]]``) is invisible to the form, to
  Ctrl-K and to the protected-path check, and that is exactly what the walk catches.
* **Mode is not config.** ``phase`` is no longer stored: :attr:`EarnConfig.phase` is
  computed from the signed ``var/state/mode.json`` (``ops.lib.mode_state``), which fails
  closed to ``paper``. A legacy ``phase:`` key in a file is accepted, warned about and
  ignored. Likewise ``sleeves.*.capital_usdt`` moved to ``modes.test.seed_usdt`` and the
  live seed inside the mode file; :func:`seed_for` is the single reader.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ops.lib.paths import REPO_ROOT

DEFAULT_CONFIG = REPO_ROOT / "config" / "earn.yaml"

CONFIG_VERSION = 2

Tier = Literal["human", "tier1", "generated", "invariant"]
Effect = Literal[
    "regen",
    "restart:freqtrade-a",
    "restart:freqtrade-b",
    "crontab",
    "restart:telegram",
    "restart:console",
    "reset_required",
]

SLOT_RE = re.compile(r"^\d{2}:\d{2}$")


class ConfigError(Exception):
    pass


class ConfigWarning(UserWarning):
    """Raised as a warning for accepted-but-deprecated keys (``phase``, ``capital_usdt``)."""


#: The effects of touching anything the two bots' generated configs are rendered from.
#: Declared once and hung on the *section* fields (``risk``, ``trading``, ``universe``,
#: ``exchange``) rather than on every leaf under them: a consumer resolves a leaf's
#: effects by walking up to the nearest annotated ancestor, so ``risk.usdt_floor`` means
#: "regenerate and restart" without ``risk`` needing 40 identical annotations. A leaf that
#: declares its own ``x-effects`` still wins. This schema is the ONLY owner of the mapping
#: — ``ops.config_store`` used to carry a parallel ``SECTION_EFFECTS`` table that the
#: console's own ``schema_meta`` never saw, so the two disagreed about what a save implies.
REGEN_AND_RESTART: tuple[str, ...] = ("regen", "restart:freqtrade-a", "restart:freqtrade-b")


def F(
    default: Any = ...,
    *,
    desc: str,
    group: str,
    tier: Tier = "human",
    unit: str | None = None,
    widget: str | None = None,
    effects: Sequence[str] | None = None,
    protected: bool = False,
    help_md: str | None = None,
    deprecated: bool = False,
    **kwargs: Any,
) -> Any:
    """A ``Field`` carrying the console's ``x-*`` annotations.

    ``desc``/``group``/``tier`` are mandatory by construction, which is what makes the
    schema-walk test satisfiable for every key at once.
    """
    extra: dict[str, Any] = {"x-tier": tier, "x-group": group}
    if unit:
        extra["x-unit"] = unit
    if widget:
        extra["x-widget"] = widget
    if effects:
        extra["x-effects"] = list(effects)
    if protected:
        extra["x-protected"] = True
    if help_md:
        extra["x-help-md"] = help_md
    if deprecated:
        extra["x-deprecated"] = True
    return Field(default, description=desc, json_schema_extra=extra, **kwargs)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- meta / host


class Meta(_Model):
    config_version: int = F(
        1, desc="Schema version of this file; 2 is the console-era schema.", group="meta",
        tier="generated",
    )
    display_timezone: str = F(
        "Asia/Dubai", desc="Timezone the UI and the schedules are quoted in.", group="meta",
        widget="path", effects=["restart:console"],
    )


class DockerCfg(_Model):
    runtime: Literal["engine", "desktop"] = F(
        "engine", desc="Docker flavour on this host: native engine or Docker Desktop.",
        group="docker",
    )
    compose_project: str = F(
        "earn", desc="Compose project name; must match `name:` in ops/docker-compose.yml.",
        group="docker", effects=["restart:freqtrade-a", "restart:freqtrade-b"],
    )


class Runtime(_Model):
    data_root: str | None = F(
        None,
        desc="Absolute path of the live data root; null means the repo root. Must be ext4 "
             "(a 9p/drvfs path corrupts SQLite WAL and git worktrees).",
        group="host", widget="path", protected=True,
        help_md="Set by `ops/setup.sh`. Overridden at runtime by `$EARN_STATE_ROOT`.",
    )
    docker: DockerCfg = F(
        default_factory=DockerCfg, desc="Docker runtime facts the bot controls depend on.",
        group="host",
    )
    require_host_awake_for_live: bool = F(
        True,
        desc="Block going live unless Windows cannot sleep on AC (powercfg standby-timeout-ac 0).",
        group="host", protected=True,
    )
    require_keepalive_task_for_live: bool = F(
        True, desc="Block going live unless the WSL keep-alive scheduled task exists.",
        group="host", protected=True,
    )


class ConsolePoll(_Model):
    db: int = F(2000, desc="How often the console polls the databases, in milliseconds.",
                group="console", widget="duration", ge=250)
    bots: int = F(10000,
                  desc="How often the console polls the Freqtrade REST APIs, in milliseconds.",
                  group="console", widget="duration", ge=1000)


class Console(_Model):
    port: int = F(
        8765, desc="Console listen port. The bind address is the code constant 127.0.0.1.",
        group="console", protected=True, effects=["restart:console"], ge=1024, le=65535,
    )
    session_hours: int = F(
        12, desc="Lifetime of a console session cookie.", group="console", unit="hours",
        protected=True, ge=1, le=168,
    )
    stepup_minutes: int = F(
        10, desc="How long a step-up re-authentication stays valid for dangerous actions.",
        group="console", unit="minutes", protected=True, ge=1, le=120,
    )
    git_commit_on_save: bool = F(
        True, desc="Commit each config save to the live branch with the audit reason.",
        group="console",
    )
    effects_default: Literal["apply_now", "save_only"] = F(
        "apply_now", desc="Default action for a save that implies regen/restart effects.",
        group="console",
    )
    poll_ms: ConsolePoll = F(
        default_factory=ConsolePoll, desc="Console polling intervals in milliseconds.",
        group="console",
    )


class GitCfg(_Model):
    live_branch: str = F(
        "claude/code-building-plan-qxzqsg",
        desc="The one branch the live checkout tracks and apply_changes merges into.",
        group="git", protected=True,
    )
    worktree_root: str = F(
        "../earn-worktrees",
        desc="Where review/daily sessions get their isolated git worktrees.",
        group="git", widget="path", protected=True,
    )
    worktree_ttl_days: int = F(
        14, desc="Worktrees older than this are pruned by maintenance.", group="git",
        unit="days", ge=1,
    )


class Security(_Model):
    agent_user: str | None = F(
        None, desc="Unix user the Claude CLI runs as; null until ops/setup_agent_user.sh ran.",
        group="security", protected=True,
    )
    agent_cli_wrapper: str | None = F(
        None,
        desc="Wrapper script that runs the CLI as the agent user. Required for LIVE_EXECUTE.",
        group="security", widget="path", protected=True,
    )
    console_bind_note: str = F(
        "127.0.0.1 only (invariant)",
        desc="Reminder that the console bind address is a code constant, not a setting.",
        group="security", tier="invariant",
    )


# --------------------------------------------------------------------------- autonomy


class AutonomyKind(_Model):
    test: Literal["auto", "approve", "off"] = F(
        "approve", desc="What happens to this change kind while the sleeve is in TEST.",
        group="autonomy",
    )
    live: Literal["auto", "approve", "off"] = F(
        "approve", desc="What happens to this change kind while any sleeve is live.",
        group="autonomy", protected=True,
    )


class AutoRevert(_Model):
    enabled: bool = F(True, desc="Let the daily review request a revert of a recent change.",
                      group="autonomy")
    window_days: int = F(7, desc="How far back an auto-revert may reach.", group="autonomy",
                         unit="days", ge=1)
    validity_drop_pct: int = F(
        10, desc="Drop in proposal validity (percentage points) that justifies a revert.",
        group="autonomy", unit="pct", ge=1,
    )
    breach_increase: int = F(
        1, desc="Extra gate breaches versus the pre-change baseline that justify a revert.",
        group="autonomy", ge=1,
    )


class Autonomy(_Model):
    tier1_auto_merge: bool = F(
        True, desc="Master switch: false holds every tier-1 change for a human.",
        group="autonomy", protected=True,
    )
    kinds: dict[str, AutonomyKind] = F(
        default_factory=dict,
        desc="Per change kind (params, prompt, skill_edit, skill_new, skill_bind, model, "
             "revert): auto, approve or off, separately for test and live.",
        group="autonomy", protected=True,
    )
    live_forces_human: bool = F(
        True, desc="While any sleeve is live, every kind falls back to its live setting.",
        group="autonomy", protected=True,
    )
    max_auto_merges_per_week: int = F(
        5, desc="Ceiling on automatic tier-1 merges per calendar week.", group="autonomy",
        protected=True, ge=0,
    )
    auto_revert: AutoRevert = F(
        default_factory=AutoRevert, desc="Automatic revert of a change that made things worse.",
        group="autonomy",
    )


# --------------------------------------------------------------------------- universe


class Universe(_Model):
    quote: str = F("USDT", desc="Quote currency for every tradeable pair.", group="universe",
                   protected=True)
    assets: list[str] = F(..., desc="Base assets Earn may hold. Locked decision: BTC and ETH.",
                          group="universe", protected=True, widget="pair")
    pairs: list[str] = F(..., desc="Tradeable pairs; must equal <asset>/<quote> in order.",
                         group="universe", protected=True, widget="pair")
    data_only_symbols: list[str] = F(
        default_factory=list,
        desc="Symbols ingested for data only (fee conversion); never tradeable.",
        group="universe", protected=True, widget="pair",
    )


class Exchange(_Model):
    name: str = F("binance", desc="CCXT exchange id. Spot only, UAE entity.", group="exchange",
                  protected=True, effects=["regen"])
    fee_bps_assumed: float = F(
        10, desc="Assumed taker fee; TCA measures the real one rather than trusting this.",
        group="exchange", unit="bps", protected=True, ge=0,
    )


class Sleeve(_Model):
    label: str = F(..., desc="Short human label for this sleeve.", group="sleeves")
    strategy: str = F(
        ...,
        desc="Freqtrade strategy class; must subclass EarnBaseStrategy. Scaffold is refused "
             "for a live sleeve.",
        group="sleeves", protected=True, effects=["regen", "restart:freqtrade-a",
                                                  "restart:freqtrade-b"],
    )
    capital_usdt: float | None = F(
        None,
        desc="DEPRECATED alias for the sleeve's seed. Filled from modes.test.seed_usdt on "
             "load; set the seed there (test) or through a mode transition (live).",
        group="legacy", tier="generated", unit="usdt", deprecated=True,
    )


class BenchmarkSleeve(_Model):
    label: str = F(..., desc="Label for the buy-and-hold benchmark sleeve.", group="sleeves")
    pair: str = F(..., desc="The pair the benchmark holds.", group="sleeves", widget="pair",
                  protected=True)


class Sleeves(_Model):
    a: Sleeve = F(..., desc="Sleeve A — deterministic rules.", group="sleeves")
    b: Sleeve = F(..., desc="Sleeve B — Claude's proposals inside the gate.", group="sleeves")
    benchmark: BenchmarkSleeve = F(..., desc="The hold-BTC benchmark Earn is measured against.",
                                   group="sleeves")


# --------------------------------------------------------------------------- modes


class TestMode(_Model):
    seed_usdt: dict[str, float] = F(
        default_factory=lambda: {"a": 10000.0, "b": 10000.0},
        desc="Starting balance per sleeve for the NEXT test run.", group="modes", unit="usdt",
        effects=["reset_required"],
    )
    label: str = F("baseline", desc="Label stamped on new test runs.", group="modes")
    simulate_approval: bool = F(
        False, desc="Rehearse propose-mode approvals in TEST without real orders.",
        group="modes",
    )
    benchmark_from_run_start: bool = F(
        True, desc="Anchor the BTC benchmark at the run start rather than at the epoch.",
        group="modes",
    )


class LiveMode(_Model):
    max_seed_usdt: dict[str, float] = F(
        default_factory=lambda: {"a": 1000.0, "b": 1000.0},
        desc="Hard ceiling on the live seed per sleeve; preflight blocks above it.",
        group="modes", unit="usdt", protected=True,
    )
    min_test_days: int = F(
        90, desc="Days of clean TEST running required before a sleeve may go live.",
        group="modes", unit="days", protected=True, ge=0,
    )
    min_propose_days: int = F(
        30, desc="Days in LIVE_PROPOSE required before LIVE_EXECUTE.", group="modes",
        unit="days", protected=True, ge=0,
    )
    require_zero_breach_days: int = F(
        30, desc="Consecutive days without a gate breach required before going live.",
        group="modes", unit="days", protected=True, ge=0,
    )
    submode_default: Literal["propose", "execute"] = F(
        "propose", desc="Sub-mode a sleeve enters when it first goes live.", group="modes",
        protected=True,
    )
    approval_ttl_hours: int = F(
        6, desc="How long a proposal approval stays valid in propose mode.", group="modes",
        unit="hours", protected=True, ge=1,
    )
    flatten_on_exit: bool = F(
        True, desc="Flatten positions when leaving live unless a human types the override.",
        group="modes", protected=True,
    )
    require_agent_user_for_execute: bool = F(
        True, desc="LIVE_EXECUTE requires security.agent_user and the CLI wrapper.",
        group="modes", protected=True,
    )
    require_stoploss_on_exchange: bool = F(
        True, desc="Live sleeves must place stops on the exchange, not only in the bot.",
        group="modes", protected=True,
    )
    one_live_sleeve_per_account: bool = F(
        True, desc="Only one sleeve may be live on a given exchange account UID.",
        group="modes", protected=True,
    )
    confirm_phrase: str = F(
        "GO LIVE {sleeve} {seed} USDT",
        desc="Phrase a human must type to arm a sleeve; {sleeve} and {seed} are filled in.",
        group="modes", protected=True,
    )


class Modes(_Model):
    test: TestMode = F(default_factory=TestMode, desc="TEST-mode defaults.", group="modes")
    live: LiveMode = F(default_factory=LiveMode, desc="LIVE-mode guards and ceilings.",
                       group="modes", protected=True, effects=["regen"])


# --------------------------------------------------------------------------- risk


class StoplossGuard(_Model):
    count: int = F(..., desc="Stop-loss exits within the window that trip the guard.",
                   group="risk.guards", protected=True, ge=1)
    window_hours: int = F(..., desc="Rolling window for the stop-loss guard.",
                          group="risk.guards", unit="hours", protected=True, ge=1)
    lock_hours: int = F(..., desc="How long entries stay locked once the guard trips.",
                        group="risk.guards", unit="hours", protected=True, ge=1)


class Blackout(_Model):
    macro_events: list[str] = F(..., desc="Macro events that open a no-entry window.",
                                group="risk.guards", protected=True)
    window_minutes: int = F(..., desc="No-entry window around each macro event.",
                            group="risk.guards", unit="minutes", protected=True, ge=0)


class ProtectionDrawdown(_Model):
    lookback_candles: int = F(
        6, desc="Candles Freqtrade's MaxDrawdown protection looks back over.",
        group="risk.protections", protected=True, effects=["restart:freqtrade-a",
                                                           "restart:freqtrade-b"], ge=1,
    )
    trade_limit: int = F(
        2, desc="Minimum trades in the window before the protection can trigger.",
        group="risk.protections", protected=True, ge=1,
    )


class Protections(_Model):
    max_drawdown: ProtectionDrawdown = F(
        default_factory=ProtectionDrawdown,
        desc="Freqtrade MaxDrawdown protection parameters.", group="risk.protections",
    )


class Reconcile(_Model):
    tolerance_pct: float = F(
        0.005, desc="Ledger-vs-exchange difference tolerated before a mismatch is raised.",
        group="risk.reconcile", unit="fraction", protected=True, ge=0, le=0.1,
    )
    dust_usdt: float = F(
        10.0, desc="Balance differences below this are treated as dust.",
        group="risk.reconcile", unit="usdt", protected=True, ge=0,
    )
    block_on_mismatch: bool = F(
        True, desc="A reconciliation mismatch sets a block_entries flag.",
        group="risk.reconcile", protected=True,
    )


class Risk(_Model):
    max_weight: dict[str, float] = F(
        ..., desc="Per-asset weight cap as a fraction of sleeve NAV; 'default' is the fallback.",
        group="risk.exposure", unit="fraction", protected=True,
    )
    max_gross_exposure: float = F(
        ..., desc="Maximum total non-USDT exposure as a fraction of NAV.",
        group="risk.exposure", unit="fraction", protected=True, gt=0, le=1,
    )
    usdt_floor: float = F(
        ..., desc="Minimum USDT held as a fraction of NAV.", group="risk.exposure",
        unit="fraction", protected=True, ge=0, lt=1,
    )
    daily_loss_stop: float = F(
        ..., desc="Daily loss that flattens the sleeve and locks entries.", group="risk.stops",
        unit="fraction", protected=True, gt=0, lt=1,
    )
    daily_stop_lock_hours: int = F(
        ..., desc="How long entries stay locked after the daily stop.", group="risk.stops",
        unit="hours", protected=True, ge=1,
    )
    monthly_loss_stop: float = F(
        ..., desc="Monthly loss that pauses the sleeve; resume is human-only.",
        group="risk.stops", unit="fraction", protected=True, gt=0, lt=1,
    )
    max_trades_per_day: int = F(
        ..., desc="Discretionary trades per sleeve per Gulf day.", group="risk.throughput",
        protected=True, ge=1,
    )
    stoploss_guard: StoplossGuard = F(..., desc="Repeated-stop circuit breaker.",
                                      group="risk.guards", protected=True)
    cooldown_candles: int = F(
        ..., desc="Candles to wait after an exit before re-entering the same pair.",
        group="risk.throughput", protected=True, ge=0,
    )
    staleness_minutes: int = F(
        ..., desc="No candle or book snapshot within this window blocks entries.",
        group="risk.data", unit="minutes", protected=True, ge=1,
    )
    min_notional_usdt: float = F(
        ..., desc="Smallest order notional the gate will allow.", group="risk.throughput",
        unit="usdt", protected=True, gt=0,
    )
    stoploss_per_trade: float = F(
        ..., desc="Loosest per-trade stop allowed; trading.*.stoploss.fixed_pct must be <= this.",
        group="risk.stops", unit="fraction", protected=True, gt=0, lt=1,
    )
    blackout: Blackout = F(..., desc="Macro-event blackout windows.", group="risk.guards",
                           protected=True)
    kill_file: str = F(
        ..., desc="Path whose existence engages the kill switch.", group="risk.guards",
        widget="path", tier="invariant",
    )
    max_entries_per_trade: int = F(
        4, desc="Initial entry plus DCA and pyramid adds allowed on one trade.",
        group="risk.mechanics", protected=True, ge=1,
    )
    max_order_notional_pct: float = F(
        0.20, desc="Largest single order as a fraction of ledger NAV.", group="risk.mechanics",
        unit="fraction", protected=True, gt=0, le=1,
    )
    max_orders_per_day: int = F(
        12, desc="Discretionary orders per sleeve per Gulf day; risk exits are exempt.",
        group="risk.throughput", protected=True, ge=1,
    )
    max_turnover_pct_per_day: float = F(
        0.50, desc="Traded notional divided by NAV per Gulf day.", group="risk.throughput",
        unit="fraction", protected=True, gt=0,
    )
    max_fee_pct_per_month: float = F(
        0.01, desc="Fee budget per month as a fraction of NAV.", group="risk.throughput",
        unit="fraction", protected=True, gt=0,
    )
    market_entries_allowed: bool = F(
        False, desc="Allow market ENTRY orders. Off by default; exits may always be market.",
        group="risk.mechanics", protected=True,
    )
    protections: Protections = F(
        default_factory=Protections, desc="Freqtrade protection settings rendered into the bots.",
        group="risk.protections", protected=True,
    )
    reconcile: Reconcile = F(
        default_factory=Reconcile, desc="Ledger-vs-exchange reconciliation policy.",
        group="risk.reconcile", protected=True,
    )


# --------------------------------------------------------------------------- trading mechanics


class OrderTypes(_Model):
    entry: Literal["limit", "market"] = F(
        "limit", desc="Order type for entries. Market entries need risk.market_entries_allowed.",
        group="trading.orders", protected=True, effects=["regen", "restart:freqtrade-a",
                                                         "restart:freqtrade-b"],
    )
    exit: Literal["limit", "market"] = F(
        "limit", desc="Order type for discretionary exits. Risk exits are always market.",
        group="trading.orders", protected=True, effects=["regen", "restart:freqtrade-a",
                                                         "restart:freqtrade-b"],
    )


class PriceSide(_Model):
    side: Literal["bid", "ask", "same", "other"] = F(
        "bid", desc="Book side the limit price is taken from.", group="trading.orders",
        protected=True,
    )
    offset_bps: float = F(
        0, desc="Signed offset applied to the chosen side, in basis points.",
        group="trading.orders", unit="bps", protected=True,
    )


class Trailing(_Model):
    enabled: bool = F(False, desc="Enable the trailing stop.", group="trading.stops",
                      protected=True)
    activate_profit_pct: float = F(
        0.04, desc="Profit at which trailing starts.", group="trading.stops", unit="fraction",
        protected=True, gt=0,
    )
    distance_pct: float = F(
        0.03, desc="Trailing distance below the high-water mark; at least 0.005.",
        group="trading.stops", unit="fraction", protected=True, gt=0,
    )
    only_offset_reached: bool = F(
        True, desc="Only trail once the activation profit was reached.", group="trading.stops",
        protected=True,
    )


class AtrStop(_Model):
    enabled: bool = F(False, desc="Enable the ATR-based stop.", group="trading.stops",
                      protected=True)
    period: int = F(14, desc="ATR period in candles.", group="trading.stops", protected=True,
                    ge=2)
    mult: float = F(3.0, desc="ATR multiple used as the stop distance.", group="trading.stops",
                    protected=True, gt=0)
    timeframe: str = F("4h", desc="Timeframe the ATR is computed on.", group="trading.stops",
                       protected=True)


class StoplossCfg(_Model):
    fixed_pct: float = F(
        0.10, desc="Fixed stop distance; must be <= risk.stoploss_per_trade.",
        group="trading.stops", unit="fraction", protected=True, gt=0, lt=1,
    )
    on_exchange: Literal["auto"] | bool = F(
        "auto", desc="Place the stop on the exchange. 'auto' means true in LIVE, false in TEST.",
        group="trading.stops", protected=True,
    )
    trailing: Trailing = F(default_factory=Trailing, desc="Trailing stop settings.",
                           group="trading.stops", protected=True)
    atr: AtrStop = F(default_factory=AtrStop, desc="ATR stop settings.", group="trading.stops",
                     protected=True)
    reentry_cooldown_hours: int = F(
        24, desc="After a stop or take-profit exit, no re-entry unless a newer proposal exists.",
        group="trading.stops", unit="hours", protected=True, ge=0,
    )


class LadderStep(_Model):
    at_profit_pct: float = F(..., desc="Profit at which this rung sells.",
                             group="trading.take_profit", unit="fraction", gt=0)
    sell_fraction: float = F(..., desc="Fraction of the position sold at this rung.",
                             group="trading.take_profit", unit="fraction", gt=0, le=1)


class TakeProfit(_Model):
    roi_table: dict[str, float] = F(
        default_factory=lambda: {"0": 10.0},
        desc="Freqtrade minimal_roi: minutes-in-trade to profit. 10.0 is effectively off.",
        group="trading.take_profit", protected=True,
    )
    ladder: list[LadderStep] = F(
        default_factory=list, desc="Partial take-profit rungs, applied in order.",
        group="trading.take_profit", protected=True,
    )


class Dca(_Model):
    """Averaging down into an existing position."""

    enabled: bool = F(False, desc="Enable averaging down.", group="trading.adds", protected=True)
    max_adds: int = F(2, desc="Maximum averaging-down adds per trade.", group="trading.adds",
                      protected=True, ge=0)
    step_pct: float = F(0.05, desc="Adverse move between adds.", group="trading.adds",
                        unit="fraction", protected=True, gt=0)
    size_multiplier: float = F(1.0, desc="Size of each add relative to the initial entry.",
                               group="trading.adds", protected=True, gt=0)
    cooldown_hours: int = F(24, desc="Minimum hours between adds.", group="trading.adds",
                            unit="hours", protected=True, ge=0)
    only_if_regime_up: bool = F(True, desc="Only average down while the regime is up.",
                                group="trading.adds", protected=True)


class Pyramid(_Model):
    """Adding into a winning position."""

    enabled: bool = F(False, desc="Enable pyramiding.", group="trading.adds", protected=True)
    max_adds: int = F(1, desc="Maximum pyramid adds per trade.", group="trading.adds",
                      protected=True, ge=0)
    trigger_profit_pct: float = F(0.05, desc="Profit required before each pyramid add.",
                                  group="trading.adds", unit="fraction", protected=True, gt=0)
    size_multiplier: float = F(0.5, desc="Size of each add relative to the initial entry.",
                               group="trading.adds", protected=True, gt=0)
    cooldown_hours: int = F(24, desc="Minimum hours between pyramid adds.", group="trading.adds",
                            unit="hours", protected=True, ge=0)


class ScheduledDca(_Model):
    enabled: bool = F(True, desc="Sleeve A's calendar DCA.", group="trading.adds", protected=True)
    interval_days: int = F(7, desc="Days between scheduled DCA chunks.", group="trading.adds",
                           unit="days", protected=True, ge=1)
    chunk_pct_nav: float = F(0.05, desc="Size of each scheduled chunk as a fraction of NAV.",
                             group="trading.adds", unit="fraction", protected=True, gt=0)


class RebalanceCfg(_Model):
    min_interval_hours: int = F(4, desc="Minimum hours between rebalances.",
                                group="trading.rebalance", unit="hours", protected=True, ge=0)
    band_source: Literal["execution", "trading"] = F(
        "execution",
        desc="Where the rebalance dead-band comes from: execution.rebalance_band (today) or "
             "this section once P2 moves it.",
        group="trading.rebalance", protected=True,
    )


class ExchangeLimits(_Model):
    respect_min_stake: bool = F(True, desc="Never send an order below the exchange minimum.",
                                group="trading.limits", protected=True)
    full_exit_below_min: bool = F(
        True, desc="If a partial exit would fall below the minimum, exit the whole position.",
        group="trading.limits", protected=True,
    )
    backoff_minutes: list[int] = F(
        default_factory=lambda: [15, 60, 240],
        desc="Retry backoff after an exchange rejection, in minutes.", group="trading.limits",
        unit="minutes", protected=True,
    )


class TradingDefaults(_Model):
    sizing_mode: Literal["target_weight", "signal_entries"] = F(
        "target_weight", desc="How position size is derived: from target weights or per signal.",
        group="trading.sizing", protected=True,
    )
    order_types: OrderTypes = F(default_factory=OrderTypes, desc="Entry and exit order types.",
                                group="trading.orders", protected=True)
    entry_price: PriceSide = F(default_factory=lambda: PriceSide(side="bid", offset_bps=0),
                               desc="Entry limit price construction.", group="trading.orders",
                               protected=True)
    exit_price: PriceSide = F(default_factory=lambda: PriceSide(side="ask", offset_bps=0),
                              desc="Exit limit price construction.", group="trading.orders",
                              protected=True)
    reprice_on_timeout: bool = F(False, desc="Replace an unfilled order at a new price.",
                                 group="trading.orders", protected=True)
    stoploss: StoplossCfg = F(default_factory=StoplossCfg, desc="Stop-loss mechanics.",
                              group="trading.stops", protected=True)
    take_profit: TakeProfit = F(default_factory=TakeProfit, desc="Take-profit mechanics.",
                                group="trading.take_profit", protected=True)
    dca: Dca = F(default_factory=Dca, desc="Averaging down.", group="trading.adds",
                 protected=True)
    pyramid: Pyramid = F(default_factory=Pyramid, desc="Adding to winners.", group="trading.adds",
                         protected=True)
    scheduled_dca: ScheduledDca = F(default_factory=ScheduledDca, desc="Calendar DCA.",
                                    group="trading.adds", protected=True)
    rebalance: RebalanceCfg = F(default_factory=RebalanceCfg, desc="Rebalancing cadence.",
                                group="trading.rebalance", protected=True)
    exchange_limits: ExchangeLimits = F(default_factory=ExchangeLimits,
                                        desc="Exchange minimum/backoff handling.",
                                        group="trading.limits", protected=True)


#: ``TradingDefaults`` sub-model -> its all-optional twin, so the tree is built once.
_OVERRIDE_MODELS: dict[type[BaseModel], type[BaseModel]] = {}


def _override_model(model: type[BaseModel]) -> type[BaseModel]:
    """A copy of ``model`` in which every field is optional and ``None`` means "inherit".

    ``trading.sleeves.<sleeve>`` is a per-sleeve override of ``trading.defaults``: the same
    tree, every leaf optional. It used to be typed ``dict[str, dict[str, Any]]``, which
    validated nothing and — because a loosely typed map has no schema below it — gave those
    keys no form field, no Ctrl-K entry and no tier/protected metadata. Deriving the
    override model from the defaults model instead of hand-writing a parallel one is what
    keeps "everything is configurable" literally true: a new key under ``trading.defaults``
    becomes a per-sleeve override, carrying its description, tier, group, unit and widget,
    in the same edit.

    Numeric bounds (``gt``, ``ge``, …) ride along, so an override is rejected here as well
    as by the re-validation :func:`trading_for` does on the merged result.
    """
    cached = _OVERRIDE_MODELS.get(model)
    if cached is not None:
        return cached
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        annotation = info.annotation
        metadata: list[Any] = list(info.metadata)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            annotation, metadata = _override_model(annotation), []
        inner = Annotated[tuple([annotation, *metadata])] if metadata else annotation
        extra = dict(info.json_schema_extra or {})  # type: ignore[arg-type]
        # The alias rides along too: the merge in ``trading_for`` and the schema the console
        # renders are both ``by_alias``, so an aliased key must spell itself the same on both
        # sides of the override.
        alias = {"alias": info.alias} if info.alias else {}
        fields[name] = (
            Optional[inner],  # noqa: UP045 - a runtime value, not an annotation expression
            Field(None, description=info.description or "", json_schema_extra=extra, **alias),
        )
    created = create_model(  # type: ignore[call-overload]
        f"{model.__name__}Override", __base__=_Model, __module__=__name__, **fields
    )
    created.__doc__ = (
        f"Per-sleeve overrides of ``{model.__name__}``; a null key inherits the default."
    )
    _OVERRIDE_MODELS[model] = created
    return created


#: ``trading.defaults`` with every key optional — the shape of one sleeve's override block.
TradingOverrides = _override_model(TradingDefaults)


class TradingSleeves(_Model):
    """Per-sleeve overrides, deep-merged over ``trading.defaults`` by :func:`trading_for`."""

    a: TradingOverrides = F(  # type: ignore[valid-type]
        default_factory=TradingOverrides,
        desc="Sleeve A's overrides of trading.defaults; an unset key inherits the default.",
        group="trading.core", protected=True,
    )
    b: TradingOverrides = F(  # type: ignore[valid-type]
        default_factory=TradingOverrides,
        desc="Sleeve B's overrides of trading.defaults; an unset key inherits the default.",
        group="trading.core", protected=True,
    )


class MinMax(_Model):
    min: float = F(..., desc="Lower clamp.", group="trading.plan_bounds")
    max: float = F(..., desc="Upper clamp.", group="trading.plan_bounds")


class PlanBounds(_Model):
    stop_pct: MinMax = F(default_factory=lambda: MinMax(min=0.03, max=0.15),
                         desc="Clamp for a model-proposed stop distance.",
                         group="trading.plan_bounds", protected=True)
    take_profit_pct: MinMax = F(default_factory=lambda: MinMax(min=0.03, max=0.50),
                                desc="Clamp for a model-proposed take-profit.",
                                group="trading.plan_bounds", protected=True)
    allowed_entry_styles: list[str] = F(
        default_factory=lambda: ["passive", "cross"],
        desc="Entry styles a proposal's plan block may ask for.", group="trading.plan_bounds",
        protected=True,
    )
    allow_model_urgency: bool = F(
        False, desc="Let a proposal raise execution urgency. Off: the host decides.",
        group="trading.plan_bounds", protected=True,
    )


class Trading(_Model):
    timeframe: str = F("4h", desc="Strategy timeframe for both sleeves.", group="trading.core",
                       protected=True, effects=["regen", "restart:freqtrade-a",
                                                "restart:freqtrade-b"])
    startup_candles: Literal["auto"] | int = F(
        "auto",
        desc="Startup candle count; 'auto' derives it from the largest MA bound plus 20 days.",
        group="trading.core", protected=True, effects=["restart:freqtrade-a",
                                                       "restart:freqtrade-b"],
    )
    defaults: TradingDefaults = F(default_factory=TradingDefaults,
                                  desc="Mechanics both sleeves inherit.", group="trading.core",
                                  protected=True)
    sleeves: TradingSleeves = F(
        default_factory=TradingSleeves,
        desc="Per-sleeve overrides, deep-merged over defaults. Same shape as trading.defaults.",
        group="trading.core", protected=True,
    )
    plan_bounds: PlanBounds = F(default_factory=PlanBounds,
                                desc="Clamps for the optional proposal.plan block.",
                                group="trading.plan_bounds", protected=True)


# --------------------------------------------------------------------------- proposals / exec


class ProposalCfg(_Model):
    schema_path: str = F(..., alias="schema", desc="JSON Schema a proposal must validate against.",
                         group="proposal", widget="path", tier="invariant")
    max_age_hours: int = F(..., desc="After this, sleeve B drifts to sleeve A's rules targets.",
                           group="proposal", unit="hours", protected=True, ge=1)
    sum_tolerance: float = F(..., desc="The one epsilon for target-weight sums.",
                             group="proposal", protected=True, gt=0)


class Execution(_Model):
    entry_unfilled_timeout_min: int = F(..., desc="Cancel an unfilled entry after this long.",
                                        group="execution", unit="minutes", ge=1,
                                        effects=["regen"])
    exit_unfilled_timeout_min: int = F(..., desc="Cancel an unfilled exit after this long.",
                                       group="execution", unit="minutes", ge=1,
                                       effects=["regen"])
    exit_timeout_count: int = F(
        ..., desc="Timed-out exit replacements before freqtrade uses emergency_exit (market).",
        group="execution", ge=1, effects=["regen"],
    )
    cross_ticks_buffer: float = F(
        ..., desc="How far a risk-stop exit crosses the spread, as a fraction of price.",
        group="execution", unit="fraction", ge=0,
    )
    rebalance_band: float = F(..., desc="Dead-band as a fraction of NAV before sleeve B rebalances.",
                              group="execution", unit="fraction", ge=0)
    dust_weight: float = F(..., desc="Below this target weight the position is closed.",
                           group="execution", unit="fraction", ge=0)


class Trend(_Model):
    ma_days: int = F(..., desc="Moving-average length in days.", group="sleeve_a", tier="tier1",
                     unit="days", ge=1)
    hysteresis_pct: float = F(..., desc="Band around the MA that avoids whipsaw flips.",
                              group="sleeve_a", tier="tier1", unit="fraction", ge=0)


class Vol(_Model):
    target_annual: float = F(..., desc="Annualised volatility target used for sizing.",
                             group="sleeve_a", tier="tier1", unit="fraction", gt=0)
    lookback_days: int = F(..., desc="Realised-volatility lookback.", group="sleeve_a",
                           tier="tier1", unit="days", ge=2)


class DcaParams(_Model):
    interval_days: int = F(..., desc="Days between sleeve A DCA chunks.", group="sleeve_a",
                           tier="tier1", unit="days", ge=1)
    chunk_pct_nav: float = F(..., desc="Chunk size as a fraction of NAV.", group="sleeve_a",
                             tier="tier1", unit="fraction", gt=0)


class SleeveAParams(_Model):
    trend: Trend = F(..., desc="Trend filter parameters.", group="sleeve_a", tier="tier1")
    vol: Vol = F(..., desc="Volatility targeting parameters.", group="sleeve_a", tier="tier1")
    base_weights: dict[str, float] = F(..., desc="Base target weight per asset.",
                                       group="sleeve_a", tier="tier1", unit="fraction")
    dca: DcaParams = F(..., desc="Sleeve A's calendar DCA.", group="sleeve_a", tier="tier1")


class SleeveBParams(_Model):
    drift_to_a_after_h: int = F(
        ..., desc="Hours without a fresh proposal before sleeve B follows sleeve A.",
        group="sleeve_b", unit="hours", ge=1,
    )


class Paper(_Model):
    # No ``x-widget``: this is a calendar date, and ``time`` (now a real HH:MM clock picker)
    # is the wrong control for it. The vocabulary of spec §3 has no date widget, so a plain
    # text input with the format in the description is the honest rendering.
    start_date: str = F(..., desc="First day of the paper record, as YYYY-MM-DD.",
                        group="paper")


class Bound(_Model):
    min: float = F(..., desc="Smallest value a tier-1 change may set.", group="bounds",
                   protected=True)
    max: float = F(..., desc="Largest value a tier-1 change may set.", group="bounds",
                   protected=True)
    max_step: float = F(..., desc="Largest single-change movement allowed.", group="bounds",
                        protected=True, gt=0)


class Escalation(_Model):
    stop_proximity_pct: float = F(
        ..., desc="Drawdown within this distance of a stop raises a hard-case flag.",
        group="escalation", unit="pct", gt=0,
    )


class RateLimitBudget(_Model):
    degrade_at_utilization: float = F(
        0.80, desc="Subscription rate-limit utilisation at which the brief degrades first.",
        group="budgets", unit="fraction", gt=0, le=1,
    )


class Budgets(_Model):
    mode: Literal["telemetry", "hard"] = F(
        "telemetry", desc="telemetry: USD figures report only. hard: budgets block calls.",
        group="budgets",
    )
    run_usd: dict[str, float] = F(..., desc="Per-run spend ceiling by job.", group="budgets",
                                  unit="usdt")
    monthly_usd: float = F(..., desc="Monthly spend ceiling across all jobs.", group="budgets",
                           unit="usdt", gt=0)
    context_tokens: dict[str, int] = F(..., desc="Context budget per job, in tokens.",
                                       group="budgets")
    degrade_at_pct: int = F(..., desc="Legacy USD throttle threshold (telemetry only).",
                            group="budgets", unit="pct", ge=0, le=100)
    rate_limit: RateLimitBudget = F(default_factory=RateLimitBudget,
                                    desc="The real degradation signal: rate-limit events.",
                                    group="budgets")


class Tca(_Model):
    freeze_ratio: float = F(..., desc="Measured/assumed cost ratio that starts the freeze clock.",
                            group="tca", gt=0)
    freeze_days: int = F(..., desc="Days above the ratio before tier-1 changes freeze.",
                         group="tca", unit="days", ge=1)
    calibration_min_fills: int = F(..., desc="Fills required before a monthly calibration.",
                                   group="tca", ge=1)
    quote_fallback_max_s: int = F(..., desc="How stale a book snapshot may be as a TCA anchor.",
                                  group="tca", ge=1)
    alert_bps: float = F(..., desc="Rolling 7-day cost above this alerts and flags a hard case.",
                         group="tca", unit="bps", gt=0)


# --------------------------------------------------------------------------- research / signals


class StagePrompts(_Model):
    flags: str = F("prompts/stages/flags.v1.md", desc="Prompt file for the flags stage.",
                   group="research.prompts", tier="tier1", widget="path")
    brief: str = F("prompts/stages/brief.v1.md", desc="Prompt file for the brief stage.",
                   group="research.prompts", tier="tier1", widget="path")
    brief_short: str = F("prompts/stages/brief_short.v1.md",
                         desc="Prompt file for the degraded short brief.",
                         group="research.prompts", tier="tier1", widget="path")
    classify: str = F("prompts/stages/classify.v1.md",
                      desc="Prompt file for news classification.", group="research.prompts",
                      tier="tier1", widget="path")
    scan: str = F("prompts/stages/scan.v1.md", desc="Prompt file for the signal screener.",
                  group="research.prompts", tier="tier1", widget="path")
    # `validate` shadows a BaseModel attribute, so the field is aliased; read it through
    # ops.config.stage_prompt(cfg, "validate") rather than by attribute name.
    validate_: str = F("prompts/stages/validate.v1.md", alias="validate",
                       desc="Prompt file for the signal validator.", group="research.prompts",
                       tier="tier1", widget="path")


class Research(_Model):
    slots: list[str] = F(
        default_factory=lambda: ["08:30", "16:00"],
        desc="Gulf-time research slots. THE single source for cron lines, nearest_slot and the "
             "healthcheck artifact name.",
        group="research.schedule", widget="time", effects=["crontab"],
    )
    deadline_s: int = F(2400, desc="Wall-clock budget for one research run.",
                        group="research.schedule", ge=60, effects=["crontab"])
    stage_deadlines_s: dict[str, int] = F(
        default_factory=lambda: {"flags": 240, "brief": 360, "decide": 900, "shadow": 240},
        desc="Per-stage budget; the sum must fit inside deadline_s minus postflight_margin_s.",
        group="research.schedule",
    )
    postflight_margin_s: int = F(120, desc="Time reserved after the last stage for postflight.",
                                 group="research.schedule", ge=0)
    prompt_version: str = F("research.v3", desc="Prompt version build_prompt renders.",
                            group="research.prompts", tier="tier1")
    stage_prompts: StagePrompts = F(default_factory=StagePrompts,
                                    desc="Stage prompt files (moved out of code).",
                                    group="research.prompts", tier="tier1")


class DetectorNewsEvent(_Model):
    enabled: bool = F(True, desc="Enable the news-event detector.", group="signals.detectors")
    events: list[str] = F(default_factory=lambda: ["hack", "depeg", "delist", "lawsuit", "outage"],
                          desc="Event classes that raise a candidate.", group="signals.detectors")
    corroborated_only: bool = F(True, desc="Require two whitelisted sources.",
                                group="signals.detectors")
    fast_path_events: list[str] = F(
        default_factory=lambda: ["hack", "depeg", "delist"],
        desc="Events that skip the screener and the validator and go straight to valid.",
        group="signals.detectors",
    )


class DetectorSimple(_Model):
    enabled: bool = F(True, desc="Enable this detector.", group="signals.detectors")


class DetectorMove(_Model):
    enabled: bool = F(True, desc="Enable the price-move detector.", group="signals.detectors")
    pct: dict[str, float] = F(
        default_factory=lambda: {"1h": 2.5, "4h": 5.0, "24h": 8.0},
        desc="Absolute move per window that raises a candidate.", group="signals.detectors",
        unit="pct",
    )


class DetectorNearStop(_Model):
    enabled: bool = F(True, desc="Enable the near-stop detector.", group="signals.detectors")
    fast_path: bool = F(True, desc="Near-stop candidates skip the screener and validator.",
                        group="signals.detectors")


class DetectorFunding(_Model):
    enabled: bool = F(True, desc="Enable the funding-rate detector.", group="signals.detectors")
    abs_8h: float = F(0.0010, desc="Absolute 8h funding rate that raises a candidate.",
                      group="signals.detectors", unit="fraction", gt=0)


class DetectorBreakout(_Model):
    enabled: bool = F(True, desc="Enable the range-breakout detector.", group="signals.detectors")
    tf: str = F("1d", desc="Timeframe the range is measured on.", group="signals.detectors")
    lookback: int = F(20, desc="Candles in the range lookback.", group="signals.detectors", ge=2)
    confirm_close: bool = F(True, desc="Require a closed candle beyond the range.",
                            group="signals.detectors")


class DetectorRsi(_Model):
    enabled: bool = F(True, desc="Enable the RSI-extreme detector.", group="signals.detectors")
    tf: str = F("4h", desc="Timeframe RSI is computed on.", group="signals.detectors")
    period: int = F(14, desc="RSI period.", group="signals.detectors", ge=2)
    low: float = F(25, desc="RSI at or below this is oversold.", group="signals.detectors", ge=0,
                   le=100)
    high: float = F(75, desc="RSI at or above this is overbought.", group="signals.detectors",
                    ge=0, le=100)


class DetectorVolumeSpike(_Model):
    enabled: bool = F(True, desc="Enable the volume-spike detector.", group="signals.detectors")
    tf: str = F("1h", desc="Timeframe volume is measured on.", group="signals.detectors")
    zscore: float = F(3.0, desc="Volume z-score that raises a candidate.",
                      group="signals.detectors", gt=0)
    lookback: int = F(72, desc="Candles in the z-score window.", group="signals.detectors", ge=10)


class DetectorDipFromHigh(_Model):
    enabled: bool = F(True, desc="Enable the dip-from-high detector.", group="signals.detectors")
    tf: str = F("1d", desc="Timeframe the high is measured on.", group="signals.detectors")
    pct: float = F(12.0, desc="Drop from the lookback high that raises a candidate.",
                   group="signals.detectors", unit="pct", gt=0)
    lookback_days: int = F(30, desc="How far back the high is taken from.",
                           group="signals.detectors", unit="days", ge=2)


class DetectorMaCross(_Model):
    enabled: bool = F(False, desc="Enable the moving-average cross detector.",
                      group="signals.detectors")
    tf: str = F("1d", desc="Timeframe the averages are computed on.", group="signals.detectors")
    fast: int = F(50, desc="Fast moving-average length.", group="signals.detectors", ge=2)
    slow: int = F(200, desc="Slow moving-average length.", group="signals.detectors", ge=3)


class Detectors(_Model):
    news_event: DetectorNewsEvent = F(default_factory=DetectorNewsEvent,
                                      desc="Corroborated news events.", group="signals.detectors")
    regime_flip: DetectorSimple = F(default_factory=DetectorSimple,
                                    desc="Trend regime changed since the last scan.",
                                    group="signals.detectors")
    move: DetectorMove = F(default_factory=DetectorMove, desc="Large price move.",
                           group="signals.detectors")
    near_stop: DetectorNearStop = F(default_factory=DetectorNearStop,
                                    desc="Open position close to its stop.",
                                    group="signals.detectors")
    funding: DetectorFunding = F(default_factory=DetectorFunding, desc="Funding-rate extreme.",
                                 group="signals.detectors")
    breakout: DetectorBreakout = F(default_factory=DetectorBreakout, desc="Range breakout.",
                                   group="signals.detectors")
    rsi_extreme: DetectorRsi = F(default_factory=DetectorRsi, desc="RSI extreme.",
                                 group="signals.detectors")
    volume_spike: DetectorVolumeSpike = F(default_factory=DetectorVolumeSpike,
                                          desc="Volume spike.", group="signals.detectors")
    dip_from_high: DetectorDipFromHigh = F(default_factory=DetectorDipFromHigh,
                                           desc="Dip from a recent high.",
                                           group="signals.detectors")
    ma_cross: DetectorMaCross = F(default_factory=DetectorMaCross,
                                  desc="Moving-average cross.", group="signals.detectors")


class ScreenCfg(_Model):
    enabled: bool = F(True, desc="Run the cheap LLM screener over candidates.",
                      group="signals.screen")
    task: str = F("scan", desc="models.yaml task used for screening.", group="signals.screen",
                  widget="model-ref")
    min_score: float = F(0.60, desc="Combined score required to reach 'screened'.",
                         group="signals.screen", ge=0, le=1)
    gray_zone: list[float] = F(
        default_factory=lambda: [0.45, 0.65],
        desc="Scores inside this band re-run on the next chain entry. Must bracket min_score.",
        group="signals.screen", min_length=2, max_length=2,
    )
    allow_novel: bool = F(True, desc="Allow items with no matching detector, if corroborated.",
                          group="signals.screen")
    news_since_min: int = F(60, desc="How far back news is considered fresh for screening.",
                            group="signals.screen", unit="minutes", ge=1)


class Scoring(_Model):
    detector_weight: float = F(0.6, desc="Weight of the deterministic detector score.",
                               group="signals.screen", ge=0, le=1)
    screen_weight: float = F(0.4, desc="Weight of the screener score.", group="signals.screen",
                             ge=0, le=1)


class Scanner(_Model):
    cron: str = F("*/5 * * * *", desc="Scanner schedule.", group="signals.scanner",
                  widget="cron", effects=["crontab"])
    run_after_ingest: bool = F(True, desc="Also run the scanner right after each ingest.",
                               group="signals.scanner")
    deadline_s: int = F(240, desc="Wall-clock budget for one scan.", group="signals.scanner",
                        ge=30)
    dedupe_minutes: int = F(240, desc="Bucket width for the detector:pair:direction dedupe key.",
                            group="signals.scanner", unit="minutes", ge=1)
    max_candidates_per_cycle: int = F(5, desc="Most candidates screened in one cycle.",
                                      group="signals.scanner", ge=1)
    detectors: Detectors = F(default_factory=Detectors, desc="Deterministic candidate detectors.",
                             group="signals.detectors")
    screen: ScreenCfg = F(default_factory=ScreenCfg, desc="Cheap LLM screening stage.",
                          group="signals.screen")
    scoring: Scoring = F(default_factory=Scoring, desc="How detector and screen scores combine.",
                         group="signals.screen")


class Validator(_Model):
    task: str = F("validate", desc="models.yaml task used for validation.",
                  group="signals.validator", widget="model-ref")
    min_confidence: float = F(0.65, desc="Confidence required before a signal may act.",
                              group="signals.validator", ge=0, le=1)
    max_per_day: int = F(6, desc="Validations per UTC day.", group="signals.validator", ge=0)
    cooldown_min_per_asset: int = F(120, desc="Minimum minutes between validations of one asset.",
                                    group="signals.validator", unit="minutes", ge=0)
    expire_after_min: int = F(90, desc="A screened signal expires unvalidated after this long.",
                              group="signals.validator", unit="minutes", ge=1)
    deadline_s: int = F(600, desc="Wall-clock budget for one validation.",
                        group="signals.validator", ge=30)


class Planner(_Model):
    enabled: bool = F(True, desc="false = observe only: validate signals but never fire research.",
                      group="signals.planner")
    min_verdict: Literal["valid", "uncertain"] = F(
        "valid", desc="Weakest validator verdict that may fire a research run.",
        group="signals.planner",
    )
    max_per_day: int = F(3, desc="Signal-fired research runs per UTC day.",
                         group="signals.planner", ge=0)
    cooldown_hours: int = F(4, desc="No signal-fired run within this long of any decide run.",
                            group="signals.planner", unit="hours", ge=0)


class Outcomes(_Model):
    resolve_after_hours: int = F(24, desc="How long after a signal its outcome is measured.",
                                 group="signals.outcomes", unit="hours", ge=1)
    measure_tf: str = F("1h", desc="Timeframe the outcome return is measured on.",
                        group="signals.outcomes")


class Signals(_Model):
    enabled: bool = F(True, desc="Master switch for the signal pipeline.", group="signals.core")
    integration: Literal["legacy", "pipeline"] = F(
        "pipeline",
        desc="legacy keeps today's TriggerEngine behaviour; pipeline runs the tiered scanner.",
        group="signals.core",
    )
    scanner: Scanner = F(default_factory=Scanner, desc="Detector and screening stage.",
                         group="signals.scanner")
    validator: Validator = F(default_factory=Validator, desc="Strong-model validation stage.",
                             group="signals.validator")
    planner: Planner = F(default_factory=Planner, desc="When a validated signal fires research.",
                         group="signals.planner")
    outcomes: Outcomes = F(default_factory=Outcomes, desc="How signal outcomes are resolved.",
                           group="signals.outcomes")


class Triggers(_Model):
    """Legacy trigger tuning. Unset keys are derived from ``signals.*`` at load time."""

    enabled: bool = F(True, desc="Keep event-driven decision runs enabled.", group="legacy")
    cooldown_hours: int | None = F(None, desc="Legacy alias for signals.planner.cooldown_hours.",
                                   group="legacy", tier="generated", unit="hours",
                                   deprecated=True)
    max_per_day: int | None = F(None, desc="Legacy alias for signals.planner.max_per_day.",
                                group="legacy", tier="generated", deprecated=True)
    news_events: list[str] | None = F(
        None, desc="Legacy alias for signals.scanner.detectors.news_event.events.",
        group="legacy", tier="generated", deprecated=True,
    )
    move_4h_pct: float | None = F(
        None, desc="Legacy alias for signals.scanner.detectors.move.pct['4h'].", group="legacy",
        tier="generated", unit="pct", deprecated=True,
    )
    funding_abs_8h: float | None = F(
        None, desc="Legacy alias for signals.scanner.detectors.funding.abs_8h.", group="legacy",
        tier="generated", unit="fraction", deprecated=True,
    )


# --------------------------------------------------------------------------- ingest / news


class Ingest(_Model):
    timeframes: list[str] = F(default_factory=lambda: ["1h", "4h", "1d"],
                              desc="Candle timeframes ingest keeps up to date.", group="ingest")
    cold_start_days: int = F(7, desc="History pulled when a timeframe has no candles yet.",
                             group="ingest", unit="days", ge=1)


class NewsClassify(_Model):
    enabled: bool = F(..., desc="Classify news items with a cheap model.", group="news")
    model: str = F(..., desc="Model alias or id used for classification.", group="news",
                   widget="model-ref")
    monthly_budget_usd: float = F(..., desc="Monthly ceiling for classification spend.",
                                  group="news", unit="usdt", ge=0)


class Feed(_Model):
    name: str = F(..., desc="Feed name as it appears in the archive.", group="news.feeds")
    url: str = F(..., desc="RSS/Atom URL.", group="news.feeds", widget="path")
    class_: Literal["primary", "secondary"] = F(
        "secondary", alias="class",
        desc="primary sources corroborate on their own; secondary need a second source.",
        group="news.feeds",
    )


class News(_Model):
    window_hours: int = F(..., desc="How far back the brief looks.", group="news", unit="hours",
                          ge=1)
    min_sources: int = F(..., desc="Sources required to treat a claim as corroborated.",
                         group="news", ge=1)
    classify: NewsClassify = F(..., desc="News classification settings.", group="news")
    whitelist: list[Feed] = F(..., desc="The only feeds Earn reads.", group="news.feeds")
    event_keywords: dict[str, list[str]] = F(
        default_factory=dict,
        desc="Event class to title keywords (moved out of runs/ingest.py).",
        group="news.keywords",
    )
    asset_keywords: dict[str, list[str]] = F(
        default_factory=dict,
        desc="Asset to title keywords; must cover every universe asset.",
        group="news.keywords",
    )


# --------------------------------------------------------------------------- skills


class SkillPolicy(_Model):
    body: Literal["human", "gated"] = F("gated", desc="Who may edit SKILL.md.", group="skills")
    scripts: Literal["human", "gated"] = F("human", desc="Who may edit scripts/**.",
                                           group="skills", protected=True)
    tests: Literal["human", "gated"] = F("gated", desc="Who may edit the skill's tests.",
                                         group="skills")


class SkillEval(_Model):
    min_pass_rate: float = F(0.8, desc="Pass rate a skill eval must reach.", group="skills",
                             unit="fraction", ge=0, le=1)
    timeout_s: int = F(300, desc="Wall-clock budget for one skill eval.", group="skills", ge=10)


class Skills(_Model):
    bindings: dict[str, list[str]] = F(
        default_factory=dict,
        desc="Task or stage to the skills loaded for it. The tier-1 overlay "
             "config/skills-registry.auto.yaml merges on top.",
        group="skills", widget="skill-ref",
    )
    policy: dict[str, SkillPolicy] = F(
        default_factory=dict,
        desc="Per-skill edit policy; the 'default' entry applies to unlisted skills.",
        group="skills", protected=True,
    )
    eval: SkillEval = F(default_factory=SkillEval, desc="Skill eval thresholds.", group="skills")


# --------------------------------------------------------------------------- ops


class Telegram(_Model):
    chat_id: int = F(..., desc="The one chat the ops bot accepts; 0 until configured.",
                     group="telegram", protected=True)
    user_id: int = F(..., desc="The one Telegram user allowed to command the bot.",
                     group="telegram", protected=True)
    dedupe_ttl_min: int = F(..., desc="Suppress duplicate alerts within this window.",
                            group="telegram", unit="minutes", ge=0)


class BotEndpoint(_Model):
    service: str = F(..., desc="Compose service name for this sleeve's bot.", group="ops.bots",
                     effects=["restart:freqtrade-a", "restart:freqtrade-b"])
    api: str = F(..., desc="Loopback URL of the bot's REST API.", group="ops.bots",
                 widget="path")
    password_env: str = F(..., desc="Env var holding the bot's API password.", group="ops.bots",
                          widget="secret-ref")


class Schedule(_Model):
    cron: str = F(..., desc="Cron expression in Gulf time, or 'derived' for research slots.",
                  group="ops.schedules", widget="cron", effects=["crontab"])
    deadline_s: int = F(..., desc="timeout(1) budget the cron line wraps the job in.",
                        group="ops.schedules", effects=["crontab"], ge=1)
    artifact: str = F(..., desc="Artifact the healthcheck expects, or 'none'.",
                      group="ops.schedules")


class OpsCfg(_Model):
    staleness_min: int = F(..., desc="Data age the healthcheck treats as stale.", group="ops",
                           unit="minutes", ge=1)
    missed_run_grace_min: int = F(..., desc="Grace after a schedule before a run counts missed.",
                                  group="ops", unit="minutes", ge=0)
    container_heartbeat_min: int = F(..., desc="Longest gap between container heartbeats.",
                                     group="ops", unit="minutes", ge=1)
    max_restarts_per_hour: int = F(..., desc="Automatic container restarts allowed per hour.",
                                   group="ops", ge=0)
    summary_time_local: str = F(..., desc="Gulf time of the daily digest; silence is an alert.",
                                group="ops", widget="time")
    cron_mailto: str = F("", desc="MAILTO for the generated crontab; empty keeps cron silent.",
                         group="ops", effects=["crontab"])
    bots: dict[Literal["a", "b"], BotEndpoint] = F(..., desc="Per-sleeve bot endpoints.",
                                                   group="ops.bots")
    schedules: dict[str, Schedule] = F(..., desc="Every scheduled job: cron, timeout, artifact.",
                                       group="ops.schedules", effects=["crontab"])


class Backup(_Model):
    dest: str = F(..., desc="Backup destination; ~ and $VARS are expanded.", group="backup",
                  widget="path")
    mirror_dest: str | None = F(None, desc="Optional second copy; failures warn, never alert.",
                                group="backup", widget="path")
    keep_daily: int = F(..., desc="Daily backups retained.", group="backup", ge=1)
    keep_weekly: int = F(..., desc="Weekly backups retained.", group="backup", ge=1)


class MaintenanceCfg(_Model):
    sdk_floor: str = F(..., desc="Lowest claude-agent-sdk version the upgrade window accepts.",
                       group="maintenance")
    sdk_ceiling: str = F(..., desc="Exclusive upper bound of the SDK upgrade window.",
                         group="maintenance")
    shadow_days: int = F(30, desc="Auto-shadow window for a newly detected model.",
                         group="maintenance", unit="days", ge=1)


class Replay(_Model):
    days: int = F(..., desc="Days of snapshots a replay covers.", group="review.replay",
                  unit="days", ge=1)
    min_snapshots: int = F(..., desc="Snapshots required before a replay counts.",
                           group="review.replay", ge=1)
    runs_per_snapshot: int = F(..., desc="Re-decisions per snapshot (determinism check).",
                               group="review.replay", ge=1)
    budget_usd: float = F(..., desc="Spend ceiling for one replay.", group="review.replay",
                          unit="usdt", gt=0)
    target_tolerance: float = F(..., desc="Target-weight difference treated as identical.",
                                group="review.replay", unit="fraction", ge=0)
    determinism_min_identical: float = F(..., desc="Fraction of runs that must agree.",
                                         group="review.replay", unit="fraction", ge=0, le=1)
    turnover_max_ratio: float = F(..., desc="Largest turnover increase a candidate may cause.",
                                  group="review.replay", gt=0)


class ChangeGates(_Model):
    backtest_min_years: int = F(..., desc="Years of backtest required for a param change.",
                                group="review.gates", ge=1)
    walk_forward_min_out_sample_delta: float = F(
        ..., desc="Minimum out-of-sample improvement a param change must show.",
        group="review.gates",
    )
    max_param_changes_per_month: int = F(..., desc="Param changes merged per calendar month.",
                                         group="review.gates", ge=0)


class Fewshot(_Model):
    best: int = F(..., desc="Best-graded examples included in the prompt.", group="review",
                  ge=0)
    worst: int = F(..., desc="Worst-graded examples included in the prompt.", group="review",
                   ge=0)
    refresh: str = F(..., desc="How often the few-shot set is rebuilt.", group="review")


class DailyReviewCfg(_Model):
    model: str = F(..., desc="Model alias that grades the previous Gulf day.",
                   group="daily_review", widget="model-ref")
    fallback_model: str = F(..., desc="Explicit rerun model; its changes are always HELD.",
                            group="daily_review", widget="model-ref")
    max_turns: int = F(..., desc="Turn cap for the daily review session.", group="daily_review",
                       ge=1)
    max_budget_usd: float = F(..., desc="Runaway-loop safety cap.", group="daily_review",
                              unit="usdt", gt=0)
    wrong_process_below: int = F(..., desc="Process grade below this counts as a wrong decision.",
                                 group="daily_review", ge=0, le=100)


class Review(_Model):
    model: str = F(..., desc="Model alias for the weekly review.", group="review",
                   widget="model-ref")
    fallback_model: str = F(..., desc="Explicit rerun model; its changes are always HELD.",
                            group="review", widget="model-ref")
    max_turns: int = F(..., desc="Turn cap for the weekly review session.", group="review", ge=1)
    max_budget_usd: float = F(..., desc="Runaway-loop safety cap.", group="review", unit="usdt",
                              gt=0)
    replay: Replay = F(..., desc="Decision-replay settings.", group="review.replay")
    change_gates: ChangeGates = F(..., desc="Evidence gates a tier-1 change must clear.",
                                  group="review.gates", protected=True)
    outcome_stats_min_decisions: int = F(..., desc="Decisions needed before outcome stats count.",
                                         group="review", ge=1)
    lessons_reconfirm_days: int = F(..., desc="How often a lesson must be reconfirmed.",
                                    group="review", unit="days", ge=1)
    recurring_cause_weeks: int = F(..., desc="Weeks a cause must recur before it escalates.",
                                   group="review", ge=1)
    fewshot: Fewshot = F(..., desc="Few-shot example selection.", group="review")


class Paths(_Model):
    knowledge_db: str = F(..., desc="Knowledge database, relative to the state root.",
                          group="paths", widget="path", protected=True)
    journal_db: str = F(..., desc="Journal database, relative to the state root.", group="paths",
                        widget="path", protected=True)
    flags_file: str = F(..., desc="The flags file the gate reads.", group="paths", widget="path",
                        protected=True)
    state_latest: str = F(..., desc="Latest computed market state.", group="paths",
                          widget="path", protected=True)
    proposals_dir: str = F(..., desc="Where proposals are written.", group="paths",
                           widget="path", protected=True)
    data_dir: str = F(..., desc="Candle store shared with the containers.", group="paths",
                      widget="path", protected=True)
    fewshot: str = F(..., desc="Few-shot example file.", group="paths", widget="path")
    params_a: str = F(..., desc="Sleeve A tier-1 params file.", group="paths", widget="path")
    params_b: str = F(..., desc="Sleeve B tier-1 params file.", group="paths", widget="path")


# --------------------------------------------------------------------------- root


class EarnConfig(_Model):
    meta: Meta = F(..., desc="File identity and display settings.", group="meta")
    runtime: Runtime = F(default_factory=Runtime, desc="Host facts setup and preflight check.",
                         group="host")
    console: Console = F(default_factory=Console, desc="Local web console settings.",
                         group="console", effects=["restart:console"])
    git: GitCfg = F(default_factory=GitCfg, desc="Branch and worktree layout.", group="git")
    security: Security = F(default_factory=Security, desc="Agent user and bind reminders.",
                           group="security")
    autonomy: Autonomy = F(..., desc="What the system may change about itself.",
                           group="autonomy")
    universe: Universe = F(..., desc="What Earn may trade.", group="universe",
                           effects=REGEN_AND_RESTART)
    exchange: Exchange = F(..., desc="Exchange identity and assumed costs.", group="exchange",
                           effects=REGEN_AND_RESTART)
    sleeves: Sleeves = F(..., desc="The two trading sleeves and the benchmark.",
                         group="sleeves", effects=["regen"])
    modes: Modes = F(default_factory=Modes, desc="TEST and LIVE defaults and ceilings.",
                     group="modes")
    risk: Risk = F(..., desc="Every limit the deterministic gate enforces.", group="risk",
                   effects=REGEN_AND_RESTART)
    trading: Trading = F(default_factory=Trading, desc="Order, stop and sizing mechanics.",
                         group="trading", effects=REGEN_AND_RESTART)
    proposal: ProposalCfg = F(..., desc="Proposal validity rules.", group="proposal",
                              effects=["regen"])
    execution: Execution = F(..., desc="Order timeouts and rebalance bands.",
                             group="execution", effects=["regen"])
    sleeve_a: SleeveAParams = F(..., desc="Sleeve A defaults (live values are tier-1 params).",
                                group="sleeve_a", effects=["regen"])
    sleeve_b: SleeveBParams = F(..., desc="Sleeve B defaults.", group="sleeve_b",
                                effects=["regen"])
    paper: Paper = F(..., desc="Paper-record anchor.", group="paper")
    bounds: dict[str, Bound] = F(..., desc="Tier-2 bounds every tier-1 param change must obey.",
                                 group="bounds", protected=True, effects=["regen"])
    escalation: Escalation = F(..., desc="When a decision escalates to the strongest model.",
                               group="escalation")
    budgets: Budgets = F(..., desc="Spend and context budgets.", group="budgets")
    tca: Tca = F(..., desc="Execution-cost thresholds.", group="tca")
    research: Research = F(default_factory=Research, desc="Research slots, deadlines and prompts.",
                           group="research")
    signals: Signals = F(default_factory=Signals, desc="The tiered signal pipeline.",
                         group="signals")
    ingest: Ingest = F(default_factory=Ingest, desc="What ingest pulls and how far back.",
                       group="ingest")
    news: News = F(..., desc="News window, feeds and keyword tables.", group="news")
    skills: Skills = F(default_factory=Skills, desc="Skill bindings and edit policy.",
                       group="skills")
    telegram: Telegram = F(..., desc="Ops bot identity.", group="telegram",
                           effects=["restart:telegram"])
    ops: OpsCfg = F(..., desc="Schedules, bot endpoints and healthcheck thresholds.",
                    group="ops")
    backup: Backup = F(..., desc="Backup destinations and retention.", group="backup")
    maintenance: MaintenanceCfg = F(..., desc="Weekly self-maintenance window.",
                                    group="maintenance")
    triggers: Triggers = F(default_factory=Triggers,
                           desc="Legacy trigger keys, derived from signals.* when unset.",
                           group="legacy")
    daily_review: DailyReviewCfg = F(..., desc="Nightly learning loop.", group="daily_review")
    review: Review = F(..., desc="Weekly review and change gates.", group="review")
    models_config: str = F(..., desc="Path to the model routing file.", group="meta",
                           widget="path")
    paths: Paths = F(..., desc="Where Earn's data lives, relative to the state root.",
                     group="paths")

    # ---- computed, never stored -------------------------------------------------

    @property
    def phase(self) -> str:
        """``paper`` | ``live_propose`` | ``live_execute``, derived from the signed mode file.

        Fails closed to ``paper`` when the mode file is missing, unsigned or unverifiable.
        """
        from ops.lib import mode_state

        return mode_state.load().phase

    @property
    def mode_state(self) -> Any:
        from ops.lib import mode_state

        return mode_state.load()


# --------------------------------------------------------------------------- accessors


def max_weight_for(cfg: EarnConfig, asset: str) -> float:
    return cfg.risk.max_weight.get(asset, cfg.risk.max_weight["default"])


def seed_for(cfg: EarnConfig, sleeve: str, *, state: Any | None = None) -> float:
    """The single reader of a sleeve's starting balance.

    A verified live sleeve uses the seed pinned in ``var/state/mode.json``; everything else
    falls back to ``modes.test.seed_usdt``.
    """
    s = sleeve.lower()
    if state is None:
        from ops.lib import mode_state as _ms

        state = _ms.load()
    sl = state.sleeve(s)
    if state.verified and sl.is_live and sl.seed_usdt is not None:
        return float(sl.seed_usdt)
    if state.verified and sl.seed_usdt is not None:
        return float(sl.seed_usdt)
    return float(cfg.modes.test.seed_usdt.get(s, 0.0))


def stage_prompt(cfg: EarnConfig, stage: str) -> str:
    """Path of a stage prompt file by its config key (``flags``, ``brief``, … ``validate``)."""
    prompts = cfg.research.stage_prompts.model_dump(by_alias=True)
    try:
        return str(prompts[stage])
    except KeyError as e:
        raise ConfigError(
            f"research.stage_prompts has no stage {stage!r}; known: {sorted(prompts)}"
        ) from e


def slots_for(cfg: EarnConfig) -> list[str]:
    """The single source of research slot times (cron, nearest_slot, healthcheck artifact)."""
    return list(cfg.research.slots)


def startup_candles(cfg: EarnConfig) -> int:
    """Resolve ``trading.startup_candles: auto`` into a candle count.

    ``auto`` = (largest allowed ``sleeve_a.trend.ma_days`` + 20 days) worth of candles at
    ``trading.timeframe``.
    """
    if cfg.trading.startup_candles != "auto":
        return int(cfg.trading.startup_candles)
    bound = cfg.bounds.get("sleeve_a.trend.ma_days")
    ma_days = int(bound.max) if bound else cfg.sleeve_a.trend.ma_days
    days = ma_days + 20
    per_day = _candles_per_day(cfg.trading.timeframe)
    return int(days * per_day)


def _candles_per_day(timeframe: str) -> float:
    unit = timeframe[-1]
    try:
        size = int(timeframe[:-1])
    except ValueError as e:
        raise ConfigError(f"trading.timeframe: cannot parse {timeframe!r}") from e
    per_day = {"m": 24 * 60, "h": 24, "d": 1, "w": 1 / 7}.get(unit)
    if per_day is None or size <= 0:
        raise ConfigError(f"trading.timeframe: unsupported timeframe {timeframe!r}")
    return per_day / size


def trading_for(cfg: EarnConfig, sleeve: str) -> TradingDefaults:
    """``trading.defaults`` deep-merged with ``trading.sleeves.<sleeve>``, re-validated."""
    base = cfg.trading.defaults.model_dump(by_alias=True)
    block = getattr(cfg.trading.sleeves, sleeve.lower(), None)
    # ``exclude_none`` is what makes null mean "inherit": only the keys the sleeve actually
    # set reach the merge, at every depth.
    override = block.model_dump(by_alias=True, exclude_none=True) if block is not None else {}
    merged = _deep_merge(base, override)
    try:
        return TradingDefaults.model_validate(merged)
    except ValidationError as e:
        raise ConfigError(f"trading.sleeves.{sleeve}: {_first_error(e)}") from e


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def stoploss_on_exchange(cfg: EarnConfig, sleeve: str, *, live: bool) -> bool:
    """Resolve ``stoploss.on_exchange: auto`` — required in LIVE, off in TEST."""
    value = trading_for(cfg, sleeve).stoploss.on_exchange
    return bool(live) if value == "auto" else bool(value)


# --------------------------------------------------------------------------- validation


def _strategy_classes(root: Path) -> dict[str, list[str]]:
    """``{class name: [base names]}`` for every class defined under ``strategies/``.

    An AST scan, not an import: importing a strategy pulls in freqtrade, which the console
    and most host jobs do not have.
    """
    out: dict[str, list[str]] = {}
    strategies_dir = root / "strategies"
    if not strategies_dir.is_dir():
        return out
    for path in sorted(strategies_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):  # pragma: no cover - a broken strategy file
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                bases += [b.attr for b in node.bases if isinstance(b, ast.Attribute)]
                out[node.name] = bases
    return out


def _first_error(e: ValidationError) -> str:
    first = e.errors()[0]
    loc = ".".join(str(x) for x in first["loc"])
    return f"invalid at '{loc}': {first['msg']}"


def _validate_trading(cfg: EarnConfig) -> None:
    for sleeve in ("a", "b"):
        t = trading_for(cfg, sleeve)
        where = f"trading.sleeves.{sleeve}"
        if t.stoploss.fixed_pct > cfg.risk.stoploss_per_trade + 1e-12:
            raise ConfigError(
                f"{where}.stoploss.fixed_pct ({t.stoploss.fixed_pct}) exceeds "
                f"risk.stoploss_per_trade ({cfg.risk.stoploss_per_trade})"
            )
        if t.stoploss.trailing.enabled and t.stoploss.trailing.distance_pct < 0.005:
            raise ConfigError(f"{where}.stoploss.trailing.distance_pct must be >= 0.005")
        entries = 1 + t.dca.max_adds + t.pyramid.max_adds
        if entries > cfg.risk.max_entries_per_trade:
            raise ConfigError(
                f"{where}: 1 + dca.max_adds + pyramid.max_adds = {entries} exceeds "
                f"risk.max_entries_per_trade ({cfg.risk.max_entries_per_trade})"
            )
        if t.order_types.entry == "market" and not cfg.risk.market_entries_allowed:
            raise ConfigError(
                f"{where}.order_types.entry is 'market' but risk.market_entries_allowed is false"
            )
    pb = cfg.trading.plan_bounds
    for name, mm in (("stop_pct", pb.stop_pct), ("take_profit_pct", pb.take_profit_pct)):
        if mm.min > mm.max:
            raise ConfigError(f"trading.plan_bounds.{name}: min must be <= max")


def _validate_deadlines(cfg: EarnConfig) -> None:
    r = cfg.research
    total = sum(r.stage_deadlines_s.values())
    if total > r.deadline_s - r.postflight_margin_s:
        raise ConfigError(
            f"research.stage_deadlines_s sum ({total}s) exceeds deadline_s - "
            f"postflight_margin_s ({r.deadline_s - r.postflight_margin_s}s)"
        )
    sched = cfg.ops.schedules
    checks = {
        "research_run": r.deadline_s + r.postflight_margin_s,
        "scanner": cfg.signals.scanner.deadline_s,
    }
    for job, needed in checks.items():
        s = sched.get(job)
        if s is None:
            raise ConfigError(f"ops.schedules: missing required job {job!r}")
        if s.deadline_s < needed:
            raise ConfigError(
                f"ops.schedules.{job}.deadline_s ({s.deadline_s}s) is below the stage budget "
                f"it must contain ({needed}s)"
            )


def _validate_slots(cfg: EarnConfig) -> None:
    slots = cfg.research.slots
    if not slots:
        raise ConfigError("research.slots must not be empty")
    for s in slots:
        if not SLOT_RE.match(s):
            raise ConfigError(f"research.slots: {s!r} is not HH:MM")
        hh, mm = (int(x) for x in s.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ConfigError(f"research.slots: {s!r} is not a valid time")
    if len(set(slots)) != len(slots):
        raise ConfigError(f"research.slots must be unique, got {slots}")
    if slots != sorted(slots):
        raise ConfigError(f"research.slots must be sorted, got {slots}")


def _validate_seeds(cfg: EarnConfig) -> None:
    for s in ("a", "b"):
        test_seed = cfg.modes.test.seed_usdt.get(s)
        if test_seed is None or test_seed <= 0:
            raise ConfigError(f"modes.test.seed_usdt.{s} must be > 0")
        live_max = cfg.modes.live.max_seed_usdt.get(s)
        if live_max is None:
            raise ConfigError(f"modes.live.max_seed_usdt.{s} is required")
        floor = 4 * cfg.risk.min_notional_usdt
        if live_max < floor:
            raise ConfigError(
                f"modes.live.max_seed_usdt.{s} ({live_max}) must be >= 4 * "
                f"risk.min_notional_usdt ({floor})"
            )


def _validate_signals(cfg: EarnConfig) -> None:
    screen = cfg.signals.scanner.screen
    lo, hi = screen.gray_zone
    if not (lo < screen.min_score <= hi):
        raise ConfigError(
            f"signals.scanner.screen: gray_zone[0] < min_score <= gray_zone[1] violated "
            f"({lo} < {screen.min_score} <= {hi})"
        )
    sc = cfg.signals.scanner.scoring
    if abs(sc.detector_weight + sc.screen_weight - 1.0) > 1e-9:
        raise ConfigError("signals.scanner.scoring weights must sum to 1.0")
    known = set(cfg.news.event_keywords)
    if known:
        missing = [e for e in cfg.signals.scanner.detectors.news_event.events if e not in known]
        if missing:
            raise ConfigError(f"news.event_keywords is missing event classes {missing}")
        fast = [
            e
            for e in cfg.signals.scanner.detectors.news_event.fast_path_events
            if e not in cfg.signals.scanner.detectors.news_event.events
        ]
        if fast:
            raise ConfigError(f"news_event.fast_path_events not in events: {fast}")


def _validate_skills(cfg: EarnConfig, root: Path) -> None:
    available = {p.name for p in (root / ".claude" / "skills").glob("*") if p.is_dir()}
    if not available:  # a checkout without skills (e.g. a sparse worktree): nothing to check
        return
    for task, names in cfg.skills.bindings.items():
        missing = [n for n in names if n not in available]
        if missing:
            raise ConfigError(f"skills.bindings.{task} references missing skills {missing}")
    for name in cfg.skills.policy:
        if name != "default" and name not in available:
            raise ConfigError(f"skills.policy references missing skill {name!r}")


def _validate_strategies(cfg: EarnConfig, root: Path) -> None:
    classes = _strategy_classes(root)
    if not classes:  # pragma: no cover - a checkout without strategies/
        return
    from ops.lib import mode_state as _ms

    state = _ms.load()
    for sleeve in ("a", "b"):
        name = getattr(cfg.sleeves, sleeve).strategy
        if name not in classes:
            raise ConfigError(
                f"sleeves.{sleeve}.strategy {name!r} is not a class in strategies/"
            )
        if "EarnBaseStrategy" not in classes[name]:
            if state.is_live(sleeve):
                raise ConfigError(
                    f"sleeves.{sleeve}.strategy {name!r} does not subclass EarnBaseStrategy "
                    f"and sleeve {sleeve} is live"
                )
            warnings.warn(
                f"sleeves.{sleeve}.strategy {name!r} does not subclass EarnBaseStrategy; "
                "it will not trade",
                ConfigWarning,
                stacklevel=2,
            )


def _validate_news(cfg: EarnConfig) -> None:
    if not cfg.news.asset_keywords:
        return
    missing = [a for a in cfg.universe.assets if a not in cfg.news.asset_keywords]
    if missing:
        raise ConfigError(f"news.asset_keywords does not cover universe assets {missing}")


def _cross_validate(cfg: EarnConfig, root: Path | None = None) -> None:
    base = root or REPO_ROOT
    r, u = cfg.risk, cfg.universe
    if r.usdt_floor + r.max_gross_exposure > 1.0 + 1e-9:
        raise ConfigError("risk: usdt_floor + max_gross_exposure must be <= 1.0")
    if "default" not in r.max_weight:
        missing = [a for a in u.assets if a not in r.max_weight]
        if missing:
            raise ConfigError(f"risk.max_weight: no cap (and no default) for {missing}")
    expected = [f"{a}/{u.quote}" for a in u.assets]
    if u.pairs != expected:
        raise ConfigError(f"universe.pairs must equal {expected}, got {u.pairs}")
    overlap = set(u.pairs) & set(u.data_only_symbols)
    if overlap:
        raise ConfigError(f"universe.data_only_symbols overlaps tradeable pairs: {overlap}")
    for a in cfg.sleeve_a.base_weights:
        if a not in u.assets:
            raise ConfigError(f"sleeve_a.base_weights references non-universe asset {a}")
    _validate_slots(cfg)
    _validate_deadlines(cfg)
    _validate_seeds(cfg)
    _validate_trading(cfg)
    _validate_signals(cfg)
    _validate_news(cfg)
    _validate_skills(cfg, base)
    _validate_strategies(cfg, base)


# --------------------------------------------------------------------------- legacy shims


def _strip_legacy(raw: dict[str, Any], source: str) -> dict[str, Any]:
    """Accept v1 keys that v2 removed: warn, then drop or translate them."""
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    if "phase" in out:
        warnings.warn(
            f"{source}: 'phase' is no longer stored in earn.yaml — it is computed from "
            "var/state/mode.json. The key is ignored.",
            ConfigWarning,
            stacklevel=3,
        )
        out.pop("phase")
    sleeves = out.get("sleeves")
    if isinstance(sleeves, dict):
        for name in ("a", "b"):
            sl = sleeves.get(name)
            if isinstance(sl, dict) and sl.get("capital_usdt") is not None:
                warnings.warn(
                    f"{source}: sleeves.{name}.capital_usdt is deprecated — set "
                    "modes.test.seed_usdt (test) or go live through a mode transition.",
                    ConfigWarning,
                    stacklevel=3,
                )
    return out


def _fill_derived(cfg: EarnConfig) -> EarnConfig:
    """Fill the deprecated aliases that in-repo callers still read."""
    det = cfg.signals.scanner.detectors
    t = cfg.triggers
    if t.cooldown_hours is None:
        t.cooldown_hours = cfg.signals.planner.cooldown_hours
    if t.max_per_day is None:
        t.max_per_day = cfg.signals.planner.max_per_day
    if t.news_events is None:
        t.news_events = list(det.news_event.events)
    if t.move_4h_pct is None:
        t.move_4h_pct = det.move.pct.get("4h", 5.0)
    if t.funding_abs_8h is None:
        t.funding_abs_8h = det.funding.abs_8h
    for name in ("a", "b"):
        sl = getattr(cfg.sleeves, name)
        if sl.capital_usdt is None:
            sl.capital_usdt = cfg.modes.test.seed_usdt.get(name, 0.0)
    return cfg


def load_config(path: Path | str | None = None, *, root: Path | None = None) -> EarnConfig:
    p = Path(path) if path else DEFAULT_CONFIG
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    raw = _strip_legacy(raw, str(p))
    try:
        cfg = EarnConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"earn.yaml {_first_error(e)}") from e
    cfg = _fill_derived(cfg)
    _cross_validate(cfg, root)
    return cfg


def config_schema() -> dict[str, Any]:
    """The JSON Schema the console generates its config forms from."""
    return EarnConfig.model_json_schema(by_alias=True)


__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_CONFIG",
    "REPO_ROOT",
    "ConfigError",
    "ConfigWarning",
    "EarnConfig",
    "TradingDefaults",
    "TradingOverrides",
    "TradingSleeves",
    "config_schema",
    "load_config",
    "max_weight_for",
    "seed_for",
    "slots_for",
    "stage_prompt",
    "startup_candles",
    "stoploss_on_exchange",
    "trading_for",
]
