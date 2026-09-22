"""The twice-daily research run (08:30 / 16:00 Gulf): three model stages —
flags (haiku) -> brief (sonnet) -> decide (opus, escalating to fable on hard-case
flags) — with market state computed by plain Python BEFORE any model call.

Structural guarantees:
- The host writes all machine-read files: proposals are pydantic-validated and
  written atomically AFTER validation, so an invalid proposal file can never exist.
- evals.snapshot.write_snapshot() runs immediately before every decision call —
  week-5 replay depends on it.
- Error path per spec: retry once (never on a budget error), then keep the last
  valid proposal (write nothing) and alert. Never a silent model downgrade.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals import snapshot as snapshotlib
from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import kill as killlib
from ops.lib import locks, tg
from runs import build_prompt, decision_core, router
from runs.common import (
    atomic_write_json,
    guard_env,
    nearest_slot,
    proposal_filename,
    run_id_for,
    utc_iso,
)
from schemas.flags import FLAG_LIST_SCHEMA, apply_reg_flags, parse_reg_flags
from schemas.proposal import ProposalInvalid, json_schema, validate_proposal

FLAGS_PROMPT = """Run the reg-watch procedure (the reg-watch skill): scan the last 7
days of regulator and exchange notices in the news archive for delistings,
stablecoin depegs, licence changes and trading halts affecting BTC, ETH or Binance,
and return the flag proposals as the JSON object the schema requires. Propose only
what the evidence supports; an empty flags list is a normal result. Do NOT include
scheduled macro events (CPI/FOMC) — a deterministic calendar handles those."""

BRIEF_PROMPT = """Write today's crypto brief using the crypto-brief skill: pull the
corroborated news for the last 24 hours from the knowledge archive, apply the
two-source rule (single-source items are marked [unconfirmed]), and write
knowledge/briefs/{date}.md (append an '## Update 16:00' section if the file already
exists today). Max 600 words, every claim carries its source link, no price
predictions, no numbers that are not in the inputs."""

BRIEF_PROMPT_SHORT = """Write a SHORT brief (max 200 words) for today using the
crypto-brief skill: only corroborated, high-impact items from the last 24 hours into
knowledge/briefs/{date}.md (append '## Update 16:00' if it exists)."""


def load_compute_state(root: Path):
    p = root / ".claude" / "skills" / "market-state" / "scripts" / "compute_state.py"
    spec = importlib.util.spec_from_file_location("earn_compute_state", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ResearchRun:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 now: datetime | None = None, stage_runner=None, alert=None,
                 models_cfg: dict | None = None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.stage_runner = stage_runner or decision_core.run_stage
        self.alert = alert or (lambda text, sev="warn": tg.send(text, sev, conn=kdb))
        self.models_cfg = models_cfg or router.load_models_cfg()
        self.slot = ""
        self.run_id = ""

    # ------------------------------------------------------------- journaling

    def journal(self, stage: str, status: str, *, choice=None, meta=None,
                prompt_version: str | None = None, error: str | None = None) -> None:
        import json as _json

        m = meta or decision_core.StageMeta()
        self.jdb.execute(
            "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc, finished_utc,"
            " requested_model, served_model, prompt_version, escalated,"
            " escalation_reasons, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, cost_usd, num_turns, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, stage, "research", utc_iso(self.now), utc_iso(),
             choice.model if choice else None, m.served_model, prompt_version,
             int(bool(choice and choice.escalated)),
             _json.dumps(choice.escalation_reasons) if choice else None,
             m.input_tokens, m.output_tokens, m.cache_read_tokens,
             m.cache_write_tokens, m.cost_usd, m.num_turns, status,
             error or m.error))
        self.jdb.commit()

    def journal_proposal(self, prop, *, path: str | None, valid: bool,
                         invalid_reason: str | None, model: str,
                         prompt_version: str, hard_flags: list[str],
                         shadow: bool = False) -> None:
        import json as _json

        self.jdb.execute(
            "INSERT OR REPLACE INTO proposals(run_id, shadow, ts_utc, path,"
            " prompt_version, model, module, targets_json, exposure_scale, confidence,"
            " abstain, horizon_days, rationale_json, invalidation,"
            " hard_case_flags_json, valid, invalid_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, int(shadow), utc_iso(self.now), path, prompt_version, model,
             prop.module if prop else None,
             _json.dumps(prop.targets.model_dump()) if prop else None,
             prop.exposure_scale if prop else None,
             prop.confidence if prop else None,
             int(prop.abstain) if prop else None,
             prop.horizon_days if prop else None,
             _json.dumps(prop.rationale) if prop else None,
             prop.invalidation if prop else None,
             _json.dumps(hard_flags), int(valid), invalid_reason))
        self.jdb.commit()

    # ------------------------------------------------------------- stages

    def stage_flags(self) -> None:
        choice = router.resolve("flags", models_cfg=self.models_cfg)
        skill_bash = "Bash(python3 .claude/skills/reg-watch/scripts/*)"
        for _attempt in range(1 + choice.retry):
            res = self.stage_runner(
                FLAGS_PROMPT, model=choice.model, max_turns=choice.max_turns,
                max_usd=choice.max_usd, cwd=self.root,
                allowed_tools=["Read", "Glob", "Grep", "Skill", skill_bash],
                output_schema=FLAG_LIST_SCHEMA, skills=["reg-watch"])
            if res.ok:
                try:
                    import json as _json

                    flags = parse_reg_flags(_json.loads(res.text))
                    apply_reg_flags(self.root / self.cfg.paths.flags_file, flags,
                                    self.now, audit_conn=self.kdb)
                    self.journal("flags", "success", choice=choice, meta=res.meta)
                    return
                except Exception as e:  # noqa: BLE001
                    res.meta.error = f"flag merge failed: {e}"
            if res.meta.subtype == "error_max_budget_usd":
                break  # never retry a budget error (it would double-spend)
        self.journal("flags", "failed", choice=choice, meta=res.meta)
        self.alert(f"flags stage failed ({res.meta.error}); keeping last flags file")

    def stage_brief(self, throttled: bool) -> None:
        from runs.common import gulf_now

        date_s = gulf_now(self.now).strftime("%Y-%m-%d")
        if throttled and self.slot == "1600":
            self.journal("brief", "throttled")
            return
        choice = router.resolve("brief", models_cfg=self.models_cfg)
        tools = ["Read", "Glob", "Grep", "Skill", "Write",
                 "Bash(python3 .claude/skills/crypto-brief/scripts/*)"]
        res = self.stage_runner(
            BRIEF_PROMPT.format(date=date_s), model=choice.model,
            max_turns=choice.max_turns, max_usd=choice.max_usd, cwd=self.root,
            allowed_tools=tools, skills=["crypto-brief"])
        if not res.ok and res.meta.subtype != "error_max_budget_usd":
            # semantic fallback per spec: shorter brief on haiku (explicit re-dispatch)
            res = self.stage_runner(
                BRIEF_PROMPT_SHORT.format(date=date_s),
                model=self.models_cfg["models"]["haiku"],
                max_turns=choice.max_turns, max_usd=choice.max_usd, cwd=self.root,
                allowed_tools=tools, skills=["crypto-brief"])
        self.journal("brief", "success" if res.ok else "failed", choice=choice,
                     meta=res.meta)
        if not res.ok:
            self.alert(f"brief stage failed: {res.meta.error}")

    def _decide_once(self, choice, bp) -> tuple:
        res = self.stage_runner(
            bp.text, model=choice.model, max_turns=choice.max_turns,
            max_usd=choice.max_usd, cwd=self.root,
            allowed_tools=decision_core.READ_ONLY_TOOLS,
            extra_disallowed=["Write", "Bash"],
            output_schema=json_schema())
        if not res.ok:
            return None, res
        try:
            prop = validate_proposal(res.text)
        except ProposalInvalid as e:
            res.meta.error = f"schema: {e}"
            return None, res
        if prop.run_id != self.run_id or prop.prompt_version != bp.prompt_version:
            res.meta.error = "run_id/prompt_version mismatch with RUN header"
            return None, res
        return prop, res

    def stage_decide(self) -> bool:
        hard = router.compute_hardcase_flags(self.cfg, self.jdb, root=self.root,
                                             now=self.now)
        choice = router.resolve("decide", hard, models_cfg=self.models_cfg)
        bp, inputs, limits, fewshot = build_prompt.build_research_prompt(
            self.cfg, self.jdb, self.run_id, choice.escalation_reasons, self.now,
            root=self.root)
        meta = snapshotlib.SnapshotMeta(
            run_id=self.run_id, created_at=utc_iso(self.now),
            prompt_version=bp.prompt_version, model=choice.model,
            git_commit=snapshotlib.git_commit(self.root),
            token_budget=self.cfg.budgets.context_tokens["research"],
            escalation_reasons=choice.escalation_reasons)
        try:
            snapshotlib.write_snapshot(self.run_id, inputs=inputs, limits=limits,
                                       fewshot=fewshot, rendered_prompt=bp.text,
                                       meta=meta, jdb=self.jdb, root=self.root)
        except snapshotlib.SnapshotError:
            pass  # retry of a failed run: snapshot already exists

        prop = None
        for _attempt in range(1 + choice.retry):
            prop, res = self._decide_once(choice, bp)
            if prop is not None or res.meta.subtype == "error_max_budget_usd":
                break
        if prop is None:
            self.journal("decide", "failed", choice=choice, meta=res.meta,
                         prompt_version=bp.prompt_version)
            self.journal_proposal(None, path=None, valid=False,
                                  invalid_reason=res.meta.error, model=choice.model,
                                  prompt_version=bp.prompt_version,
                                  hard_flags=choice.escalation_reasons)
            self.alert(f"decide stage failed ({res.meta.error}); sleeve B keeps last"
                       " valid targets", "critical")
            return False

        rel = f"{self.cfg.paths.proposals_dir}/{proposal_filename(self.slot, self.now)}"
        atomic_write_json(self.root / rel, prop.model_dump())
        self.journal("decide", "success", choice=choice, meta=res.meta,
                     prompt_version=bp.prompt_version)
        self.journal_proposal(prop, path=rel, valid=True, invalid_reason=None,
                              model=choice.model, prompt_version=bp.prompt_version,
                              hard_flags=choice.escalation_reasons)
        self._maybe_shadow(bp, choice)
        return True

    def _maybe_shadow(self, bp, base_choice) -> None:
        shadow_model = router.shadow_active(self.models_cfg, self.now.date())
        if not shadow_model:
            return
        res = self.stage_runner(
            bp.text, model=shadow_model, max_turns=base_choice.max_turns,
            max_usd=base_choice.max_usd, cwd=self.root,
            allowed_tools=decision_core.READ_ONLY_TOOLS,
            extra_disallowed=["Write", "Bash"], output_schema=json_schema())
        status, prop, reason = "failed", None, res.meta.error
        if res.ok:
            try:
                prop = validate_proposal(res.text)
                status, reason = "success", None
            except ProposalInvalid as e:
                reason = str(e)
        self.journal("decide_shadow", status, meta=res.meta,
                     prompt_version=bp.prompt_version)
        rel = f"{self.cfg.paths.proposals_dir}/shadow/{proposal_filename(self.slot, self.now)}"
        if prop is not None:
            atomic_write_json(self.root / rel, prop.model_dump())
        self.journal_proposal(prop, path=rel if prop else None,
                              valid=prop is not None, invalid_reason=reason,
                              model=shadow_model, prompt_version=bp.prompt_version,
                              hard_flags=[], shadow=True)

    # ------------------------------------------------------------- flow

    def main_flow(self, slot: str) -> int:
        self.slot = slot
        self.run_id = run_id_for(slot, self.now)

        if killlib.is_engaged(self.cfg, self.root):
            self.journal("decide", "killed")
            return 0
        target = self.root / self.cfg.paths.proposals_dir / proposal_filename(slot, self.now)
        if target.exists():
            return 0  # idempotent rerun by healthcheck

        try:
            cs = load_compute_state(self.root)
            cs.compute_and_write(self.cfg, self.kdb, self.root, self.now)
        except Exception as e:  # noqa: BLE001 — decide abstains on stale state
            self.alert(f"compute_state failed: {e}")

        self.stage_flags()
        throttle = router.throttle_state(self.jdb, self.models_cfg, self.now)
        self.stage_brief(throttle["brief_throttled"])
        ok = self.stage_decide()

        try:  # postflight: refresh the human view (spec: after each run)
            from runs import excel_view

            excel_view.main()
        except Exception as e:  # noqa: BLE001
            print(f"excel_view postflight failed: {e}", file=sys.stderr)
        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    guard_env()
    cfg = load_config()
    slot = argv[0] if argv else nearest_slot()
    with locks.acquire("research"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return ResearchRun(cfg, jdb, kdb).main_flow(slot)


if __name__ == "__main__":
    sys.exit(main())
