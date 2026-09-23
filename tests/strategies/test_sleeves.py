"""Sleeve behaviour through the adapter: fill-time DCA stamping (Sleeve A), and
Sleeve B's approval gate, re-entry cooldown, rebalance cadence and target clamping."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops.config import REPO_ROOT
from ops.lib import signing

pytest.importorskip("freqtrade")

from strategies import SleeveB as sleeve_b  # noqa: E402

from .conftest import container_paths  # noqa: E402
from .test_ledger_nav import (  # noqa: E402
    PRICE,
    FakeOrder,
    FakeTrade,
    FakeWallets,
    with_trades,
)

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
SECRET = "test-approval-secret-0123456789"
RUN_ID = "2026-09-22T08:30+04:00"


def _make(monkeypatch, tmp_path, sleeve: str, *, runtime: dict | None = None,
          mutate=None):
    raw = json.loads((REPO_ROOT / "config" / "riskgate.json").read_text())
    raw["container_paths"] = container_paths(tmp_path)
    if mutate:
        mutate(raw)
    p = tmp_path / "riskgate.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setenv("EARN_RISKGATE", str(p))
    monkeypatch.setenv("EARN_SLEEVE", sleeve)
    if runtime is not None:
        rp = tmp_path / f"runtime-{sleeve}.json"
        rp.write_text(json.dumps(runtime))
        monkeypatch.setenv("EARN_RUNTIME", str(rp))
    else:
        monkeypatch.delenv("EARN_RUNTIME", raising=False)

    from freqtrade.enums import CandleType, RunMode

    cls = __import__(f"strategies.Sleeve{sleeve.upper()}",
                     fromlist=[f"Sleeve{sleeve.upper()}"])
    strategy_cls = getattr(cls, f"Sleeve{sleeve.upper()}")
    config = {"runmode": RunMode.BACKTEST, "stake_currency": "USDT", "timeframe": "4h",
              "strategy": strategy_cls.__name__, "candle_type_def": CandleType.SPOT,
              "exchange": {"name": "binance", "pair_whitelist": []},
              "dry_run": True, "user_data_dir": "/tmp"}
    s = strategy_cls(config)
    monkeypatch.setattr(type(s), "_last_price", lambda self, pair: PRICE)
    monkeypatch.setattr(type(s), "_regime_up", lambda self, pair: True)
    monkeypatch.setattr(type(s), "_book", lambda self, pair: None)
    return s


def _flat(monkeypatch, strategy, free=10_000.0):
    with_trades(monkeypatch, strategy, [])
    strategy.wallets = FakeWallets(start=10_000.0, free=free)
    return strategy._portfolio_state(NOW)


# --------------------------------------------------------------------------- Sleeve A

class TestSleeveA:
    def test_scheduled_dca_stamps_on_the_fill_not_on_submission(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        monkeypatch.setattr(type(s), "_target_weight", lambda self, pair: 0.30)
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=9_000.0)
        plan = s._mechanics_plan(trade, NOW, PRICE, 0.01, 25.0, 9_999.0)
        assert plan is not None and plan.stake > 0
        assert plan.tag == "scheduled_dca"
        assert s.gate.store.get("last_dca_fill_BTC/USDT") is None   # not yet filled

        # freqtrade puts the plan's tag on the order it creates (see tests/contract),
        # and that is what order_filled reads back.
        order = FakeOrder(ft_order_side="buy", status="closed", safe_amount=0.01,
                          safe_filled=0.01, safe_price=PRICE, ft_order_tag=plan.tag)
        s.order_filled("BTC/USDT", trade, order, NOW)
        assert s.gate.store.get("last_dca_fill_BTC/USDT") == "2026-09-22T08:00:00Z"
        # ... and the next chunk is not due for another interval
        assert not s._dca_due("BTC/USDT", NOW + timedelta(days=6))
        assert s._dca_due("BTC/USDT", NOW + timedelta(days=8))

    def test_no_dca_inside_the_rebalance_band(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        monkeypatch.setattr(type(s), "_target_weight", lambda self, pair: 0.21)
        trade = FakeTrade(amount=0.04, stake_amount=2_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=8_000.0)
        # target 2100 vs position 2000: a 1% gap, inside the 5% band
        assert s._mechanics_adjust(trade, NOW, PRICE, 0.01, 25.0, 9_999.0) is None

    def test_a_recent_stop_blocks_the_dca(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        monkeypatch.setattr(type(s), "_target_weight", lambda self, pair: 0.30)
        s.gate.store.set("stopped_BTC/USDT", "2026-09-22T06:00:00Z")
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=9_000.0)
        assert s._mechanics_adjust(trade, NOW, PRICE, 0.01, 25.0, 9_999.0) is None

    def test_a_stop_exit_arms_the_cooldown(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0, exit_reason="stop_loss")
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=10_000.0)
        order = FakeOrder(ft_order_side="sell", status="closed", safe_amount=0.02,
                          safe_filled=0.02)
        s.order_filled("BTC/USDT", trade, order, NOW)
        assert s.gate.store.get("stopped_BTC/USDT") == "2026-09-22T08:00:00Z"
        assert s._reentry_blocked("BTC/USDT", NOW + timedelta(hours=2))
        assert not s._reentry_blocked("BTC/USDT", NOW + timedelta(hours=25))

    def test_a_routine_exit_does_not_arm_the_cooldown(self, monkeypatch, tmp_path):
        s = _make(monkeypatch, tmp_path, "a")
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0, exit_reason="exit_signal")
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=10_000.0)
        s.order_filled("BTC/USDT", trade,
                       FakeOrder(ft_order_side="sell", status="closed"), NOW)
        assert s.gate.store.get("stopped_BTC/USDT") is None


# --------------------------------------------------------------------------- Sleeve B

def _proposal(tmp_path, run_id=RUN_ID, **over):
    d = tmp_path / "proposals"
    d.mkdir(exist_ok=True)
    payload = {
        "run_id": run_id, "prompt_version": "research.v3", "module": "trend",
        "targets": {"BTC": 0.45, "ETH": 0.25, "USDT": 0.30},
        "exposure_scale": 0.8, "confidence": 0.6, "abstain": False,
        "horizon_days": 7, "rationale": ["BTC above 200d"],
        "invalidation": "BTC daily close below 200d MA",
    }
    payload.update(over)
    (d / "2026-09-22-0830.json").write_text(json.dumps(payload))
    return payload


def _approve(tmp_path, run_id=RUN_ID, secret=SECRET, decision="approved"):
    d = tmp_path / "proposals" / "approved"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "kind": "proposal", "run_id": run_id, "decision": decision,
               "actor": "human:telegram",
               "expires_at": (NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    payload["sig"] = signing.sign(payload, secret)
    (d / "approved.json").write_text(json.dumps(payload))


PROPOSE_RUNTIME = {
    "version": 1, "sleeve": "b", "mode": "live", "state": "LIVE_PROPOSE",
    "submode": "propose", "run_id": "live-b-1", "seed_usdt": 500.0,
    "require_approval": True,
}


class TestSleeveBApprovals:
    def _sleeve(self, monkeypatch, tmp_path, *, runtime=None):
        if runtime is not None:
            runtime = dict(runtime, approval_dir=str(tmp_path / "proposals" / "approved"))
        s = _make(monkeypatch, tmp_path, "b", runtime=runtime)
        _flat(monkeypatch, s)
        return s

    def test_propose_mode_ignores_an_unapproved_proposal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EARN_APPROVAL_KEY", SECRET)
        _proposal(tmp_path)
        s = self._sleeve(monkeypatch, tmp_path, runtime=PROPOSE_RUNTIME)
        assert s.gate_cfg.require_approval
        s.bot_loop_start(NOW)
        assert s.gate.store.get("sleeveb_run_id") is None
        assert s._targets.get("BTC/USDT", 0.0) == 0.0

    def test_propose_mode_adopts_an_approved_proposal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EARN_APPROVAL_KEY", SECRET)
        _proposal(tmp_path)
        _approve(tmp_path)
        s = self._sleeve(monkeypatch, tmp_path, runtime=PROPOSE_RUNTIME)
        s.bot_loop_start(NOW)
        assert s.gate.store.get("sleeveb_run_id") == RUN_ID
        assert s._targets["BTC/USDT"] == pytest.approx(0.36)

    def test_a_forged_approval_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EARN_APPROVAL_KEY", SECRET)
        _proposal(tmp_path)
        _approve(tmp_path, secret="some-other-secret-value")
        s = self._sleeve(monkeypatch, tmp_path, runtime=PROPOSE_RUNTIME)
        s.bot_loop_start(NOW)
        assert s.gate.store.get("sleeveb_run_id") is None

    def test_test_mode_needs_no_approval(self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = self._sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s.gate.store.get("sleeveb_run_id") == RUN_ID

    def test_the_plan_block_is_clamped_before_use(self, monkeypatch, tmp_path):
        _proposal(tmp_path, plan={"stop_pct": 0.99, "urgency": "now"})
        s = self._sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s._plan == {"stop_pct": pytest.approx(0.15)}
        assert json.loads(s.gate.store.get("sleeveb_plan"))["stop_pct"] == 0.15


class TestSleeveBSizing:
    def _sleeve(self, monkeypatch, tmp_path, targets):
        s = _make(monkeypatch, tmp_path, "b")
        s._targets = targets
        # These tests are about sizing, not about the mandate: pretend a proposal
        # produced the targets (the no-proposal state is tests/test_sleeve_b_mandate.py)
        # and NAMED every one of them, which a dense pre-v4 proposal did by construction.
        s._target_source = sleeve_b.SOURCE_PROPOSAL
        s._named = frozenset(targets)
        return s

    def test_desired_stake_respects_the_dead_band(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.30})
        ps = _flat(monkeypatch, s)
        assert s._desired_stake("BTC/USDT", ps, 0.0, "proposal") == pytest.approx(3_000.0)
        ps.positions["BTC/USDT"] = 2_900.0
        assert s._desired_stake("BTC/USDT", ps, 0.0, "proposal") == 0.0

    def test_a_dust_target_means_no_stake_and_a_target_zero_exit(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.001})
        ps = _flat(monkeypatch, s)
        assert s._desired_stake("BTC/USDT", ps, 0.0, "proposal") == 0.0
        assert s._custom_exit_extra("BTC/USDT", FakeTrade()) == "target_zero"

    def test_the_reentry_cooldown_blocks_a_fresh_entry(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.30})
        ps = _flat(monkeypatch, s)
        s.gate.store.set("stopped_BTC/USDT", "2026-09-22T06:00:00Z")
        assert s._desired_stake("BTC/USDT", ps, 0.0, "proposal") == 0.0

    def test_a_newer_proposal_overrides_the_cooldown(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.30})
        ps = _flat(monkeypatch, s)
        s.gate.store.set("stopped_BTC/USDT", "2026-09-22T06:00:00Z")
        s.gate.store.set("sleeveb_targets_ts", "2026-09-22T07:00:00Z")
        assert s._desired_stake("BTC/USDT", ps, 0.0, "proposal") == pytest.approx(3_000.0)

    def test_rebalance_trim_is_negative_and_respects_the_cadence(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.10})
        trade = FakeTrade(amount=0.06, stake_amount=3_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=7_000.0)
        got = s._mechanics_adjust(trade, NOW, PRICE, 0.01, 25.0, 9_999.0)
        assert got == pytest.approx(-2_000.0)
        # a second attempt inside min_interval_hours is refused
        assert s._mechanics_adjust(trade, NOW + timedelta(hours=1), PRICE, 0.01, 25.0,
                                   9_999.0) is None
        assert s._mechanics_adjust(trade, NOW + timedelta(hours=5), PRICE, 0.01, 25.0,
                                   9_999.0) == pytest.approx(-2_000.0)

    def test_a_rebalance_add_goes_through_the_gate(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.30})
        trade = FakeTrade(amount=0.02, stake_amount=1_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=9_000.0)
        got = s._mechanics_adjust(trade, NOW, PRICE, 0.01, 25.0, 9_999.0)
        assert got == pytest.approx(2_000.0)     # clamped to max_order_notional_pct

    def test_a_trim_blocked_by_turnover_does_nothing(self, monkeypatch, tmp_path):
        s = self._sleeve(monkeypatch, tmp_path, {"BTC/USDT": 0.10})
        trade = FakeTrade(amount=0.06, stake_amount=3_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=10_000.0, free=7_000.0)
        s.gate.record_order_fill(NOW, notional=4_900.0)
        assert s._mechanics_adjust(trade, NOW, PRICE, 0.01, 25.0, 9_999.0) is None
