"""EarnConfig: the typed loader for config/earn.yaml.

Every host-side job loads configuration through ``load_config()`` and nothing else.
``extra="forbid"`` everywhere: an unknown key in earn.yaml is a hard error, so a typo
can never silently disable a limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_Model):
    config_version: int
    display_timezone: str


class Autonomy(_Model):
    tier1_auto_merge: bool


class Universe(_Model):
    quote: str
    assets: list[str]
    pairs: list[str]
    data_only_symbols: list[str] = []


class Exchange(_Model):
    name: str
    fee_bps_assumed: float


class Sleeve(_Model):
    label: str
    strategy: str
    capital_usdt: float


class BenchmarkSleeve(_Model):
    label: str
    pair: str


class Sleeves(_Model):
    a: Sleeve
    b: Sleeve
    benchmark: BenchmarkSleeve


class StoplossGuard(_Model):
    count: int
    window_hours: int
    lock_hours: int


class Blackout(_Model):
    macro_events: list[str]
    window_minutes: int


class Risk(_Model):
    max_weight: dict[str, float]
    max_gross_exposure: float
    usdt_floor: float
    daily_loss_stop: float
    daily_stop_lock_hours: int
    monthly_loss_stop: float
    max_trades_per_day: int
    stoploss_guard: StoplossGuard
    cooldown_candles: int
    staleness_minutes: int
    min_notional_usdt: float
    stoploss_per_trade: float
    blackout: Blackout
    kill_file: str


class ProposalCfg(_Model):
    schema_path: str = Field(alias="schema")
    max_age_hours: int
    sum_tolerance: float


class Execution(_Model):
    entry_unfilled_timeout_min: int
    exit_unfilled_timeout_min: int
    exit_timeout_count: int
    cross_ticks_buffer: float
    rebalance_band: float
    dust_weight: float


class Trend(_Model):
    ma_days: int
    hysteresis_pct: float


class Vol(_Model):
    target_annual: float
    lookback_days: int


class Dca(_Model):
    interval_days: int
    chunk_pct_nav: float


class SleeveAParams(_Model):
    trend: Trend
    vol: Vol
    base_weights: dict[str, float]
    dca: Dca


class SleeveBParams(_Model):
    drift_to_a_after_h: int


class Paper(_Model):
    start_date: str


class Bound(_Model):
    min: float
    max: float
    max_step: float


class Escalation(_Model):
    stop_proximity_pct: float


class Budgets(_Model):
    run_usd: dict[str, float]
    monthly_usd: float
    context_tokens: dict[str, int]
    degrade_at_pct: int


class Tca(_Model):
    freeze_ratio: float
    freeze_days: int
    calibration_min_fills: int
    quote_fallback_max_s: int
    alert_bps: float


class NewsClassify(_Model):
    enabled: bool
    model: str
    monthly_budget_usd: float


class Feed(_Model):
    name: str
    url: str
    class_: Literal["primary", "secondary"] = Field(alias="class")


class News(_Model):
    window_hours: int
    min_sources: int
    classify: NewsClassify
    whitelist: list[Feed]


class Telegram(_Model):
    chat_id: int
    user_id: int
    dedupe_ttl_min: int


class BotEndpoint(_Model):
    service: str
    api: str
    password_env: str


class Schedule(_Model):
    cron: str
    deadline_s: int
    artifact: str


class OpsCfg(_Model):
    staleness_min: int
    missed_run_grace_min: int
    container_heartbeat_min: int
    max_restarts_per_hour: int
    summary_time_local: str
    bots: dict[Literal["a", "b"], BotEndpoint]
    schedules: dict[str, Schedule]


class Backup(_Model):
    dest: str
    keep_daily: int
    keep_weekly: int


class Replay(_Model):
    days: int
    min_snapshots: int
    runs_per_snapshot: int
    budget_usd: float
    target_tolerance: float
    determinism_min_identical: float
    turnover_max_ratio: float


class ChangeGates(_Model):
    backtest_min_years: int
    walk_forward_min_out_sample_delta: float
    max_param_changes_per_month: int


class Fewshot(_Model):
    best: int
    worst: int
    refresh: str


class Review(_Model):
    model: str
    fallback_model: str
    max_turns: int
    max_budget_usd: float
    replay: Replay
    change_gates: ChangeGates
    outcome_stats_min_decisions: int
    lessons_reconfirm_days: int
    recurring_cause_weeks: int
    fewshot: Fewshot


class Paths(_Model):
    knowledge_db: str
    journal_db: str
    flags_file: str
    state_latest: str
    proposals_dir: str
    data_dir: str
    fewshot: str
    params_a: str
    params_b: str


class EarnConfig(_Model):
    meta: Meta
    phase: Literal["paper", "live_propose", "live_execute"]
    autonomy: Autonomy
    universe: Universe
    exchange: Exchange
    sleeves: Sleeves
    risk: Risk
    proposal: ProposalCfg
    execution: Execution
    sleeve_a: SleeveAParams
    sleeve_b: SleeveBParams
    paper: Paper
    bounds: dict[str, Bound]
    escalation: Escalation
    budgets: Budgets
    tca: Tca
    news: News
    telegram: Telegram
    ops: OpsCfg
    backup: Backup
    review: Review
    models_config: str
    paths: Paths


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "earn.yaml"


def _cross_validate(cfg: EarnConfig) -> None:
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


def load_config(path: Path | str | None = None) -> EarnConfig:
    p = Path(path) if path else DEFAULT_CONFIG
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    try:
        cfg = EarnConfig.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise ConfigError(f"earn.yaml invalid at '{loc}': {first['msg']}") from e
    _cross_validate(cfg)
    return cfg


def max_weight_for(cfg: EarnConfig, asset: str) -> float:
    return cfg.risk.max_weight.get(asset, cfg.risk.max_weight["default"])
