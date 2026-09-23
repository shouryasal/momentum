"""Run metrics and comparison, against a hand-computed NAV series and fill book."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from runs import test_metrics as tm
from tests.test_modes.conftest import seed_run

START = datetime(2026, 10, 1, tzinfo=UTC)


def _nav(
    jdb, run_id: str, sleeve: str, values, *, mode="test", step_days=1, cash=None,
    offset_days=0,
):
    for i, value in enumerate(values):
        ts = (START + timedelta(days=offset_days + i * step_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.write(
            jdb,
            "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt)"
            " VALUES (?,?,?,?,?,?)",
            (ts, sleeve, run_id, mode, float(value), cash[i] if cash else None),
        )


def _fill(jdb, run_id, *, side, amount, price, day, fee=0.0, sleeve="a", pair="BTC/USDT"):
    db.write(
        jdb,
        "INSERT INTO fills(ts_utc, sleeve, pair, side, fill_amount, fill_price, fee_amount,"
        " fee_currency, run_id) VALUES (?,?,?,?,?,?,?, 'USDT', ?)",
        (
            (START + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sleeve, pair, side, amount, price, fee, run_id,
        ),
    )


class TestPrimitives:
    def test_simple_returns(self):
        assert tm.simple_returns([100, 110, 99]) == pytest.approx([0.1, -0.1])

    def test_max_drawdown(self):
        # 100 -> 120 -> 90: the trough is 25% below the 120 peak
        assert tm.max_drawdown([100, 120, 90, 110]) == pytest.approx(-0.25)

    def test_no_drawdown_on_a_monotonic_series(self):
        assert tm.max_drawdown([100, 101, 102]) == 0.0

    def test_cagr_doubles_in_a_year(self):
        assert tm.cagr(100, 200, 365.25) == pytest.approx(1.0)

    def test_sharpe_scales_with_the_period(self):
        rets = [0.01, -0.005, 0.02, 0.0, 0.01]
        daily = tm.sharpe(rets, 365.25)
        assert daily is not None
        mean = sum(rets) / len(rets)
        sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        assert daily == pytest.approx(mean / sd * math.sqrt(365.25))

    def test_sharpe_needs_variance(self):
        assert tm.sharpe([0.01, 0.01, 0.01], 365) is None
        assert tm.sharpe([0.01], 365) is None

    def test_sortino_only_counts_downside(self):
        assert tm.sortino([0.01, 0.02], 365) is None  # no losing period
        assert tm.sortino([0.01, -0.02, 0.01], 365) is not None

    def test_annualisation_from_spacing(self):
        assert tm.annualisation(86400) == pytest.approx(365.25)
        assert tm.annualisation(0) == 0.0

    def test_median_spacing_ignores_duplicates(self):
        stamps = [START, START, START + timedelta(hours=1), START + timedelta(hours=2)]
        assert tm.median_spacing_s(stamps) == 3600


class TestRoundTrips:
    def test_fifo_matching_and_pnl(self):
        fills = [
            {"pair": "BTC/USDT", "side": "buy", "fill_amount": 1.0, "fill_price": 100.0,
             "fee_amount": 1.0, "fee_currency": "USDT", "ts_utc": "t1"},
            {"pair": "BTC/USDT", "side": "buy", "fill_amount": 1.0, "fill_price": 120.0,
             "fee_amount": 1.2, "fee_currency": "USDT", "ts_utc": "t2"},
            {"pair": "BTC/USDT", "side": "sell", "fill_amount": 2.0, "fill_price": 130.0,
             "fee_amount": 2.6, "fee_currency": "USDT", "ts_utc": "t3"},
        ]
        trips = tm.round_trips(fills)
        assert len(trips) == 2
        # first lot: 130 - 100 = 30, minus its 1.0 entry fee and half the 2.6 exit fee
        assert trips[0].pnl_usdt == pytest.approx(30 - 1.0 - 1.3)
        assert trips[1].pnl_usdt == pytest.approx(10 - 1.2 - 1.3)
        assert [t.win for t in trips] == [True, True]

    def test_a_partial_sell_leaves_the_rest_open(self):
        fills = [
            {"pair": "BTC/USDT", "side": "buy", "fill_amount": 2.0, "fill_price": 100.0,
             "fee_amount": 0.0, "fee_currency": "USDT", "ts_utc": "t1"},
            {"pair": "BTC/USDT", "side": "sell", "fill_amount": 0.5, "fill_price": 110.0,
             "fee_amount": 0.0, "fee_currency": "USDT", "ts_utc": "t2"},
        ]
        trips = tm.round_trips(fills)
        assert len(trips) == 1 and trips[0].amount == 0.5
        assert trips[0].pnl_usdt == pytest.approx(5.0)

    def test_win_rate_and_profit_factor(self):
        fills = [
            {"pair": "X/USDT", "side": "buy", "fill_amount": 1, "fill_price": 100,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t1"},
            {"pair": "X/USDT", "side": "sell", "fill_amount": 1, "fill_price": 120,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t2"},
            {"pair": "X/USDT", "side": "buy", "fill_amount": 1, "fill_price": 100,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t3"},
            {"pair": "X/USDT", "side": "sell", "fill_amount": 1, "fill_price": 90,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t4"},
        ]
        trips = tm.round_trips(fills)
        assert tm.win_rate(trips) == 0.5
        assert tm.profit_factor(trips) == pytest.approx(2.0)

    def test_profit_factor_is_infinite_without_losses(self):
        fills = [
            {"pair": "X/USDT", "side": "buy", "fill_amount": 1, "fill_price": 100,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t1"},
            {"pair": "X/USDT", "side": "sell", "fill_amount": 1, "fill_price": 120,
             "fee_amount": 0, "fee_currency": "USDT", "ts_utc": "t2"},
        ]
        assert tm.profit_factor(tm.round_trips(fills)) == math.inf

    def test_no_trades_means_no_ratios(self):
        assert tm.win_rate([]) is None and tm.profit_factor([]) is None


class TestCompute:
    @pytest.fixture
    def run(self, cfg, jdb):
        seed_run(jdb, cfg, run_id="test-a-1", started=START.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _nav(jdb, "test-a-1", "a", [10000, 11000, 9900, 12000], cash=[10000, 5000, 5000, 6000])
        _nav(jdb, "test-a-1", "benchmark", [10000, 10500, 10200, 10800])
        _fill(jdb, "test-a-1", side="buy", amount=0.1, price=50000, day=1, fee=5.0)
        _fill(jdb, "test-a-1", side="sell", amount=0.1, price=55000, day=3, fee=5.5)
        return "test-a-1"

    def test_headline_numbers(self, cfg, jdb, run):
        m = tm.compute(jdb, run)
        assert m.sleeve == "a" and m.mode == "test"
        assert m.points == 4
        assert m.nav_start == 10000 and m.nav_end == 12000
        assert m.return_pct == pytest.approx(20.0)
        assert m.days == pytest.approx(3.0)
        # peak 11000 then 9900 => -10%
        assert m.max_drawdown_pct == pytest.approx(-10.0)
        assert m.benchmark_return_pct == pytest.approx(8.0)
        assert m.excess_return_pct == pytest.approx(12.0)

    def test_trades_and_costs(self, cfg, jdb, run):
        m = tm.compute(jdb, run)
        assert m.trades == 1
        assert m.win_rate == 1.0
        assert m.fees_usdt == pytest.approx(10.5)
        assert m.turnover_usdt == pytest.approx(0.1 * 50000 + 0.1 * 55000)

    def test_exposure_from_cash(self, cfg, jdb, run):
        m = tm.compute(jdb, run)
        # 0%, 1-5000/11000, 1-5000/9900, 1-6000/12000
        expected = (0 + (1 - 5000 / 11000) + (1 - 5000 / 9900) + 0.5) / 4 * 100
        assert m.exposure_pct == pytest.approx(expected, abs=1e-3)

    def test_gate_rejects_and_breaches(self, cfg, jdb, run):
        for severity in ("reject", "reject", "breach"):
            db.write(
                jdb,
                "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed,"
                " reason, severity, run_id) VALUES ('2026-10-02T00:00:00Z','a','BTC/USDT',"
                " 'entry','confirm_trade_entry',0,'cap',?,?)",
                (severity, run),
            )
        m = tm.compute(jdb, run)
        assert m.gate_rejects == 2 and m.gate_breaches == 1

    def test_signal_conversion(self, cfg, jdb, run):
        for i, status in enumerate(("candidate", "screened", "acted")):
            db.write(
                jdb,
                "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, strength,"
                " features_json, dedupe_key, status, updated_utc)"
                " VALUES (?, '2026-10-02T00:00:00Z','scan','detector','move',1.0,'{}',?,?,"
                " '2026-10-02T00:00:00Z')",
                (f"sig-{i}", f"k{i}", status),
            )
        m = tm.compute(jdb, run)
        assert m.signals_total == 3 and m.signals_acted == 1
        assert m.signal_conversion == pytest.approx(1 / 3, abs=1e-4)

    def test_cost_per_decision(self, cfg, jdb, run):
        db.write(
            jdb,
            "INSERT INTO proposals(run_id, shadow, ts_utc, valid, abstain, module,"
            " targets_json, exposure_scale) VALUES ('p1',0,'2026-10-02T00:00:00Z',1,0,"
            " 'trend','{}',1.0)",
            (),
        )
        db.write(
            jdb,
            "INSERT INTO llm_calls(ts_utc, task, provider, model, attempt, status, cost_usd)"
            " VALUES ('2026-10-02T00:00:00Z','decide','claude','opus',1,'ok',2.5)",
            (),
        )
        m = tm.compute(jdb, run)
        assert m.decisions == 1 and m.llm_cost_usd == pytest.approx(2.5)
        assert m.cost_per_decision_usd == pytest.approx(2.5)

    def test_an_unknown_run_raises(self, cfg, jdb):
        with pytest.raises(tm.MetricsError, match="unknown run"):
            tm.compute(jdb, "nope")

    def test_a_run_with_no_points_still_computes(self, cfg, jdb):
        seed_run(jdb, cfg, run_id="test-b-1", sleeve="b")
        m = tm.compute(jdb, "test-b-1")
        assert m.points == 0 and m.return_pct is None and m.trades == 0

    def test_benchmark_falls_back_to_the_window(self, cfg, jdb):
        """Only one run owns the benchmark rows, but both must show "vs BTC"."""
        seed_run(jdb, cfg, run_id="test-b-9", sleeve="b",
                 started=START.strftime("%Y-%m-%dT%H:%M:%SZ"))
        _nav(jdb, "test-b-9", "b", [10000, 12000])
        _nav(jdb, "owned-by-a", "benchmark", [10000, 10800])
        m = tm.compute(jdb, "test-b-9")
        assert m.benchmark_return_pct == pytest.approx(8.0)


class TestCompare:
    @pytest.fixture
    def two_runs(self, cfg, jdb):
        seed_run(jdb, cfg, run_id="test-a-1", started="2026-10-01T00:00:00Z")
        seed_run(jdb, cfg, run_id="test-a-2", started="2026-10-05T00:00:00Z")
        # runs never overlap in time for the same sleeve, so neither do their nav points
        _nav(jdb, "test-a-1", "a", [10000, 11000])
        _nav(jdb, "test-a-2", "a", [20000, 22000], offset_days=4)
        return ["test-a-1", "test-a-2"]

    def test_series_are_rebased_to_100(self, cfg, jdb, two_runs):
        result = tm.compare(jdb, two_runs)
        for run_id in two_runs:
            series = result.series[run_id]
            assert series[0]["index"] == 100.0
            assert series[-1]["index"] == pytest.approx(110.0)

    def test_deltas_are_against_the_first_run(self, cfg, jdb, two_runs):
        result = tm.compare(jdb, two_runs)
        assert set(result.deltas) == {"test-a-2"}
        assert result.deltas["test-a-2"]["return_pct"] == pytest.approx(0.0)

    def test_config_changes_between_the_runs_are_listed(self, cfg, jdb, two_runs):
        db.write(
            jdb,
            "INSERT INTO config_audit(ts_utc, actor, file, after_sha, changed_paths_json, diff)"
            " VALUES ('2026-10-03T00:00:00Z','human:console:1','config/earn.yaml','sha',"
            " '[\"risk.max_weight.BTC\"]','-a\\n+b')",
            (),
        )
        result = tm.compare(jdb, two_runs)
        assert result.config_diff[0]["changed_paths"] == ["risk.max_weight.BTC"]

    def test_between_two_and_five_runs(self, cfg, jdb, two_runs):
        with pytest.raises(tm.MetricsError, match="2–5 runs"):
            tm.compare(jdb, ["test-a-1"])
        with pytest.raises(tm.MetricsError, match="2–5 runs"):
            tm.compare(jdb, [f"r{i}" for i in range(6)])

    def test_duplicates_collapse(self, cfg, jdb, two_runs):
        with pytest.raises(tm.MetricsError):
            tm.compare(jdb, ["test-a-1", "test-a-1"])

    def test_json_round_trip(self, cfg, jdb, two_runs):
        payload = tm.compare(jdb, two_runs).to_json()
        assert payload["run_ids"] == two_runs
        assert len(payload["metrics"]) == 2
