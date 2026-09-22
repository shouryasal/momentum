"""Model routing (spec §3a): per-task defaults from config/models.yaml, the six
hard-case escalation flags, monthly budget tracking with the 80% brief throttle
(decide runs are never skipped for budget), and the 30-day shadow window."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import flags as flagslib


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


def load_models_cfg(path: Path | None = None) -> dict:
    return yaml.safe_load((path or REPO_ROOT / "config" / "models.yaml").read_text())


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
            models_cfg: dict | None = None) -> ModelChoice:
    mc = models_cfg or load_models_cfg()
    t = mc["tasks"][task]
    models = mc["models"]
    escalated = bool(task == "decide" and flags and flags.any() and t.get("escalation"))
    key = t["escalation"] if escalated else t["model"]
    return ModelChoice(
        task=task, model=models[key], escalated=escalated,
        escalation_reasons=flags.reasons() if (escalated and flags) else [],
        fallback=t.get("fallback"), retry=int(t.get("retry", 0)),
        max_usd=float(t["max_usd_per_run"]), max_turns=int(t["max_turns"]),
    )


def month_spend(jdb: sqlite3.Connection, month: str, stage: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(cost_usd),0) AS c FROM runs WHERE started_utc LIKE ?"
    args: list = [month + "%"]
    if stage:
        q += " AND stage = ?"
        args.append(stage)
    return float(jdb.execute(q, args).fetchone()["c"])


def throttle_state(jdb: sqlite3.Connection, models_cfg: dict | None = None,
                   now: datetime | None = None) -> dict:
    mc = models_cfg or load_models_cfg()
    now = now or datetime.now(UTC)
    month = now.strftime("%Y-%m")
    pct = mc["budget"]["throttle_at_pct"] / 100
    total = month_spend(jdb, month)
    brief = month_spend(jdb, month, "brief")
    throttled = (total >= pct * mc["budget"]["monthly_total_usd"]
                 or brief >= pct * mc["tasks"]["brief"]["monthly_budget_usd"])
    return {"brief_throttled": throttled, "decide_blocked": False,
            "month_total_usd": total}


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
