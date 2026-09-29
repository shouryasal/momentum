"""The trend ensemble as ENTRY GATE and POSITION SCALE for core positions (dip-strategy.md §8.1 #1).

What is pinned, in order of how much it would cost to get wrong:

* no core entry at weight 0, the stake scaled at 0.5, a satellite untouched;
* a missing, stale or warming-up ``knowledge/state/trend.json`` is a SHUT gate with the
  plumbing reason named — never an open one, never a silent flat (§10.2 item 6);
* every add goes through the same scale and stops at the scaled target;
* the gate never causes an exit and never touches the risk gate;
* the journal gets one row per state change, not one per candle.

This module reads the REAL file through ``strategies/trend_state.py``; the autouse fixture
in ``conftest.py`` that opens the gate for every other module is switched off here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("freqtrade")

from strategies import _journal  # noqa: E402
from strategies import trend_state as ts  # noqa: E402
from strategies.earn_base import EarnBaseStrategy  # noqa: E402

from .test_ledger_nav import PRICE, FakeTrade, FakeWallets, with_trades  # noqa: E402
from .test_sleeves import _make  # noqa: E402

#: Tells ``conftest.open_trend_gate`` to leave ``_trend_weight`` alone in this module.
REAL_TREND_GATE = True

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
BTC, ETH, SAT = "BTC/USDT", "ETH/USDT", "AAVE/USDT"
NAV = 10_000.0
TARGET = 0.20          # 2,000 USDT flat — under every gate cap, so the scale is what shows


def _write_trend(tmp_path: Path, weights: dict[str, float | None], *,
                 asof_close: datetime = NOW - timedelta(hours=8),
                 status: str = "ok", bump_mtime: float | None = None) -> Path:
    """The file ``runs.features.trend.write_state`` produces, minimal but shape-exact."""
    path = ts.state_path(str(tmp_path / "earn.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    assets = {}
    for asset, w in weights.items():
        assets[asset] = {
            "asset": asset, "weight": w, "status": status if w is not None else "warmup",
            "asof_open_utc": (asof_close - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asof_close_utc": asof_close.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "members_on": 0 if w is None else round(w * 15), "members": {},
        }
    path.write_text(json.dumps({"version": 1, "computed_utc": "2026-09-22T07:55:00Z",
                                "assets": assets}))
    if bump_mtime is not None:
        os.utime(path, (bump_mtime, bump_mtime))
    return path


def _sleeve_a(monkeypatch, tmp_path, *, target: float = TARGET):
    s = _make(monkeypatch, tmp_path, "a")
    monkeypatch.setattr(type(s), "_target_weight", lambda self, pair: target)
    with_trades(monkeypatch, s, [])
    s.wallets = FakeWallets(start=NAV, free=NAV)
    return s


def _stake(s, pair: str = BTC) -> float:
    return s.custom_stake_amount(pair, NOW, PRICE, proposed_stake=NAV, min_stake=None,
                                 max_stake=NAV, leverage=1.0, entry_tag="trend", side="long")


# ---------------------------------------------------------------- the two acceptance tests

class TestEntryGateAndScale:
    def test_it_is_the_real_reader_in_this_module(self, monkeypatch, tmp_path):
        s = _sleeve_a(monkeypatch, tmp_path)
        assert type(s)._trend_weight is EarnBaseStrategy._trend_weight

    def test_no_core_entry_at_weight_zero(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_ZERO
        # ...and the other core asset, at full weight, is sized as before.
        assert _stake(s, ETH) == pytest.approx(TARGET * NAV)

    def test_the_stake_is_scaled_by_the_weight(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": 0.5, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == pytest.approx(0.5 * TARGET * NAV)
        assert _stake(s, ETH) == pytest.approx(TARGET * NAV)

    def test_the_weight_grid_is_honoured_not_rounded(self, monkeypatch, tmp_path):
        """Twelve of fifteen members on is 0.8 of the target, not 'mostly on'."""
        _write_trend(tmp_path, {"BTC": 12 / 15, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == pytest.approx(0.8 * TARGET * NAV)

    def test_a_satellite_is_never_gated(self, monkeypatch, tmp_path):
        """The ensemble is a core-asset signal; satellites answer to their own caps."""
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 0.0})
        s = _sleeve_a(monkeypatch, tmp_path, target=0.04)
        assert s._is_core(BTC) and s._is_core(ETH) and not s._is_core(SAT)
        tw = s._trend_weight(SAT, NOW)
        assert tw.weight == 1.0 and tw.reason == "not_core"
        assert _stake(s, SAT) > 0

    def test_the_risk_gate_still_sizes_the_scaled_stake(self, monkeypatch, tmp_path):
        """Scaling is an input to the gate, never a way round it: 0.5 × 0.60 NAV is still
        clamped by ``max_order_notional_pct`` (20% of NAV) exactly as an unscaled ask is."""
        _write_trend(tmp_path, {"BTC": 0.5, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path, target=0.60)
        full = float(s.gate_cfg.max_order_notional_pct) * NAV
        assert _stake(s, BTC) == pytest.approx(full)          # 3,000 asked, 2,000 allowed


# ---------------------------------------------------------------- plumbing fails CLOSED

class TestMissingStaleWarmup:
    def test_no_file_means_no_core_entry_and_names_it(self, monkeypatch, tmp_path):
        s = _sleeve_a(monkeypatch, tmp_path)
        assert not ts.state_path(str(tmp_path / "earn.db")).exists()
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_MISSING

    def test_an_unparseable_file_is_missing(self, monkeypatch, tmp_path):
        p = _write_trend(tmp_path, {"BTC": 1.0})
        p.write_text("{not json")
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_MISSING

    def test_a_stale_bar_shuts_the_gate(self, monkeypatch, tmp_path):
        """The bar closed three days ago: the daily job is down, not the market."""
        _write_trend(tmp_path, {"BTC": 1.0, "ETH": 1.0}, asof_close=NOW - timedelta(days=3))
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_STALE

    def test_freshness_is_judged_on_the_bar_not_the_file(self, monkeypatch, tmp_path):
        """A file rewritten every fifteen minutes from a stalled candle store is stale."""
        p = _write_trend(tmp_path, {"BTC": 1.0}, asof_close=NOW - timedelta(days=3))
        os.utime(p, None)   # freshly written, just now
        s = _sleeve_a(monkeypatch, tmp_path)
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_STALE

    def test_the_max_age_comes_from_trading_config_when_present(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": 1.0}, asof_close=NOW - timedelta(hours=60))
        s = _sleeve_a(monkeypatch, tmp_path)
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_STALE       # default 48h
        s.mech["trend_ensemble"] = {"max_age_hours": 72}
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_OK
        assert _stake(s, BTC) == pytest.approx(TARGET * NAV)

    def test_a_warming_up_asset_is_not_flat_and_not_full(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": None, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_WARMUP
        assert _stake(s, ETH) == pytest.approx(TARGET * NAV)

    def test_an_asset_the_file_does_not_carry_is_refused(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        assert s._trend_weight(ETH, NOW).reason == ts.REASON_NO_ASSET
        assert _stake(s, ETH) == 0.0

    def test_a_rewritten_file_is_picked_up_without_a_restart(self, monkeypatch, tmp_path):
        p = _write_trend(tmp_path, {"BTC": 1.0}, bump_mtime=1_700_000_000.0)
        s = _sleeve_a(monkeypatch, tmp_path)
        assert _stake(s, BTC) == pytest.approx(TARGET * NAV)
        _write_trend(tmp_path, {"BTC": 0.0}, bump_mtime=1_700_000_900.0)
        assert p.exists()
        assert _stake(s, BTC) == 0.0
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_ZERO


# ---------------------------------------------------------------- adds, exits, the journal

class TestAddsExitsJournal:
    def test_an_add_stops_at_the_scaled_target(self, monkeypatch, tmp_path):
        """Position 1,000 at target 0.20 and weight 0.5: the scaled target IS 1,000, so the
        1,000 gap the sleeve asks for has no headroom; at full weight it is filled."""
        _write_trend(tmp_path, {"BTC": 0.5, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        trade = FakeTrade(amount=1_000.0 / PRICE, stake_amount=1_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=NAV, free=NAV - 1_000.0)
        ps = s._portfolio_state(NOW)
        assert s._gated_add(BTC, 1_000.0, ps, trade=trade, tag="scheduled_dca") is None
        _write_trend(tmp_path, {"BTC": 1.0, "ETH": 1.0}, bump_mtime=1_700_000_900.0)
        plan = s._gated_add(BTC, 1_000.0, ps, trade=trade, tag="scheduled_dca")
        assert plan is not None and plan.stake == pytest.approx(1_000.0)

    def test_an_add_is_refused_outright_at_weight_zero(self, monkeypatch, tmp_path):
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        trade = FakeTrade(amount=500.0 / PRICE, stake_amount=500.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=NAV, free=NAV - 500.0)
        assert s._gated_add(BTC, 500.0, s._portfolio_state(NOW), trade=trade, tag="x") is None

    def test_weight_zero_never_causes_an_exit(self, monkeypatch, tmp_path):
        """Exits stay with the stop, the ladder, ROI and the exit signal — not with this."""
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 0.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        trade = FakeTrade(amount=1_000.0 / PRICE, stake_amount=1_000.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=NAV, free=NAV - 1_000.0)
        assert s.custom_exit(BTC, trade, NOW, PRICE, 0.01) is None
        assert s._custom_exit_extra(BTC, trade) is None

    def test_sleeve_b_is_gated_the_same_way(self, monkeypatch, tmp_path):
        from strategies import SleeveB as sleeve_b

        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 0.5})
        s = _make(monkeypatch, tmp_path, "b")
        (tmp_path / "proposals").mkdir(exist_ok=True)
        s._targets = {BTC: 0.20, ETH: 0.20}
        s._target_source = sleeve_b.SOURCE_PROPOSAL
        s._named = frozenset({BTC, ETH})
        with_trades(monkeypatch, s, [])
        s.wallets = FakeWallets(start=NAV, free=NAV)
        tag = sleeve_b.ENTRY_TAGS[sleeve_b.SOURCE_PROPOSAL]
        assert s.custom_stake_amount(BTC, NOW, PRICE, proposed_stake=NAV, min_stake=None,
                                     max_stake=NAV, leverage=1.0, entry_tag=tag,
                                     side="long") == 0.0
        assert s.custom_stake_amount(ETH, NOW, PRICE, proposed_stake=NAV, min_stake=None,
                                     max_stake=NAV, leverage=1.0, entry_tag=tag,
                                     side="long") == pytest.approx(0.5 * 0.20 * NAV)
        # A proposal's zero target is still the proposal's exit, not the ensemble's.
        assert s._trend_weight(BTC, NOW).reason == ts.REASON_ZERO

    def test_the_journal_gets_one_row_per_state_change(self, monkeypatch, tmp_path):
        rows: list[dict] = []

        def fake(sleeve, pair, callback, intent, allowed, reason, **kw):
            rows.append({"pair": pair, "callback": callback, "intent": intent,
                         "allowed": allowed, "reason": reason, **kw})
            return 1

        monkeypatch.setattr(_journal, "record_gate_decision", fake)
        _write_trend(tmp_path, {"BTC": 1.0, "ETH": 1.0}, bump_mtime=1_700_000_000.0)
        s = _sleeve_a(monkeypatch, tmp_path)
        s._journal_on = True
        _stake(s, BTC)
        _stake(s, BTC)
        trend_rows = [r for r in rows if r["reason"].startswith("trend_gate:")]
        assert len(trend_rows) == 1 and trend_rows[0]["allowed"] is True
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 1.0}, bump_mtime=1_700_000_900.0)
        _stake(s, BTC)
        _stake(s, BTC)
        trend_rows = [r for r in rows if r["reason"].startswith("trend_gate:")]
        assert len(trend_rows) == 2
        shut = trend_rows[-1]
        assert shut["reason"] == f"trend_gate:{ts.REASON_ZERO}"
        assert shut["allowed"] is False and shut["action"] == "reject"
        assert shut["callback"] == "custom_stake_amount" and shut["intent"] == "entry"
        assert shut["side"] == "buy"

    def test_journal_rows_use_only_callbacks_and_intents_the_schema_accepts(
            self, monkeypatch, tmp_path):
        """``gate_decisions`` has CHECK constraints on both; a row that violates them is
        silently lost by the never-raising journal, which is worse than no row."""
        from ops.config import REPO_ROOT

        ddl = (REPO_ROOT / "ops" / "sql" / "journal.sql").read_text()
        rows: list[dict] = []
        monkeypatch.setattr(
            _journal, "record_gate_decision",
            lambda sleeve, pair, callback, intent, allowed, reason, **kw: rows.append(
                {"callback": callback, "intent": intent}) or 1)
        _write_trend(tmp_path, {"BTC": 0.0, "ETH": 1.0})
        s = _sleeve_a(monkeypatch, tmp_path)
        s._journal_on = True
        _stake(s, BTC)
        trade = FakeTrade(amount=500.0 / PRICE, stake_amount=500.0)
        with_trades(monkeypatch, s, [trade])
        s.wallets = FakeWallets(start=NAV, free=NAV - 500.0)
        s._trend_gate_last.clear()
        s._gated_add(BTC, 500.0, s._portfolio_state(NOW), trade=trade, tag="x")
        assert {r["callback"] for r in rows} == {"custom_stake_amount", "adjust_trade_position"}
        for r in rows:
            assert f"'{r['callback']}'" in ddl and f"'{r['intent']}'" in ddl


# ---------------------------------------------------------------- the stdlib reader itself

class TestReader:
    def test_the_reader_is_stdlib_only(self):
        import ast

        from ops.config import REPO_ROOT

        src = (REPO_ROOT / "strategies" / "trend_state.py").read_text()
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        assert mods <= {"json", "dataclasses", "datetime", "pathlib", "typing", "__future__"}

    def test_weight_outside_unit_interval_is_refused(self, tmp_path):
        p = _write_trend(tmp_path, {"BTC": 1.5})
        st = ts.load(p)
        assert ts.weight_for(st, "BTC", NOW).reason == ts.REASON_WARMUP

    def test_state_path_is_the_freshness_sidecars_sibling(self):
        assert ts.state_path("/freqtrade/knowledge/earn.db") == Path(
            "/freqtrade/knowledge/state/trend.json")
