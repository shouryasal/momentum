"""Research-prompt assembly (spec §8): static-first for prompt caching, hard
constraints copied byte-verbatim from earn.yaml, inputs inlined under per-input
token budgets, no recent-trade P&L (anti-anchoring), abstain-by-default contract.

Two entry points:
  gather_inputs(...)  -> the exact input texts used (snapshotted before the model call)
  build(...)          -> BuiltPrompt from a template + inputs (pure; replay re-runs it
                          via build_from_snapshot over the snapshot copies)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from ops.config import REPO_ROOT, EarnConfig
from runs.common import token_estimate

DYNAMIC_MARKER = "<!-- DYNAMIC -->"

# per-input token budgets (spec §8); truncation drops the OLDEST content first
INPUT_BUDGETS = {
    "state": 1000, "brief": 2500, "positions": 1000,
    "graded": 2000, "lessons": 2500, "flags": 500,
    "dossiers": 1500, "event_stats": 600,   # v2 asset intelligence
    "signal": 1500,                          # v3 validated-signal block
    "universe": 1500,                        # v4 point-in-time universe block
}

#: How many NON-quote assets a proposal may name. ``risk.max_open_positions`` is the
#: number the gate enforces; this only ever reads it, never restates it.
DEFAULT_MAX_ASSETS = 8

#: The tier-1 overlay the change gate may write; it may only pin a prompt version.
PROMPTS_OVERLAY = "config/prompts-auto.yaml"
HARD_CAP = 20000
TARGET_CAP = 12000


class PromptBudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class BuiltPrompt:
    text: str
    prompt_version: str
    token_estimate: int
    static_prefix_len: int


def max_assets_for(cfg: EarnConfig) -> int:
    """``risk.max_open_positions`` — how many assets one proposal may name."""
    value = getattr(cfg.risk, "max_open_positions", None)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ASSETS


def limits_yaml(cfg: EarnConfig) -> str:
    """The limits block copied verbatim — tests assert byte-equality with earn.yaml
    values so the prompt can never disagree with the gate.

    Under a wide universe the per-asset ceilings are **per tier**, not a list of names: a
    hundred-entry ``max_weight`` map would be a hundred lines the model has to read to
    learn one rule. The tier of each tradeable asset is in the UNIVERSE block instead.
    """
    block = {
        "universe": {"core": list(getattr(cfg.universe, "core", None) or cfg.universe.assets),
                     "quote": cfg.universe.quote,
                     "tradeable_assets": len(cfg.universe.assets)},
        "max_weight": dict(cfg.risk.max_weight),
        "max_gross_exposure": cfg.risk.max_gross_exposure,
        "usdt_floor": cfg.risk.usdt_floor,
        "daily_loss_stop": cfg.risk.daily_loss_stop,
        "monthly_loss_stop": cfg.risk.monthly_loss_stop,
        "max_trades_per_day": cfg.risk.max_trades_per_day,
        "min_notional_usdt": cfg.risk.min_notional_usdt,
    }
    for key in ("tier_caps", "max_open_positions", "max_satellite_positions",
                "max_satellite_gross", "max_beta_to_btc", "max_avg_pairwise_corr",
                "min_position_pct_nav"):
        value = getattr(cfg.risk, key, None)
        if value is None:
            continue
        block[key] = value.model_dump() if hasattr(value, "model_dump") else value
    return yaml.safe_dump(block, sort_keys=True)


def universe_block(cfg: EarnConfig, held: tuple[str, ...] = ()) -> str:
    """The point-in-time universe the model is deciding against.

    It carries the snapshot identity the proposal must quote back (``universe_snapshot``),
    the tradeable set with each asset's tier and resolved cap, and the size of the wider
    watchlist. The tradeable list is the CONTRACT: an asset that is not in it cannot be
    named, and naming one rejects the whole proposal rather than clamping it.
    """
    from ops.config import max_weight_for

    u = cfg.universe
    core = list(getattr(u, "core", None) or u.assets)
    tier_of = getattr(u, "tier_of", None)
    rows = []
    for asset in u.assets:
        tier = tier_of(asset) if callable(tier_of) else ("core" if asset in core else "major")
        try:
            cap = max_weight_for(cfg, asset, tier)
        except Exception:  # noqa: BLE001 — a prompt must never fail on a config edge
            cap = None
        rows.append({"asset": asset, "tier": tier, "max_weight": cap})
    watchlist = list(getattr(u, "watchlist_pairs", None) or u.pairs)
    payload = {
        "snapshot": getattr(u, "snapshot_ref", None),
        "quote": u.quote,
        "core": core,
        "tradeable": rows,
        "watchlist_pairs": len(watchlist),
        "max_assets_per_proposal": max_assets_for(cfg),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _truncate(text: str, budget_tokens: int) -> str:
    max_chars = int(budget_tokens * 3.5)
    if len(text) <= max_chars:
        return text
    # keep the NEWEST content (inputs are newest-last by construction)
    return "[...truncated...]\n" + text[-max_chars:]


def _forbid_pnl(graded: str) -> str:
    # anti-anchoring: the graded table carries grades, never P&L numbers
    assert "pnl" not in graded.lower() and "p&l" not in graded.lower(), \
        "graded-decisions block must not contain P&L"
    return graded


def prompt_version_for(cfg: EarnConfig, root: Path | None = None) -> str:
    """``research.prompt_version`` with the tier-1 ``config/prompts-auto.yaml`` overlay.

    The overlay may pin only the version (``research: {prompt_version: research.v4}``);
    anything else in it is ignored here, because the file's whole remit is "which prompt
    version is active" and the change gate is what decides whether it may say so.
    """
    root = root or REPO_ROOT
    version = cfg.research.prompt_version
    try:
        overlay = yaml.safe_load((root / PROMPTS_OVERLAY).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return version
    candidate = (overlay.get("research") or {}).get("prompt_version")
    if isinstance(candidate, str) and candidate.startswith("research.v"):
        return candidate
    return version


def signal_block(jdb: sqlite3.Connection, signal_id: str | None) -> str:
    """The VALIDATED SIGNAL block: candidate, features, thesis, counter-evidence.

    Built from the journal, not from anything a model said in passing — the validator's
    row is the record. Missing or unvalidated ⇒ ``""``, which renders as "(none)".
    """
    if not signal_id:
        return ""
    try:
        sig = jdb.execute("SELECT * FROM signals WHERE signal_id=?",
                          (signal_id,)).fetchone()
        val = jdb.execute(
            "SELECT * FROM signal_validations WHERE signal_id=? ORDER BY id DESC LIMIT 1",
            (signal_id,)).fetchone()
    except sqlite3.Error:
        return ""
    if sig is None:
        return ""

    def _j(text, default):
        try:
            return json.loads(text) if text else default
        except (TypeError, json.JSONDecodeError):
            return default

    payload = {
        "signal_id": sig["signal_id"], "detected_at": sig["ts_utc"],
        "detector": sig["detector"], "pair": sig["pair"], "direction": sig["direction"],
        "detector_score": sig["detector_score"], "screen_score": sig["screen_score"],
        "screen_rationale": sig["screen_rationale"], "status": sig["status"],
        "features": _j(sig["features_json"], {}).get("cited", {}),
        "detector_detail": _j(sig["features_json"], {}).get("detail", {}),
    }
    if val is not None:
        payload["validation"] = {
            "verdict": val["verdict"], "confidence": val["confidence"],
            "model": val["model"], "provider": val["provider"],
            "thesis": val["thesis"], "reasons": _j(val["reasons_json"], []),
            "counter_evidence": _j(val["counter_evidence_json"], []),
            "invalidation": val["invalidation"],
            "horizon_hours": val["horizon_hours"],
            "suggested": _j(val["suggested_json"], None),
        }
    return json.dumps(payload, indent=2, sort_keys=True)


def gather_inputs(cfg: EarnConfig, jdb: sqlite3.Connection,
                  root: Path | None = None,
                  signal_id: str | None = None) -> dict[str, str]:
    root = root or REPO_ROOT

    def read(p: Path, default: str) -> str:
        try:
            return p.read_text().strip() or default
        except OSError:
            return default

    state = read(root / cfg.paths.state_latest, "{}")
    from runs.common import gulf_now

    brief = read(root / "knowledge" / "briefs" / f"{gulf_now().strftime('%Y-%m-%d')}.md",
                 "(no brief today)")
    navs = [dict(r) for r in jdb.execute(
        "SELECT date_utc, sleeve, nav_usdt, positions_json FROM nav_daily"
        " WHERE date_utc = (SELECT MAX(date_utc) FROM nav_daily)")]
    graded_rows = jdb.execute(
        "SELECT g.run_id, p.module, p.abstain, g.process_grade, g.outcome_grade"
        " FROM decision_grades g LEFT JOIN proposals p"
        " ON p.run_id = g.run_id AND p.shadow = 0"
        " ORDER BY g.graded_at DESC LIMIT 30").fetchall()
    graded = "\n".join(
        f"- {r['run_id']} module={r['module']} abstain={bool(r['abstain'])}"
        f" process={r['process_grade']} outcome={r['outcome_grade'] or 'unresolved'}"
        for r in graded_rows) or "(no graded decisions yet)"
    lessons = read(root / "lessons.md", "(no lessons yet)")
    flags = read(root / cfg.paths.flags_file, "{}")
    try:
        active = {n: f for n, f in json.loads(flags).get("flags", {}).items()
                  if f.get("active")}
    except json.JSONDecodeError:
        active = {"_unreadable": True}
    # v2 asset intelligence: the dossiers' Summary sections + the event studies.
    # Missing files read as "" so a v1 template (no placeholders) is unaffected.
    #
    # v4: dossiers for CORE plus whatever is actually held, not for every tradeable asset.
    # A dossier is ~300 tokens of prose; fanning out over a 30-name tradeable tier would
    # spend the entire dossier budget on assets the run has no position in and will most
    # likely not open one in. The UNIVERSE block carries the rest as one compact table.
    held = _held_assets(navs)
    core = list(getattr(cfg.universe, "core", None) or cfg.universe.assets)
    wanted = [a for a in (*core, *held) if a in set(cfg.universe.assets) | set(core)]
    seen: list[str] = []
    for asset in wanted:
        if asset not in seen:
            seen.append(asset)
    dossiers = "\n\n".join(
        s for s in (_dossier_summary(root / "knowledge" / "assets" / f"{a}.md")
                    for a in seen) if s)
    event_stats = read(root / "knowledge" / "state" / "event_stats.json", "")
    return {
        "state": state,
        "brief": brief,
        "positions": json.dumps(navs, indent=2),
        "graded": graded,
        "lessons": lessons,
        "flags": json.dumps(active, indent=2, sort_keys=True),
        "dossiers": dossiers,
        "event_stats": event_stats,
        "signal": signal_block(jdb, signal_id),
        "universe": universe_block(cfg, tuple(held)),
    }


def _held_assets(navs: list[dict]) -> list[str]:
    """Base assets with a non-zero position in the newest NAV row. Never raises."""
    out: list[str] = []
    for row in navs:
        try:
            positions = json.loads(row.get("positions_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(positions, dict):
            continue
        for key, amount in positions.items():
            if not isinstance(amount, (int, float)) or amount <= 0:
                continue
            asset = str(key).split("/")[0].upper()
            if asset not in out:
                out.append(asset)
    return out


def _dossier_summary(path: Path) -> str:
    """The '## Summary' section of one asset dossier (the prompt-facing part)."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    lines = []
    in_summary = False
    for line in text.splitlines():
        if line.startswith("## "):
            if in_summary:
                break
            in_summary = line.strip().lower() == "## summary"
            if in_summary:
                lines.append(f"### {path.stem}")
                continue
        if in_summary:
            lines.append(line)
    return "\n".join(lines).strip()


def load_fewshot(root: Path, cfg: EarnConfig, now: datetime) -> str:
    p = root / cfg.paths.fewshot
    try:
        data = json.loads(p.read_text())
        refreshed = datetime.fromisoformat(data["refreshed_at"].replace("Z", "+00:00"))
        if (now - refreshed).days > 45:
            return "No graded examples yet (few-shot file stale)."
        out = []
        for ex in data.get("examples", [])[:4]:
            out.append(f"### {ex['kind'].upper()} (grade {ex['grade']}) — {ex['run_id']}\n"
                       f"{ex.get('snapshot_excerpt', '')}\n"
                       f"Proposal: {json.dumps(ex.get('proposal', {}))}\n"
                       f"Note: {ex.get('outcome_note', '')}")
        return "\n\n".join(out) or "No graded examples yet."
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return "No graded examples yet."


def build(template: str, *, run_id: str, limits: str, fewshot: str,
          inputs: dict[str, str], escalation_reasons: list[str],
          prompt_version: str) -> BuiltPrompt:
    inputs = {k: _truncate(v, INPUT_BUDGETS[k]) for k, v in inputs.items()
              if k in INPUT_BUDGETS}
    _forbid_pnl(inputs["graded"])
    text = (template
            .replace("{{LIMITS}}", limits.rstrip("\n"))
            .replace("{{FEWSHOT}}", fewshot)
            .replace("{{RUN_ID}}", run_id)
            .replace("{{ESCALATION}}", ", ".join(escalation_reasons) or "none")
            .replace("{{STATE}}", inputs["state"])
            .replace("{{BRIEF}}", inputs["brief"])
            .replace("{{POSITIONS}}", inputs["positions"])
            .replace("{{GRADED}}", inputs["graded"])
            .replace("{{LESSONS}}", inputs["lessons"])
            .replace("{{FLAGS}}", inputs["flags"])
            .replace("{{DOSSIERS}}", inputs.get("dossiers") or "(no dossiers yet)")
            .replace("{{EVENT_STATS}}", inputs.get("event_stats") or "{}")
            .replace("{{UNIVERSE}}", inputs.get("universe") or "null")
            .replace("{{SIGNAL}}", inputs.get("signal") or "null"))
    est = token_estimate(text)
    if est > HARD_CAP:
        raise PromptBudgetExceeded(f"prompt estimate {est} > {HARD_CAP} tokens")
    marker = text.find(DYNAMIC_MARKER)
    static_len = marker if marker >= 0 else 0
    return BuiltPrompt(text=text, prompt_version=prompt_version,
                       token_estimate=est, static_prefix_len=static_len)


def build_research_prompt(cfg: EarnConfig, jdb: sqlite3.Connection, run_id: str,
                          escalation_reasons: list[str], now: datetime,
                          root: Path | None = None,
                          prompt_version: str | None = None,
                          signal_id: str | None = None
                          ) -> tuple[BuiltPrompt, dict[str, str], str, str]:
    """Returns (prompt, inputs, limits, fewshot) — inputs/limits/fewshot go into the
    snapshot so replay can rebuild byte-identically.

    ``prompt_version`` defaults to ``research.prompt_version`` (plus the tier-1
    ``config/prompts-auto.yaml`` overlay) instead of a constant in this file.
    """
    root = root or REPO_ROOT
    prompt_version = prompt_version or prompt_version_for(cfg, root)
    template = (root / "prompts" / f"{prompt_version}.md").read_text()
    limits = limits_yaml(cfg)
    fewshot = load_fewshot(root, cfg, now)
    inputs = gather_inputs(cfg, jdb, root, signal_id=signal_id)
    bp = build(template, run_id=run_id, limits=limits, fewshot=fewshot, inputs=inputs,
               escalation_reasons=escalation_reasons, prompt_version=prompt_version)
    return bp, inputs, limits, fewshot


def build_from_snapshot(snapshot_dir: Path, prompt_version: str | None = None,
                        template_override: str | None = None) -> BuiltPrompt:
    """Rebuild the prompt from a decision snapshot (evals/snapshot.py layout).
    With no overrides the result must byte-equal the stored rendered_prompt.md."""
    manifest = json.loads((snapshot_dir / "manifest.json").read_text())
    meta = manifest["meta"]
    version = prompt_version or meta["prompt_version"]
    template = template_override if template_override is not None else (
        REPO_ROOT / "prompts" / f"{version}.md").read_text()
    def _read(name: str) -> str:
        # missing file -> "" so PRE-v2 snapshots (no dossiers/event_stats files)
        # rebuild byte-identically under their own v1 template
        try:
            return (snapshot_dir / name).read_text().strip()
        except OSError:
            return ""

    inputs = {
        "state": _read("state.json"),
        "brief": _read("brief.md"),
        "positions": _read("positions.json"),
        "graded": _read("graded_recent.txt"),
        "lessons": _read("lessons.md"),
        "flags": _read("flags.json"),
        "dossiers": _read("dossiers.md"),
        "event_stats": _read("event_stats.json"),
        "signal": _read("signal.json"),
        "universe": _read("universe.json"),
    }
    limits = (snapshot_dir / "limits.yaml").read_text()
    fewshot = (snapshot_dir / "fewshot.txt").read_text()
    return build(template, run_id=meta["run_id"], limits=limits, fewshot=fewshot,
                 inputs=inputs, escalation_reasons=meta.get("escalation_reasons", []),
                 prompt_version=version)
