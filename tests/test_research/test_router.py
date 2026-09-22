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


def test_throttle_brief_at_80pct_decide_untouched(jdb):
    _, conn = jdb
    mc = router.load_models_cfg()
    budget = mc["tasks"]["brief"]["monthly_budget_usd"]
    _run_row(conn, "brief", 0.8 * budget)
    st = router.throttle_state(conn, mc, NOW)
    assert st["brief_throttled"] and not st["decide_blocked"]


def test_total_budget_throttles_brief(jdb):
    _, conn = jdb
    mc = router.load_models_cfg()
    _run_row(conn, "decide", 0.8 * mc["budget"]["monthly_total_usd"])
    assert router.throttle_state(conn, mc, NOW)["brief_throttled"]


def test_under_budget_no_throttle(jdb):
    _, conn = jdb
    _run_row(conn, "brief", 1.0)
    assert not router.throttle_state(conn, router.load_models_cfg(), NOW)["brief_throttled"]


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
