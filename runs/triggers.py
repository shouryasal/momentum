"""Event-driven decision runs (spec: triggers). Evaluated at the end of every
15-minute ingest cycle, fully deterministic, fully journaled.

Conditions (any one is a reason to decide NOW instead of waiting for the next
08:30/16:00 slot):
  a. corroborated news in triggers.news_events newer than the last decision
     (and never older than 24h),
  b. regime flip vs the regime recorded at the last decision,
  c. |last closed 4h move| > triggers.move_4h_pct on a tradeable pair,
  d. drawdown within stop proximity (compute_hardcase_flags().near_stop),
  e. |current funding| > triggers.funding_abs_8h.

Guards (any one blocks the fire, journaled in blocked_json): KILL engaged,
cooldown_hours since ANY decide run, max_per_day fired triggers, stale market
data (a triggered decision on stale inputs would be worse than none).

Every evaluation writes a trigger_events row — fired or not — so the engine's
behaviour is fully reconstructable. The fire path is a DETACHED spawn of
`envwrap.sh research -- python -m runs.research_run <HHMM> --triggered-by ...`:
the research flock and the proposal-file idempotency own the run from there;
a trigger failure never fails ingest (the caller isolates it).
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
from runs import router
from runs.common import gulf_now, run_id_for, utc_iso


class TriggerEngine:
    def __init__(self, cfg: EarnConfig, jdb: sqlite3.Connection,
                 kdb: sqlite3.Connection, *, root: Path | None = None,
                 now: datetime | None = None, spawn=None):
        self.cfg = cfg
        self.jdb = jdb
        self.kdb = kdb
        self.root = root or REPO_ROOT
        self.now = now or datetime.now(UTC)
        self.spawn = spawn or self._detached

    def _detached(self, cmd: list[str]) -> None:
        subprocess.Popen(cmd, cwd=self.root, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    # ------------------------------------------------------------- conditions

    def _last_decide_started(self) -> str | None:
        row = self.jdb.execute(
            "SELECT MAX(started_utc) AS t FROM runs WHERE stage='decide'").fetchone()
        return row["t"] if row else None

    def news_reasons(self) -> list[str]:
        events = self.cfg.triggers.news_events
        if not events:
            return []
        floor = utc_iso(self.now - timedelta(hours=24))
        threshold = max(self._last_decide_started() or "", floor)
        rows = self.kdb.execute(
            "SELECT DISTINCT event_class FROM news_items WHERE corroborated=1"
            " AND event_class IN ({}) AND COALESCE(published_at, fetched_at) > ?"
            .format(",".join("?" * len(events))), (*events, threshold)).fetchall()
        return [f"news:{r['event_class']}" for r in rows]

    def regime_flip(self) -> list[str]:
        last = self._last_decide_started()
        if not last:
            return []
        cur = self.kdb.execute(
            "SELECT regime FROM state_snapshots WHERE regime IS NOT NULL"
            " ORDER BY ts_utc DESC LIMIT 1").fetchone()
        old = self.kdb.execute(
            "SELECT regime FROM state_snapshots WHERE regime IS NOT NULL"
            " AND ts_utc <= ? ORDER BY ts_utc DESC LIMIT 1", (last,)).fetchone()
        if cur and old and cur["regime"] != old["regime"]:
            return [f"regime:{old['regime']}->{cur['regime']}"]
        return []

    def move_4h(self) -> list[str]:
        out = []
        for pair in self.cfg.universe.pairs:
            row = self.kdb.execute(
                "SELECT open, close FROM candles WHERE pair=? AND tf='4h'"
                " AND is_closed=1 ORDER BY open_time DESC LIMIT 1", (pair,)).fetchone()
            if row and row["open"]:
                pct = (row["close"] / row["open"] - 1) * 100
                if abs(pct) > self.cfg.triggers.move_4h_pct:
                    out.append(f"move_4h:{pair.split('/')[0]}:{pct:+.1f}")
        return out

    def near_stop(self) -> list[str]:
        flags = router.compute_hardcase_flags(self.cfg, self.jdb, root=self.root,
                                              now=self.now)
        return ["near_stop"] if flags.near_stop else []

    def funding(self) -> list[str]:
        out = []
        for r in self.kdb.execute("SELECT symbol, last_rate FROM funding_current"):
            if r["last_rate"] is not None and abs(r["last_rate"]) > self.cfg.triggers.funding_abs_8h:
                out.append(f"funding:{r['symbol']}:{r['last_rate']:+.4f}")
        return out

    # ------------------------------------------------------------- guards

    def guards(self) -> list[str]:
        blocked = []
        if killlib.is_engaged(self.cfg, self.root):
            blocked.append("kill")
        cd = utc_iso(self.now - timedelta(hours=self.cfg.triggers.cooldown_hours))
        row = self.jdb.execute(
            "SELECT 1 FROM runs WHERE stage='decide' AND started_utc >= ? LIMIT 1",
            (cd,)).fetchone()
        if row:
            blocked.append("cooldown")
        fired_today = self.kdb.execute(
            "SELECT COUNT(*) AS n FROM trigger_events WHERE fired=1 AND ts_utc"
            " LIKE ?", (self.now.strftime("%Y-%m-%d") + "%",)).fetchone()["n"]
        if fired_today >= self.cfg.triggers.max_per_day:
            blocked.append("daily_cap")
        from strategies.riskgate import data_age_minutes

        age = data_age_minutes(self.root / self.cfg.paths.knowledge_db, self.now)
        if age > self.cfg.ops.staleness_min:
            blocked.append("stale_data")
        return blocked

    # ------------------------------------------------------------- evaluate

    def journal(self, fired: bool, reasons: list[str], blocked: list[str],
                run_id: str | None) -> None:
        self.kdb.execute(
            "INSERT INTO trigger_events(ts_utc, fired, reasons_json, blocked_json,"
            " run_id, detail_json) VALUES (?,?,?,?,?,?)",
            (utc_iso(self.now), int(fired), json.dumps(reasons),
             json.dumps(blocked) if blocked else None, run_id, None))
        self.kdb.commit()

    def fire(self, reasons: list[str]) -> str:
        slot = gulf_now(self.now).strftime("%H%M")
        run_id = run_id_for(slot, self.now)
        self.spawn(["bash", str(self.root / "ops" / "envwrap.sh"), "research", "--",
                    sys.executable, "-m", "runs.research_run", slot,
                    "--triggered-by", ",".join(reasons)])
        return run_id

    def evaluate(self) -> dict:
        reasons = [*self.news_reasons(), *self.regime_flip(), *self.move_4h(),
                   *self.near_stop(), *self.funding()]
        blocked = self.guards() if reasons else []
        fired = bool(reasons) and not blocked
        run_id = self.fire(reasons) if fired else None
        self.journal(fired, reasons, blocked, run_id)
        return {"fired": fired, "reasons": reasons, "blocked": blocked,
                "run_id": run_id}


def evaluate_and_fire(cfg: EarnConfig, kdb: sqlite3.Connection,
                      jdb: sqlite3.Connection | None = None,
                      root: Path | None = None, now: datetime | None = None,
                      spawn=None) -> dict | None:
    """Ingest's entry point. Opens its own journal connection when not given one."""
    if not cfg.triggers.enabled:
        return None
    root = root or REPO_ROOT
    own = jdb is None
    if own:
        jdb = db.connect(root / cfg.paths.journal_db)
    try:
        return TriggerEngine(cfg, jdb, kdb, root=root, now=now,
                             spawn=spawn).evaluate()
    finally:
        if own:
            jdb.close()
