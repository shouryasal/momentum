"""The 13-item live preflight (spec §8.3).

A preflight is a *snapshot of evidence*, not an opinion: every item names what it looked
at and what it saw, so the Mode page can show the operator the same facts the transition
will re-check ten minutes later. :func:`run_preflight` never raises — a probe that blows
up becomes a failing item with the exception text, because a check that could not be
completed must never read as a pass.

``blocking`` items refuse the transition. Non-blocking items are advisory and are shown in
amber. Exactly one item (``track_record``) can be overridden, and only with a typed reason
that is journalled on the transition and kept on the run forever.

Every external probe is injected through :class:`PreflightDeps`, so the whole file is
exercised in tests with fake bots, a fake exchange and a fake host: nothing in the test
suite opens a socket or shells out.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import binance_check, config_guard, paths, signing
from ops.lib import flags as flagslib
from ops.lib import kill as killlib
from ops.lib import mode_state as ms

PREFLIGHT_TTL_MINUTES = 10
BACKUP_MAX_AGE_HOURS = 26
MIN_SEED_NOTIONAL_MULTIPLE = 4
MIN_APPROVAL_RATE = 0.80
NTP_SKEW_MAX_S = 2.0

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

#: the only item a human may override, and only with a typed reason
OVERRIDABLE: frozenset[str] = frozenset({"track_record"})

LIVE_TARGETS: frozenset[str] = frozenset({"LIVE_PROPOSE", "LIVE_EXECUTE"})

#: item id -> (title, blocking) in the order the UI shows them
CHECK_ORDER: tuple[tuple[str, str, bool], ...] = (
    ("kill_clear", "Kill switch clear, no blocking flags, data fresh", True),
    ("bots_healthy", "Both bots healthy on a real strategy, gate active", True),
    ("config_blessed", "Config blessed, git clean, generated files in sync", True),
    ("exchange_keys", "Exchange key present, spot-only, account not already live", True),
    ("seed_ok", "Seed within ceiling and covered by free balance", True),
    ("track_record", "Test track record and breach-free streak", True),
    ("telegram", "Telegram reachable (approvals and alerts depend on it)", True),
    ("stoploss_on_exchange", "Exchange-side stops supported and enabled", True),
    ("host_ready", "Host cannot sleep, docker up, clock and cron sane", True),
    ("automation_isolation", "Automation cannot reach console or approval secrets", True),
    ("backups", "Recent backup and a writable destination", False),
    ("strategy_tests", "strategies test suite green", False),
    ("propose_track_record", "Time in LIVE_PROPOSE with a high approval rate", True),
)


class PreflightError(Exception):
    pass


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Check:
    """One preflight item and the evidence behind it."""

    id: str
    title: str
    blocking: bool
    status: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    overridden: bool = False

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "blocking": self.blocking,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
            "overridden": self.overridden,
        }


@dataclass(frozen=True)
class PreflightRequest:
    """What the operator asked for. ``target`` is a mode-state name."""

    sleeve: str
    target: str
    submode: str | None = None
    seed_usdt: float = 0.0
    override_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.target in LIVE_TARGETS

    @property
    def is_execute(self) -> bool:
        return self.target == "LIVE_EXECUTE"

    def to_json(self) -> dict[str, Any]:
        return {
            "sleeve": self.sleeve,
            "target": self.target,
            "submode": self.submode,
            "seed_usdt": self.seed_usdt,
            "override_reason": self.override_reason,
        }


@dataclass(frozen=True)
class PreflightResult:
    preflight_id: str
    request: PreflightRequest
    items: list[Check]
    created_utc: str
    expires_utc: str
    baseline: dict[str, float] = field(default_factory=dict)

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.items if c.blocking and c.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.blocking_failures

    def item(self, check_id: str) -> Check | None:
        return next((c for c in self.items if c.id == check_id), None)

    def expired(self, now: datetime | None = None) -> bool:
        return _parse(self.expires_utc) <= (now or datetime.now(UTC))

    def to_json(self) -> dict[str, Any]:
        return {
            "preflight_id": self.preflight_id,
            "request": self.request.to_json(),
            "created_utc": self.created_utc,
            "expires_utc": self.expires_utc,
            "ok": self.ok,
            "baseline": self.baseline,
            "items": [c.to_json() for c in self.items],
        }


# --------------------------------------------------------------------------- deps


@dataclass
class HostFacts:
    """What the host probes report. ``None`` means "could not determine"."""

    filesystem: str | None = None
    sleep_on_ac_disabled: bool | None = None
    keepalive_task: bool | None = None
    docker_running: bool | None = None
    ntp_skew_s: float | None = None
    cron_installed: bool | None = None
    cron_matches: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PreflightDeps:
    """Every probe the preflight needs, injected. ``None`` = "not available here"."""

    jdb: sqlite3.Connection | None = None
    kdb: sqlite3.Connection | None = None
    root: Path = REPO_ROOT
    env: Mapping[str, str] | None = None
    state: ms.ModeState | None = None
    now: datetime | None = None

    bot_status: Callable[[str], dict[str, Any]] | None = None
    exchange_account: Callable[[str], dict[str, Any]] | None = None
    exchange_restrictions: Callable[[str], dict[str, Any]] | None = None
    exchange_info: Callable[[Sequence[str]], dict[str, Any]] | None = None
    host_facts: Callable[[], HostFacts] | None = None
    git_status: Callable[[], dict[str, Any]] | None = None
    drift_check: Callable[[], tuple[bool, str]] | None = None
    telegram_probe: Callable[[], tuple[bool, str]] | None = None
    strategy_tests: Callable[[], tuple[bool, str]] | None = None
    backup_status: Callable[[], dict[str, Any]] | None = None
    envwrap_allowlist: Callable[[], str] | None = None
    agent_user_ok: Callable[[], tuple[bool, str]] | None = None

    def clock(self) -> datetime:
        return self.now or datetime.now(UTC)


# --------------------------------------------------------------------------- helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def _verdict(ok: bool, *, blocking: bool = True) -> str:
    return PASS if ok else (FAIL if blocking else WARN)


def new_preflight_id() -> str:
    return f"pf-{signing.new_nonce(8)}"


def _applies(check_id: str, request: PreflightRequest) -> bool:
    """Disarming (back to TEST) only needs the safety items, not the go-live gauntlet."""
    if check_id == "propose_track_record":
        return request.is_execute
    if request.is_live:
        return True
    return check_id in {"kill_clear", "bots_healthy", "config_blessed"}


# --------------------------------------------------------------------------- checks


def _check_kill_clear(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}

    engaged = killlib.is_engaged(cfg, d.root)
    evidence["kill_engaged"] = engaged
    if engaged:
        problems.append(f"KILL engaged: {killlib.reason(cfg, d.root) or 'no reason recorded'}")

    flags_path = d.root / cfg.paths.flags_file
    blocked, why = flagslib.entries_blocked(flags_path, "ALL", d.clock())
    evidence["entries_blocked"] = blocked
    evidence["flag"] = why
    if blocked:
        problems.append(f"entries blocked by {why}")

    if d.jdb is not None:
        row = d.jdb.execute(
            "SELECT value FROM risk_state WHERE sleeve=? AND key='monthly_locked'",
            (req.sleeve,),
        ).fetchone()
        locked = bool(row and str(row["value"]).lower() in ("1", "true"))
        evidence["monthly_locked"] = locked
        if locked:
            problems.append("sleeve is under a monthly loss lock")

    age = _data_age_minutes(d)
    evidence["data_age_minutes"] = age
    if age is None:
        problems.append("no candle data found")
    elif age > cfg.risk.staleness_minutes:
        problems.append(f"market data is {age:.0f} min old (limit {cfg.risk.staleness_minutes})")

    return Check(
        "kill_clear",
        CHECK_TITLES["kill_clear"],
        True,
        _verdict(not problems),
        "; ".join(problems) or "kill clear, no blocking flags, data fresh",
        evidence,
    )


def _data_age_minutes(d: PreflightDeps) -> float | None:
    if d.kdb is None:
        return None
    try:
        row = d.kdb.execute("SELECT MAX(close_time) AS t FROM candles").fetchone()
    except sqlite3.Error:
        return None
    if row is None or row["t"] is None:
        return None
    newest = datetime.fromtimestamp(float(row["t"]) / 1000, tz=UTC)
    return max(0.0, (d.clock() - newest).total_seconds() / 60)


def _check_bots_healthy(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    if d.bot_status is None:
        return Check(
            "bots_healthy", CHECK_TITLES["bots_healthy"], True, FAIL,
            "no bot probe available", evidence,
        )
    for sleeve in paths.SLEEVES:
        try:
            info = d.bot_status(sleeve) or {}
        except Exception as e:  # noqa: BLE001 - a probe failure is a failing check
            problems.append(f"bot {sleeve}: {e}")
            continue
        evidence[sleeve] = info
        if not info.get("up"):
            problems.append(f"bot {sleeve} is down")
            continue
        strategy = str(info.get("strategy") or "")
        expected = getattr(cfg.sleeves, sleeve).strategy
        if strategy == "Scaffold" or not strategy:
            problems.append(f"bot {sleeve} runs {strategy or 'no'} strategy")
        elif strategy != expected:
            problems.append(f"bot {sleeve} runs {strategy}, config says {expected}")

    gate_age_h = _hours_since_last_gate_decision(d, req.sleeve)
    evidence["gate_decision_age_hours"] = gate_age_h
    if gate_age_h is None:
        problems.append("no gate decision journalled yet")
    elif gate_age_h > 24:
        problems.append(f"last gate decision was {gate_age_h:.0f} h ago")

    return Check(
        "bots_healthy", CHECK_TITLES["bots_healthy"], True, _verdict(not problems),
        "; ".join(problems) or "both bots healthy, gate active", evidence,
    )


def _hours_since_last_gate_decision(d: PreflightDeps, sleeve: str) -> float | None:
    if d.jdb is None:
        return None
    row = d.jdb.execute(
        "SELECT MAX(ts_utc) AS t FROM gate_decisions WHERE sleeve=?", (sleeve,)
    ).fetchone()
    if row is None or row["t"] is None:
        return None
    return max(0.0, (d.clock() - _parse(row["t"])).total_seconds() / 3600)


def _check_config_blessed(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}

    bless = config_guard.verify(root=d.root)
    evidence["bless"] = {"ok": bless.ok, "reason": bless.reason, "changed": list(bless.changed)}
    if not bless.ok:
        problems.append(f"config not blessed ({bless.reason})")

    if d.git_status is not None:
        try:
            git = d.git_status() or {}
        except Exception as e:  # noqa: BLE001
            git = {"error": str(e)}
        evidence["git"] = git
        if git.get("error"):
            problems.append(f"git status failed: {git['error']}")
        else:
            if git.get("dirty"):
                problems.append("git tree is dirty")
            branch = git.get("branch")
            if branch and branch != cfg.git.live_branch:
                problems.append(f"on branch {branch}, expected {cfg.git.live_branch}")

    if d.drift_check is not None:
        try:
            ok, detail = d.drift_check()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        evidence["drift"] = detail
        if not ok:
            problems.append(f"generated files drifted: {detail}")

    return Check(
        "config_blessed", CHECK_TITLES["config_blessed"], True, _verdict(not problems),
        "; ".join(problems) or "config blessed and generated files in sync", evidence,
    )


def _check_exchange_keys(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    warnings: list[str] = []
    evidence: dict[str, Any] = {}

    keys = binance_check.keys_for(req.sleeve, d.env)
    evidence["keys"] = keys.describe()
    if not keys.present:
        problems.append(f"{keys.key_env}/{keys.secret_env} not set")

    if d.exchange_restrictions is None:
        problems.append("no exchange probe available")
    else:
        try:
            report = binance_check.check_restrictions(d.exchange_restrictions(req.sleeve) or {})
            evidence["permissions"] = report.permissions
            problems.extend(report.blocking)
            warnings.extend(report.warnings)
        except Exception as e:  # noqa: BLE001
            problems.append(f"apiRestrictions probe failed: {e}")

    if cfg.modes.live.one_live_sleeve_per_account:
        other_live = [
            s
            for s in paths.SLEEVES
            if s != req.sleeve and (d.state or ms.load()).is_live(s)
        ]
        evidence["other_live_sleeves"] = other_live
        if other_live and d.exchange_account is not None:
            uids: dict[str, str | None] = {}
            for sleeve in [req.sleeve, *other_live]:
                try:
                    uids[sleeve] = binance_check.account_uid(d.exchange_account(sleeve) or {})
                except Exception as e:  # noqa: BLE001
                    uids[sleeve] = None
                    problems.append(f"account probe for sleeve {sleeve} failed: {e}")
            evidence["account_uids"] = uids
            mine = uids.get(req.sleeve)
            if mine is None:
                problems.append("cannot read this account's UID; refusing to assume it is unique")
            for sleeve in other_live:
                if uids.get(sleeve) is not None and uids[sleeve] == mine:
                    problems.append(
                        f"sleeve {sleeve} is already live on account {mine}"
                        " (modes.live.one_live_sleeve_per_account)"
                    )

    status = _verdict(not problems)
    if status == PASS and warnings:
        status = WARN
    return Check(
        "exchange_keys", CHECK_TITLES["exchange_keys"], True, status,
        "; ".join(problems or warnings) or "spot-only key, account free", evidence,
    )


def _check_seed_ok(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {"seed_usdt": req.seed_usdt}
    ceiling = float(cfg.modes.live.max_seed_usdt.get(req.sleeve, 0.0))
    floor = MIN_SEED_NOTIONAL_MULTIPLE * cfg.risk.min_notional_usdt
    evidence["max_seed_usdt"] = ceiling
    evidence["min_seed_usdt"] = floor

    if req.seed_usdt > ceiling:
        problems.append(f"seed {req.seed_usdt:g} exceeds modes.live.max_seed_usdt {ceiling:g}")
    if req.seed_usdt < floor:
        problems.append(
            f"seed {req.seed_usdt:g} is below {MIN_SEED_NOTIONAL_MULTIPLE} x"
            f" risk.min_notional_usdt ({floor:g})"
        )

    if d.exchange_account is None:
        problems.append("no exchange probe available")
    else:
        try:
            account = d.exchange_account(req.sleeve) or {}
            free = binance_check.free_balance(account, cfg.universe.quote)
            evidence["free_quote"] = free
            if free < req.seed_usdt:
                problems.append(
                    f"free {cfg.universe.quote} {free:g} < seed {req.seed_usdt:g}"
                )
            evidence["baseline"] = binance_check.total_balances(account, cfg.universe.assets)
        except Exception as e:  # noqa: BLE001
            problems.append(f"balance probe failed: {e}")

    return Check(
        "seed_ok", CHECK_TITLES["seed_ok"], True, _verdict(not problems),
        "; ".join(problems) or f"seed {req.seed_usdt:g} funded and within the ceiling", evidence,
    )


def _check_track_record(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    now = d.clock()

    run = active_run(d.jdb, req.sleeve) if d.jdb is not None else None
    days = 0.0
    if run is not None:
        days = max(0.0, (now - _parse(run["started_utc"])).total_seconds() / 86400)
    evidence["test_days"] = round(days, 2)
    evidence["min_test_days"] = cfg.modes.live.min_test_days
    if run is None:
        problems.append("no active test run to measure a track record on")
    elif run["mode"] != "test":
        problems.append(f"active run {run['run_id']} is not a test run")
    elif days < cfg.modes.live.min_test_days:
        problems.append(
            f"{days:.1f} test days < modes.live.min_test_days {cfg.modes.live.min_test_days}"
        )

    breaches = breach_count(d.jdb, req.sleeve, cfg.modes.live.require_zero_breach_days, now)
    evidence["breaches"] = breaches
    evidence["require_zero_breach_days"] = cfg.modes.live.require_zero_breach_days
    if breaches:
        problems.append(
            f"{breaches} gate breach(es) in the last"
            f" {cfg.modes.live.require_zero_breach_days} days"
        )

    if problems and req.override_reason:
        return Check(
            "track_record", CHECK_TITLES["track_record"], True, WARN,
            f"OVERRIDDEN: {req.override_reason} (was: {'; '.join(problems)})",
            evidence, overridden=True,
        )
    return Check(
        "track_record", CHECK_TITLES["track_record"], True, _verdict(not problems),
        "; ".join(problems) or f"{days:.0f} clean test days, no breaches", evidence,
    )


def breach_count(
    conn: sqlite3.Connection | None, sleeve: str, days: int, now: datetime
) -> int:
    if conn is None:
        return 0
    since = _iso(now - timedelta(days=max(0, days)))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM gate_decisions"
        " WHERE sleeve=? AND severity='breach' AND ts_utc >= ?",
        (sleeve, since),
    ).fetchone()
    return int(row["n"] if row else 0)


def _check_telegram(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    evidence: dict[str, Any] = {
        "chat_id_configured": cfg.telegram.chat_id != 0,
        "user_id_configured": cfg.telegram.user_id != 0,
    }
    if cfg.telegram.chat_id == 0 or cfg.telegram.user_id == 0:
        return Check(
            "telegram", CHECK_TITLES["telegram"], True, FAIL,
            "telegram.chat_id/user_id not configured", evidence,
        )
    if d.telegram_probe is None:
        return Check(
            "telegram", CHECK_TITLES["telegram"], True, FAIL,
            "no telegram probe available", evidence,
        )
    try:
        ok, detail = d.telegram_probe()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    evidence["probe"] = detail
    return Check(
        "telegram", CHECK_TITLES["telegram"], True, _verdict(ok),
        detail or ("test message delivered" if ok else "test message not delivered"), evidence,
    )


def _check_stoploss_on_exchange(
    cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps
) -> Check:
    from ops.config import stoploss_on_exchange as resolve_soe

    problems: list[str] = []
    enabled = resolve_soe(cfg, req.sleeve, live=True)
    evidence: dict[str, Any] = {
        "required": cfg.modes.live.require_stoploss_on_exchange,
        "enabled_in_overlay": enabled,
        "pairs": list(cfg.universe.pairs),
    }
    if cfg.modes.live.require_stoploss_on_exchange and not enabled:
        problems.append("trading.*.stoploss.on_exchange resolves to false for a live sleeve")
    if d.exchange_info is None:
        problems.append("no exchange-info probe available")
    else:
        try:
            ok, missing = binance_check.supports_stop_orders(
                d.exchange_info(list(cfg.universe.pairs)) or {}, cfg.universe.pairs
            )
            evidence["unsupported"] = missing
            if not ok:
                problems.append(f"no exchange-side stop for {', '.join(missing)}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"exchangeInfo probe failed: {e}")
    return Check(
        "stoploss_on_exchange", CHECK_TITLES["stoploss_on_exchange"], True,
        _verdict(not problems),
        "; ".join(problems) or "exchange-side stops supported and enabled", evidence,
    )


def _check_host_ready(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    if d.host_facts is None:
        return Check(
            "host_ready", CHECK_TITLES["host_ready"], True, FAIL,
            "no host probe available", {},
        )
    try:
        facts = d.host_facts()
    except Exception as e:  # noqa: BLE001
        return Check(
            "host_ready", CHECK_TITLES["host_ready"], True, FAIL, f"host probe failed: {e}", {}
        )
    problems: list[str] = []
    evidence = facts.to_json()
    if facts.filesystem is not None and facts.filesystem not in ("ext4", "btrfs", "xfs"):
        problems.append(f"repo filesystem is {facts.filesystem}, not a native Linux one")
    if cfg.runtime.require_host_awake_for_live and facts.sleep_on_ac_disabled is not True:
        problems.append("Windows may sleep on AC (powercfg standby-timeout-ac != 0)")
    if cfg.runtime.require_keepalive_task_for_live and facts.keepalive_task is not True:
        problems.append("WSL keep-alive scheduled task is missing")
    if facts.docker_running is not True:
        problems.append("docker is not running")
    if facts.ntp_skew_s is not None and abs(facts.ntp_skew_s) > NTP_SKEW_MAX_S:
        problems.append(f"clock skew {facts.ntp_skew_s:+.1f}s exceeds {NTP_SKEW_MAX_S}s")
    if facts.cron_installed is not True:
        problems.append("crontab is not installed")
    elif facts.cron_matches is not True:
        problems.append("installed crontab differs from the rendered one")
    return Check(
        "host_ready", CHECK_TITLES["host_ready"], True, _verdict(not problems),
        "; ".join(problems) or "host awake, docker up, clock and cron sane", evidence,
    )


def _check_automation_isolation(
    cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps
) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}

    if d.envwrap_allowlist is None:
        problems.append("no envwrap allowlist probe available")
    else:
        try:
            text = d.envwrap_allowlist() or ""
        except Exception as e:  # noqa: BLE001
            text = ""
            problems.append(f"envwrap probe failed: {e}")
        leaked = [
            name
            for name in (signing.SECRET_ENV, "EARN_CONSOLE_TOKEN")
            if name in text
        ]
        evidence["leaked_env"] = leaked
        if leaked:
            problems.append(f"envwrap allowlists expose {', '.join(leaked)}")

    hook = d.root / ".claude" / "hooks" / "protect_tier2.py"
    evidence["hook_installed"] = hook.exists()
    if not hook.exists():
        problems.append("the tier-2 PreToolUse hook is not installed")

    if req.is_execute and cfg.modes.live.require_agent_user_for_execute:
        evidence["agent_user"] = cfg.security.agent_user
        if not cfg.security.agent_user or not cfg.security.agent_cli_wrapper:
            problems.append("security.agent_user / agent_cli_wrapper are required for EXECUTE")
        elif d.agent_user_ok is None:
            problems.append("no agent-user probe available")
        else:
            try:
                ok, detail = d.agent_user_ok()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)
            evidence["agent_user_probe"] = detail
            if not ok:
                problems.append(f"agent user unusable: {detail}")

    return Check(
        "automation_isolation", CHECK_TITLES["automation_isolation"], True,
        _verdict(not problems),
        "; ".join(problems) or "automation is fenced off from console and approval secrets",
        evidence,
    )


def _check_backups(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    if d.backup_status is None:
        return Check(
            "backups", CHECK_TITLES["backups"], False, WARN, "no backup probe available", {}
        )
    try:
        info = d.backup_status() or {}
    except Exception as e:  # noqa: BLE001
        return Check(
            "backups", CHECK_TITLES["backups"], False, WARN, f"backup probe failed: {e}", {}
        )
    age_h = info.get("age_hours")
    writable = info.get("writable")
    if writable is False:
        return Check(
            "backups", CHECK_TITLES["backups"], True, FAIL,
            f"backup destination {info.get('dest', '')} is not writable", dict(info),
        )
    if age_h is None or float(age_h) > BACKUP_MAX_AGE_HOURS:
        return Check(
            "backups", CHECK_TITLES["backups"], False, WARN,
            f"newest backup is {age_h if age_h is not None else 'unknown'} h old", dict(info),
        )
    return Check(
        "backups", CHECK_TITLES["backups"], False, PASS,
        f"backup {float(age_h):.0f} h old, destination writable", dict(info),
    )


def _check_strategy_tests(cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps) -> Check:
    blocking = req.is_execute
    if d.strategy_tests is None:
        return Check(
            "strategy_tests", CHECK_TITLES["strategy_tests"], blocking,
            FAIL if blocking else WARN, "no test runner available", {},
        )
    try:
        ok, detail = d.strategy_tests()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)
    return Check(
        "strategy_tests", CHECK_TITLES["strategy_tests"], blocking,
        _verdict(ok, blocking=blocking), detail or ("green" if ok else "red"), {"output": detail},
    )


def _check_propose_track_record(
    cfg: EarnConfig, req: PreflightRequest, d: PreflightDeps
) -> Check:
    problems: list[str] = []
    evidence: dict[str, Any] = {}
    now = d.clock()
    run = active_run(d.jdb, req.sleeve) if d.jdb is not None else None
    days = 0.0
    if run is not None and run["mode"] == "live" and run["submode"] == "propose":
        days = max(0.0, (now - _parse(run["started_utc"])).total_seconds() / 86400)
    evidence["propose_days"] = round(days, 2)
    evidence["min_propose_days"] = cfg.modes.live.min_propose_days
    if days < cfg.modes.live.min_propose_days:
        problems.append(
            f"{days:.1f} days in LIVE_PROPOSE < modes.live.min_propose_days"
            f" {cfg.modes.live.min_propose_days}"
        )
    rate = approval_rate(d.jdb, since=now - timedelta(days=max(1, cfg.modes.live.min_propose_days)))
    evidence["approval_rate"] = rate
    if rate is None:
        problems.append("no proposal approvals recorded in propose mode")
    elif rate < MIN_APPROVAL_RATE:
        problems.append(f"approval rate {rate:.0%} < {MIN_APPROVAL_RATE:.0%} (G6)")
    detail = "; ".join(problems)
    if not detail:
        detail = f"{days:.0f} propose days, {(rate or 0.0):.0%} approved"
    return Check(
        "propose_track_record", CHECK_TITLES["propose_track_record"], True,
        _verdict(not problems), detail, evidence,
    )


def approval_rate(conn: sqlite3.Connection | None, *, since: datetime) -> float | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT SUM(decision='approve') AS approved, COUNT(*) AS total"
        " FROM proposal_approvals WHERE decided_utc >= ?",
        (_iso(since),),
    ).fetchone()
    if row is None or not row["total"]:
        return None
    return float(row["approved"] or 0) / float(row["total"])


def active_run(conn: sqlite3.Connection | None, sleeve: str) -> sqlite3.Row | None:
    if conn is None:
        return None
    return conn.execute(
        "SELECT * FROM sleeve_runs WHERE sleeve=? AND status='active'"
        " ORDER BY started_utc DESC LIMIT 1",
        (sleeve.lower(),),
    ).fetchone()


CHECK_TITLES: dict[str, str] = {cid: title for cid, title, _ in CHECK_ORDER}
CHECK_BLOCKING: dict[str, bool] = {cid: blocking for cid, _, blocking in CHECK_ORDER}

_CHECKS: dict[str, Callable[[EarnConfig, PreflightRequest, PreflightDeps], Check]] = {
    "kill_clear": _check_kill_clear,
    "bots_healthy": _check_bots_healthy,
    "config_blessed": _check_config_blessed,
    "exchange_keys": _check_exchange_keys,
    "seed_ok": _check_seed_ok,
    "track_record": _check_track_record,
    "telegram": _check_telegram,
    "stoploss_on_exchange": _check_stoploss_on_exchange,
    "host_ready": _check_host_ready,
    "automation_isolation": _check_automation_isolation,
    "backups": _check_backups,
    "strategy_tests": _check_strategy_tests,
    "propose_track_record": _check_propose_track_record,
}


# --------------------------------------------------------------------------- driver


def run_preflight(
    cfg: EarnConfig,
    request: PreflightRequest,
    *,
    deps: PreflightDeps | None = None,
    preflight_id: str | None = None,
) -> PreflightResult:
    """Run every applicable check. Never raises; a broken probe is a failing item."""
    d = deps or PreflightDeps()
    now = d.clock()
    items: list[Check] = []
    for check_id, title, blocking in CHECK_ORDER:
        if not _applies(check_id, request):
            items.append(Check(check_id, title, blocking, SKIP, "not applicable to this transition"))
            continue
        try:
            items.append(_CHECKS[check_id](cfg, request, d))
        except Exception as e:  # noqa: BLE001 - an errored check is never a pass
            items.append(
                Check(check_id, title, blocking, FAIL, f"check errored: {e}", {"error": str(e)})
            )
    baseline = {}
    seed_item = next((c for c in items if c.id == "seed_ok"), None)
    if seed_item is not None:
        baseline = {
            str(k): float(v) for k, v in (seed_item.evidence.get("baseline") or {}).items()
        }
    return PreflightResult(
        preflight_id=preflight_id or new_preflight_id(),
        request=request,
        items=items,
        created_utc=_iso(now),
        expires_utc=_iso(now + timedelta(minutes=PREFLIGHT_TTL_MINUTES)),
        baseline=baseline,
    )


# --------------------------------------------------------------------------- real probes


def default_git_status(root: Path | None = None) -> dict[str, Any]:  # pragma: no cover - shells out
    cwd = str(root or REPO_ROOT)
    try:
        branch = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
            capture_output=True, text=True, timeout=15, check=False,
        )
        status = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain"], cwd=cwd,
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    if branch.returncode or status.returncode:
        return {"error": (branch.stderr or status.stderr).strip()[:200]}
    return {
        "branch": branch.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "changed": [ln[3:] for ln in status.stdout.splitlines()[:20]],
    }


def default_drift_check(root: Path | None = None) -> tuple[bool, str]:  # pragma: no cover
    from ops import gen_freqtrade_config

    try:
        rc = gen_freqtrade_config.main(["--check"])
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    return rc == 0, "in sync" if rc == 0 else "gen_freqtrade_config --check reported drift"


def default_backup_status(cfg: EarnConfig) -> dict[str, Any]:  # pragma: no cover - touches disk
    dest = Path(str(cfg.backup.dest)).expanduser()
    info: dict[str, Any] = {"dest": str(dest)}
    if not dest.exists():
        info["writable"] = False
        return info
    probe = dest / ".earn-write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        info["writable"] = True
    except OSError:
        info["writable"] = False
        return info
    newest = max((p.stat().st_mtime for p in dest.glob("*")), default=None)
    if newest is not None:
        info["age_hours"] = (datetime.now(UTC).timestamp() - newest) / 3600
    return info


def preflight_to_json(result: PreflightResult) -> str:
    return json.dumps(result.to_json(), sort_keys=True)


__all__ = [
    "BACKUP_MAX_AGE_HOURS",
    "CHECK_ORDER",
    "FAIL",
    "MIN_APPROVAL_RATE",
    "MIN_SEED_NOTIONAL_MULTIPLE",
    "OVERRIDABLE",
    "PASS",
    "PREFLIGHT_TTL_MINUTES",
    "SKIP",
    "WARN",
    "Check",
    "HostFacts",
    "PreflightDeps",
    "PreflightError",
    "PreflightRequest",
    "PreflightResult",
    "active_run",
    "approval_rate",
    "breach_count",
    "new_preflight_id",
    "preflight_to_json",
    "run_preflight",
]
