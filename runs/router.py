"""Model routing (spec §3a): per-task defaults from config/models.yaml merged with
the tier-1 overlay config/models-auto.yaml (auto-shadow / auto-promotion writes),
the six hard-case escalation flags, the code-enforced reasoning-effort floor
("high"; decide/review run at "max"), rate-limit-aware brief throttling under the
Claude Max subscription (decide runs are never skipped), and the 30-day shadow
window."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import flags as flagslib

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_FLOOR = "high"   # code-enforced: no config or overlay can go below this
OVERLAY_PATH = REPO_ROOT / "config" / "models-auto.yaml"


@dataclass(frozen=True)
class ModelChoice:
    task: str
    model: str                 # exact pinned string
    escalated: bool
    escalation_reasons: list[str]
    fallback: str | None
    retry: int
    max_usd: float
    max_turns: int
    effort: str = EFFORT_FLOOR


@dataclass(frozen=True)
class HardCaseFlags:
    regime_change_48h: bool = False
    module_disagreement: bool = False
    near_stop: bool = False
    regwatch_active: bool = False
    two_abstains: bool = False
    tca_above_threshold: bool = False

    def any(self) -> bool:
        return any(vars(self).values())

    def reasons(self) -> list[str]:
        return [k for k, v in vars(self).items() if v]


def clamp_effort(effort: str | None) -> str:
    """Never below the floor; unknown values fall back to the floor."""
    if effort in EFFORT_ORDER and EFFORT_ORDER.index(effort) >= EFFORT_ORDER.index(EFFORT_FLOOR):
        return effort
    return EFFORT_FLOOR


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_models_cfg(path: Path | None = None,
                    overlay_path: Path | None = None) -> dict:
    """config/models.yaml (tier-2, human base) deep-merged with the tier-1 overlay
    config/models-auto.yaml where auto-shadow windows and auto-promotions live.
    The overlay may add `models:` pins, override `tasks.<t>.model` and own the
    whole `shadow:` block; it can never lower effort below the floor (clamped in
    resolve())."""
    base = yaml.safe_load((path or REPO_ROOT / "config" / "models.yaml").read_text())
    op = overlay_path or (path.parent / "models-auto.yaml" if path else OVERLAY_PATH)
    try:
        overlay = yaml.safe_load(Path(op).read_text()) or {}
    except OSError:
        overlay = {}
    return _deep_merge(base, overlay)


def write_models_overlay(mutate_fn, overlay_path: Path | None = None) -> dict:
    """Atomically read-modify-write config/models-auto.yaml (the ONLY file the
    auto-shadow/auto-promotion machinery edits; models.yaml stays human-only)."""
    from runs.common import atomic_write_text

    op = Path(overlay_path or OVERLAY_PATH)
    try:
        current = yaml.safe_load(op.read_text()) or {}
    except OSError:
        current = {}
    updated = mutate_fn(current) or current
    atomic_write_text(op, yaml.safe_dump(updated, sort_keys=True))
    return updated


def compute_hardcase_flags(cfg: EarnConfig, jdb: sqlite3.Connection,
                           root: Path | None = None,
                           now: datetime | None = None) -> HardCaseFlags:
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)

    regime_change = disagreement = False
    try:
        state = json.loads((root / cfg.paths.state_latest).read_text())
        changed = state.get("portfolio", {}).get("regime_changed_utc")
        if changed:
            dt = datetime.fromisoformat(changed.replace("Z", "+00:00"))
            regime_change = now - dt <= timedelta(hours=48)
        disagreement = bool(state.get("portfolio", {}).get("modules", {}).get("disagreement"))
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    near_stop = False
    try:
        row = jdb.execute(
            "SELECT nav_usdt FROM nav_daily WHERE sleeve='b' ORDER BY date_utc DESC LIMIT 1"
        ).fetchone()
        anchors = {r["key"]: float(r["value"]) for r in jdb.execute(
            "SELECT key, value FROM risk_state WHERE sleeve='b'"
            " AND key IN ('day_anchor_nav','month_anchor_nav')")}
        if row and anchors:
            nav = row["nav_usdt"]
            prox = cfg.escalation.stop_proximity_pct / 100
            if "day_anchor_nav" in anchors and anchors["day_anchor_nav"] > 0:
                near_stop |= nav / anchors["day_anchor_nav"] - 1 <= -(cfg.risk.daily_loss_stop - prox)
            if "month_anchor_nav" in anchors and anchors["month_anchor_nav"] > 0:
                near_stop |= nav / anchors["month_anchor_nav"] - 1 <= -(cfg.risk.monthly_loss_stop - prox)
    except sqlite3.Error:
        pass

    regwatch = False
    try:
        active = flagslib.active_flags(root / cfg.paths.flags_file, now)
        regwatch = any(f.get("set_by") == "reg-watch" for f in active.values())
    except flagslib.FlagsError:
        regwatch = True  # unreadable flags is itself a hard case

    two_abstains = False
    try:
        last2 = jdb.execute(
            "SELECT abstain FROM proposals WHERE shadow=0 AND valid=1"
            " ORDER BY ts_utc DESC LIMIT 2").fetchall()
        two_abstains = len(last2) == 2 and all(r["abstain"] for r in last2)
    except sqlite3.Error:
        pass

    tca_high = False
    try:
        row = jdb.execute(
            "SELECT MAX(total_bps_med) AS m FROM tca_rolling WHERE window='7d'"
            " AND day >= ?", ((now - timedelta(days=2)).strftime("%Y-%m-%d"),)).fetchone()
        tca_high = row is not None and row["m"] is not None and row["m"] > cfg.tca.alert_bps
    except sqlite3.Error:
        pass

    return HardCaseFlags(regime_change, disagreement, near_stop, regwatch,
                         two_abstains, tca_high)


def resolve(task: str, flags: HardCaseFlags | None = None,
            models_cfg: dict | None = None,
            force_escalation: list[str] | None = None) -> ModelChoice:
    mc = models_cfg or load_models_cfg()
    t = mc["tasks"][task]
    models = mc["models"]
    reasons: list[str] = []
    escalated = False
    if task == "decide" and t.get("escalation"):
        if force_escalation:
            escalated = True
            reasons += [f"trigger:{r}" for r in force_escalation]
        if flags and flags.any():
            escalated = True
            reasons += flags.reasons()
    key = t["escalation"] if escalated else t["model"]
    return ModelChoice(
        task=task, model=models[key], escalated=escalated,
        escalation_reasons=reasons,
        fallback=t.get("fallback"), retry=int(t.get("retry", 0)),
        max_usd=float(t["max_usd_per_run"]), max_turns=int(t["max_turns"]),
        effort=clamp_effort(t.get("effort")),
    )


def month_spend(jdb: sqlite3.Connection, month: str, stage: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) AS c FROM runs WHERE started_utc LIKE ?"
    args: list = [month + "%"]
    if stage:
        q += " AND stage = ?"
        args.append(stage)
    return float(jdb.execute(q, args).fetchone()["c"])


def throttle_state(jdb: sqlite3.Connection, models_cfg: dict | None = None,
                   now: datetime | None = None,
                   kdb: sqlite3.Connection | None = None,
                   degrade_at_utilization: float = 0.80) -> dict:
    """Under the Claude Max subscription, USD spend is telemetry — degradation keys
    on the persisted rate-limit signal (ops_state keys written by the runs from
    RateLimitEvent frames). Decide runs are never blocked."""
    del models_cfg  # kept in the signature for callers; USD caps no longer gate
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    total = month_spend(jdb, month)

    throttled = False
    status = utilization = resets_at = None
    if kdb is not None:
        try:
            rows = {r["key"]: r["value"] for r in kdb.execute(
                "SELECT key, value FROM ops_state WHERE key IN"
                " ('rate_limit_status','rate_limit_utilization','rate_limit_resets_at')")}
            status = rows.get("rate_limit_status")
            utilization = float(rows["rate_limit_utilization"]) \
                if rows.get("rate_limit_utilization") else None
            resets_at = rows.get("rate_limit_resets_at")
            in_window = True
            if resets_at:
                try:
                    in_window = datetime.fromisoformat(
                        resets_at.replace("Z", "+00:00")) > now
                except ValueError:
                    in_window = True
            throttled = in_window and (
                status in ("allowed_warning", "rejected")
                or (utilization is not None and utilization >= degrade_at_utilization))
        except sqlite3.Error:
            pass
    return {"brief_throttled": throttled, "decide_blocked": False,
            "month_total_usd": total, "rate_limit_status": status,
            "rate_limit_utilization": utilization}


def shadow_active(models_cfg: dict | None = None, today: date | None = None) -> str | None:
    """The shadow model's pinned string while the 30-day window is open, else None."""
    mc = models_cfg or load_models_cfg()
    sh = mc.get("shadow", {})
    if not sh.get("enabled") or not sh.get("model") or not sh.get("started"):
        return None
    today = today or datetime.now(UTC).date()
    started = date.fromisoformat(str(sh["started"]))
    if 0 <= (today - started).days < int(sh.get("days", 30)):
        return mc["models"][sh["model"]]
    return None
