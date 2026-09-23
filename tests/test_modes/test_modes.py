"""The mode state machine: transitions, rollback, recovery and the test-run reset."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ops import modes
from ops import preflight as pf
from ops.lib import kill as killlib
from ops.lib import mode_state as ms
from ops.lib import oplock, paths, signing
from tests.test_modes.conftest import SECRET, FakeBot, seed_run, write_mode


def _live_request(**over):
    base = {
        "sleeve": "a",
        "target": "LIVE_PROPOSE",
        "seed_usdt": 500.0,
        "preflight_id": "pf-test",
        "confirm_phrase": "GO LIVE A 500 USDT",
    }
    base.update(over)
    return modes.TransitionRequest(**base)


class TestGuards:
    def test_confirm_phrase_per_transition(self, cfg):
        assert modes.confirm_phrase_for(
            cfg, sleeve="a", from_state="TEST", target="LIVE_PROPOSE", seed_usdt=500
        ) == "GO LIVE A 500 USDT"
        assert modes.confirm_phrase_for(
            cfg, sleeve="b", from_state="LIVE_PROPOSE", target="LIVE_EXECUTE", seed_usdt=500
        ) == modes.CONFIRM_EXECUTE
        assert modes.confirm_phrase_for(
            cfg, sleeve="b", from_state="LIVE_EXECUTE", target="LIVE_PROPOSE", seed_usdt=500
        ) == modes.CONFIRM_DERISK
        # leaving live flattens by default and needs no phrase; not flattening does
        assert modes.confirm_phrase_for(
            cfg, sleeve="a", from_state="LIVE_PROPOSE", target="TEST", seed_usdt=500
        ) == ""
        assert modes.confirm_phrase_for(
            cfg, sleeve="a", from_state="LIVE_PROPOSE", target="TEST", seed_usdt=500,
            flatten=False,
        ) == modes.CONFIRM_LEAVE_POSITIONS

    def test_wrong_phrase_refuses_before_anything_happens(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with pytest.raises(modes.ModeError, match="confirmation phrase"):
            modes.transition(cfg, _live_request(confirm_phrase="go live a"), actor, deps=deps)
        assert jdb.execute("SELECT COUNT(*) FROM mode_transitions").fetchone()[0] == 0

    def test_step_up_is_required(self, cfg, deps):
        weak = modes.HumanActor.console("sid", step_up_ok=False)
        with pytest.raises(modes.ModeError, match="step-up"):
            modes.transition(cfg, _live_request(), weak, deps=deps)

    def test_automated_run_is_refused(self, cfg, deps, actor, monkeypatch):
        monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
        with pytest.raises(modes.ModeError, match="EARN_AUTOMATED_RUN"):
            modes.transition(cfg, _live_request(), actor, deps=deps)

    def test_a_transient_state_is_never_a_requestable_target(self, cfg, deps, actor):
        write_mode({"a": ms.SleeveState("TEST")})
        with pytest.raises(modes.ModeError, match="unknown target state"):
            modes.transition(
                cfg,
                modes.TransitionRequest(sleeve="a", target="ARMING", confirm_phrase=""),
                actor,
                deps=deps,
            )

    def test_illegal_transition_is_refused(self, cfg, deps, actor):
        # a sleeve stuck mid-arm may only go back to TEST, never forward
        write_mode({"a": ms.SleeveState("ARMING", "propose", "live-a-1", 500)})
        with pytest.raises(modes.ModeError, match="not an allowed transition"):
            modes.transition(cfg, _live_request(), actor, deps=deps)

    def test_only_a_human_actor_is_accepted(self):
        with pytest.raises(modes.ModeError, match="not a human actor"):
            modes.HumanActor(actor="system:cron")


class TestHappyPath:
    def test_test_to_live_propose(self, cfg, deps, actor, jdb, bots, docker, state_root):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-20260701-01", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-20260701-01")
        bots["a"].dry_run = False  # the recreated container comes back live
        bots["a"].bot_name = "earn-a-live"

        result = modes.transition(cfg, _live_request(), actor, deps=deps)

        assert result.to_state == "LIVE_PROPOSE"
        assert result.run_id.startswith("live-a-")
        names = [s["step"] for s in result.steps]
        assert names == list(modes.STEPS)
        assert all(s["status"] in ("ok", "skipped") for s in result.steps)

        # the signed file now says LIVE_PROPOSE with the new run and seed
        state = ms.load(secret=SECRET)
        assert state.verified and state.sleeve("a").state == "LIVE_PROPOSE"
        assert state.sleeve("a").submode == "propose"
        assert state.sleeve("a").seed_usdt == 500.0
        assert state.sleeve("a").run_id == result.run_id

        # the old run is closed with metrics, the new one is active
        rows = {r["run_id"]: dict(r) for r in jdb.execute("SELECT * FROM sleeve_runs")}
        assert rows["test-a-20260701-01"]["status"] == "closed"
        assert rows["test-a-20260701-01"]["final_metrics_json"]
        assert rows[result.run_id]["status"] == "active"
        assert rows[result.run_id]["mode"] == "live"
        assert rows[result.run_id]["ft_db_path"].endswith(f"{result.run_id}.sqlite")

        # transition row completed, steps journalled, audit written
        row = jdb.execute("SELECT * FROM mode_transitions WHERE id=?", (result.transition_id,)).fetchone()
        assert row["status"] == "completed" and row["finished_utc"]
        assert len(json.loads(row["steps_json"])) == len(modes.STEPS)
        assert row["confirm_hash"] == signing.sha256_text("GO LIVE A 500 USDT")
        assert jdb.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='mode.transition' AND result='ok'"
        ).fetchone()[0] == 1

        # entries were stopped and the container recreated with the earn service name
        assert "post:stopentry" in bots["a"].calls
        assert docker.calls and docker.calls[-1][-3:] == ["up", "-d", "--force-recreate"] or (
            "--force-recreate" in docker.calls[-1]
        )
        assert cfg.ops.bots["a"].service in docker.calls[-1]

        # var/runtime was rendered from the NEW state
        overlay = json.loads(paths.mode_overlay_path("a").read_text())
        assert overlay["dry_run"] is False
        assert result.run_id in overlay["db_url"]
        runtime = json.loads(paths.sleeve_runtime_path("a").read_text())
        assert runtime["require_approval"] is True and runtime["mode"] == "live"

    def test_leaving_live_flattens_and_returns_to_test(self, cfg, deps, actor, jdb, bots):
        write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        seed_run(jdb, cfg, run_id="live-a-1", mode="live", seed=500, submode="propose")
        bots["a"].trades = [{"trade_id": 7, "pair": "BTC/USDT"}]

        result = modes.transition(
            cfg,
            modes.TransitionRequest(sleeve="a", target="TEST", confirm_phrase="", flatten=True),
            actor,
            deps=deps,
        )
        assert result.to_state == "TEST"
        assert "forceexit:all" in bots["a"].calls
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"
        step = next(s for s in result.steps if s["step"] == "flatten")
        assert step["status"] == "ok"

    def test_leaving_live_without_flatten_needs_the_typed_override(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        seed_run(jdb, cfg, run_id="live-a-1", mode="live", seed=500, submode="propose")
        with pytest.raises(modes.ModeError, match="LEAVE POSITIONS UNMANAGED"):
            modes.transition(
                cfg,
                modes.TransitionRequest(sleeve="a", target="TEST", confirm_phrase="", flatten=False),
                actor,
                deps=deps,
            )


class TestFailureRollsBackAndKills:
    @pytest.mark.parametrize(
        ("step", "arrange"),
        [
            ("stopentry", lambda bots, deps, docker: setattr(bots["a"], "fail", "post:stopentry,post:stopbuy")),
            ("compose", lambda bots, deps, docker: setattr(docker, "ok", False)),
            ("verify", lambda bots, deps, docker: setattr(bots["a"], "up", False)),
            ("show_config", lambda bots, deps, docker: setattr(bots["a"], "dry_run", True)),
            (
                "reconcile",
                lambda bots, deps, docker: setattr(
                    deps, "reconcile", lambda s, r: ("mismatch", "BTC off by 0.5")
                ),
            ),
        ],
    )
    def test_each_failure_rolls_back(
        self, cfg, deps, actor, jdb, bots, docker, step, arrange
    ):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-1")
        bots["a"].bot_name = "earn-a-live"
        if step != "show_config":
            bots["a"].dry_run = False
        arrange(bots, deps, docker)

        with pytest.raises(modes.ModeTransitionError):
            modes.transition(cfg, _live_request(), actor, deps=deps)

        # the sleeve is back in TEST and the kill switch is engaged
        state = ms.load(secret=SECRET)
        assert state.sleeve("a").state == "TEST", f"{step}: not rolled back"
        assert killlib.is_engaged(cfg, deps.root), f"{step}: kill not engaged"

        row = jdb.execute("SELECT * FROM mode_transitions ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert row["error"]
        steps = json.loads(row["steps_json"])
        assert {"rollback", "kill"} <= {s["step"] for s in steps}

        # and no new run was opened
        assert jdb.execute(
            "SELECT COUNT(*) FROM sleeve_runs WHERE mode='live'"
        ).fetchone()[0] == 0

    def test_failing_preflight_never_writes_the_mode_file(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})

        def failing(request):
            return pf.PreflightResult(
                preflight_id="pf-test", request=request,
                items=[pf.Check("kill_clear", "Kill", True, pf.FAIL, "KILL engaged")],
                created_utc="2026-10-27T05:00:00Z", expires_utc="2099-01-01T00:00:00Z",
            )

        deps.preflight = failing
        with pytest.raises(modes.ModeTransitionError, match="preflight failed"):
            modes.transition(cfg, _live_request(), actor, deps=deps)
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"

    def test_a_failed_transition_stores_the_preflight_evidence(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-1")
        deps.reconcile = lambda s, r: ("mismatch", "ledger short 0.4 BTC")
        with pytest.raises(modes.ModeTransitionError):
            modes.transition(cfg, _live_request(), actor, deps=deps)
        row = jdb.execute("SELECT preflight_json FROM mode_transitions ORDER BY id DESC").fetchone()
        assert json.loads(row["preflight_json"])["ok"] is True


class TestRecovery:
    def test_interrupted_transition_is_recovered_at_start_up(self, cfg, deps, jdb, bots):
        write_mode({"a": ms.SleeveState("ARMING", "propose", "live-a-1", 500)})
        from ops import db as opsdb

        opsdb.write(
            jdb,
            "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
            " actor) VALUES ('a','TEST','LIVE_PROPOSE','2026-10-27T04:00:00Z','running','human:cli')",
            (),
        )
        recovered = modes.recover(cfg, deps=deps)

        assert [r.sleeve for r in recovered] == ["a"]
        assert "post:stopentry" in bots["a"].calls
        assert ms.load(secret=SECRET).sleeve("a").state == "TEST"
        assert killlib.is_engaged(cfg, deps.root)
        assert jdb.execute(
            "SELECT status FROM mode_transitions ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"] == "failed"

    def test_abandoned_row_without_a_stuck_sleeve_is_just_failed(self, cfg, deps, jdb, bots):
        write_mode({"a": ms.SleeveState("TEST")})
        from ops import db as opsdb

        opsdb.write(
            jdb,
            "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
            " actor) VALUES ('b','TEST','LIVE_PROPOSE','2026-10-27T04:00:00Z','running','human:cli')",
            (),
        )
        recovered = modes.recover(cfg, deps=deps)
        assert [r.sleeve for r in recovered] == ["b"]
        assert "post:stopentry" not in bots["b"].calls
        assert jdb.execute(
            "SELECT status FROM mode_transitions LIMIT 1"
        ).fetchone()["status"] == "failed"

    def test_nothing_to_recover_is_a_no_op(self, cfg, deps):
        write_mode({"a": ms.SleeveState("TEST"), "b": ms.SleeveState("TEST")})
        assert modes.recover(cfg, deps=deps) == []


class TestRunIds:
    def test_run_ids_are_unique_per_day(self, cfg, jdb):
        first = modes.new_run_id(jdb, "a", "test", datetime(2026, 10, 27, tzinfo=UTC))
        assert first == "test-a-20261027-01"
        seed_run(jdb, cfg, run_id=first)
        second = modes.new_run_id(jdb, "a", "test", datetime(2026, 10, 27, tzinfo=UTC))
        assert second == "test-a-20261027-02"

    def test_run_state_snapshot_is_run_scoped(self, cfg, jdb):
        jdb.execute(
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','run:test-a-1:day_anchor_nav','10000','2026-10-27T00:00:00Z')"
        )
        jdb.execute(
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','run:test-a-2:day_anchor_nav','9000','2026-10-27T00:00:00Z')"
        )
        jdb.commit()
        snap = modes.run_state_snapshot(jdb, "a", "test-a-1")
        assert snap == {"run:test-a-1:day_anchor_nav": "10000"}


class TestReset:
    def test_reset_archives_and_opens_a_fresh_run(self, cfg, deps, actor, jdb, bots):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-20260701-01", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-20260701-01")
        jdb.execute(
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','run:test-a-20260701-01:day_anchor_nav','10000','2026-07-01T00:00:00Z')"
        )
        jdb.commit()

        result = modes.reset_test_run(
            cfg, sleeve="a", actor=actor, deps=deps, seed_usdt=25000.0,
            label="wider stops", notes="trying 12% stop", confirm_phrase="RESET",
        )

        assert result.previous_run_id == "test-a-20260701-01"
        assert result.seed_usdt == 25000.0
        old = jdb.execute(
            "SELECT * FROM sleeve_runs WHERE run_id='test-a-20260701-01'"
        ).fetchone()
        assert old["status"] == "closed"
        # the closed run keeps its run-scoped anchors, so nothing is lost
        assert json.loads(old["final_state_json"]) == {
            "run:test-a-20260701-01:day_anchor_nav": "10000"
        }
        new = jdb.execute("SELECT * FROM sleeve_runs WHERE run_id=?", (result.run_id,)).fetchone()
        assert new["status"] == "active" and new["seed_usdt"] == 25000.0
        assert new["label"] == "wider stops"
        # fresh anchors: the new run has no risk_state rows of its own yet
        assert jdb.execute(
            "SELECT COUNT(*) FROM risk_state WHERE key LIKE ?", (f"run:{result.run_id}:%",)
        ).fetchone()[0] == 0
        # a new per-run freqtrade database
        assert new["ft_db_path"] != old["ft_db_path"]
        overlay = json.loads(paths.mode_overlay_path("a").read_text())
        assert overlay["dry_run"] is True and overlay["dry_run_wallet"] == 25000.0

    def test_reset_requires_the_typed_word(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with pytest.raises(modes.ModeError, match="RESET"):
            modes.reset_test_run(cfg, sleeve="a", actor=actor, deps=deps, confirm_phrase="reset")

    def test_reset_refuses_on_a_live_sleeve(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        with pytest.raises(modes.ModeError, match="only a TEST run"):
            modes.reset_test_run(cfg, sleeve="a", actor=actor, deps=deps, confirm_phrase="RESET")

    def test_reset_refuses_a_zero_seed(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with pytest.raises(modes.ModeError, match="greater than zero"):
            modes.reset_test_run(
                cfg, sleeve="a", actor=actor, deps=deps, seed_usdt=0.0, confirm_phrase="RESET"
            )

    def test_reset_survives_a_down_bot(self, cfg, deps, actor, jdb, bots):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        seed_run(jdb, cfg, run_id="test-a-1")
        bots["a"].fail = "post:stopentry,post:stopbuy"
        result = modes.reset_test_run(
            cfg, sleeve="a", actor=actor, deps=deps, confirm_phrase="RESET"
        )
        assert result.run_id
        assert any(s["step"] == "stopentry" and s["status"] == "warn" for s in result.steps)


class TestOpsLock:
    def test_a_held_lock_refuses_the_transition(self, cfg, deps, actor, jdb):
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        with oplock.acquire("someone-else", timeout_s=1), pytest.raises(oplock.OpsLockBusy):
            modes.transition(cfg, _live_request(), actor, deps=deps)


class TestBotControl:
    def test_stopentry_falls_back_to_stopbuy(self):
        class Old(FakeBot):
            def _post(self, path: str, payload: dict | None = None):
                if path == "stopentry":
                    raise RuntimeError("404")
                return super()._post(path, payload)

        bot = modes.BotControl(Old())
        assert bot.stopentry()["status"] == "stopbuy"
