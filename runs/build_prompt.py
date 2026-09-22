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
}
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


def limits_yaml(cfg: EarnConfig) -> str:
    """The limits block copied verbatim — tests assert byte-equality with earn.yaml
    values so the prompt can never disagree with the gate."""
    block = {
        "universe": {"assets": list(cfg.universe.assets), "quote": cfg.universe.quote},
        "max_weight": dict(cfg.risk.max_weight),
        "max_gross_exposure": cfg.risk.max_gross_exposure,
        "usdt_floor": cfg.risk.usdt_floor,
        "daily_loss_stop": cfg.risk.daily_loss_stop,
        "monthly_loss_stop": cfg.risk.monthly_loss_stop,
        "max_trades_per_day": cfg.risk.max_trades_per_day,
        "min_notional_usdt": cfg.risk.min_notional_usdt,
    }
    return yaml.safe_dump(block, sort_keys=True)


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


def gather_inputs(cfg: EarnConfig, jdb: sqlite3.Connection,
                  root: Path | None = None) -> dict[str, str]:
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
    return {
        "state": state,
        "brief": brief,
        "positions": json.dumps(navs, indent=2),
        "graded": graded,
        "lessons": lessons,
        "flags": json.dumps(active, indent=2, sort_keys=True),
    }


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
    inputs = {k: _truncate(v, INPUT_BUDGETS[k]) for k, v in inputs.items()}
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
            .replace("{{FLAGS}}", inputs["flags"]))
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
                          prompt_version: str = "research.v1"
                          ) -> tuple[BuiltPrompt, dict[str, str], str, str]:
    """Returns (prompt, inputs, limits, fewshot) — inputs/limits/fewshot go into the
    snapshot so replay can rebuild byte-identically."""
    root = root or REPO_ROOT
    template = (root / "prompts" / f"{prompt_version}.md").read_text()
    limits = limits_yaml(cfg)
    fewshot = load_fewshot(root, cfg, now)
    inputs = gather_inputs(cfg, jdb, root)
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
    inputs = {
        "state": (snapshot_dir / "state.json").read_text().strip(),
        "brief": (snapshot_dir / "brief.md").read_text().strip(),
        "positions": (snapshot_dir / "positions.json").read_text().strip(),
        "graded": (snapshot_dir / "graded_recent.txt").read_text().strip(),
        "lessons": (snapshot_dir / "lessons.md").read_text().strip(),
        "flags": (snapshot_dir / "flags.json").read_text().strip(),
    }
    limits = (snapshot_dir / "limits.yaml").read_text()
    fewshot = (snapshot_dir / "fewshot.txt").read_text()
    return build(template, run_id=meta["run_id"], limits=limits, fewshot=fewshot,
                 inputs=inputs, escalation_reasons=meta.get("escalation_reasons", []),
                 prompt_version=version)
