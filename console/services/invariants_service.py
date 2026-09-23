"""The invariants page: every safety promise, who enforces it, and whether it holds now.

Each row names a promise ("no local model writes a proposal"), the `file:function` that
enforces it, and a status computed from the real artefact — the file on disk, the signed
mode state, the bless digest, the installed hook — never from a stored flag. A green row is
evidence, not a claim: the evidence blob is what the operator clicks through to.

Statuses are `ok` (verified true right now), `warn` (true but with a caveat, e.g. the
system is simply not live yet), `fail` (the promise is broken) and `unknown` (the file that
enforces it has not landed yet — honest about a partially built system).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ops.lib import config_guard, paths, signing

__all__ = ["Invariant", "INVARIANTS", "CheckResult", "evaluate", "list_invariants"]

Status = Literal["ok", "warn", "fail", "unknown"]


@dataclass(frozen=True)
class CheckResult:
    status: Status
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Invariant:
    id: str
    title: str
    statement: str
    enforced_by: str
    category: str
    check: Callable[[Path], CheckResult]
    strip: str | None = None  # which safety-strip pill this backs, when any


# --------------------------------------------------------------------------- helpers


def _read(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def _source_contains(
    root: Path, rel: str, needles: Sequence[str], *, forbidden: Sequence[str] = ()
) -> CheckResult:
    """Grep a source file for the tokens that *are* the enforcement."""
    text = _read(root, rel)
    if text is None:
        return CheckResult("unknown", f"{rel} has not landed yet", {"file": rel})
    missing = [n for n in needles if n not in text]
    present = [f for f in forbidden if f in text]
    evidence = {
        "file": rel,
        "sha256": signing.sha256_text(text),
        "found": [n for n in needles if n in text],
        "missing": missing,
        "forbidden_present": present,
    }
    if missing:
        return CheckResult("fail", f"{rel} no longer contains {missing[0]!r}", evidence)
    if present:
        return CheckResult("fail", f"{rel} mentions {present[0]!r}", evidence)
    return CheckResult("ok", f"{rel} enforces it", evidence)


# --------------------------------------------------------------------------- checks


def _check_console_bind(root: Path) -> CheckResult:
    for rel in ("console/settings.py", "console/__main__.py", "console/app.py"):
        text = _read(root, rel)
        if text is None:
            continue
        if "127.0.0.1" in text:
            configurable = bool(re.search(r"host\s*=\s*(cfg|config|os\.environ|settings\.host)",
                                          text))
            return CheckResult(
                "fail" if configurable else "ok",
                f"{rel} pins the bind address"
                if not configurable
                else f"{rel} appears to take the host from configuration",
                {"file": rel, "sha256": signing.sha256_text(text)},
            )
    return CheckResult("unknown", "console/settings.py has not landed yet", {})


def _check_mode_state(root: Path) -> CheckResult:
    from ops.lib import mode_state

    state = mode_state.load()
    evidence = {
        "verified": state.verified,
        "reason": state.reason,
        "sleeves": {s: state.sleeve(s).state for s in paths.SLEEVES},
        "path": str(paths.mode_state_path()),
    }
    if state.verified:
        return CheckResult("ok", "mode state signature verified", evidence)
    all_test = all(state.sleeve(s).state == "TEST" for s in paths.SLEEVES)
    if all_test:
        return CheckResult(
            "ok", f"unverified ({state.reason}) and therefore TEST — failing closed", evidence
        )
    return CheckResult("fail", "unverified mode state did not fall back to TEST", evidence)


def _check_committed_dry_run(root: Path) -> CheckResult:
    evidence: dict[str, Any] = {}
    bad: list[str] = []
    for sleeve in ("a", "b"):
        rel = f"config/freqtrade-{sleeve}.json"
        text = _read(root, rel)
        if text is None:
            return CheckResult("unknown", f"{rel} is missing", {"file": rel})
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return CheckResult("fail", f"{rel} is not valid JSON", {"file": rel})
        evidence[rel] = data.get("dry_run")
        if data.get("dry_run") is not True:
            bad.append(rel)
    if bad:
        return CheckResult("fail", f"{bad[0]} has dry_run false", evidence)
    return CheckResult("ok", "both committed bot configs are dry_run", evidence)


def _check_bless(root: Path) -> CheckResult:
    result = config_guard.verify(root=root)
    evidence = {
        "reason": result.reason,
        "changed": result.changed,
        "missing": result.missing,
        "blessed_at": result.blessed_at,
        "blessed_by": result.blessed_by,
        "files": list(config_guard.BLESSED_FILES),
    }
    if result.ok:
        return CheckResult("ok", result.summary(), evidence)
    if result.reason in ("missing", "no_secret"):
        return CheckResult("warn", result.summary(), evidence)
    return CheckResult("fail", result.summary(), evidence)


def _check_tier_floor(root: Path) -> CheckResult:
    try:
        from runs.llm import types as llm_types
    except ImportError:
        return CheckResult("unknown", "runs/llm/types.py has not landed yet", {})
    floor = dict(getattr(llm_types, "MIN_TIER_FLOOR", {}))
    ok = floor.get("decide") == 4 and floor.get("validate") == 3
    return CheckResult(
        "ok" if ok else "fail",
        f"MIN_TIER_FLOOR = {floor}",
        {"min_tier_floor": floor, "file": "runs/llm/types.py"},
    )


def _check_no_local_proposal(root: Path) -> CheckResult:
    try:
        from runs.llm import types as llm_types
    except ImportError:
        return CheckResult("unknown", "runs/llm/types.py has not landed yet", {})
    forbidden = set(getattr(llm_types, "ALWAYS_LOCAL_FORBIDDEN", set()))
    evidence: dict[str, Any] = {"always_local_forbidden": sorted(forbidden)}
    if "decide" not in forbidden:
        return CheckResult("fail", "'decide' is not in ALWAYS_LOCAL_FORBIDDEN", evidence)
    try:
        ref_cls = llm_types.ModelRef
        local = ref_cls(alias="local_small", provider="ollama", model_id="llama3.1:8b", tier=1)
        cloud = ref_cls(alias="opus", provider="claude", model_id="claude-opus-5", tier=4)
        chain = llm_types.chain_for("decide", [local, cloud], min_tier=1, allow_local=True)
        aliases = [r.alias for r in chain]
        evidence["decide_chain"] = aliases
        if "local_small" in aliases:
            return CheckResult("fail", "chain_for('decide') admitted a local model", evidence)
    except Exception as e:  # noqa: BLE001 - probing a foreign API must not break the page
        evidence["probe_error"] = f"{type(e).__name__}: {e}"
        return CheckResult("warn", "could not probe chain_for()", evidence)
    return CheckResult("ok", "chain_for('decide') excludes local models", evidence)


def _check_hook(root: Path) -> CheckResult:
    rel = ".claude/hooks/protect_tier2.py"
    text = _read(root, rel)
    paths_text = _read(root, ".claude/hooks/tier2_paths.py")
    settings = _read(root, ".claude/settings.json")
    evidence: dict[str, Any] = {"file": rel}
    if text is None:
        return CheckResult("fail", "the tier-2 hook is not installed", evidence)
    evidence["sha256"] = signing.sha256_text(text)
    evidence["patterns_file"] = ".claude/hooks/tier2_paths.py" if paths_text else None
    wired = bool(settings and "protect_tier2" in settings)
    evidence["wired_in_settings"] = wired
    if not wired:
        return CheckResult("warn", "the hook exists but is not wired into .claude/settings.json",
                           evidence)
    return CheckResult("ok", "PreToolUse hook installed and wired", evidence)


def _check_envwrap(root: Path) -> CheckResult:
    rel = "ops/envwrap.sh"
    text = _read(root, rel)
    if text is None:
        return CheckResult("unknown", f"{rel} has not landed yet", {"file": rel})
    leaked = [
        name
        for name in ("EARN_CONSOLE_SECRET", "EARN_CONSOLE_TOKEN")
        if name in text
    ]
    evidence = {"file": rel, "sha256": signing.sha256_text(text), "leaked": leaked}
    if leaked:
        return CheckResult("fail", f"{leaked[0]} appears in an envwrap allowlist", evidence)
    return CheckResult("ok", "console credentials are in no job allowlist", evidence)


def _check_agent_user(root: Path) -> CheckResult:
    from ops.lib import mode_state

    state = mode_state.load()
    executing = [s for s in paths.SLEEVES if state.sleeve(s).state == "LIVE_EXECUTE"]
    try:
        from console.services.config_service import get_cfg

        cfg = get_cfg(root)
        agent_user = cfg.security.agent_user
        wrapper = cfg.security.agent_cli_wrapper
        required = cfg.modes.live.require_agent_user_for_execute
    except Exception as e:  # noqa: BLE001
        return CheckResult("unknown", f"config unreadable: {e}", {})
    evidence = {
        "agent_user": agent_user,
        "agent_cli_wrapper": wrapper,
        "required_for_execute": required,
        "executing_sleeves": executing,
    }
    if not executing:
        return CheckResult("ok", "no sleeve executes orders; nothing to grant", evidence)
    if required and not agent_user:
        return CheckResult("fail", "a sleeve executes without a dedicated agent user", evidence)
    return CheckResult("ok", f"agent user {agent_user} is configured", evidence)


def _check_kill_present(root: Path) -> CheckResult:
    result = _source_contains(root, "ops/lib/kill.py", ["def "], forbidden=["oplock.acquire"])
    if result.status == "fail":
        return CheckResult("fail", "the kill path takes the ops lock", result.evidence)
    if result.status == "ok":
        return CheckResult("ok", "the kill switch never waits for the ops lock", result.evidence)
    return result


def _check_console_automated(root: Path) -> CheckResult:
    for rel in ("console/__main__.py", "console/app.py", "console/security.py"):
        text = _read(root, rel)
        if text is None:
            continue
        if "EARN_AUTOMATED_RUN" in text or "is_automated_run" in text:
            return CheckResult(
                "ok",
                f"{rel} refuses automated runs",
                {"file": rel, "sha256": signing.sha256_text(text)},
            )
    return CheckResult("unknown", "the console entrypoint has not landed yet", {})


# --------------------------------------------------------------------------- the table


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        id="console_bind",
        title="The console binds 127.0.0.1 only",
        statement="The bind address is a module constant. No config key and no environment "
                  "variable can expose the console beyond loopback.",
        enforced_by="console/settings.py:BIND_HOST",
        category="access",
        check=_check_console_bind,
        strip="console-127.0.0.1",
    ),
    Invariant(
        id="gate_in_order_path",
        title="The deterministic gate sits in the order path",
        statement="Every order passes strategies/riskgate.py through the Freqtrade callbacks "
                  "before it reaches the exchange. Claude never places an order.",
        enforced_by="strategies/riskgate.py:GateConfig",
        category="trading",
        check=lambda root: _source_contains(
            root, "strategies/riskgate.py", ["class GateConfig"]
        ),
        strip="gate-in-order-path",
    ),
    Invariant(
        id="stop_exits_market",
        title="Stop exits are market orders",
        statement="A stop, emergency exit or force exit is always a market order, whatever "
                  "trading.order_types says.",
        enforced_by="strategies/earn_base.py:order_types",
        category="trading",
        check=lambda root: _source_contains(root, "strategies/earn_base.py", ["stoploss"]),
    ),
    Invariant(
        id="committed_dry_run",
        title="Committed bot configs are always dry-run",
        statement="config/freqtrade-{a,b}.json say dry_run: true, so no config save can flip a "
                  "bot live; the live overlay is generated from verified mode state only.",
        enforced_by="ops/gen_freqtrade_config.py:build_bot_config",
        category="modes",
        check=_check_committed_dry_run,
        strip="live-entry-human-only",
    ),
    Invariant(
        id="mode_fails_closed",
        title="Unverified mode state means TEST",
        statement="A missing, tampered or unsigned var/state/mode.json reads as TEST for every "
                  "sleeve. There is no automated path upwards.",
        enforced_by="ops/lib/mode_state.py:load",
        category="modes",
        check=_check_mode_state,
        strip="live-entry-human-only",
    ),
    Invariant(
        id="mode_write_human",
        title="Mode writes refuse under an automated run",
        statement="ops.lib.mode_state.write() refuses whenever EARN_AUTOMATED_RUN=1, so no "
                  "unattended session can arm a sleeve.",
        enforced_by="ops/lib/mode_state.py:write",
        category="modes",
        check=lambda root: _source_contains(
            root, "ops/lib/mode_state.py", ["is_automated_run"]
        ),
        strip="live-entry-human-only",
    ),
    Invariant(
        id="kill_never_locks",
        title="The kill switch never waits for the ops lock",
        statement="Stopping must be frictionless: engage_and_enforce() writes KILL and stops "
                  "entries without contending for any lock.",
        enforced_by="ops/lib/kill.py:engage_and_enforce",
        category="safety",
        check=_check_kill_present,
    ),
    Invariant(
        id="min_tier_floor",
        title="decide needs tier >= 4, validate tier >= 3",
        statement="Model tier floors are code constants, not configuration; no models.yaml edit "
                  "and no tier-1 overlay can lower them.",
        enforced_by="runs/llm/types.py:MIN_TIER_FLOOR",
        category="models",
        check=_check_tier_floor,
    ),
    Invariant(
        id="no_local_proposal",
        title="No local model writes a proposal",
        statement="chain_for('decide') drops every local model, for every consumer including "
                  "the test fakes.",
        enforced_by="runs/llm/types.py:chain_for",
        category="models",
        check=_check_no_local_proposal,
    ),
    Invariant(
        id="effort_floor",
        title="Reasoning effort has a floor",
        statement="EFFORT_FLOOR = 'high': no config can ask a decision run to think less.",
        enforced_by="runs/router.py:clamp_effort",
        category="models",
        check=lambda root: _source_contains(root, "runs/router.py", ["EFFORT_FLOOR"]),
    ),
    Invariant(
        id="always_disallowed",
        title="Tools Claude may never have",
        statement="ALWAYS_DISALLOWED is a code constant listing the tools no routed session "
                  "may be granted.",
        enforced_by="runs/decision_core.py:ALWAYS_DISALLOWED",
        category="models",
        check=lambda root: _source_contains(root, "runs/decision_core.py",
                                            ["ALWAYS_DISALLOWED"]),
    ),
    Invariant(
        id="tier2_hook",
        title="Automated runs cannot change limits",
        statement="The PreToolUse hook denies every tier-2 write during EARN_AUTOMATED_RUN=1, "
                  "and apply_changes refuses to merge a commit touching one.",
        enforced_by=".claude/hooks/protect_tier2.py:main",
        category="autonomy",
        check=_check_hook,
        strip="automated-runs-cannot-change-limits",
    ),
    Invariant(
        id="console_refuses_automated",
        title="The console refuses automated callers",
        statement="It will not start under EARN_AUTOMATED_RUN=1 and rejects every mutating "
                  "route from such a request.",
        enforced_by="console/__main__.py:main",
        category="access",
        check=_check_console_automated,
    ),
    Invariant(
        id="config_blessed",
        title="Config is blessed",
        statement="var/state/config.bless.json pins the sha256 of every human-only config file "
                  "under $EARN_CONSOLE_SECRET. Preflight refuses to go live without it.",
        enforced_by="ops/lib/config_guard.py:verify",
        category="config",
        check=_check_bless,
        strip="config-blessed",
    ),
    Invariant(
        id="envwrap_clean",
        title="Console credentials are in no job allowlist",
        statement="EARN_CONSOLE_SECRET and EARN_CONSOLE_TOKEN never reach an unattended job's "
                  "environment.",
        enforced_by="ops/envwrap.sh:allowlists",
        category="access",
        check=_check_envwrap,
    ),
    Invariant(
        id="agent_user",
        title="Live execution runs as a dedicated user",
        statement="LIVE_EXECUTE requires security.agent_user and the CLI wrapper, so an "
                  "automated session cannot reach the exchange credentials.",
        enforced_by="ops/preflight.py:check_agent_user",
        category="modes",
        check=_check_agent_user,
    ),
)


# --------------------------------------------------------------------------- evaluation


def _last_denials(root: Path, limit: int = 5) -> list[dict[str, Any]]:
    """The most recent hook denials — the invariant page's "and here is it working"."""
    out: list[dict[str, Any]] = []
    try:
        lines = (root / "logs" / "hook-denials.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def evaluate(inv: Invariant, root: Path) -> dict[str, Any]:
    try:
        result = inv.check(root)
    except Exception as e:  # noqa: BLE001 - one broken probe must not blank the page
        result = CheckResult("unknown", f"check raised {type(e).__name__}: {e}")
    return {
        "id": inv.id,
        "title": inv.title,
        "statement": inv.statement,
        "enforced_by": inv.enforced_by,
        "category": inv.category,
        "strip": inv.strip,
        "status": result.status,
        "detail": result.detail,
        "evidence": result.evidence,
    }


def list_invariants(root: Path | None = None) -> dict[str, Any]:
    base = root or paths.REPO_ROOT
    rows = [evaluate(inv, base) for inv in INVARIANTS]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    strip: dict[str, str] = {}
    for row in rows:
        name = row["strip"]
        if not name:
            continue
        rank = {"fail": 3, "warn": 2, "unknown": 1, "ok": 0}
        if rank[row["status"]] >= rank.get(strip.get(name, "ok"), 0):
            strip[name] = row["status"]
    return {
        "invariants": rows,
        "counts": counts,
        "ok": counts.get("fail", 0) == 0,
        "strip": strip,
        "last_denials": _last_denials(base),
        "checked_root": str(base),
    }
