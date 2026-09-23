"""Step 7 — the kill switch: engage from the console, block everywhere, disengage.

"Everywhere" is the assertion that matters, and it is made against the three independent
readers of the file rather than against the console's own answer:

* ``ops.lib.kill.is_engaged`` — what ``ops/healthcheck.py`` and the jobs read;
* ``strategies.riskgate.RiskGate.check_entry`` — what the in-container gate reads, and the
  first check in ``CHECK_ORDER`` after ``nav_valid``;
* ``runs.triggers.TriggerEngine.guards`` — what the signal planner reads before firing.

Engaging is frictionless (a session and a reason). Releasing needs step-up *and* the typed
phrase, and both refusals are proved.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops import db
from ops.config import load_config
from ops.lib import kill as killlib
from strategies import riskgate
from tests.e2e.conftest import Api, Sandbox

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 22, 8, 35, tzinfo=UTC)

#: Builds all six job entry points the way cron builds them — ``root`` is the CHECKOUT,
#: nothing names a state root — and reports where each one looked for the KILL file, what
#: it saw and what its ``main_flow`` did about it. Run in a subprocess so the sandbox's
#: ``$EARN_STATE_ROOT`` is the real environment and not a monkeypatch.
ENTRY_POINT_PROBE = """
import json
from ops import db
from ops.config import REPO_ROOT, load_config
from ops.healthcheck import Healthcheck
from ops.lib import kill as killlib
from ops.lib import paths
from runs.daily_review import DailyReview
from runs.maintenance import Maintenance
from runs.research_run import ResearchRun
from runs.review_run import ReviewRun
from runs.triggers import TriggerEngine

cfg = load_config()
j, k = db.init_all(cfg)
jdb, kdb = db.connect(j), db.connect(k)
quiet = lambda *a, **kw: True

jobs = {
    "TriggerEngine": TriggerEngine(cfg, jdb, kdb, root=REPO_ROOT),
    "ResearchRun": ResearchRun(cfg, jdb, kdb, root=REPO_ROOT, alert=quiet),
    "ReviewRun": ReviewRun(cfg, jdb, kdb, root=REPO_ROOT, alert=quiet),
    "DailyReview": DailyReview(cfg, jdb, kdb, root=REPO_ROOT, alert=quiet),
    "Maintenance": Maintenance(cfg, jdb, kdb, root=REPO_ROOT, alert=quiet),
    "Healthcheck": Healthcheck(cfg, jdb, kdb, {}, root=REPO_ROOT, sender=quiet,
                               deliver=quiet),
}
report = {
    "roots_differ": str(REPO_ROOT) != str(paths.state_root()),
    "state_root": {n: str(o.state_root) for n, o in jobs.items()},
    "sees_kill": {n: killlib.is_engaged(cfg, o.state_root) for n, o in jobs.items()},
    "planner_guards": jobs["TriggerEngine"].guards(),
}
report["exit_codes"] = {
    "ResearchRun": jobs["ResearchRun"].main_flow("0830"),
    "ReviewRun": jobs["ReviewRun"].main_flow(),
    "DailyReview": jobs["DailyReview"].main_flow(),
    "Maintenance": jobs["Maintenance"].main_flow(),
}
run_ids = [jobs[n].run_id for n in ("ResearchRun", "ReviewRun", "DailyReview",
                                    "Maintenance")]
placeholders = ",".join("?" * len(run_ids))
report["killed_stages"] = sorted({
    r["stage"] for r in jdb.execute(
        "SELECT stage FROM runs WHERE status='killed' AND run_id IN (%s)"
        % placeholders, run_ids)})
jobs["Healthcheck"].check_kill()
report["healthcheck_processed"] = bool(kdb.execute(
    "SELECT 1 FROM ops_state WHERE key='kill_processed'").fetchone())
print(json.dumps(report))
"""


def kill_file(sandbox: Sandbox) -> Path:
    cfg = load_config()
    return killlib.kill_path(cfg, sandbox.state)


def gate_for(root: Path, *, kill_path: Path) -> riskgate.RiskGate:
    base = riskgate.GateConfig.load(REPO / "config" / "riskgate.json", sleeve="b")
    flags = root / "knowledge" / "flags.json"
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(json.dumps({
        "version": 1, "updated_at": "2026-09-22T08:30:00Z", "flags": {}}))
    freshness = root / "knowledge" / "state" / "freshness.json"
    freshness.parent.mkdir(parents=True, exist_ok=True)
    freshness.write_text(json.dumps({
        "version": 1, "updated_at": "2026-09-22T08:30:00Z",
        "sources": {"candles_1h": {"latest_utc": "2026-09-22T08:30:00Z"}}}))
    cfg = dataclasses.replace(base, kill_path=str(kill_path), flags_path=str(flags),
                              freshness_path=str(freshness))
    return riskgate.RiskGate(cfg, riskgate.MemoryStateStore())


def portfolio() -> riskgate.PortfolioState:
    return riskgate.PortfolioState(
        nav=10000.0, free_usdt=8000.0, positions={"BTC/USDT": 2000.0}, now=NOW,
        ledger_cash=8000.0)


class TestKillSwitch:
    def test_engage_block_everywhere_then_release(self, api: Api, sandbox: Sandbox,
                                                  tmp_path: Path) -> None:
        path = kill_file(sandbox)
        assert not path.exists(), f"the sandbox started with {path} already engaged"

        gate = gate_for(tmp_path, kill_path=path)
        before = gate.check_entry("BTC/USDT", 1500.0, portfolio())
        assert before.allowed, before.reason

        # ---- engage: a session and a reason, no step-up ------------------------
        response = api.post("/api/kill", json={"reason": "end-to-end proof",
                                               "flatten": False})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["engaged"] is True
        assert "end-to-end proof" in body["reason"]
        assert path.exists(), f"{path} was not written"

        state = api.json("/api/kill")
        assert state["engaged"] is True
        assert state["since"], state
        assert "end-to-end proof" in state["reason"]

        # ---- blocked everywhere ------------------------------------------------
        cfg = load_config()
        assert killlib.is_engaged(cfg, sandbox.state) is True

        blocked = gate.check_entry("BTC/USDT", 1500.0, portfolio())
        assert blocked.allowed is False
        assert blocked.reason == "kill", blocked.reason
        assert blocked.checks["kill"] is False
        assert gate.cap_stake("BTC/USDT", 1500.0, portfolio()) >= 0.0

        # A risk exit is still allowed — the switch stops entries, not escapes.
        exit_decision = gate.check_exit("BTC/USDT", "stop_loss", portfolio())
        assert exit_decision.allowed, exit_decision.reason

        guards = self._planner_guards(sandbox, root=str(sandbox.state))
        assert "kill" in guards, guards

        # The header/meta the operator actually looks at agrees.
        meta = api.json("/api/meta")
        assert meta["kill"]["engaged"] is True, meta["kill"]

        # ---- releasing is deliberate -------------------------------------------
        wrong_phrase = api.delete("/api/kill", json={"confirm_phrase": "resume trading"})
        assert wrong_phrase.status_code in (400, 403), wrong_phrase.text
        assert path.exists(), "a refused release removed the file anyway"

        api.step_up()
        wrong_phrase = api.delete("/api/kill", json={"confirm_phrase": "resume trading"})
        assert wrong_phrase.status_code == 400, wrong_phrase.text
        assert "RESUME TRADING" in wrong_phrase.json()["error"]["message"]
        assert path.exists()

        released = api.delete("/api/kill", json={"confirm_phrase": "RESUME TRADING"})
        assert released.status_code == 200, released.text
        assert released.json()["engaged"] is False
        assert not path.exists()

        assert killlib.is_engaged(cfg, sandbox.state) is False
        after = gate.check_entry("BTC/USDT", 1500.0, portfolio())
        assert after.allowed, after.reason

    def _planner_guards(self, sandbox: Sandbox, *, root: str | None) -> list[str]:
        """``TriggerEngine.guards`` in a subprocess, as the scanner cron would run it."""
        arg = "None" if root is None else repr(root)
        script = (
            "import json;"
            "from datetime import UTC, datetime;"
            "from pathlib import Path;"
            "from ops import db;"
            "from ops.config import load_config;"
            "from runs.triggers import TriggerEngine;"
            "cfg = load_config();"
            "j, k = db.init_all(cfg);"
            f"root = None if {arg} is None else Path({arg});"
            "engine = TriggerEngine(cfg, db.connect(j), db.connect(k), root=root,"
            " now=datetime.now(UTC));"
            "print(json.dumps(engine.guards()))"
        )
        result = sandbox.py("-c", script)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_reader_that_defaults_its_root_still_sees_the_kill_file(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        """REGRESSION: the checkout root and the state root are not the same thing.

        ``console/routers/kill.py`` writes the KILL file under ``paths.state_root()``.
        The six job entry points used to default their ``root`` to
        ``ops.config.REPO_ROOT`` — the *checkout* — and hand that to
        ``kill.is_engaged``, so with ``$EARN_STATE_ROOT`` set (the configuration
        ``docs/contracts.md`` §1 documents, and the one this sandbox runs) the human
        could press KILL and every scheduled job would keep trading.

        They now carry a separate ``state_root``, always derived from
        ``paths.state_root()``. Passing the state root explicitly and leaving it to
        default must give the same answer — and neither may be a function of ``root``.
        """
        response = api.post("/api/kill", json={"reason": "root divergence probe",
                                               "flatten": False})
        assert response.status_code == 200, response.text
        try:
            assert kill_file(sandbox).exists()
            assert "kill" in self._planner_guards(sandbox, root=str(sandbox.state))
            assert "kill" in self._planner_guards(sandbox, root=None), \
                "a job that did not name a root read a different KILL file"
            # The checkout is a real, separate directory here, and naming it as the
            # engine's `root` must not move the switch.
            assert sandbox.repo != sandbox.state
            assert "kill" in self._planner_guards(sandbox, root=str(sandbox.repo)), \
                "the checkout root leaked back into the kill lookup"
        finally:
            api.step_up()
            released = api.delete("/api/kill",
                                  json={"confirm_phrase": "RESUME TRADING"})
            assert released.status_code == 200, released.text

    def test_a_state_root_kill_file_stops_every_job_entry_point(
        self, api: Api, sandbox: Sandbox
    ) -> None:
        """One KILL file, six entry points, each constructed the way cron constructs it.

        ``TriggerEngine`` refuses to plan; ``ResearchRun``, ``ReviewRun``,
        ``DailyReview`` and ``Maintenance`` short-circuit their ``main_flow`` and journal
        a ``killed`` run; ``Healthcheck`` finds the file and reports the incident. All
        six are built with ``root`` pointing at the CHECKOUT, which is what
        ``ops.config.REPO_ROOT`` gives them in production — the file is under the state
        root, and they have to find it anyway.
        """
        response = api.post("/api/kill", json={"reason": "every job must stop",
                                               "flatten": False})
        assert response.status_code == 200, response.text
        try:
            assert kill_file(sandbox).exists()
            report = self._every_entry_point(sandbox)
            assert report["roots_differ"] is True, report
            for name in ("TriggerEngine", "ResearchRun", "ReviewRun", "DailyReview",
                         "Maintenance", "Healthcheck"):
                assert report["state_root"][name] == str(sandbox.state), \
                    f"{name} resolved its state root to {report['state_root'][name]}"
                assert report["sees_kill"][name] is True, f"{name} missed the KILL file"
            assert "kill" in report["planner_guards"], report["planner_guards"]
            # ...and the four jobs with a kill short-circuit actually took it.
            assert report["exit_codes"] == {"ResearchRun": 0, "ReviewRun": 0,
                                            "DailyReview": 0, "Maintenance": 0}, report
            assert set(report["killed_stages"]) == {
                "decide", "review", "daily_review", "maintenance"}, report
            # ...and healthcheck, which both reads and (on a mode mismatch) writes the
            # file, processed the same one.
            assert report["healthcheck_processed"] is True, report
        finally:
            api.step_up()
            released = api.delete("/api/kill",
                                  json={"confirm_phrase": "RESUME TRADING"})
            assert released.status_code == 200, released.text

    def _every_entry_point(self, sandbox: Sandbox) -> dict:
        """Build all six jobs in a subprocess and report what each one saw."""
        result = sandbox.py("-c", ENTRY_POINT_PROBE)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_audit_log_records_both_halves(self, api: Api, sandbox: Sandbox) -> None:
        engaged = api.post("/api/kill", json={"reason": "audit probe", "flatten": False})
        assert engaged.status_code == 200, engaged.text
        api.step_up()
        released = api.delete("/api/kill", json={"confirm_phrase": "RESUME TRADING"})
        assert released.status_code == 200, released.text

        conn = db.connect(sandbox.state / "journal" / "journal.db")
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT actor, action, result FROM audit_log"
                " WHERE action IN ('kill.engage','kill.release') ORDER BY id")]
        finally:
            conn.close()
        actions = [r["action"] for r in rows]
        assert "kill.engage" in actions, actions
        assert "kill.release" in actions, actions
        assert all(r["actor"].startswith("human:console:") for r in rows), rows
        assert all(r["result"] in ("ok", "denied", "failed") for r in rows), rows

    def test_engaging_never_waits_on_the_ops_lock(self, api: Api, sandbox: Sandbox,
                                                  tmp_path: Path) -> None:
        """A transition wedged holding ops.lock must not be able to block the kill."""
        lock = sandbox.state / "ops" / "locks" / "ops.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(  # noqa: S603 - our own interpreter, fixed argv
            [sandbox.python, "-c",
             "import fcntl, sys, time;"
             f"fh = open({str(lock)!r}, 'w');"
             "fcntl.flock(fh, fcntl.LOCK_EX);"
             "sys.stdout.write('held\\n'); sys.stdout.flush();"
             "time.sleep(30)"],
            stdout=subprocess.PIPE, env=dict(sandbox.env), text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "held"
            response = api.post("/api/kill", json={"reason": "lock held", "flatten": False},
                                timeout=15.0)
            assert response.status_code == 200, response.text
            assert kill_file(sandbox).exists()
        finally:
            holder.kill()
            holder.wait(timeout=10)

        api.step_up()
        released = api.delete("/api/kill", json={"confirm_phrase": "RESUME TRADING"})
        assert released.status_code == 200, released.text
        assert not kill_file(sandbox).exists()
