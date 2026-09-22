"""Each hard-case flag alone escalates decide to fable; budget throttle hits briefs
never decides; shadow window arithmetic; pinned strings only."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from ops import db
from ops.config import load_config
from runs import router

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def jdb(cfg, tmp_path):
    journal, _ = db.init_all(cfg, root=tmp_path)
    conn = db.connect(journal)
    yield tmp_path, conn
    conn.close()


def test_no_flags_opus_with_flags_fable():
    assert router.resolve("decide", router.HardCaseFlags()).model == "claude-opus-5"
    for flag in ("regime_change_48h", "module_disagreement", "near_stop",
                 "regwatch_active", "two_abstains", "tca_above_threshold"):
        c = router.resolve("decide", router.HardCaseFlags(**{flag: True}))
        assert c.model == "claude-fable-5-1", flag
        assert c.escalated and c.escalation_reasons == [flag]


def test_other_tasks_never_escalate():
    flags = router.HardCaseFlags(near_stop=True)
    assert router.resolve("brief", flags).model == "claude-sonnet-5"
    assert router.resolve("flags", flags).model == "claude-haiku-4-5-20251001"


def test_pinned_strings_are_exact():
    mc = router.load_models_cfg()
    assert set(mc["models"].values()) == {
        "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5",
        "claude-haiku-4-5-20251001"}


def _run_row(conn, stage, cost, started="2026-09-05T04:30:00Z"):
    conn.execute(
        "INSERT INTO runs(run_id, stage, kind, started_utc, status, cost_usd)"
        " VALUES (?,?,?,?,?,?)",
        (f"r-{stage}-{cost}-{started}", stage, "research", started, "success", cost))
    conn.commit()


@pytest.fixture
def kdb(cfg, jdb):
    root, _ = jdb
    from ops import db as dbmod

    conn = dbmod.connect(root / cfg.paths.knowledge_db)
    yield conn
    conn.close()


def _set_rl(kdb, status=None, utilization=None, resets_at="2099-01-01T00:00:00Z"):
    for key, value in (("rate_limit_status", status),
                       ("rate_limit_utilization", utilization),
                       ("rate_limit_resets_at", resets_at)):
        if value is not None:
            kdb.execute("INSERT OR REPLACE INTO ops_state(key, value, updated_at)"
                        " VALUES (?,?, 'x')", (key, str(value)))
    kdb.commit()


class TestThrottle:
    """USD spend is TELEMETRY under Claude Max; only rate-limit pressure throttles
    briefs, and decide runs are never blocked."""

    def test_usd_spend_never_throttles(self, jdb, kdb):
        _, conn = jdb
        mc = router.load_models_cfg()
        _run_row(conn, "decide", 10 * mc["budget"]["monthly_total_usd"])
        st = router.throttle_state(conn, mc, NOW, kdb=kdb)
        assert not st["brief_throttled"] and not st["decide_blocked"]
        assert st["month_total_usd"] > 0  # telemetry still reported

    def test_rate_limit_warning_throttles_brief(self, jdb, kdb):
        _, conn = jdb
        _set_rl(kdb, status="allowed_warning")
        st = router.throttle_state(conn, None, NOW, kdb=kdb)
        assert st["brief_throttled"] and not st["decide_blocked"]

    def test_high_utilization_throttles_brief(self, jdb, kdb):
        _, conn = jdb
        _set_rl(kdb, utilization=0.9)
        assert router.throttle_state(conn, None, NOW, kdb=kdb)["brief_throttled"]
        _set_rl(kdb, utilization=0.5)
        assert not router.throttle_state(conn, None, NOW, kdb=kdb)["brief_throttled"]

    def test_expired_window_clears_throttle(self, jdb, kdb):
        _, conn = jdb
        _set_rl(kdb, status="rejected", resets_at="2026-09-21T00:00:00Z")  # past
        assert not router.throttle_state(conn, None, NOW, kdb=kdb)["brief_throttled"]


class TestEffortAndOverlay:
    def test_effort_floor_and_task_levels(self):
        assert router.resolve("decide", router.HardCaseFlags()).effort == "max"
        assert router.resolve("review").effort == "max"
        assert router.resolve("brief").effort == "high"
        assert router.clamp_effort("low") == "high"       # floor
        assert router.clamp_effort("medium") == "high"
        assert router.clamp_effort(None) == "high"
        assert router.clamp_effort("nonsense") == "high"
        assert router.clamp_effort("xhigh") == "xhigh"

    def test_overlay_cannot_lower_effort(self, tmp_path):
        import shutil

        from ops.config import REPO_ROOT

        base = tmp_path / "models.yaml"
        shutil.copy(REPO_ROOT / "config" / "models.yaml", base)
        (tmp_path / "models-auto.yaml").write_text(
            "tasks:\n  decide: { effort: low }\n")
        mc = router.load_models_cfg(base)
        assert mc["tasks"]["decide"]["effort"] == "low"    # merged raw...
        assert router.resolve("decide", models_cfg=mc).effort == "high"  # ...clamped

    def test_overlay_promotes_decide_model_and_owns_shadow(self, tmp_path):
        import shutil

        from ops.config import REPO_ROOT

        base = tmp_path / "models.yaml"
        shutil.copy(REPO_ROOT / "config" / "models.yaml", base)
        (tmp_path / "models-auto.yaml").write_text(
            "models: { nova: claude-nova-6 }\n"
            "tasks: { decide: { model: nova } }\n"
            "shadow: { enabled: true, model: sonnet, started: '2026-09-01', days: 30 }\n")
        mc = router.load_models_cfg(base)
        assert router.resolve("decide", models_cfg=mc).model == "claude-nova-6"
        assert router.shadow_active(mc, date(2026, 9, 10)) == "claude-sonnet-5"

    def test_write_models_overlay_atomic(self, tmp_path):
        op = tmp_path / "models-auto.yaml"

        def mutate(cur):
            cur.setdefault("tasks", {})["decide"] = {"model": "nova"}
            return cur

        router.write_models_overlay(mutate, overlay_path=op)
        router.write_models_overlay(lambda c: c, overlay_path=op)  # idempotent RMW
        import yaml as _y

        assert _y.safe_load(op.read_text())["tasks"]["decide"]["model"] == "nova"

    def test_missing_overlay_is_base_only(self):
        mc = router.load_models_cfg()
        assert router.resolve("decide", models_cfg=mc).model == "claude-opus-5"


def test_force_escalation_from_triggers():
    c = router.resolve("decide", router.HardCaseFlags(),
                       force_escalation=["news:hack"])
    assert c.model == "claude-fable-5-1" and c.escalated
    assert c.escalation_reasons == ["trigger:news:hack"]
    both = router.resolve("decide", router.HardCaseFlags(near_stop=True),
                          force_escalation=["move_4h:BTC:-6.0"])
    assert set(both.escalation_reasons) == {"trigger:move_4h:BTC:-6.0", "near_stop"}


def test_shadow_window_day30_no_day31_yes():
    mc = router.load_models_cfg()
    mc = json.loads(json.dumps(mc))
    mc["shadow"].update({"enabled": True, "model": "sonnet", "started": "2026-09-01",
                         "days": 30})
    assert router.shadow_active(mc, date(2026, 9, 1)) == "claude-sonnet-5"
    assert router.shadow_active(mc, date(2026, 9, 30)) == "claude-sonnet-5"  # day 29
    assert router.shadow_active(mc, date(2026, 10, 1)) is None               # day 30
    mc["shadow"]["enabled"] = False
    assert router.shadow_active(mc, date(2026, 9, 15)) is None


class TestHardCaseComputation:
    def test_two_abstains(self, cfg, jdb):
        root, conn = jdb
        for i, abstain in ((1, 1), (2, 1)):
            conn.execute(
                "INSERT INTO proposals(run_id, shadow, ts_utc, valid, abstain)"
                " VALUES (?,0,?,1,?)",
                (f"2026-09-2{i}T08:30+04:00", f"2026-09-2{i}T04:30:00Z", abstain))
        conn.commit()
        flags = router.compute_hardcase_flags(cfg, conn, root=root, now=NOW)
        assert flags.two_abstains

    def test_near_stop_from_anchor(self, cfg, jdb):
        root, conn = jdb
        conn.execute("INSERT INTO nav_daily(date_utc, sleeve, nav_usdt)"
                     " VALUES ('2026-09-22','b',9790)")
        conn.execute("INSERT INTO risk_state(sleeve, key, value, updated_utc)"
                     " VALUES ('b','day_anchor_nav','10000','x')")
        conn.commit()
        # -2.1% loss, daily stop 3%, proximity 1% -> within 1% of the stop
        assert router.compute_hardcase_flags(cfg, conn, root=root, now=NOW).near_stop

    def test_regime_change_and_disagreement(self, cfg, jdb):
        root, conn = jdb
        state = {"portfolio": {"regime_changed_utc": (NOW - timedelta(hours=10)
                                                      ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "modules": {"disagreement": True}}}
        p = root / cfg.paths.state_latest
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
        flags = router.compute_hardcase_flags(cfg, conn, root=root, now=NOW)
        assert flags.regime_change_48h and flags.module_disagreement

    def test_unreadable_flags_file_is_hard_case(self, cfg, jdb):
        root, conn = jdb
        assert router.compute_hardcase_flags(cfg, conn, root=root, now=NOW).regwatch_active
