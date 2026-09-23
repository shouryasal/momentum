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
from ops.lib import autopilot
from ops.lib import kill as killlib
from ops.lib import locks, paths, tg
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
from schemas.proposal import ProposalInvalid, json_schema, to_file, validate_proposal


def stage_text(cfg: EarnConfig, stage: str, root: Path) -> str:
    """The stage prompt body, from ``research.stage_prompts.<stage>`` (tier 1).

    These were module constants; they are files now, so the console can edit them and the
    change gate can replay a candidate against the same inputs. The comment header of a
    prompt file is stripped so the model sees the instruction only.
    """
    from runs.signals import stage_prompt_text

    text = stage_prompt_text(cfg, stage, root)
    if text.lstrip().startswith("<!--"):
        _, _, text = text.partition("-->")
    return text.strip()


def load_compute_state(root: Path):
    p = root / ".claude" / "skills" / "market-state" / "scripts" / "compute_state.py"
    spec = importlib.util.spec_from_file_location("earn_compute_state", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ResearchRun:
    def __init__(self, cfg: EarnConfig, jdb, kdb, *, root: Path | None = None,
                 state_root: Path | None = None,
                 now: datetime | None = None, stage_runner=None, alert=None,
                 models_cfg: dict | None = None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT                       # the checkout
        #: the data/state root — where the KILL file is, via ``paths.state_root()``.
        self.state_root = Path(state_root) if state_root is not None else paths.state_root()
        self.now = now or datetime.now(UTC)
        self.stage_runner = stage_runner or decision_core.run_stage
        self.alert = alert or (lambda text, sev="warn": tg.send(text, sev, conn=kdb))
        self.models_cfg = models_cfg or router.load_models_cfg()
        self.slot = ""
        self.run_id = ""
        self.trigger_reasons: list[str] = []
        self.signal_id: str | None = None

    # ------------------------------------------------------------- journaling

    def journal(self, stage: str, status: str, *, choice=None, meta=None,
                prompt_version: str | None = None, error: str | None = None) -> None:
        import json as _json

        m = meta or decision_core.StageMeta()
        self.jdb.execute(
            "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc, finished_utc,"
            " requested_model, served_model, prompt_version, escalated,"
            " escalation_reasons, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, cost_usd, num_turns, effort, auth_source,"
            " trigger_reason, signal_id, status, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, stage, "research", utc_iso(self.now), utc_iso(),
             choice.model if choice else None, m.served_model, prompt_version,
             int(bool(choice and choice.escalated)),
             _json.dumps(choice.escalation_reasons) if choice else None,
             m.input_tokens, m.output_tokens, m.cache_read_tokens,
             m.cache_write_tokens, m.cost_usd, m.num_turns,
             m.applied_effort or (choice.effort if choice else None),
             m.auth_source, ",".join(self.trigger_reasons) or None, self.signal_id,
             status, error or m.error))
        self.jdb.commit()
        self._persist_rate_limit(m)

    def _persist_rate_limit(self, m) -> None:
        """RateLimitEvent frames (subscription auth) feed the router's throttle."""
        if m.rate_limit_status is None and m.rate_limit_utilization is None:
            return
        try:
            for key, value in (("rate_limit_status", m.rate_limit_status),
                               ("rate_limit_utilization", m.rate_limit_utilization),
                               ("rate_limit_resets_at", m.rate_limit_resets_at)):
                if value is not None:
                    self.kdb.execute(
                        "INSERT OR REPLACE INTO ops_state(key, value, updated_at)"
                        " VALUES (?,?,?)", (key, str(value), utc_iso()))
            self.kdb.commit()
        except Exception:  # noqa: BLE001 — telemetry only, never fails a run
            pass

    def journal_proposal(self, prop, *, path: str | None, valid: bool,
                         invalid_reason: str | None, model: str,
                         prompt_version: str, hard_flags: list[str],
                         shadow: bool = False,
                         approval_status: str | None = None) -> None:
        import json as _json

        self.jdb.execute(
            "INSERT OR REPLACE INTO proposals(run_id, shadow, ts_utc, path,"
            " prompt_version, model, module, targets_json, exposure_scale, confidence,"
            " abstain, horizon_days, rationale_json, invalidation,"
            " hard_case_flags_json, valid, invalid_reason, signal_id, approval_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, int(shadow), utc_iso(self.now), path, prompt_version, model,
             prop.module if prop else None,
             _json.dumps(prop.targets.model_dump()) if prop else None,
             prop.exposure_scale if prop else None,
             prop.confidence if prop else None,
             int(prop.abstain) if prop else None,
             prop.horizon_days if prop else None,
             _json.dumps(prop.rationale) if prop else None,
             prop.invalidation if prop else None,
             _json.dumps(hard_flags), int(valid), invalid_reason,
             None if shadow else self.signal_id, approval_status))
        self.jdb.commit()

    def _save_response(self, name: str, res, model: str) -> None:
        """Raw model output + stage metadata into the snapshot's outputs/ dir —
        the traceability record runs/trace.py renders. Never fails the run."""
        import json as _json
        from dataclasses import asdict

        try:
            snapshotlib.write_output(self.run_id, name, _json.dumps(
                {"model": model, "ok": res.ok, "text": res.text,
                 "meta": asdict(res.meta)}, indent=2) + "\n", root=self.root)
        except Exception as e:  # noqa: BLE001
            print(f"response capture failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------- stages

    def stage_flags(self) -> None:
        choice = router.resolve("flags", models_cfg=self.models_cfg)
        skill_bash = "Bash(python3 .claude/skills/reg-watch/scripts/*)"
        for _attempt in range(1 + choice.retry):
            res = self.stage_runner(
                stage_text(self.cfg, "flags", self.root), model=choice.model, max_turns=choice.max_turns,
                max_usd=choice.max_usd, cwd=self.root, effort=choice.effort,
                allowed_tools=["Read", "Glob", "Grep", "Skill", skill_bash],
                output_schema=FLAG_LIST_SCHEMA, skills=["reg-watch"],
                deadline_s=self.stage_deadline("flags", 240))
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
            stage_text(self.cfg, "brief", self.root).format(date=date_s), model=choice.model,
            max_turns=choice.max_turns, max_usd=choice.max_usd, cwd=self.root,
            effort=choice.effort, allowed_tools=tools, skills=["crypto-brief"],
            deadline_s=self.stage_deadline("brief", 360))
        if not res.ok and res.meta.subtype != "error_max_budget_usd":
            # semantic fallback per spec: shorter brief on haiku (explicit re-dispatch)
            res = self.stage_runner(
                stage_text(self.cfg, "brief_short", self.root).format(date=date_s),
                model=self.models_cfg["models"]["haiku"],
                max_turns=choice.max_turns, max_usd=choice.max_usd, cwd=self.root,
                effort=choice.effort, allowed_tools=tools, skills=["crypto-brief"],
                deadline_s=self.stage_deadline("brief", 360))
        self.journal("brief", "success" if res.ok else "failed", choice=choice,
                     meta=res.meta)
        if not res.ok:
            self.alert(f"brief stage failed: {res.meta.error}")

    def stage_deadline(self, stage: str, default: float) -> float:
        """The per-stage wall-clock budget from ``research.stage_deadlines_s``.

        The run-level budget is the sum plus ``postflight_margin_s``, and the config
        cross-validation already proved it fits inside the cron ``timeout``; here we only
        hand the stage its share so a stuck call cannot eat the whole run.
        """
        return float(self.cfg.research.stage_deadlines_s.get(stage, default))

    def _universe_checks(self) -> dict:
        """The wide-universe arguments ``validate_proposal`` takes beyond the asset list.

        ``max_assets`` is ``risk.max_open_positions`` — a proposal naming more assets than
        the gate will let the sleeve hold is rejected here rather than half-executed.
        ``snapshot`` is the point-in-time universe THIS run resolved its tradeable set
        from: the proposal must quote it back, so a decision stays replayable and a model
        cannot answer against a universe it was not shown.
        """
        out: dict = {}
        cap = getattr(self.cfg.risk, "max_open_positions", None)
        if cap:
            out["max_assets"] = int(cap)
        ref = getattr(self.cfg.universe, "snapshot_ref", None)
        if ref:
            out["snapshot"] = ref
        return out

    def _decide_once(self, choice, bp) -> tuple:
        res = self.stage_runner(
            bp.text, model=choice.model, max_turns=choice.max_turns,
            max_usd=choice.max_usd, cwd=self.root, effort=choice.effort,
            allowed_tools=decision_core.READ_ONLY_TOOLS,
            extra_disallowed=["Write", "Bash"],
            output_schema=json_schema(),
            deadline_s=self.stage_deadline("decide", 900))
        if not res.ok:
            return None, res
        try:
            prop = validate_proposal(res.text, self.cfg.universe.assets,
                                     self.cfg.universe.quote,
                                     **self._universe_checks())
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
        # a triggered run ALWAYS escalates to the top model — something moved
        choice = router.resolve("decide", hard, models_cfg=self.models_cfg,
                                force_escalation=self.trigger_reasons or None)
        bp, inputs, limits, fewshot = build_prompt.build_research_prompt(
            self.cfg, self.jdb, self.run_id, choice.escalation_reasons, self.now,
            root=self.root, signal_id=self.signal_id)
        meta = snapshotlib.SnapshotMeta(
            run_id=self.run_id, created_at=utc_iso(self.now),
            prompt_version=bp.prompt_version, model=choice.model,
            git_commit=snapshotlib.git_commit(self.root),
            token_budget=self.cfg.budgets.context_tokens["research"],
            escalation_reasons=choice.escalation_reasons,
            signal_id=self.signal_id)
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
        self._save_response("response.json", res, choice.model)
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

        subdir, approval = self.proposal_destination()
        rel = (f"{self.cfg.paths.proposals_dir}/{subdir}"
               f"{proposal_filename(self.slot, self.now)}")
        atomic_write_json(self.root / rel, to_file(prop, signal_id=self.signal_id))
        self.journal("decide", "success", choice=choice, meta=res.meta,
                     prompt_version=bp.prompt_version)
        self.journal_proposal(prop, path=rel, valid=True, invalid_reason=None,
                              model=choice.model, prompt_version=bp.prompt_version,
                              hard_flags=choice.escalation_reasons,
                              approval_status=approval)
        self._mark_signal_acted()
        self._maybe_shadow(bp, choice)
        return True

    def proposal_destination(self) -> tuple[str, str]:
        """``("", "n/a")`` normally; ``("", "pending")`` when a human must sign first.

        In ``LIVE_PROPOSE`` a proposal is a REQUEST, not an instruction: the strategy
        refuses it (``SleeveB._approved``) until an HMAC-signed approval file exists, and
        the console's approval queue and the overview's awaiting-approval tile both filter
        ``proposals.approval_status='pending'``.

        Two bugs lived here. The status came from ``mode_state.load()``, and this job runs
        under ``ops/envwrap.sh research`` — no ``EARN_CONSOLE_SECRET`` — so ``verified``
        was always False and every proposal was journalled ``n/a``: the container refused
        each one for want of an approval while the queue the operator approves *from* was
        permanently empty. Sleeve B would have done nothing for the whole 30-day propose
        stage. The requirement now comes from :mod:`ops.lib.mode_view`, which reads the
        same rendered ``require_approval`` the container enforces, and UNKNOWN requires
        approval.

        And the "correct" branch was broken too: it wrote to ``proposals/pending/``, which
        ``strategies.proposal_loader.load_newest_valid`` does not recurse into and
        ``runs.approvals.proposal_file_for`` never looks in — so an approved proposal could
        never be found. The path stays flat; the *status* is what gates execution.
        """
        try:
            from ops.lib import mode_view

            view = mode_view.load(jdb=self.jdb, root=self.state_root)
        except Exception:  # noqa: BLE001 — an unreadable view still needs a human
            return "", "pending"
        if any(view.sleeve(s).requires_approval for s in paths.SLEEVES):
            return "", "pending"
        return "", "n/a"

    def _mark_signal_acted(self) -> None:
        """Close the loop: the signal that fired this run now has a proposal."""
        if not self.signal_id:
            return
        try:
            from runs.signals import pipeline as pipelinelib

            pipelinelib.mark_acted(self.jdb, self.signal_id, self.run_id, now=self.now)
        except Exception as e:  # noqa: BLE001 — bookkeeping never fails a run
            print(f"signal status update failed: {e}", file=sys.stderr)

    def _maybe_shadow(self, bp, base_choice) -> None:
        shadow_model = router.shadow_active(self.models_cfg, self.now.date())
        if not shadow_model:
            return
        res = self.stage_runner(
            bp.text, model=shadow_model, max_turns=base_choice.max_turns,
            max_usd=base_choice.max_usd, cwd=self.root, effort=base_choice.effort,
            allowed_tools=decision_core.READ_ONLY_TOOLS,
            extra_disallowed=["Write", "Bash"], output_schema=json_schema(),
            deadline_s=self.stage_deadline("shadow", 240))
        self._save_response("response_shadow.json", res, shadow_model)
        status, prop, reason = "failed", None, res.meta.error
        if res.ok:
            try:
                prop = validate_proposal(res.text, self.cfg.universe.assets,
                                         self.cfg.universe.quote,
                                         **self._universe_checks())
                status, reason = "success", None
            except ProposalInvalid as e:
                reason = str(e)
        self.journal("decide_shadow", status, meta=res.meta,
                     prompt_version=bp.prompt_version)
        rel = f"{self.cfg.paths.proposals_dir}/shadow/{proposal_filename(self.slot, self.now)}"
        if prop is not None:
            atomic_write_json(self.root / rel, to_file(prop))
        self.journal_proposal(prop, path=rel if prop else None,
                              valid=prop is not None, invalid_reason=reason,
                              model=shadow_model, prompt_version=bp.prompt_version,
                              hard_flags=[], shadow=True)

    # ------------------------------------------------------------- flow

    def main_flow(self, slot: str, triggered_by: list[str] | None = None,
                  signal_id: str | None = None) -> int:
        self.slot = slot
        self.run_id = run_id_for(slot, self.now)
        self.trigger_reasons = triggered_by or []
        self.signal_id = signal_id

        if killlib.is_engaged(self.cfg, self.state_root):
            self.journal("decide", "killed")
            return 0
        # "How much it does by itself" is a second, independent switch from "whose money".
        # Below `proposing` this run must not spend a model call or write a plan: the
        # operator has said the system may watch but not decide. This is the only place a
        # decision can start — cron, a trigger spawn and the console's "Run now" all land
        # here — so one check makes the level true for every path.
        if not autopilot.may_decide(autopilot.load(
                path=autopilot.autopilot_path({"EARN_STATE_ROOT": str(self.state_root)}))):
            self.journal("decide", "paused")
            return 0
        target = self.root / self.cfg.paths.proposals_dir / proposal_filename(slot, self.now)
        if target.exists():
            return 0  # idempotent rerun by healthcheck (and triggered double-fires)

        try:
            cs = load_compute_state(self.root)
            cs.compute_and_write(self.cfg, self.kdb, self.root, self.now)
        except Exception as e:  # noqa: BLE001 — decide abstains on stale state
            self.alert(f"compute_state failed: {e}")

        self.stage_flags()
        if not self.trigger_reasons:
            throttle = router.throttle_state(
                self.jdb, self.models_cfg, self.now, kdb=self.kdb,
                degrade_at_utilization=self.cfg.budgets.rate_limit.degrade_at_utilization)
            self.stage_brief(throttle["brief_throttled"])
        # triggered runs skip the brief: the 15-min-old one is already on disk
        ok = self.stage_decide()

        try:  # postflight: refresh the human view (spec: after each run)
            from runs import excel_view

            excel_view.main()
        except Exception as e:  # noqa: BLE001
            print(f"excel_view postflight failed: {e}", file=sys.stderr)
        return 0 if ok else 1


def parse_args(argv: list[str]) -> tuple[str, list[str], str | None]:
    """``<slot> [--triggered-by a,b] [--signal-id sig-...]``."""
    slot, triggered, signal_id = None, [], None
    it = iter(argv)
    for a in it:
        if a == "--triggered-by":
            triggered = [r for r in (next(it, "") or "").split(",") if r]
        elif a == "--signal-id":
            signal_id = next(it, None) or None
        elif slot is None:
            slot = a
    return slot or nearest_slot(), triggered, signal_id


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    guard_env()
    cfg = load_config()
    slot, triggered, signal_id = parse_args(argv)
    with locks.acquire("research"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            return ResearchRun(cfg, jdb, kdb).main_flow(
                slot, triggered_by=triggered, signal_id=signal_id)


if __name__ == "__main__":
    sys.exit(main())
