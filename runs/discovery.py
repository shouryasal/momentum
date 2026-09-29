"""The discovery loop: the system doing its own research instead of being handed someone
else's finished config.

Why this file exists
--------------------
Every strategy finding Earn holds today was produced by a **build-time agent** — a session
like the one that wrote this module — and handed to the system as config. That arrangement
tests the builders. It does not test the product. A system that is supposed to form
hypotheses, measure them against history, validate them honestly and propose changes
through its own gate had, until this file, never once been made to try.

The loop is five steps and it runs on a schedule:

``GENERATE``
    Draft candidate hypotheses from what the system already has — its lessons file, the
    hypotheses it has already graded, the standing rejection ledger and the open questions
    in its own seed queue. The cheap model tier drafts; the stronger tier selects. Both are
    optional: with ``generate: false`` the pass drains the seed queue and needs no model at
    all, which is what makes the whole loop testable without a credential.

``TEST``
    Express the hypothesis as a config patch and measure it through
    :mod:`evals.backtest_api` — costs on (the module has no way to turn them off), the
    shipped configuration as the baseline arm, buy-and-hold BTC as the benchmark, per-year
    and per-regime splits, and a walk-forward with contiguous out-of-sample windows.

``VALIDATE``
    Apply the edge-audit discipline to what came back: effective sample size rather than a
    row count, purged and embargoed folds with a leakage assertion, and the **deflated**
    hurdle — ``baseline + expected_max_sharpe(N, years)`` — where ``N`` is the number of
    trials the candidate was actually selected from. See :class:`TrialCounter` for what
    goes into ``N`` and what does not: a nightly measurement that could never have become
    a change is not part of the maximum the hurdle is deflating, and counting it made the
    bar climb for ever while the loop proposed nothing.

``PROPOSE``
    A survivor becomes a ``changes/*.json`` authored in a git worktree and handed to the
    **existing** gate. Nothing it claims is trusted: ``evals/verify_change.py`` recomputes
    the bounds arithmetic, the backtest and the walk-forward from the commit, and
    ``runs/apply_changes.py`` decides. Only ``params.*`` keys inside ``bounds:`` can be
    proposed at all; a ``trading.*`` or ``execution.*`` finding is reported for a human
    because those land in generated, tier-2 files. The single question asked here is the
    grade's ``may_become_a_change`` — not its prediction verdict.

``RECORD``
    Every hypothesis, its prediction, its falsifier, the measured numbers, the validation
    and the computed verdicts go into the ledger (:mod:`evals.hypothesis`), which refuses
    to overwrite a prediction, refuses to grade one that was edited after the fact, and
    refuses to grade the same one twice. A grade carries ONE decisive field,
    ``may_become_a_change``, computed from the prediction verdict **and** the validation;
    "my predictions came true" on its own never licenses a change. The negatives are kept
    — they are the only thing that stops the loop proposing the same dead idea next month,
    and they are what its own hit rate will eventually be computed from.

What it cannot do
-----------------
Not by promise, by construction:

* every patch goes through :func:`evals.backtest_api.validate_patch`, which refuses every
  tier-2 key **by name** — risk limits, the universe, capital, mode, credentials, the
  timeframe, the strategy, the exchange block;
* only ``params.*`` leaves inside ``config/earn.yaml: bounds`` may become a proposal, and
  the step size is re-checked here and then recomputed by the gate;
* the selecting model is refused outright unless its task declares ``min_tier >= 3`` and
  ``allow_local: false`` — a local model may never author what becomes a proposal;
* a change containing ``scripts/**`` is held for a human by ``apply_changes`` regardless;
* nothing here writes a proposal in the trading sense, and nothing here reaches an order.
  The deterministic risk gate validates every order whatever this file concludes.

Usage::

    python -m runs.discovery light          # the nightly pass: drain the queue
    python -m runs.discovery deep           # the weekly pass: generate, measure, propose
    python -m runs.discovery light --hypothesis 2026-09-24-profit-booking-roi-ladder
    python -m runs.discovery deep --no-model --no-propose --dry-run
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.hypothesis import Hypothesis, HypothesisError, Ledger, Measurement, Prediction
from ops.config import REPO_ROOT, EarnConfig, load_config
from runs.common import atomic_write_text, guard_env, gulf_now, utc_iso

__all__ = [
    "DEFAULT_LABEL_PAIR",
    "DiscoveryError",
    "DiscoveryRun",
    "Study",
    "TrialCounter",
    "Validation",
    "bounded_param_change",
    "load_seeds",
    "main",
]

#: The pair whose triple-barrier labels stand in for the sample size of a study. The panel
#: measured on it — 19,879 labelled 4h rows carrying 3,020 independent observations, 6.6
#: rows per real one — is the reason ``effective_n`` is reported instead of ``n``.
DEFAULT_LABEL_PAIR = "BTC/USDT"
DEFAULT_LABEL_TF = "4h"

#: A backtest of this system over a multi-year window takes minutes, not seconds. The loop
#: refuses to START a hypothesis it cannot finish inside the pass deadline rather than
#: being killed by ``timeout(1)`` halfway through and leaving a recorded prediction with no
#: grade — which would then block the id for ever.
BACKTEST_COST_S = 180.0

SCOUT = ".claude/skills/research-scout/scripts/scout.py"


class DiscoveryError(RuntimeError):
    """The loop cannot proceed. Never a silently degraded result."""


# ============================================================================ trial count


class TrialCounter:
    """The persistent count of search trials, shared with the ``edge-audit`` skill.

    Three counts in one file, and the one the hurdle uses is the narrowest
    ---------------------------------------------------------------------
    The deflated hurdle asks: *if I ran N zero-skill trials and reported the best, how good
    would the best look by luck alone?* So ``N`` has to be the size of the family the
    reported candidate was actually selected from. The first version of this class had one
    number and used it for everything, and the loop's own schedule broke it:

    * the nightly ``light`` pass measures a hypothesis and **cannot propose**
      (``passes.light.propose: false``), yet it incremented the same counter;
    * six of every seven measurements are therefore trials no change could ever come out
      of, and each one permanently raised the bar for the seventh.

    Counting a measurement that could not produce a change is not conservatism, it is a
    category error — the loop never took a maximum over those trials. So this class keeps:

    ``n_trials``
        Every measurement ever made, whatever pass made it. The audit trail. Only grows.
        Never used to form a hurdle on its own.

    ``n_selection_trials``
        All-time count of trials that *could* have produced a change: measurements made in
        a pass whose spec says ``propose: true``, plus screened trials promoted by
        :meth:`promote`. Only grows. Kept so the gap to ``n_trials`` is visible and nobody
        has to take the narrower figure on trust.

    ``family.n_selection_trials``
        **The hurdle's N.** The selection trials in the *open* family: those spent since
        the last change this loop proposed that the gate actually merged. Reset only by
        :meth:`close_family`.

    Why a family may close, and why that is not a loophole
    -----------------------------------------------------
    A count that only ever grows becomes unreachable — which is its own failure, because a
    loop that can never propose anything is not protected, it is broken. The escape has to
    be something the loop cannot manufacture. A **merged** change is exactly that: to reach
    it a candidate must first clear the current (high) hurdle and every other gate here,
    then ``evals/verify_change.py`` re-runs the backtest and the walk-forward from the
    commit and ``runs/apply_changes.py`` decides. Closing a family is gated behind the very
    bar it lowers, so the loop cannot lower the bar without first clearing it.

    A proposal that was *rejected* or *held* closes nothing: being wrong must not buy a
    clean slate. Nor does a revert reopen a family that already closed — the selection
    event happened, and the all-time counts still carry every trial it contained.

    ``knowledge/state/**`` is tier 2, which is exactly right: this file is written by
    tier-2 code (this module, and the skill's tier-2 script), never by a model's own tool
    call.
    """

    #: A legacy file has no ``n_selection_trials``. It is read as equal to ``n_trials`` —
    #: assume every past trial counted — so the migration can only leave the hurdle where
    #: it was or above it, never below.
    def __init__(self, root: Path | None = None) -> None:
        self.path = (root or REPO_ROOT) / "knowledge" / "state" / "trial_counter.json"

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"n_trials": 0, "n_selection_trials": 0, "history": [],
                "family": {"opened_utc": None, "closed_by": None,
                           "n_selection_trials": 0}}

    def read(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(doc, dict):
            return self._empty()
        n = max(int(doc.get("n_trials") or 0), 0)
        doc["n_trials"] = n
        # Legacy (and hand-edited) files: assume every recorded trial was a selection
        # trial. The migration is allowed to raise the hurdle, never to lower it.
        doc["n_selection_trials"] = max(int(doc.get("n_selection_trials") or 0), 0) \
            if "n_selection_trials" in doc else n
        fam = doc.get("family")
        if not isinstance(fam, dict):
            fam = {"opened_utc": None, "closed_by": None,
                   "n_selection_trials": doc["n_selection_trials"]}
        fam["n_selection_trials"] = max(int(fam.get("n_selection_trials") or 0), 0)
        fam.setdefault("opened_utc", None)
        fam.setdefault("closed_by", None)
        doc["family"] = fam
        doc.setdefault("history", [])
        return doc

    # -- the hurdle's N ----------------------------------------------------------

    def hurdle_trials(self) -> int:
        """The N the deflated hurdle is formed from: the OPEN family's selection trials."""
        return int(self.read()["family"]["n_selection_trials"])

    # -- writes ------------------------------------------------------------------

    def _write(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return doc

    def add(self, what: str, *, selection: bool, hypothesis: str = "",
            pass_name: str = "") -> dict[str, Any]:
        """Count a trial BEFORE it is spent, and never fewer than were there before.

        ``selection`` is a required keyword so no caller can forget to say whether this
        measurement could have produced a change. It is the pass spec's ``propose`` flag
        and nothing else: operator flags like ``--no-propose`` and ``--dry-run`` still
        count as selection trials, because treating a human override as a screen would let
        somebody run a search without paying for it.
        """
        doc = self.read()
        doc["n_trials"] = max(int(doc["n_trials"]) + 1, 1)
        if selection:
            doc["n_selection_trials"] = max(int(doc["n_selection_trials"]) + 1, 1)
            doc["family"]["n_selection_trials"] = int(
                doc["family"]["n_selection_trials"]) + 1
            doc["family"].setdefault("opened_utc", None)
            if not doc["family"].get("opened_utc"):
                doc["family"]["opened_utc"] = utc_iso()
        history = list(doc.get("history") or [])
        history.append({"utc": utc_iso(), "what": what, "selection": bool(selection),
                        "hypothesis": hypothesis, "pass": pass_name})
        doc["history"] = history[-500:]
        return self._write(doc)

    def promote(self, hypothesis_id: str, why: str) -> dict[str, Any]:
        """Charge a screened trial to the selection family, because it was acted on.

        This is what keeps the split from weakening the protection. A nightly screen that
        nobody acts on costs nothing. A screened idea a later proposable hypothesis is
        seeded from *was* part of the search that produced the candidate — that is a
        two-stage search, and the screening stage has to be paid for. It raises the
        selection counts without touching ``n_trials``, because no new measurement
        happened, and it is idempotent per hypothesis id.
        """
        doc = self.read()
        promoted = {str(x) for x in (doc.get("promoted") or [])}
        if hypothesis_id in promoted:
            return doc
        promoted.add(hypothesis_id)
        doc["promoted"] = sorted(promoted)
        doc["n_selection_trials"] = max(int(doc["n_selection_trials"]) + 1, 1)
        doc["family"]["n_selection_trials"] = int(doc["family"]["n_selection_trials"]) + 1
        history = list(doc.get("history") or [])
        history.append({"utc": utc_iso(), "what": why, "selection": True,
                        "hypothesis": hypothesis_id, "pass": "promoted"})
        doc["history"] = history[-500:]
        return self._write(doc)

    def close_family(self, change_id: str, merged_utc: str = "") -> dict[str, Any]:
        """A change of ours was MERGED: the selection event is over, the next one starts.

        Idempotent per ``change_id``, so re-running a pass cannot close the same family
        twice. Nothing is forgotten: ``n_trials`` and ``n_selection_trials`` are untouched.
        """
        doc = self.read()
        if doc["family"].get("closed_by") == change_id:
            return doc
        doc["family"] = {"opened_utc": merged_utc or utc_iso(), "closed_by": change_id,
                         "n_selection_trials": 0}
        history = list(doc.get("history") or [])
        history.append({
            "utc": utc_iso(), "selection": False, "hypothesis": "", "pass": "family",
            "what": f"family closed: {change_id} was merged after the gate recomputed it; "
                    f"the next candidate is a new selection"})
        doc["history"] = history[-500:]
        return self._write(doc)

    def screened(self) -> set[str]:
        """Hypothesis ids measured by a pass that could not propose — the screen."""
        doc = self.read()
        return {str(e.get("hypothesis")) for e in (doc.get("history") or [])
                if e.get("hypothesis") and not e.get("selection")}


# ============================================================================ validation


@dataclass(frozen=True)
class Validation:
    """What the statistics say about a measured result, and why it may not be reported.

    ``problems`` is the operative field. It is empty only when the result clears every bar
    the loop owns; anything in it stops a proposal, whatever the headline said.
    """

    rows: int
    effective_n: float
    rows_per_effective_sample: float
    folds: int
    purged: int
    embargoed: int
    leaks: int
    n_trials: int                        # every measurement ever made — the audit trail
    n_selection_trials: int              # the OPEN family: the N the hurdle is formed from
    n_selection_trials_all_time: int     # all-time selection trials, so the gap is visible
    years: float
    baseline_sharpe: float
    candidate_sharpe: float
    expected_max_sharpe: float
    deflated_hurdle: float
    clears_hurdle: bool
    years_to_detect: float
    oos_win_rate: float | None
    oos_mean_delta_pct: float | None
    trades: int
    beat_strategy_baseline: bool
    beat_btc_buy_and_hold: bool
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        out = dict(vars(self))
        out["problems"] = list(self.problems)
        out["ok"] = self.ok
        return out

    def describe(self) -> str:
        lines = [
            f"  sample           {self.rows:,} rows -> effective_n {self.effective_n:,.1f}"
            f" ({self.rows_per_effective_sample:.2f} rows per independent observation)",
            f"  folds            {self.folds} purged, {self.purged} purged rows,"
            f" {self.embargoed} embargoed, {self.leaks} leaks",
            f"  trials           N={self.n_selection_trials} in the open selection family"
            f" over T={self.years:.1f}y -> expected max Sharpe"
            f" {self.expected_max_sharpe:.3f}"
            f"  ({self.n_selection_trials_all_time} selection trials all time,"
            f" {self.n_trials} measurements all time)",
            f"  hurdle           {self.baseline_sharpe:.3f} + {self.expected_max_sharpe:.3f}"
            f" = {self.deflated_hurdle:.3f}; candidate {self.candidate_sharpe:.3f}"
            f" -> {'CLEARS' if self.clears_hurdle else 'does not clear'}",
            f"  power            telling these two Sharpes apart at 80% needs"
            f" {self.years_to_detect:,.1f} years of live data",
        ]
        if self.oos_win_rate is not None:
            lines.append(f"  walk-forward     OOS win rate {self.oos_win_rate:.0%},"
                         f" mean delta {self.oos_mean_delta_pct:+.2f}pp")
        lines.append(f"  both baselines   beats shipped strategy: "
                     f"{'yes' if self.beat_strategy_baseline else 'NO'};"
                     f" beats buy-and-hold BTC: "
                     f"{'yes' if self.beat_btc_buy_and_hold else 'NO'}")
        for p in self.problems:
            lines.append(f"  PROBLEM          {p}")
        return "\n".join(lines)


# ============================================================================ one study


@dataclass
class Study:
    """One hypothesis taken through all five steps, with everything it produced."""

    hypothesis: Hypothesis
    baseline: Any = None                 # evals.backtest_api.Metrics
    variant: Any = None                  # evals.backtest_api.Metrics
    comparison: Any = None               # evals.backtest_api.Comparison
    walk: Any = None                     # evals.backtest_api.WalkForward | None
    benchmark: Any = None                # evals.backtest_api.Benchmark | None
    validation: Validation | None = None
    grade: Any = None                    # evals.hypothesis.Grade
    change_id: str | None = None
    skipped: str = ""                    # why it never ran, when it did not
    notes: list[str] = field(default_factory=list)

    @property
    def prediction_verdict(self) -> str:
        """Did the sealed predictions come true. NOT whether this may become a change."""
        return getattr(self.grade, "prediction_verdict", "open") if self.grade else "open"

    @property
    def may_become_a_change(self) -> bool:
        """The ONE question ``propose`` asks. False for anything ungraded."""
        return bool(getattr(self.grade, "may_become_a_change", False))

    @property
    def blocked_because(self) -> tuple[str, ...]:
        return tuple(getattr(self.grade, "blocked_because", ()) or ())

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis.as_dict(),
            "may_become_a_change": self.may_become_a_change,
            "blocked_because": list(self.blocked_because),
            "prediction_verdict": self.prediction_verdict,
            "skipped": self.skipped,
            "baseline": self.baseline.headline() if self.baseline else None,
            "variant": self.variant.headline() if self.variant else None,
            "comparison": self.comparison.as_dict() if self.comparison else None,
            "walk_forward": self.walk.as_dict() if self.walk else None,
            "benchmark": self.benchmark.as_dict() if self.benchmark else None,
            "validation": self.validation.as_dict() if self.validation else None,
            "grade": self.grade.as_dict() if self.grade else None,
            "change_id": self.change_id,
            "notes": list(self.notes),
        }


# ============================================================================ seeds


def load_seeds(root: Path, seed_dir: str) -> list[Hypothesis]:
    """The starting queue: hand-written hypotheses under ``discovery.seed_dir``.

    A seed file is ``{"hypotheses": [...], ...}`` or a bare list. Anything else in the file
    (``open_questions``, prose) is ignored here and read by the generate prompt. A malformed
    seed is a hard error rather than a skipped entry: a queue that silently drops the one
    hypothesis somebody cared about is worse than a queue that will not load.
    """
    out: list[Hypothesis] = []
    base = root / seed_dir
    if not base.exists():
        return out
    for path in sorted(base.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DiscoveryError(f"{path} is not readable JSON: {exc}") from exc
        items = doc.get("hypotheses") if isinstance(doc, dict) else doc
        for raw in items or []:
            try:
                out.append(Hypothesis.from_dict(raw))
            except (HypothesisError, KeyError, TypeError) as exc:
                raise DiscoveryError(f"{path}: bad seed hypothesis: {exc}") from exc
    return out


def open_questions(root: Path, seed_dir: str) -> list[str]:
    """The ``open_questions`` blocks from the seed files, for the generate prompt."""
    out: list[str] = []
    base = root / seed_dir
    for path in sorted(base.glob("*.json")) if base.exists() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            out.extend(str(q) for q in (doc.get("open_questions") or []))
    return out


# ============================================================================ proposals


@dataclass(frozen=True)
class BoundedChange:
    """A patch expressed as a ``params-sleeve-<s>.json`` edit the change gate can verify."""

    sleeve: str
    updates: dict[str, Any]              # dotted leaf under "params" -> new value
    bounds_check: list[dict[str, Any]]
    refusals: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.updates) and not self.refusals


def bounded_param_change(cfg: EarnConfig, patch: dict[str, Any], sleeve: str,
                         root: Path | None = None) -> BoundedChange:
    """Turn a research patch into a tier-1 params edit, or say exactly why it cannot be one.

    Three reasons a perfectly good finding is not proposable, and all three are reported
    rather than worked around:

    * a ``trading.*`` or ``execution.*`` key lands in ``config/riskgate.json``, which is
      **generated** from ``config/earn.yaml`` — a tier-2 file. The loop may measure such a
      patch; it may not propose one.
    * a ``params.*`` leaf with no entry in ``bounds:`` has no ceiling, no floor and no step
      limit, and adding one is a human decision.
    * a value outside its bounds, or a step larger than ``max_step``, is refused here and
      would be refused again by ``evals/verify_change.py`` — which is the copy that counts.
    """
    from evals.verify_change import bounds_key_for

    base = root or REPO_ROOT
    params_rel = getattr(cfg.paths, f"params_{sleeve}", f"config/params-sleeve-{sleeve}.json")
    try:
        current = json.loads((base / params_rel).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return BoundedChange(sleeve, {}, [], (f"cannot read {params_rel}: {exc}",))

    updates: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    refusals: list[str] = []
    for key, value in sorted(patch.items()):
        namespace, _, leaf = key.partition(".")
        if namespace != "params":
            refusals.append(
                f"{key}: measurable, not proposable — {namespace}.* is written into the "
                f"GENERATED config/riskgate.json, which is rendered from config/earn.yaml "
                f"(tier 2). A human moves it.")
            continue
        bkey = bounds_key_for(cfg, sleeve, leaf)
        if bkey is None:
            refusals.append(
                f"{key}: no entry in config/earn.yaml bounds:, so there is no floor, "
                f"ceiling or step limit to propose within. Adding one is tier 2.")
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            refusals.append(f"{key}: {value!r} is not a numeric parameter")
            continue
        node: Any = current.get("params") or {}
        for part in leaf.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, (int, float)) or isinstance(node, bool):
            refusals.append(f"{key}: no current numeric value in {params_rel}")
            continue
        b = cfg.bounds[bkey]
        entry = {"param": bkey, "old": float(node), "new": float(value),
                 "min": b.min, "max": b.max, "max_step": b.max_step, "ok": True}
        if not (b.min <= float(value) <= b.max):
            entry["ok"] = False
            refusals.append(f"{bkey} {value} outside [{b.min}, {b.max}]")
        elif abs(float(value) - float(node)) > b.max_step + 1e-12:
            entry["ok"] = False
            refusals.append(
                f"{bkey} step {abs(float(value) - float(node)):g} > max_step {b.max_step}")
        checks.append(entry)
        if entry["ok"]:
            updates[leaf] = value
    return BoundedChange(sleeve, updates, checks, tuple(refusals))


# ============================================================================ the run


class DiscoveryRun:
    """One pass of the loop. Every collaborator is injectable so the tests never need
    docker, a git worktree or a model credential."""

    def __init__(self, cfg: EarnConfig, jdb, kdb=None, *, pass_name: str = "light",
                 root: Path | None = None, now: datetime | None = None,
                 ledger: Ledger | None = None, trials: TrialCounter | None = None,
                 backtest: Callable[..., Any] | None = None,
                 walk_forward: Callable[..., Any] | None = None,
                 benchmark: Callable[..., Any] | None = None,
                 label_stats: Callable[[], dict[str, Any]] | None = None,
                 llm: Callable[..., Any] | None = None,
                 alert: Callable[[str, str], None] | None = None,
                 allow_model: bool = True, allow_propose: bool = True,
                 dry_run: bool = False) -> None:
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = Path(root) if root is not None else REPO_ROOT
        self.now = now or datetime.now(UTC)
        #: Budgets are measured on a MONOTONIC clock started here, never against ``self.now``.
        #: ``self.now`` is the injected stamp records carry, and subtracting it from the wall
        #: clock measures "how stale the injected timestamp is", not "how long this pass has
        #: been running". The two agree only when nobody injected a clock, so this was a time
        #: bomb: every pinned-clock test began deferring 100% of its studies the moment real
        #: time passed the pinned instant, and in production a rerun of a missed slot — which
        #: is exactly what ``ops/healthcheck.py`` spawns — computed a budget hours in deficit
        #: and refused to measure anything, silently, with a plausible "not started" note.
        self._started = time.monotonic()
        self.pass_name = pass_name
        self.ledger = ledger or Ledger(self.root)
        self.trials = trials or TrialCounter(self.root)
        self._backtest = backtest
        self._walk_forward = walk_forward
        self._benchmark = benchmark
        self._label_stats = label_stats
        self._llm = llm
        self.alert = alert or (lambda text, sev="info": print(f"[{sev}] {text}"))
        self.allow_model = allow_model
        self.allow_propose = allow_propose
        self.dry_run = dry_run
        self.run_id = (f"discovery-{gulf_now(self.now).strftime('%Y-%m-%d')}-{pass_name}")
        self.studies: list[Study] = []
        self.generated: list[str] = []
        self.model_used: str | None = None
        self.model_notes: list[str] = []
        self.wt = None

    # ---------------------------------------------------------------- config access

    @property
    def spec(self):  # noqa: ANN201 - ops.config.DiscoveryPass
        passes = self.cfg.discovery.passes or {}
        if self.pass_name not in passes:
            raise DiscoveryError(
                f"discovery.passes has no pass {self.pass_name!r}; "
                f"known: {sorted(passes)}")
        return passes[self.pass_name]

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(self.cfg.discovery.pairs or ())

    def _elapsed_s(self) -> float:
        """Wall-clock seconds since this pass started, on a clock nothing can inject."""
        return time.monotonic() - self._started

    def _budget_left_s(self) -> float:
        return float(self.spec.deadline_s) - self._elapsed_s()

    # ---------------------------------------------------------------- 1. GENERATE

    def assert_authoring_task(self, models_cfg: Any) -> None:
        """Refuse a selection task a local model could serve.

        The standing invariant is that a local model may never author a proposal or a
        validation. ``runs/llm/types.py: chain_for`` enforces the floor for ``decide`` and
        ``validate`` in code; ``discover`` declares its own (``min_tier: 3``,
        ``allow_local: false``) in ``config/models.yaml``. Config can be edited, so the
        loop re-checks it here and refuses to run rather than quietly selecting on an 8B
        model — because what this stage selects is what eventually becomes a change.
        """
        task = self.cfg.discovery.select_task
        try:
            tcfg = models_cfg.task(task)
        except Exception as exc:  # noqa: BLE001 - an undeclared task is a config error
            raise DiscoveryError(
                f"discovery.select_task {task!r} is not declared in config/models.yaml: "
                f"{exc}") from exc
        min_tier = int(getattr(tcfg, "min_tier", 0) or 0)
        allow_local = bool(getattr(tcfg, "allow_local", True))
        if min_tier < 3 or allow_local:
            raise DiscoveryError(
                f"discovery.select_task {task!r} declares min_tier={min_tier} "
                f"allow_local={allow_local}. Selection authors what becomes a change "
                f"proposal, so it needs min_tier >= 3 and allow_local: false. Refusing to "
                f"run rather than selecting on a model that may not author.")

    def generate(self) -> list[Hypothesis]:
        """Draft candidates on the cheap tier, select on the strong one, record survivors.

        Returns the hypotheses newly written into the ledger. Every failure here is
        non-fatal and reported: the queue is the fallback, and a pass that drains the queue
        is a complete pass.
        """
        if not self.spec.generate or not self.allow_model:
            self.model_notes.append(
                "generation skipped: " + ("pass declares generate: false"
                                          if not self.spec.generate else "--no-model"))
            return []
        try:
            from ops.models_config import load_models_cfg

            models_cfg = load_models_cfg()
            self.assert_authoring_task(models_cfg)
        except DiscoveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - no routing file is not a crash
            self.model_notes.append(f"generation skipped: model config unreadable ({exc})")
            return []

        room = int(self.cfg.discovery.max_open_hypotheses) - len(self.open_hypotheses())
        if room <= 0:
            self.model_notes.append(
                f"generation skipped: {self.cfg.discovery.max_open_hypotheses} open "
                f"hypotheses already queued — finish those before drafting more")
            return []

        drafts = self._call_model(self.cfg.discovery.generate_task,
                                  self._generate_prompt(room), models_cfg)
        if not drafts:
            return []
        chosen = self._call_model(self.cfg.discovery.select_task,
                                  self._select_prompt(drafts, room), models_cfg)
        picked = chosen if chosen else drafts[:room]
        out: list[Hypothesis] = []
        for raw in picked[:room]:
            try:
                h = self._hypothesis_from_draft(raw)
            except (HypothesisError, KeyError, TypeError, ValueError) as exc:
                self.model_notes.append(f"draft rejected: {exc}")
                continue
            if self._rejected_by_ledger(h.statement):
                continue
            try:
                self.ledger.record(h)
            except HypothesisError as exc:
                self.model_notes.append(f"{h.id}: not recorded — {exc}")
                continue
            out.append(h)
            self.generated.append(h.id)
        return out

    def _hypothesis_from_draft(self, raw: Any) -> Hypothesis:
        """Build a sealed hypothesis from a model draft, supplying the measurement.

        The measurement is **ours**, not the model's: window, folds, pairs, costs and
        baseline are the pass's, so a draft cannot pick the window that flatters it. The
        statement, the variable, the patch, the predictions and the falsifier are the
        model's, and all five are required by the dataclass.
        """
        if not isinstance(raw, dict):
            raise HypothesisError(f"a draft must be an object, got {type(raw).__name__}")
        day = gulf_now(self.now).strftime("%Y-%m-%d")
        slug = str(raw.get("slug") or raw.get("id") or "").strip().lower()
        slug = "".join(c if c.isalnum() else "-" for c in slug).strip("-")[:48]
        if len(slug) < 3:
            raise HypothesisError(f"draft needs a slug of 3+ characters, got {slug!r}")
        from evals.backtest_api import validate_patch

        patch = validate_patch(raw.get("patch") or {})
        if not patch:
            raise HypothesisError("a draft with an empty patch changes nothing")
        preds = tuple(Prediction(**p) for p in (raw.get("predictions") or []))
        return Hypothesis(
            id=f"{day}-{slug}", statement=str(raw.get("statement") or ""),
            variable=str(raw.get("variable") or ""), patch=patch, predictions=preds,
            measurement=Measurement(
                method="walk_forward" if self.spec.folds else "backtest",
                timerange=self.spec.timerange, pairs=self.pairs,
                folds=self.spec.folds or None,
                baseline="shipped configuration, same window, same measured costs",
                note="window, folds, pairs and costs are set by the pass, not by the "
                     "model that drafted this"),
            falsifier=str(raw.get("falsifier") or ""),
            seed=str(raw.get("seed") or f"discovery {self.pass_name} {self.run_id}"),
            author=self.model_used or self.cfg.discovery.select_task,
        )

    def _rejected_by_ledger(self, statement: str) -> bool:
        """Ask ``research-scout``'s standing rejection ledger before spending a trial."""
        script = self.root / SCOUT
        if not script.exists():
            return False
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, tier-2 script
                [sys.executable, str(script), "check", "--idea", statement],
                cwd=str(self.root), capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.model_notes.append(f"rejection-ledger check unavailable: {exc}")
            return False
        text = (proc.stdout or "") + (proc.stderr or "")
        try:
            verdict = json.loads(proc.stdout).get("verdict")
        except (ValueError, AttributeError):
            verdict = "rejected" if '"rejected"' in text or "verdict=rejected" in text else None
        if verdict == "rejected":
            self.model_notes.append(
                f"dropped (already measured and killed): {statement[:120]}")
            return True
        return False

    # ---------------------------------------------------------------- prompts

    def _context(self) -> dict[str, Any]:
        """What the system already knows about its own research — the generate inputs."""
        lessons = ""
        p = self.root / "lessons.md"
        if p.exists():
            try:
                lessons = p.read_text(encoding="utf-8")[:4000]
            except OSError:
                lessons = ""
        return {
            "open_questions": open_questions(self.root, self.cfg.discovery.seed_dir),
            "already_tried": self.ledger.listing()[:40],
            "lessons": lessons,
            "trial_state": self.trials.read() | {"history": None},
            "shipped_baseline_note": (
                "the shipped strategy has NO profit taking at all and returned +75.4% over "
                "2021-2026 against buy-and-hold BTC +194%"),
            "allowed_patch_namespaces": ["params.*", "trading.*", "execution.*"],
            "proposable_only": sorted(self.cfg.bounds),
        }

    def _generate_prompt(self, room: int) -> str:
        ctx = self._context()
        return (
            "You are drafting candidate hypotheses for Earn's discovery loop. You are NOT "
            "measuring anything and NOT proposing a trade.\n\n"
            f"Draft at most {room * 2} candidates. Each must be a falsifiable claim about "
            "ONE variable, expressed as a config patch the backtest API accepts.\n\n"
            "Hard rules:\n"
            "- The patch may only touch params.*, trading.* or execution.*. Every other "
            "namespace is refused by the code and the draft is discarded.\n"
            "- A prediction needs a metric, a direction and a min_effect greater than "
            "zero. Without a min_effect the prediction is satisfied by noise.\n"
            "- Metric names come from this list and no other: net_return_pct, cagr_pct, "
            "max_drawdown_pct, calmar, sharpe, sortino, sharpe_daily, profit_factor, "
            "trades, turnover_annual, fees_paid_quote, time_in_market_pct.\n"
            "- The falsifier is a sentence naming the result that would make you abandon "
            "this. 'If it does not work' is not a falsifier.\n"
            "- Do not repeat anything in already_tried, whatever it was graded.\n\n"
            "Answer with ONE JSON object: {\"drafts\": [{\"slug\": \"...\", "
            "\"statement\": \"...\", \"variable\": \"...\", \"patch\": {...}, "
            "\"predictions\": [{\"metric\": \"...\", \"direction\": "
            "\"increase|decrease|unchanged\", \"min_effect\": 0.0, \"unit\": \"pp\"}], "
            "\"falsifier\": \"...\"}]}\n\n"
            "## What this system already knows\n\n```json\n"
            + json.dumps(ctx, indent=2, default=str) + "\n```\n")

    def _select_prompt(self, drafts: list[Any], room: int) -> str:
        """The strong tier picks. It gets the discover stage prompt's standing rules plus
        the drafts, and returns the subset worth a trial."""
        body = ""
        prompt_path = self.root / self.cfg.discovery.prompt
        if prompt_path.exists():
            try:
                text = prompt_path.read_text(encoding="utf-8")
                if text.lstrip().startswith("<!--"):
                    _, _, text = text.partition("-->")
                body = text.strip()
            except OSError:
                body = ""
        return (
            body + "\n\n---\n\n"
            "For THIS call you are not running a study. You are choosing which of the "
            f"drafts below is worth spending a trial on. Pick at most {room}, in order of "
            "what this system's own record says the gap is — exits, profit taking, stop "
            "width, how many satellites are worth holding — rather than another entry "
            "signal. Every extra variant raises the deflated hurdle for every honest idea "
            "that comes after it, so picking fewer is usually the better answer.\n\n"
            "Return the chosen drafts VERBATIM, unedited, as ONE JSON object: "
            "{\"drafts\": [ ... ]}. Editing a draft here would mean the prediction was "
            "written after its author saw the selection, which is the ordering this loop "
            "exists to prevent.\n\n## Drafts\n\n```json\n"
            + json.dumps({"drafts": drafts}, indent=2, default=str) + "\n```\n")

    def _call_model(self, task: str, prompt: str, models_cfg: Any) -> list[Any]:
        """One model call returning ``drafts``. Never raises; a failure is a note."""
        runner = self._llm
        if runner is None:
            from runs.llm.chain import run_task as _run_task
            from runs.llm.types import RunCtx

            def runner(task: str, prompt: str, **kw: Any):  # noqa: ANN202
                ctx = RunCtx(run_id=self.run_id, stage="discover", kind="discovery",
                             cwd=self.root)
                return _run_task(task, prompt, run_ctx=ctx, models_cfg=models_cfg,
                                 jdb=self.jdb, kdb=self.kdb, root=self.root,
                                 now=self.now, **kw)

        try:
            result = runner(task, prompt)
        except Exception as exc:  # noqa: BLE001 - a dead model never fails the pass
            self.model_notes.append(f"{task}: call failed ({exc})")
            return []
        if not getattr(result, "ok", False):
            self.model_notes.append(
                f"{task}: {getattr(result, 'failure', None) or 'failed'} "
                f"({getattr(getattr(result, 'meta', None), 'error', None)})")
            return []
        served = getattr(result, "served_alias", None) or getattr(
            getattr(result, "meta", None), "served_model", None)
        if served:
            self.model_used = str(served)
        try:
            doc = json.loads(_json_block(result.text or ""))
        except (ValueError, TypeError) as exc:
            self.model_notes.append(f"{task}: output was not JSON ({exc})")
            return []
        drafts = doc.get("drafts") if isinstance(doc, dict) else doc
        return list(drafts or [])

    # ---------------------------------------------------------------- queue

    def open_hypotheses(self) -> list[str]:
        """Recorded but never graded, oldest first — the work queue."""
        return [str(r["id"]) for r in reversed(self.ledger.listing())
                if r.get("state") == "open" and r.get("id")]

    def enqueue_seeds(self) -> list[str]:
        """Record every seed the ledger has not seen. Idempotent by construction: the
        ledger refuses to overwrite an existing id, so a re-run adds nothing."""
        added: list[str] = []
        for h in load_seeds(self.root, self.cfg.discovery.seed_dir):
            if self.ledger.path(h.id).exists():
                continue
            self.ledger.record(h)
            added.append(h.id)
        return added

    # ---------------------------------------------------------------- trial accounting

    def charge_screened_ancestors(self, h: Hypothesis) -> list[str]:
        """Pay for the screen when a proposable hypothesis is seeded from one.

        ``seed`` is free text, and by convention it names the hypothesis a new one
        supersedes (``Ledger.record`` says so in its own refusal message). Any id in it
        that was measured by a pass which could not propose is charged to the selection
        family now: the candidate in hand was chosen with that screening result in view, so
        the two-stage search costs two stages. Returns the ids charged.
        """
        seed = str(getattr(h, "seed", "") or "")
        if not seed:
            return []
        screened = self.trials.screened()
        charged: list[str] = []
        for token in re.findall(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]{2,48}", seed):
            if token == h.id or token not in screened:
                continue
            self.trials.promote(
                token, f"{h.id} was seeded from screened trial {token}: the nightly "
                       f"measurement that could not propose is part of the search that "
                       f"produced this candidate, so it is charged to the family now")
            charged.append(token)
        return charged

    def settle_family(self) -> str | None:
        """Close the selection family if a change this loop proposed has been MERGED.

        Read from ``changes/*.json`` (tier 0, always present) rather than the journal, so a
        pass with no database still settles correctly. Only ``status: "merged"`` counts —
        a held or rejected proposal leaves the family open, because being wrong must not
        buy a clean slate — and only a change this loop authored, because it is this loop's
        search that has the multiplicity.
        """
        best: tuple[str, str] | None = None                # (decided_at, change_id)
        for p in sorted((self.root / "changes").glob("*.json")) \
                if (self.root / "changes").exists() else []:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict) or doc.get("status") != "merged":
                continue
            if not str(doc.get("author_run_id") or "").startswith("discovery-"):
                continue
            at = str((doc.get("decision") or {}).get("at") or doc.get("created_at") or "")
            cid = str(doc.get("id") or p.stem)
            if best is None or at > best[0]:
                best = (at, cid)
        if best is None:
            return None
        state = self.trials.read()
        if state["family"].get("closed_by") == best[1]:
            return None
        self.trials.close_family(best[1], best[0])
        self.model_notes.append(
            f"selection family closed: {best[1]} was merged after the gate recomputed it, "
            f"so the deflated hurdle is formed from the trials spent since then rather "
            f"than from every measurement ever made")
        return best[1]

    # ---------------------------------------------------------------- 2. TEST

    def _runners(self):  # noqa: ANN202
        from evals import backtest_api as bt

        return (self._backtest or bt.run_backtest,
                self._walk_forward or bt.walk_forward,
                self._benchmark or bt.buy_and_hold)

    def test(self, h: Hypothesis) -> Study:
        """Measure the hypothesis exactly as its sealed measurement says.

        The sealed measurement wins over the pass config, always. The window, the folds and
        the pairs were fixed when the prediction was, and a loop that may re-choose the
        window after seeing the hypothesis is a loop that can always find a window.
        """
        from evals.backtest_api import compare

        study = Study(hypothesis=h)
        run_backtest, walk_forward, buy_and_hold = self._runners()
        m = h.measurement
        pairs = list(m.pairs) or list(self.pairs) or None
        folds = int(m.folds or 0)

        # A trial is a SELECTION trial iff the pass that spends it may propose. The pass
        # spec is the only input: `--no-propose` and `--dry-run` are human overrides, and
        # letting them reclassify a trial as a screen would let somebody run a search
        # without paying for it.
        selection = bool(self.spec.propose)
        self.trials.add(f"{h.id}: {h.variable} [{h.measurement.timerange}]",
                        selection=selection, hypothesis=h.id, pass_name=self.pass_name)
        if selection:
            self.charge_screened_ancestors(h)

        study.baseline = run_backtest({}, m.timerange, pairs, root=self.root)
        study.variant = run_backtest(dict(h.patch), m.timerange, pairs, root=self.root)
        study.comparison = compare(study.variant, study.baseline, root=self.root)
        study.benchmark = getattr(study.comparison, "benchmark", None)
        if study.benchmark is None:
            try:
                study.benchmark = buy_and_hold("BTC/USDT", study.baseline.start,
                                               study.baseline.end, root=self.root)
            except Exception as exc:  # noqa: BLE001 - reported, never guessed at
                study.notes.append(f"buy-and-hold benchmark unavailable: {exc}")
        if folds:
            try:
                study.walk = walk_forward(dict(h.patch), folds, timerange=m.timerange,
                                          pairs=pairs, root=self.root)
            except Exception as exc:  # noqa: BLE001
                study.notes.append(f"walk-forward failed: {exc}")
        return study

    # ---------------------------------------------------------------- 3. VALIDATE

    def label_stats(self) -> dict[str, Any]:
        """Triple-barrier labels, their effective sample size, and a purge that is checked.

        The measured fact this exists for: on this repo's own 4h panel, 19,879 labelled
        rows carry 3,020 independent observations. A result quoted against 19,879 is quoted
        against a sample that does not exist, so the row count is reported beside the
        effective N and never instead of it. The leakage count is an assertion rather than
        a promise: a training label that overlaps its test window has already seen it.
        """
        if self._label_stats is not None:
            return self._label_stats()
        from runs.features import load_candles
        from runs.features import sampling as smp

        candles = load_candles(DEFAULT_LABEL_PAIR, DEFAULT_LABEL_TF)
        labels = smp.triple_barrier(candles["close"])
        splits = list(smp.purged_splits(labels, n_splits=5))
        purged = embargoed = leaks = 0
        for s in splits:
            purged += int(s.purged)
            embargoed += int(s.embargoed)
            t0, t1 = labels.t0, labels.t1
            start, end = int(t0[s.test].min()), int(t1[s.test].max())
            leaks += int(((t1[s.train] >= start) & (t0[s.train] <= end)).sum())
        return {"rows": float(len(labels)), "effective_n": float(smp.effective_n(labels)),
                "folds": len(splits), "purged": purged, "embargoed": embargoed,
                "leaks": leaks}

    def validate(self, study: Study) -> Validation:
        """The edge-audit discipline, computed — never asserted, never recalled."""
        from runs.features import sampling as smp

        gates = self.cfg.discovery.gates
        base, cand = study.baseline, study.variant
        rows = eff = 0.0
        purged = embargoed = leaks = 0
        folds_n = 0
        try:
            stats = self.label_stats()
            rows = float(stats.get("rows") or 0.0)
            eff = float(stats.get("effective_n") or 0.0)
            folds_n = int(stats.get("folds") or 0)
            purged = int(stats.get("purged") or 0)
            embargoed = int(stats.get("embargoed") or 0)
            leaks = int(stats.get("leaks") or 0)
        except Exception as exc:  # noqa: BLE001 - a missing panel is reported, not faked
            study.notes.append(f"effective sample size unavailable: {exc}")

        counter = self.trials.read()
        n_trials = int(counter.get("n_trials") or 0)
        n_selection = int(counter["family"]["n_selection_trials"])
        n_selection_all = int(counter["n_selection_trials"])
        years = float(getattr(base, "years", 0.0) or 0.0)
        b_sharpe = float(getattr(base, "sharpe_daily", 0.0) or 0.0)
        c_sharpe = float(getattr(cand, "sharpe_daily", 0.0) or 0.0)
        # N is the open selection family, not every measurement ever made. TrialCounter's
        # docstring is where that decision and its reasoning live.
        expected = (float(smp.expected_max_sharpe(max(n_selection, 1), max(years, 1e-6)))
                    if years > 0 else 0.0)
        hurdle = b_sharpe + expected
        try:
            ytd = float(smp.years_to_detect(c_sharpe, b_sharpe))
        except Exception:  # noqa: BLE001
            ytd = float("inf")

        bench_ret = float(getattr(study.benchmark, "net_return_pct", 0.0) or 0.0)
        beat_strategy = cand.net_return_pct > base.net_return_pct or (
            cand.max_drawdown_pct < base.max_drawdown_pct - 1.0
            and cand.net_return_pct > base.net_return_pct - 1.0)
        beat_btc = study.benchmark is not None and cand.net_return_pct > bench_ret

        oos_rate = getattr(study.walk, "oos_win_rate", None)
        oos_mean = getattr(study.walk, "oos_mean_delta_pct", None)

        problems: list[str] = []
        # Found by running this loop for the first time, and listed FIRST because it
        # explains everything downstream of it: `params.rebalance_band` has a bounds entry
        # and passes every other check, and the strategies read the band from the GATE
        # config instead (`strategies/SleeveA.py: gate_cfg.rebalance_band`). The patch
        # therefore measured absolutely nothing — every metric came back bit-identical —
        # and the loop was perfectly willing to package it as a change. A result where
        # nothing moved beyond the noise floor is not a small win; it is a key that is not
        # wired to anything. It belongs here, in the validation, rather than in a local
        # check inside `propose`: a reader of the grade file has to be able to see it.
        if getattr(study.comparison, "is_noise", False):
            problems.append(
                "nothing moved beyond the noise floor. Either the change does nothing, or "
                "the key it names is not the one the strategy reads — check that before "
                "spending another trial on it")
        if eff <= 0:
            problems.append(
                "no effective sample size could be computed, so there is nothing to quote "
                "this against. A row count is not a sample size when labels overlap, and "
                "on this repo's own 4h panel it overstates the independent sample 6.6x")
        if leaks:
            problems.append(
                f"{leaks} training labels overlap a test window — the purge did not hold, "
                f"so every cross-validated number here has already seen its test set")
        if int(cand.trades) < int(gates.min_trades):
            problems.append(
                f"{cand.trades} trades is below the floor of {gates.min_trades}; a result "
                f"on this few trades is the trades, not the rule")
        if gates.require_hurdle and c_sharpe <= hurdle:
            problems.append(
                f"Sharpe {c_sharpe:.3f} does not clear the deflated hurdle {hurdle:.3f} "
                f"(baseline {b_sharpe:.3f} + expected max Sharpe {expected:.3f} from "
                f"N={n_selection} trials in the open selection family over T={years:.1f}y). "
                f"Beating the baseline is a coin flip with a decimal point.")
        if study.walk is None:
            problems.append("no walk-forward: the headline is in-sample, and an "
                            "in-sample-only winner is rejected downstream anyway")
        else:
            if len(study.walk.folds) < int(gates.min_folds):
                problems.append(
                    f"{len(study.walk.folds)} folds is below the floor of {gates.min_folds}")
            if float(oos_rate or 0.0) < float(gates.min_oos_win_rate):
                problems.append(
                    f"out-of-sample win rate {float(oos_rate or 0.0):.0%} is below "
                    f"{float(gates.min_oos_win_rate):.0%}")
        if gates.require_beat_benchmark_direction and not beat_strategy and not beat_btc:
            problems.append(
                "loses to the shipped strategy AND to buy-and-hold BTC over the same "
                "window — one baseline is how a bad result gets sold, and this clears "
                "neither")

        return Validation(
            rows=int(rows), effective_n=round(eff, 1),
            rows_per_effective_sample=round(rows / eff, 2) if eff else 0.0,
            folds=folds_n, purged=purged, embargoed=embargoed, leaks=leaks,
            n_trials=n_trials, n_selection_trials=n_selection,
            n_selection_trials_all_time=n_selection_all, years=round(years, 2),
            baseline_sharpe=round(b_sharpe, 4), candidate_sharpe=round(c_sharpe, 4),
            expected_max_sharpe=round(expected, 4), deflated_hurdle=round(hurdle, 4),
            clears_hurdle=bool(c_sharpe > hurdle),
            years_to_detect=round(ytd, 1) if ytd != float("inf") else -1.0,
            oos_win_rate=oos_rate, oos_mean_delta_pct=oos_mean,
            trades=int(cand.trades),
            beat_strategy_baseline=bool(beat_strategy), beat_btc_buy_and_hold=bool(beat_btc),
            problems=tuple(problems),
        )

    # ---------------------------------------------------------------- 5. RECORD

    def record(self, study: Study) -> Any:
        """Grade the sealed prediction against the measured numbers, WITH the validation.

        Both answers are computed, never written: the prediction verdict by
        :func:`evals.hypothesis.prediction_verdict_for` from the deltas, and the decisive
        ``may_become_a_change`` by :func:`evals.hypothesis.change_blockers` from that
        verdict plus the validation. Handing the validation in is what makes the grade file
        self-contained — the reader who sees "supported" sees the refusal in the same
        breath — and a study whose validation never ran grades as "may not become a
        change", which is the honest reading of a measurement nobody checked.
        """
        evidence = {
            "run_id": self.run_id, "pass": self.pass_name,
            "timerange": study.baseline.timerange,
            "fee_bps": study.baseline.fee_bps, "slippage_bps": study.baseline.slippage_bps,
            "baseline_digest": study.baseline.digest, "variant_digest": study.variant.digest,
            "benchmark": study.benchmark.as_dict() if study.benchmark else None,
            "walk_forward": study.walk.as_dict() if study.walk else None,
            "better": list(getattr(study.comparison, "better", ())),
            "worse": list(getattr(study.comparison, "worse", ())),
            "per_regime": [r.as_dict() for r in (study.variant.per_regime or ())],
        }
        note = "; ".join(study.validation.problems) if study.validation else ""
        return self.ledger.grade(
            study.hypothesis.id, study.baseline.headline(), study.variant.headline(),
            validation=study.validation.as_dict() if study.validation else None,
            evidence=evidence, note=note)

    # ---------------------------------------------------------------- 4. PROPOSE

    def propose(self, study: Study) -> str | None:
        """Package a survivor as a ``changes/*.json`` through the existing gate.

        Everything this writes is a CLAIM. ``evals/verify_change.py`` re-reads the commit,
        recomputes the bounds arithmetic, re-runs the backtest and the walk-forward in a
        worktree and compares; ``runs/apply_changes.py`` decides. A mismatch beyond the
        tolerance is journalled against the change and the loop's hit rate.
        """
        if not (self.spec.propose and self.allow_propose):
            return None
        # ONE question, asked of the grade: may this become a change? The grade computed it
        # from the prediction verdict AND the validation, so there is no second place here
        # where the two could be weighed against each other differently — and no way to
        # read the flattering half of a graded result by accident.
        if study.grade is None:
            study.notes.append(
                "not proposed: the hypothesis was never graded, so nothing has decided "
                "whether this may become a change")
            return None
        if not study.may_become_a_change:
            for b in study.blocked_because:
                study.notes.append(f"not proposed: {b}")
            return None

        change = bounded_param_change(self.cfg, dict(study.hypothesis.patch), "a",
                                      root=self.root)
        for r in change.refusals:
            study.notes.append(f"not proposable: {r}")
        if not change.ok:
            return None

        from runs import apply_changes, worktree

        change_id = f"{gulf_now(self.now).strftime('%Y-%m-%d')}-{study.hypothesis.id[11:]}"
        change_id = change_id[:60].rstrip("-")
        if (self.root / "changes" / f"{change_id}.json").exists():
            study.notes.append(f"not proposed: {change_id} already exists")
            return None
        if self.dry_run:
            study.notes.append(f"dry run: would have proposed {change_id}")
            return None

        try:
            wt = worktree.create(self.cfg, "discovery", change_id, live_root=self.root)
        except Exception as exc:  # noqa: BLE001 - no worktree means no commit means no change
            study.notes.append(f"not proposed: no worktree ({exc})")
            return None
        self.wt = wt

        rel = getattr(self.cfg.paths, f"params_{change.sleeve}")
        target = wt.path / rel
        doc = json.loads(target.read_text(encoding="utf-8"))
        for dotted, value in change.updates.items():
            node = doc.setdefault("params", {})
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        summary = f"{study.hypothesis.variable} -> " + ", ".join(
            f"{k}={v}" for k, v in sorted(change.updates.items()))
        subprocess.run(["git", "add", rel], cwd=wt.path, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", f"discovery: {summary}"],
                       cwd=wt.path, capture_output=True, text=True)
        commit = (subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt.path,
                                 capture_output=True, text=True).stdout or "").strip()
        if not commit:
            study.notes.append("not proposed: the worktree produced no commit")
            return None

        doc_change = {
            "id": change_id,
            "created_at": utc_iso(self.now),
            "author_run_id": self.run_id,
            "author_model": self.model_used or "code:discovery",
            "prompt_version": f"discovery.{self.pass_name}",
            "tier": 1,
            "kind": "params",
            "target": rel,
            "what": {"summary": summary, "commit": commit, "op": "edit",
                     "files": [rel]},
            "why": self._why(study),
            "bounds_check": change.bounds_check,
            "backtest": {
                "timerange": study.baseline.timerange,
                "years": round(study.baseline.years, 2),
                "fee_bps": study.baseline.fee_bps,
                "slippage_bps": study.baseline.slippage_bps,
                "baseline": study.baseline.headline(),
                "candidate": study.variant.headline(),
            },
            "branch": wt.branch,
            "worktree": str(wt.path),
            "status": "proposed",
        }
        if study.walk is not None:
            doc_change["walk_forward"] = {
                "windows": len(study.walk.folds), "scheme": "expanding",
                "in_sample_delta": 0.0,
                "out_sample_delta": float(study.walk.oos_mean_delta_pct),
                "pass": bool(study.walk.passed),
            }
        changes_dir = self.root / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(changes_dir / f"{change_id}.json",
                          json.dumps(doc_change, indent=2, sort_keys=True) + "\n")
        try:
            apply_changes.record_event(self.jdb, change_id, "proposed",
                                       "system:discovery",
                                       note=f"hypothesis {study.hypothesis.id}",
                                       now=self.now)
        except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a run
            study.notes.append(f"change_events row not written: {exc}")
        self.alert(
            f"discovery proposed {change_id} from hypothesis {study.hypothesis.id} — "
            f"every number in it is recomputed by the gate before anything merges", "warn")
        return change_id

    def _why(self, study: Study) -> str:
        v = study.validation
        c = study.comparison
        bench = (f", buy-and-hold BTC {study.benchmark.net_return_pct:+.2f}%"
                 if study.benchmark else "")
        wf = (f"; out-of-sample win rate {study.walk.oos_win_rate:.0%} over "
              f"{len(study.walk.folds)} folds, mean delta "
              f"{study.walk.oos_mean_delta_pct:+.2f}pp" if study.walk else "")
        gave_up = (", ".join(getattr(c, "worse", ())) or "nothing measured worse")
        return (
            f"hypothesis {study.hypothesis.id} was pre-registered with its falsifier "
            f"({study.hypothesis.falsifier}); its predictions came true AND its validation "
            f"came back clean, which is the only combination that may become a change. "
            f"Costed at "
            f"{study.baseline.fee_bps:.0f}+{study.baseline.slippage_bps:.0f} bps over "
            f"{study.baseline.timerange}: net return "
            f"{study.baseline.net_return_pct:+.2f}% -> "
            f"{study.variant.net_return_pct:+.2f}%, max drawdown "
            f"{study.baseline.max_drawdown_pct:.2f}% -> "
            f"{study.variant.max_drawdown_pct:.2f}%{bench}{wf}. What it gave up: "
            f"{gave_up}. Sharpe {v.candidate_sharpe:.3f} clears the deflated hurdle "
            f"{v.deflated_hurdle:.3f} at N={v.n_selection_trials} trials in the open "
            f"selection family ({v.n_selection_trials_all_time} all time, "
            f"{v.n_trials} measurements all time); effective_n "
            f"{v.effective_n:,.0f} (not {v.rows:,} rows). Telling these two Sharpes apart "
            f"live at 80% power would take {v.years_to_detect:,.0f} years, so this is a "
            f"backtest result and nothing more. Every number here is a claim until "
            f"evals/verify_change.py recomputes it.")

    # ---------------------------------------------------------------- report

    def report(self) -> Path:
        """The write-up, including — especially including — the negatives."""
        g = gulf_now(self.now)
        counts = self.trials.read()
        lines = [
            f"# Discovery {g.strftime('%Y-%m-%d')} ({self.pass_name})",
            "",
            f"- run_id: `{self.run_id}`",
            f"- window: `{self.spec.timerange}`, folds: {self.spec.folds}, "
            f"propose: {self.spec.propose}",
            f"- trials: **{counts['family']['n_selection_trials']}** in the open selection "
            f"family — the N the deflated hurdle is formed from — of "
            f"{counts['n_selection_trials']} selection trials and "
            f"{counts['n_trials']} measurements all time. A pass that cannot propose "
            f"(`propose: false`) measures without adding to the hurdle; the all-time "
            f"counts never fall.",
            f"- model: {self.model_used or 'none (queue-only pass)'}",
            "",
        ]
        if self.generated:
            lines += ["## Generated", "",
                      *[f"- `{i}`" for i in self.generated], ""]
        for note in self.model_notes:
            lines.append(f"- {note}")
        if self.model_notes:
            lines.append("")
        if not self.studies:
            lines += ["## Studies", "", "Nothing ran this pass.", ""]
        for s in self.studies:
            # Both answers in the heading, with the decisive one first and named. A reader
            # skimming this file must not be able to mistake "my predictions came true" for
            # "this is real".
            eligible = "MAY BECOME A CHANGE" if s.may_become_a_change else "NOT A CHANGE"
            lines.append(f"## {s.hypothesis.id} — **{eligible}** "
                         f"(prediction: {s.prediction_verdict.upper()})")
            lines.append("")
            lines.append("```")
            lines.append(s.hypothesis.describe())
            lines.append("```")
            if s.skipped:
                lines += ["", f"Skipped: {s.skipped}", ""]
                continue
            lines.append("")
            lines.append("```")
            if s.comparison is not None:
                lines.append(s.comparison.describe())
            if s.walk is not None:
                lines.append(s.walk.describe())
            if s.validation is not None:
                lines.append(s.validation.describe())
            if s.grade is not None:
                lines.append(s.grade.describe())
            lines.append("```")
            if s.notes:
                lines += ["", *[f"- {n}" for n in s.notes]]
            if s.change_id:
                lines += ["", f"Proposed as `changes/{s.change_id}.json` — "
                              "held until the gate recomputes it."]
            lines.append("")
        path = self.root / self.cfg.discovery.report_dir / f"{self.run_id}.md"
        atomic_write_text(path, "\n".join(lines) + "\n")
        return path

    # ---------------------------------------------------------------- journal

    def journal(self, status: str, error: str | None = None) -> None:
        """Record the run. A journalling failure never destroys the work it describes.

        This is the repo's standing rule about journal writers, and the first real
        invocation of this loop earned it: six docker backtests, a graded hypothesis and a
        published report all completed, and then an uninitialised ``runs`` table turned a
        successful pass into a traceback and a non-zero exit — which cron would have
        reported as a failed job over work that had in fact succeeded.
        """
        if self.jdb is None:
            return
        try:
            self.jdb.execute(
                "INSERT OR REPLACE INTO runs(run_id, stage, kind, started_utc,"
                " finished_utc, requested_model, served_model, prompt_version, status,"
                " error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, "discover", "discovery", utc_iso(self.now), utc_iso(),
                 self.cfg.discovery.select_task, self.model_used,
                 f"discovery.{self.pass_name}", status, error))
            self.jdb.commit()
        except Exception as exc:  # noqa: BLE001 - the ledger and the report are the record
            print(f"discovery: journal row not written ({exc})", file=sys.stderr)

    # ---------------------------------------------------------------- flow

    @property
    def job_name(self) -> str:
        """The ``ops.schedules`` / autonomy-gate name for this pass."""
        return f"discovery_{self.pass_name}"

    def permit(self):  # noqa: ANN201 - ops.autonomy.Permit
        """Ask the ONE autonomy gate whether this pass may run at all.

        The cron line already asks it (``python -m ops.autonomy run discovery_<p>``), so on
        the scheduled path this is a second, identical answer. It is here for the path cron
        does not cover — a human or the console starting a pass by hand — because "the loop
        may run" is one decision with one owner, and a second copy of the policy in this
        file would be a second thing to keep in sync.
        """
        from ops import autonomy

        return autonomy.check(self.job_name, cfg=self.cfg, root=self.root)

    def main_flow(self, only: str | None = None) -> int:
        from ops.lib import kill as killlib
        from ops.lib import paths

        if not self.cfg.discovery.enabled:
            self.journal("skipped", "discovery.enabled is false")
            return 0
        if killlib.is_engaged(self.cfg, paths.state_root()):
            self.journal("killed")
            return 0
        try:
            permit = self.permit()
        except Exception as exc:  # noqa: BLE001 - an unreadable gate means NO, not yes
            self.journal("skipped", f"autonomy gate unreadable: {exc}")
            return 0
        if not permit.allowed:
            self.journal("skipped",
                         f"autonomy: {permit.reason} (needs {permit.required}, "
                         f"highest level is {permit.level})")
            return 0
        if permit.degrade:
            self.model_notes.append(
                "spend is at a ceiling: this pass runs on the cheapest permitted tier")

        try:
            # Before anything is measured: has a change of ours been merged since the last
            # pass? If so the selection family it came out of is finished, and the trials
            # spent in it are history rather than a bar for the next candidate.
            self.settle_family()
            added = self.enqueue_seeds()
            if added:
                self.model_notes.append(f"seeded {len(added)} hypotheses: {', '.join(added)}")
            self.generate()
            queue = [only] if only else self.open_hypotheses()
            budget_per = BACKTEST_COST_S * (2 + 2 * max(self.spec.folds, 0))
            for hid in queue[:max(int(self.spec.hypotheses), 1) if not only else 1]:
                try:
                    h = self.ledger.load(hid)
                except HypothesisError as exc:
                    self.model_notes.append(f"{hid}: {exc}")
                    continue
                # Every job here is idempotent and safe to rerun. A graded hypothesis is
                # FINISHED: re-measuring one is exactly the "run it again until it passes"
                # the ledger exists to prevent, so a rerun says so and moves on rather than
                # crashing on the ledger's refusal.
                if self.ledger.grade_path(hid).exists():
                    self.studies.append(Study(
                        hypothesis=h, grade=_existing_grade(self.ledger, hid),
                        skipped="already graded — a measurement is not re-run until it "
                                "passes. Record a new hypothesis with a new id, and say "
                                "in `seed` which one it supersedes."))
                    continue
                if not only and self._budget_left_s() < budget_per:
                    self.studies.append(Study(
                        hypothesis=h,
                        skipped=f"not started: {self._budget_left_s():.0f}s left of the "
                                f"{self.spec.deadline_s}s pass budget, and this study "
                                f"needs about {budget_per:.0f}s. A recorded prediction "
                                f"with no grade blocks its id for ever, so it waits."))
                    break
                study = self.test(h)
                study.validation = self.validate(study)
                study.grade = self.record(study)
                study.change_id = self.propose(study)
                self.studies.append(study)
        except Exception as exc:  # noqa: BLE001 - the report is written either way
            self.report()
            self.journal("failed", f"{type(exc).__name__}: {exc}")
            self.alert(f"discovery {self.pass_name} failed: {exc}", "warn")
            raise
        path = self.report()
        self.journal("success")
        verdicts = ", ".join(
            f"{s.hypothesis.id}:{s.prediction_verdict}"
            f"{'' if s.may_become_a_change else ' (not a change)'}" for s in self.studies)
        self.alert(f"discovery {self.pass_name} done — {verdicts or 'nothing ran'} "
                   f"({path.relative_to(self.root)})")
        return 0


# ============================================================================ helpers


class _PriorGrade:
    """A grade read back off disk, so a rerun's report says what was decided rather
    than showing a finished hypothesis as ``OPEN``.

    Built from :meth:`evals.hypothesis.Ledger.read_grade`, which normalises both schemas
    and never guesses the decisive field: a pre-split grade file reads back as "may not
    become a change" with the reason spelled out.
    """

    def __init__(self, doc: dict[str, Any]) -> None:
        self.prediction_verdict = str(doc.get("prediction_verdict") or "open")
        self.may_become_a_change = bool(doc.get("may_become_a_change"))
        self.blocked_because = tuple(str(b) for b in (doc.get("blocked_because") or ()))
        self.measured_utc = str(doc.get("measured_utc") or "")
        self._doc = doc

    def as_dict(self) -> dict[str, Any]:
        return dict(self._doc)

    def describe(self) -> str:
        head = "MAY BECOME A CHANGE" if self.may_become_a_change else "NOT A CHANGE"
        return (f"graded {head} (prediction: {self.prediction_verdict.upper()}) at "
                f"{self.measured_utc} — see knowledge/research/hypotheses/")


def _existing_grade(ledger: Ledger, hypothesis_id: str) -> _PriorGrade | None:
    doc = ledger.read_grade(hypothesis_id)
    return _PriorGrade(doc) if doc is not None else None


def _json_block(text: str) -> str:
    """The JSON object in a model answer, whether or not it came fenced."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        body = body.rsplit("```", 1)[0]
    start, end = body.find("{"), body.rfind("}")
    return body[start:end + 1] if start >= 0 and end > start else body


# ============================================================================ cli


def parse_args(argv: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"pass_name": "light", "only": None, "allow_model": True,
                           "allow_propose": True, "dry_run": False}
    it = iter(argv)
    positional = True
    for a in it:
        if a == "--hypothesis":
            out["only"] = next(it, None)
        elif a == "--no-model":
            out["allow_model"] = False
        elif a == "--no-propose":
            out["allow_propose"] = False
        elif a == "--dry-run":
            out["dry_run"] = True
        elif a.startswith("-"):
            raise SystemExit(f"unknown option {a!r}")
        elif positional:
            out["pass_name"] = a
            positional = False
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    guard_env(require_anthropic=args["allow_model"])
    cfg = load_config()

    from ops import db
    from ops.lib import locks, tg

    with locks.acquire("discovery"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            run = DiscoveryRun(
                cfg, jdb, kdb, pass_name=args["pass_name"],
                allow_model=args["allow_model"], allow_propose=args["allow_propose"],
                dry_run=args["dry_run"],
                alert=lambda text, sev="info": tg.send(text, sev, conn=kdb))
            return run.main_flow(only=args["only"])


if __name__ == "__main__":
    sys.exit(main())
