"""``ops/lib/risk_resume.py`` — the only supported human path out of the monthly stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops.config import load_config
from ops.lib import risk_resume
from strategies.riskgate import PortfolioState

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
ACTOR = "human:console:sid-1"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
    cfg = load_config()
    journal, _ = db.init_all(cfg, root=tmp_path)
    return cfg, tmp_path, journal


def _trip(cfg, root):
    gate = risk_resume.gate_for(cfg, "a", root=root)
    gate.loop_tick(_ps(10_000, NOW))
    actions = gate.loop_tick(_ps(8_900, NOW + timedelta(hours=1)))
    assert actions.monthly_lock
    return gate


def _ps(nav, now):
    return PortfolioState(nav=nav, free_usdt=nav, positions={"BTC/USDT": 0.0,
                                                            "ETH/USDT": 0.0}, now=now)


class FakeApi:
    def __init__(self, locks=None, balance=None):
        self._locks = locks if locks is not None else []
        self._balance = balance or {"total": 8_900.0}
        self.deleted: list[int] = []

    def balance(self):
        return self._balance

    def locks(self):
        return {"locks": list(self._locks)}

    def delete_lock(self, lock_id):
        self.deleted.append(int(lock_id))


def test_resume_clears_the_lock_reanchors_and_deletes_pair_locks(env):
    cfg, root, journal = env
    _trip(cfg, root)
    # The third pair is deliberately one the universe snapshot does not carry — under a
    # wide universe "a pair this bot does not trade" can no longer be spelled SOL/USDT.
    foreign = "NOTAPAIR/USDT"
    assert foreign not in cfg.universe.pairs
    api = FakeApi(locks=[{"id": 4, "pair": "BTC/USDT"}, {"id": 5, "pair": "ETH/USDT"},
                         {"id": 6, "pair": foreign}])
    report = risk_resume.resume(cfg, "a", ACTOR, api=api, root=root,
                                now=NOW + timedelta(hours=13))
    assert report.resumed and report.anchor_nav == pytest.approx(8_900.0)
    assert report.nav_source == "bot:total"
    assert api.deleted == [4, 5]              # only this bot's pairs
    gate = risk_resume.gate_for(cfg, "a", root=root)
    assert not gate.monthly_locked()
    assert not gate.loop_tick(_ps(8_900, NOW + timedelta(hours=14))).flatten


def test_resume_writes_an_audit_row(env):
    cfg, root, journal = env
    _trip(cfg, root)
    report = risk_resume.resume(cfg, "a", ACTOR, api=FakeApi(), root=root,
                                now=NOW + timedelta(hours=13))
    with db.opened(journal, readonly=True) as conn:
        row = conn.execute(
            "SELECT actor, action, target, result FROM audit_log WHERE id=?",
            (report.audit_id,)).fetchone()
    assert tuple(row) == (ACTOR, "risk.resume_monthly", "a", "ok")


def test_a_non_human_actor_is_refused_and_audited(env):
    cfg, root, journal = env
    _trip(cfg, root)
    with pytest.raises(risk_resume.ResumeError, match="human actor"):
        risk_resume.resume(cfg, "a", "system:review_run", api=FakeApi(), root=root)
    with db.opened(journal, readonly=True) as conn:
        rows = conn.execute(
            "SELECT actor, result FROM audit_log WHERE action='risk.resume_monthly'"
        ).fetchall()
    assert rows and tuple(rows[-1]) == ("system:review_run", "denied")
    assert risk_resume.gate_for(cfg, "a", root=root).monthly_locked()


def test_an_automated_run_can_never_resume(env, monkeypatch):
    cfg, root, _ = env
    _trip(cfg, root)
    monkeypatch.setenv("EARN_AUTOMATED_RUN", "1")
    with pytest.raises(risk_resume.ResumeError, match="automated run"):
        risk_resume.resume(cfg, "a", ACTOR, api=FakeApi(), root=root)
    assert risk_resume.automated_run() is True


def test_explicit_nav_wins_over_the_bot(env):
    cfg, root, _ = env
    _trip(cfg, root)
    report = risk_resume.resume(cfg, "a", ACTOR, api=FakeApi(balance={"total": 1.0}),
                                nav=7_777.0, root=root, now=NOW + timedelta(hours=13))
    assert report.nav_source == "explicit" and report.anchor_nav == pytest.approx(7_777.0)


def test_nav_falls_back_to_the_journal_when_the_bot_is_down(env):
    cfg, root, journal = env
    _trip(cfg, root)
    with db.opened(journal) as conn:
        conn.execute(
            "INSERT INTO nav_points(ts_utc, sleeve, mode, nav_usdt) VALUES (?,?,?,?)",
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "a", "test", 8_800.0))
        conn.commit()
    report = risk_resume.resume(cfg, "a", ACTOR, api=None, root=root,
                                now=NOW + timedelta(hours=13))
    assert report.nav_source == "nav_points" and report.anchor_nav == pytest.approx(8_800.0)
    assert report.lock_errors == ["no bot api"]


def test_without_any_nav_the_resume_refuses(env):
    cfg, root, _ = env
    _trip(cfg, root)
    with pytest.raises(risk_resume.ResumeError, match="no NAV available"):
        risk_resume.resume(cfg, "a", ACTOR, api=None, root=root)


def test_lock_deletion_failures_are_reported_not_raised(env):
    cfg, root, _ = env
    _trip(cfg, root)

    class Broken(FakeApi):
        def delete_lock(self, lock_id):
            raise RuntimeError("bot gone")

    report = risk_resume.resume(cfg, "a", ACTOR, root=root, now=NOW + timedelta(hours=13),
                                api=Broken(locks=[{"id": 1, "pair": "BTC/USDT"}]))
    assert report.resumed and report.lock_errors == ["1: RuntimeError"]


def test_status_is_read_only_and_reports_the_lock(env):
    cfg, root, _ = env
    _trip(cfg, root)
    status = risk_resume.status(cfg, "a", root=root)
    assert status["monthly_locked"] is True
    assert status["monthly_loss_stop"] == cfg.risk.monthly_loss_stop
    assert status["confirm_phrase"] == "RESUME SLEEVE A"
    assert risk_resume.gate_for(cfg, "a", root=root).monthly_locked()   # unchanged
    risk_resume.resume(cfg, "a", ACTOR, nav=8_900.0, root=root,
                       now=NOW + timedelta(hours=13))
    after = risk_resume.status(cfg, "a", root=root)
    assert after["monthly_locked"] is False and after["monthly_resumed_utc"]
