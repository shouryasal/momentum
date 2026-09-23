"""Event-driven decision runs (spec: triggers, §7). Evaluated at the end of every
15-minute ingest cycle, fully deterministic, fully journaled.

The five conditions now live in :mod:`runs.signals.detectors` as registered detectors;
what is left here are one-line delegators that map a detector's candidates back to the
legacy reason strings, so the behaviour and its test suite survive the move:

  a. corroborated news in a watched event class newer than the last decision
     (and never older than 24h)                                -> ``news_event``
  b. regime flip vs the regime at the last decision            -> ``regime_flip``
  c. |last closed 4h move| over the threshold on a pair        -> ``move``
  d. drawdown within stop proximity                            -> ``near_stop``
  e. |current funding| over the threshold                      -> ``funding``

:meth:`TriggerEngine.guards` stays **the** guard implementation — the signal pipeline
imports it rather than forking it, so "KILL engaged", the planner cooldown, the daily cap
and stale market data mean exactly one thing in this system. Its cooldown and daily cap
read ``signals.planner.*``, falling back to the legacy ``triggers.*`` keys.

:meth:`TriggerEngine.evaluate` branches on ``signals.integration``: ``legacy`` is today's
behaviour (detector hits fire research directly); ``pipeline`` records the hits as
``signals`` rows and hands them to the scanner flow, which screens and validates before
anything fires. Either way an evaluation writes a ``trigger_events`` row — now with
``detail_json = {"signal_ids": [...]}`` so the legacy audit trail keeps working.

The fire path is a DETACHED spawn of ``envwrap.sh research -- python -m runs.research_run
<HHMM> --triggered-by ... [--signal-id ...]``: the research flock and the proposal-file
idempotency own the run from there; a trigger failure never fails ingest.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ops import db
from ops.config import REPO_ROOT, EarnConfig
from ops.lib import kill as killlib
from ops.lib import paths
from runs.common import gulf_now, run_id_for, utc_iso
from runs.signals import detectors as detectorslib


def freshness_path(cfg: EarnConfig, root: Path) -> Path:
    """Where ingest's freshness stamp lives (``knowledge/state/freshness.json``).

    ``ops.lib.freshness`` (P1) owns the writer and, once it lands, the path helper; this
    resolves through it when it is available and derives the sibling of
    ``paths.state_latest`` otherwise, so the guard reads exactly one file either way.
    """
    try:
        from ops.lib import freshness as freshnesslib  # noqa: PLC0415 — optional (P1)

        for attr in ("freshness_path", "path", "default_path"):
            fn = getattr(freshnesslib, attr, None)
            if callable(fn):
                return Path(fn(root))
    except ImportError:
        pass
    return root / Path(cfg.paths.state_latest).parent / "freshness.json"


class TriggerEngine:
    def __init__(self, cfg: EarnConfig, jdb: sqlite3.Connection,
                 kdb: sqlite3.Connection, *, root: Path | None = None,
                 state_root: Path | None = None,
                 now: datetime | None = None, spawn=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        #: the CHECKOUT: ``ops/envwrap.sh``, the detector feature builders, freshness.
        self.root = root or REPO_ROOT
        #: the DATA/STATE root, where the KILL file lives. It defaults to
        #: ``paths.state_root()`` and NEVER to the checkout, because that is what
        #: ``console/routers/kill.py`` writes through: with ``$EARN_STATE_ROOT`` set the
        #: two used to diverge and the human's KILL never reached this guard.
        self.state_root = Path(state_root) if state_root is not None else paths.state_root()
        self.now = now or datetime.now(UTC)
        self.spawn = spawn or self._detached
        self._ctx: detectorslib.Ctx | None = None

    def _detached(self, cmd: list[str]) -> None:
        subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    # ------------------------------------------------------------- detector context

    def build_ctx(self) -> detectorslib.Ctx:
        """A fresh detector context (features recomputed from the databases)."""
        from runs.signals.features import build as build_features

        features = build_features(self.kdb, self.cfg, now=self.now, jdb=self.jdb,
                                  root=self.root)
        return detectorslib.Ctx(cfg=self.cfg, features=features, kdb=self.kdb,
                                jdb=self.jdb, root=self.root, now=self.now)

    def ctx(self) -> detectorslib.Ctx:
        """The context of the evaluation in progress, or a fresh one.

        A single ``evaluate()`` primes this so the features are computed once; a direct
        call to one of the condition delegators recomputes, because a caller asking
        ``move_4h()`` twice means "what does the database say NOW".
        """
        return self._ctx if self._ctx is not None else self.build_ctx()

    def _reasons(self, detector: str, *, where=None) -> list[str]:
        candidates = detectorslib.registry[detector](self.ctx())
        if where is not None:
            candidates = [c for c in candidates if where(c)]
        return [c.reason for c in candidates]

    # ------------------------------------------------------------- conditions

    def _last_decide_started(self) -> str | None:
        row = self.jdb.execute(
            "SELECT MAX(started_utc) AS t FROM runs WHERE stage='decide'").fetchone()
        return row["t"] if row else None

    def news_reasons(self) -> list[str]:
        return self._reasons("news_event")

    def regime_flip(self) -> list[str]:
        return self._reasons("regime_flip")

    def move_4h(self) -> list[str]:
        return self._reasons("move", where=lambda c: c.detail.get("window") == "4h")

    def near_stop(self) -> list[str]:
        return self._reasons("near_stop")

    def funding(self) -> list[str]:
        return self._reasons("funding")

    # ------------------------------------------------------------- guards

    def planner_limits(self) -> tuple[int, int]:
        """``(cooldown_hours, max_per_day)`` from ``signals.planner``, legacy as fallback."""
        planner = self.cfg.signals.planner
        cooldown = planner.cooldown_hours if planner.cooldown_hours is not None \
            else (self.cfg.triggers.cooldown_hours or 0)
        cap = planner.max_per_day if planner.max_per_day is not None \
            else (self.cfg.triggers.max_per_day or 0)
        return int(cooldown), int(cap)

    def guards(self) -> list[str]:
        """THE guard implementation. The signal pipeline calls this one, never a copy."""
        blocked = []
        cooldown_hours, max_per_day = self.planner_limits()
        if killlib.is_engaged(self.cfg, self.state_root):
            blocked.append("kill")
        cd = utc_iso(self.now - timedelta(hours=cooldown_hours))
        row = self.jdb.execute(
            "SELECT 1 FROM runs WHERE stage='decide' AND started_utc >= ? LIMIT 1",
            (cd,)).fetchone()
        if row:
            blocked.append("cooldown")
        fired_today = self.kdb.execute(
            "SELECT COUNT(*) AS n FROM trigger_events WHERE fired=1 AND ts_utc"
            " LIKE ?", (self.now.strftime("%Y-%m-%d") + "%",)).fetchone()["n"]
        if fired_today >= max_per_day:
            blocked.append("daily_cap")
        from strategies.riskgate import data_age_minutes

        age = data_age_minutes(freshness_path(self.cfg, self.root), self.now)
        if age > self.cfg.ops.staleness_min:
            blocked.append("stale_data")
        return blocked

    # ------------------------------------------------------------- evaluate

    def journal(self, fired: bool, reasons: list[str], blocked: list[str],
                run_id: str | None, signal_ids: list[str] | None = None) -> None:
        self.kdb.execute(
            "INSERT INTO trigger_events(ts_utc, fired, reasons_json, blocked_json,"
            " run_id, detail_json) VALUES (?,?,?,?,?,?)",
            (utc_iso(self.now), int(fired), json.dumps(reasons),
             json.dumps(blocked) if blocked else None, run_id,
             json.dumps({"signal_ids": list(signal_ids)}) if signal_ids else None))
        self.kdb.commit()

    @staticmethod
    def triggered_by(reasons: list[str], signal_id: str | None = None) -> list[str]:
        """What ``--triggered-by`` carries: the signal id FIRST, then the reasons.

        ``docs/signals.md`` and spec §2.1 step 12 both write ``--triggered-by
        signal:<id>``; the code used to pass the detector's reason string alone, so the
        run's audit trail named ``move:BTC:-6.0`` and nothing tied it to the signal
        except the separate ``--signal-id``. Carrying both is strictly more: the machine
        link is the first element, the human-readable reason follows, and the legacy
        (no-signal) path is byte-for-byte unchanged.
        """
        head = [f"signal:{signal_id}"] if signal_id else []
        return head + [r for r in reasons if r and r not in head]

    def fire(self, reasons: list[str], signal_id: str | None = None) -> str:
        slot = gulf_now(self.now).strftime("%H%M")
        run_id = run_id_for(slot, self.now)
        cmd = ["bash", str(self.root / "ops" / "envwrap.sh"), "research", "--",
               sys.executable, "-m", "runs.research_run", slot,
               "--triggered-by", ",".join(self.triggered_by(reasons, signal_id))]
        if signal_id:
            cmd += ["--signal-id", signal_id]
        self.spawn(cmd)
        return run_id

    def evaluate(self) -> dict:
        if self.cfg.signals.enabled and self.cfg.signals.integration == "pipeline":
            return self._evaluate_pipeline()
        return self._evaluate_legacy()

    def _evaluate_legacy(self) -> dict:
        self._ctx = self.build_ctx()
        try:
            reasons = [*self.news_reasons(), *self.regime_flip(), *self.move_4h(),
                       *self.near_stop(), *self.funding()]
        finally:
            self._ctx = None
        blocked = self.guards() if reasons else []
        fired = bool(reasons) and not blocked
        run_id = self.fire(reasons) if fired else None
        self.journal(fired, reasons, blocked, run_id)
        return {"fired": fired, "reasons": reasons, "blocked": blocked,
                "run_id": run_id}

    def _evaluate_pipeline(self) -> dict:
        """Record detector hits as ``signals`` rows and hand them to the scanner flow.

        Nothing fires from here: a candidate still has to survive the screener, the
        validator and :meth:`guards` before ``research_run`` is spawned. ``scan()`` writes
        its own ``trigger_events`` rows for the signals it plans, so this evaluation only
        journals when it found something and planned nothing.
        """
        from runs.signals import pipeline as pipelinelib

        report = pipelinelib.scan(self.cfg, self.jdb, self.kdb, root=self.root,
                                  now=self.now, spawn=self.spawn)
        reasons = [f"signal:{sid}" for sid in report.new]
        if reasons and not report.planned:
            self.journal(False, reasons, [], None, signal_ids=report.new)
        elif not reasons:
            self.journal(False, [], [], None)
        return {"fired": bool(report.planned), "reasons": reasons,
                "blocked": [], "run_id": None, "report": report}


def evaluate_and_fire(cfg: EarnConfig, kdb: sqlite3.Connection,
                      jdb: sqlite3.Connection | None = None,
                      root: Path | None = None, now: datetime | None = None,
                      spawn=None, state_root: Path | None = None) -> dict | None:
    """Ingest's entry point. Opens its own journal connection when not given one."""
    if not cfg.triggers.enabled:
        return None
    root = root or REPO_ROOT
    own = jdb is None
    if own:
        jdb = db.connect(root / cfg.paths.journal_db)
    try:
        return TriggerEngine(cfg, jdb, kdb, root=root, state_root=state_root, now=now,
                             spawn=spawn).evaluate()
    finally:
        if own:
            jdb.close()
