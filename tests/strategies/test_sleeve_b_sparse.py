"""Sleeve B under SPARSE proposals: absent is not the same as a decided zero.

With dense BTC/ETH targets those two were indistinguishable — every proposal named every
asset, so "not named" never happened. A v4 sparse proposal names a handful of a hundred
tradeable names, and absence becomes the normal state. Treating it as "market-dump this
now" would turn every rebalance into a forced sale into whatever book exists that second,
which is exactly what docs/design/wide-universe.md §1.5 forbids.

So: a NAMED zero is a decision and executes as one (``target_zero``, a risk exit, market).
An ABSENT asset is wound down through the rebalance band, and only closed outright once
the remainder is too small for the band to move.
"""

from __future__ import annotations

import pytest

pytest.importorskip("freqtrade")

from strategies import SleeveB as sleeve_b  # noqa: E402

from .test_ledger_nav import FakeTrade, FakeWallets, with_trades  # noqa: E402
from .test_sleeves import NOW, _make  # noqa: E402

BTC = "BTC/USDT"


def _sleeve(monkeypatch, tmp_path, targets, named):
    s = _make(monkeypatch, tmp_path, "b")
    s._targets = dict(targets)
    s._named = frozenset(named)
    s._target_source = sleeve_b.SOURCE_PROPOSAL
    return s


def _ps(monkeypatch, s, *, position=0.0, nav=10_000.0):
    trades = []
    if position:
        trades = [FakeTrade(amount=position / 50_000.0, stake_amount=position)]
    with_trades(monkeypatch, s, trades)
    s.wallets = FakeWallets(start=nav, free=nav - position)
    return s._portfolio_state(NOW), (trades[0] if trades else FakeTrade())


class TestANamedZeroIsADecision:
    def test_it_exits_immediately(self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=[BTC])
        assert s._custom_exit_extra(BTC, FakeTrade()) == "target_zero"

    def test_no_mandate_at_all_HOLDS_it_does_not_flatten(self, monkeypatch, tmp_path):
        """The absence of a decision is not a decision to sell.

        ``SOURCE_NONE`` used to mean "flatten". It is reached by plumbing, not judgement:
        the gate store is namespaced ``run:<run_id>:`` while freqtrade's trades table is
        not, so a minted run id, an unreadable runtime overlay or one loop with an empty
        ``proposals/`` directory made the mandate vanish while the positions stayed. On
        2026-09-23 that sold the whole sleeve at market fifteen minutes after buying it and
        57 seconds before it adopted a valid proposal (−27.56 USDT, 15.01 of it fees), and
        `crisis-policy.md` §0 measures exactly that reflex at −5.23% CAGR against +30.53%
        for holding and not buying. Entries are already refused under ``SOURCE_NONE``
        (``_desired_stake``), so "hold and buy nothing" is the whole behaviour.
        """
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=[])
        s._target_source = sleeve_b.SOURCE_NONE
        assert s._custom_exit_extra(BTC, FakeTrade()) is None

    def test_losing_the_mandate_mid_session_does_not_sell_what_it_named_before(
            self, monkeypatch, tmp_path):
        """The 09-23 shape exactly: a proposal named BTC, then the mandate vanished."""
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.30}, named=[BTC])
        s._no_targets("no_proposal_ever")
        assert s._target_source == sleeve_b.SOURCE_NONE
        assert s._custom_exit_extra(BTC, FakeTrade()) is None, "a lost mandate sold the book"
        assert s._desired_stake(BTC, _ps(monkeypatch, s)[0], 0.0, "proposal") == 0.0


class TestAnAbsentAssetIsWoundDown:
    def test_it_is_not_dumped(self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=["ETH/USDT"])
        assert s._custom_exit_extra(BTC, FakeTrade()) is None

    def test_the_rebalance_path_trims_it_toward_zero(self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=["ETH/USDT"])
        ps, trade = _ps(monkeypatch, s, position=3_000.0)
        plan = s._sleeve_adjust(trade, ps, NOW, 50_000.0, 0.0)
        assert plan is not None and plan.stake < 0     # a trim, not a full close

    def test_the_last_slice_is_closed_rather_than_stranded(self, monkeypatch, tmp_path):
        # Once the whole position sits inside the dead-band the trim path can never move
        # it again, so the remainder is handed to custom_exit instead of sitting forever.
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=["ETH/USDT"])
        ps, trade = _ps(monkeypatch, s, position=100.0)   # 1% of NAV, inside every band
        assert s._sleeve_adjust(trade, ps, NOW, 50_000.0, 0.0) is None
        assert s.gate.store.get("unwind_done_BTC/USDT") == "1"
        assert s._custom_exit_extra(BTC, FakeTrade()) == "target_zero"

    def test_a_reinstated_target_cancels_the_wind_down(self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path, {BTC: 0.0}, named=["ETH/USDT"])
        s.gate.store.set("unwind_done_BTC/USDT", "1")
        s._targets = {BTC: 0.30}
        assert s._custom_exit_extra(BTC, FakeTrade()) is None
        assert s.gate.store.get("unwind_done_BTC/USDT") == ""


class TestSparseTargetsCoverTheWholeWhitelist:
    def test_every_whitelisted_pair_gets_an_explicit_weight(self, monkeypatch, tmp_path):
        # effective_targets already reads the sleeve's tradeable set rather than the
        # proposal's keys, so an unnamed asset resolves to 0.0 — this pins that it stays
        # true once "the tradeable set" is 30 names instead of 2.
        s = _make(monkeypatch, tmp_path, "b")
        s.bot_loop_start(NOW)
        assert set(s._targets) == set(s.gate_cfg.pairs)
        assert len(s.gate_cfg.pairs) >= 2
