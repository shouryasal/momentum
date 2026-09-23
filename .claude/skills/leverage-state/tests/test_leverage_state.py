"""Golden values and invariants for leverage-state.

Run from the repo root: ``pytest .claude/skills/leverage-state/tests``

Everything here is built from synthetic frames rather than the live cache, so the suite is
offline, deterministic and fast. The numbers asserted are the ones the reference page argues
from; the invariants asserted are the two that make this skill safe to wire into sizing —
**the multiplier can never exceed 1.0** and **no direction is ever emitted**.
"""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from runs.features import binance_archive as ba  # noqa: E402
from runs.features import derivatives as dv  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / f".claude/skills/leverage-state/scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


compute_leverage = _load("compute_leverage")


# --------------------------------------------------------------------------- fixtures


def _funding(n=900, seed=0, start="2022-01-01"):
    """Synthetic 8h funding: a calm base with a crowded stretch in the middle third."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="8h", tz=UTC)
    rate = rng.normal(0.00005, 0.00004, n)
    rate[n // 3: 2 * n // 3] += 0.00035           # the crowded stretch
    df = pd.DataFrame({"funding_rate": rate, "mark_price": np.nan}, index=idx)
    df.index.name = "ts"
    return dv.FundingSeries(df, "BTCUSDT", cap=0.003, interval_h=8.0,
                            as_of=idx[-1].to_pydatetime(), stale=False, source="test")


def _spot(n=300, seed=1, start="2022-01-01"):
    """Daily candles whose drawdowns line up with the funding fixture's crowded stretch."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="1D", tz=UTC)
    ret = rng.normal(0.001, 0.02, n)
    ret[n // 3: 2 * n // 3] -= 0.004              # weaker where funding is hot
    close = 40000 * np.exp(np.cumsum(ret))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.97,
                         "close": close, "volume": np.full(n, 1000.0)}, index=idx)


def _metrics(n=300, seed=2, start="2022-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n * 24, freq="1h", tz=UTC)
    oi = 100000 * np.exp(np.cumsum(rng.normal(0, 0.002, len(idx))))
    return pd.DataFrame({
        "sum_open_interest": oi,
        "sum_open_interest_value": oi * 40000,
        "sum_toptrader_long_short_ratio": rng.normal(1.5, 0.2, len(idx)),
        "count_long_short_ratio": rng.normal(1.4, 0.2, len(idx)),
        "sum_taker_long_short_vol_ratio": rng.normal(1.0, 0.3, len(idx)),
    }, index=idx)


# --------------------------------------------------------------------------- loader traps


class TestLoaderTraps:
    """The four traps that silently corrupt a backtest. Each asserted on the shape the real
    archive file actually has, not on an invented one."""

    def test_out_of_order_and_duplicate_rows_are_repaired(self):
        """Trap 1: real files are unsorted (three ~−1,400 minute gaps) and 2020-09-01 ships
        every row twice."""
        idx = pd.to_datetime(["2023-01-01 00:10", "2023-01-01 00:00", "2023-01-01 00:05",
                              "2023-01-01 00:05", "2022-12-31 23:55"], utc=True)
        raw = pd.DataFrame({"create_time": idx,
                            "sum_open_interest": [10.0, 11.0, 12.0, 99.0, 13.0],
                            "sum_open_interest_value": [1e6] * 5})
        clean = ba.clean_metrics(raw)
        assert clean.index.is_monotonic_increasing
        assert len(clean) == 4                      # one duplicate timestamp collapsed
        assert clean["sum_open_interest"].iloc[-1] == 10.0
        # keep="last" on the duplicated 00:05
        assert clean.loc[pd.Timestamp("2023-01-01 00:05", tz=UTC), "sum_open_interest"] == 99.0

    def test_microsecond_timestamps_parse_by_digit_count(self):
        """Trap 2: spot klines switched ms -> us at 2025-01 while futures stayed ms."""
        ms = 1_735_689_600_000            # 2025-01-01 in milliseconds (13 digits)
        us = 1_735_689_600_000_000        # the same instant in microseconds (16 digits)
        sec = 1_735_689_600               # and in seconds (10 digits)
        out = ba.parse_epoch([sec, ms, us])
        assert out[0] == out[1] == out[2]
        assert out[0] == pd.Timestamp("2025-01-01", tz=UTC)

    def test_near_zero_open_interest_cannot_produce_inf(self):
        """Trap 3, in both real forms: 10 rows with OI <= 1 contract, and a *separate* 12 rows
        with a zero notional while the contract count is normal."""
        idx = pd.date_range("2023-01-01", periods=4, freq="5min", tz=UTC)
        raw = pd.DataFrame({"create_time": idx,
                            "sum_open_interest": [100000.0, 0.0, 100000.0, 100000.0],
                            "sum_open_interest_value": [4e9, 4e9, 0.0, 4e9]})
        clean = ba.clean_metrics(raw)
        assert np.isnan(clean["sum_open_interest"].iloc[1])
        assert np.isnan(clean["sum_open_interest_value"].iloc[2])
        for col in ("sum_open_interest", "sum_open_interest_value"):
            assert not np.isinf(clean[col].pct_change()).any()

    def test_empty_mark_price_is_tolerated(self):
        """Trap 4: 4,536 of 7,711 real BTCUSDT prints carry markPrice as an empty string."""
        assert dv._f("") is None
        assert dv._f(None) is None
        assert dv._f("nan") is None
        assert dv._f("123.5") == 123.5
        fund = _funding()
        feats = dv.funding_features(fund)
        assert feats["fr_8h"] is not None      # mark price absent, funding rate still read


class TestAgainstRealArchiveFiles:
    """Two genuine archive days, committed as fixtures, because a synthetic frame can only
    prove the loader handles the corruption I *thought* of. These are the two files the traps
    were found in: 2026-09-20 is the out-of-order day, 2020-09-01 is the all-duplicates day."""

    FIXTURES = Path(__file__).resolve().parent / "fixtures"

    def test_the_unsorted_day_really_is_unsorted_and_is_repaired(self):
        raw = ba.read_metrics_zip(self.FIXTURES / "BTCUSDT-metrics-2026-09-20.zip")
        assert len(raw) == 288
        assert not raw["create_time"].is_monotonic_increasing, "fixture is no longer the trap"
        gaps = raw["create_time"].diff().dt.total_seconds() / 60
        assert (gaps < -100).sum() == 3, "expected three large negative gaps"
        clean = ba.clean_metrics(raw)
        assert clean.index.is_monotonic_increasing
        assert len(clean) == 288
        assert not clean.index.has_duplicates

    def test_the_duplicated_day_collapses_to_288_rows(self):
        raw = ba.read_metrics_zip(self.FIXTURES / "BTCUSDT-metrics-2020-09-01.zip")
        assert len(raw) == 576, "fixture is no longer the trap"
        clean = ba.clean_metrics(raw)
        assert len(clean) == 288
        assert clean.index.is_monotonic_increasing

    def test_the_real_columns_are_the_ones_the_features_read(self):
        clean = ba.clean_metrics(
            ba.read_metrics_zip(self.FIXTURES / "BTCUSDT-metrics-2026-09-20.zip"))
        for col in ("sum_open_interest", "sum_open_interest_value",
                    "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                    "sum_taker_long_short_vol_ratio"):
            assert col in clean.columns
            assert clean[col].notna().any()
        assert clean["sum_open_interest"].between(1_000, 10_000_000).all()

    def test_gridding_a_real_day_gives_a_regular_index(self):
        clean = ba.clean_metrics(
            ba.read_metrics_zip(self.FIXTURES / "BTCUSDT-metrics-2026-09-20.zip"))
        grid = ba.grid_metrics(clean)
        deltas = pd.Series(grid.index).diff().dropna().dt.total_seconds().unique()
        assert set(deltas) == {300.0}
        assert not np.isinf(grid["sum_open_interest"].pct_change()).any()

    def test_a_missing_or_corrupt_file_returns_empty_not_an_exception(self, tmp_path):
        assert ba.read_metrics_zip(tmp_path / "nope.zip").empty
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        assert ba.read_metrics_zip(bad).empty


# --------------------------------------------------------------------------- the invariants


class TestTheMultiplierCanOnlyReduce:
    """The property that makes this safe to wire into sizing: a stuck, stale, absent or
    absurd input degrades to 'no change', never to 'add'."""

    @pytest.mark.parametrize("p_dd,base,quad", [
        (0.50, 0.20, None), (0.10, 0.40, None), (0.20, 0.20, None),
        (0.0001, 0.99, 2.0), (None, None, None), (0.0, 0.0, 0.0),
        (-1.0, -1.0, -5.0), (1e9, 1e-9, 1e9), (0.3, 0.2, 0.001),
    ])
    def test_never_exceeds_one(self, p_dd, base, quad):
        out = dv.exposure_multiplier(p_dd, base, quad, quadrant_active=True)
        assert 0.0 < out["exposure_multiplier"] <= 1.0

    def test_a_calm_reading_cannot_raise_exposure(self):
        """P(dd) far *below* the base rate is the case that would tempt a naive ratio upward."""
        out = dv.exposure_multiplier(0.05, 0.40, None)
        assert out["exposure_multiplier"] == 1.0

    def test_missing_inputs_leave_it_at_one(self):
        assert dv.exposure_multiplier(None, 0.2, None)["exposure_multiplier"] == 1.0
        assert dv.exposure_multiplier(0.4, None, None)["exposure_multiplier"] == 1.0

    def test_a_decayed_edge_stands_the_funding_leg_down(self):
        """The 2026 case: the ratio says haircut, the credibility gate says no."""
        live = dv.exposure_multiplier(0.40, 0.20, None, funding_edge_live=True)
        dead = dv.exposure_multiplier(0.40, 0.20, None, funding_edge_live=False)
        assert live["exposure_multiplier"] == 0.5
        assert dead["exposure_multiplier"] == 1.0
        assert "stood down" in dead["funding_leg"]

    def test_the_floor_holds(self):
        out = dv.exposure_multiplier(1.0, 0.001, None)
        assert out["exposure_multiplier"] == dv.MULTIPLIER_FLOOR

    def test_quadrant_leg_only_applies_inside_the_quadrant(self):
        off = dv.exposure_multiplier(None, None, 2.0, quadrant_active=False)
        on = dv.exposure_multiplier(None, None, 2.0, quadrant_active=True)
        assert off["exposure_multiplier"] == 1.0
        assert on["exposure_multiplier"] == 0.5


class TestNoDirection:
    """This skill forecasts the second moment. A direction field would be a bug, not a feature —
    the measured table has the *highest* funding bucket carrying the second-highest median
    7-day return, so anything directional here would be backwards as well as out of scope."""

    BANNED = {"direction", "signal", "target_weight", "target_weights", "side", "action",
              "buy", "sell", "long", "short", "expected_return", "forecast_return",
              "price_target", "recommendation"}

    def _keys(self, obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.add(str(k).lower())
                self._keys(v, out)
        elif isinstance(obj, list):
            for v in obj:
                self._keys(v, out)
        return out

    def test_state_has_no_direction_field(self):
        fund, spot, met = _funding(), _spot(), _metrics()
        state = dv.leverage_state(
            dv.LeverageInputs(funding=fund, metrics=met, spot=spot, perp=spot), "BTC/USDT")
        keys = self._keys(state, set())
        assert not (keys & self.BANNED), f"direction-ish keys leaked: {keys & self.BANNED}"
        assert state["no_direction"] is True

    def test_document_has_no_direction_field(self):
        doc = compute_leverage.build(["BTC/USDT"], online=False, as_of=None,
                                     archive_root="/tmp/earn-none", cache="/tmp/earn-none",
                                     data_root="/tmp/earn-none")
        keys = self._keys(doc, set())
        assert not (keys & self.BANNED)
        assert doc["no_direction"] is True

    def test_no_return_statistic_in_the_table(self):
        """The table reports drawdown only. A median-return column would invite exactly the
        inversion the reference page warns about."""
        table = dv.drawdown_bucket_table(_funding(), _spot(), bootstrap=False)
        for b in table.get("buckets", []):
            assert not any("ret" in k for k in b)


# --------------------------------------------------------------------------- the table


class TestDrawdownTable:
    def test_isotonic_enforces_monotonicity(self):
        assert dv._isotonic([0.1, 0.3, 0.2], [10, 10, 10]) == pytest.approx([0.1, 0.25, 0.25])
        assert dv._isotonic([0.1, 0.2, 0.3], [1, 1, 1]) == pytest.approx([0.1, 0.2, 0.3])
        assert dv._isotonic([0.3, 0.2, 0.1], [1, 1, 1]) == pytest.approx([0.2, 0.2, 0.2])
        # weights matter: a big first block should dominate the pooled mean
        out = dv._isotonic([0.3, 0.1], [90, 10])
        assert out[0] == out[1] == pytest.approx(0.28)

    def test_shipped_p_dd_is_always_monotone(self):
        table = dv.drawdown_bucket_table(_funding(), _spot(), bootstrap=False)
        ps = [b["p_dd"] for b in table["buckets"]]
        assert all(ps[i] <= ps[i + 1] + 1e-9 for i in range(len(ps) - 1))

    def test_buckets_are_rolling_percentiles_not_fixed_levels(self):
        """A fixed threshold decays as funding compresses; a percentile does not. Two fixtures
        an order of magnitude apart in level must still produce three populated buckets."""
        hot = _funding()
        cold_frame = hot.frame.copy()
        cold_frame["funding_rate"] = cold_frame["funding_rate"] / 20.0
        cold = dv.FundingSeries(cold_frame, "BTCUSDT", cap=0.003, interval_h=8.0,
                                as_of=hot.as_of, stale=False, source="test")
        for series in (hot, cold):
            table = dv.drawdown_bucket_table(series, _spot(), bootstrap=False)
            assert table["usable"]
            assert len(table["buckets"]) == 3
            assert all(b["n"] > 0 for b in table["buckets"])

    def test_effective_n_is_reported_not_just_row_count(self):
        """Overlapping 7-day windows mean a row count overstates the evidence."""
        table = dv.drawdown_bucket_table(_funding(), _spot(), bootstrap=False)
        assert table["n_effective"] == table["n"] // table["horizon_days"]
        for b in table["buckets"]:
            assert b["n_effective"] < b["n"]

    def test_lookup_puts_a_value_in_the_right_bucket(self):
        table = dv.drawdown_bucket_table(_funding(), _spot(), bootstrap=False)
        lo, hi = table["cut_points"]
        assert dv.lookup_p_drawdown(table, lo - 1.0)[1] == 0
        assert dv.lookup_p_drawdown(table, (lo + hi) / 2.0)[1] == 1
        assert dv.lookup_p_drawdown(table, hi + 1.0)[1] == 2
        assert dv.lookup_p_drawdown(table, None) == (None, None)

    def test_a_short_window_refuses_rather_than_fitting_noise(self):
        table = dv.drawdown_bucket_table(_funding(n=40), _spot(n=20), bootstrap=False)
        assert table["usable"] is False
        assert table["edge_live"] is False


class TestPointInTime:
    """No feature may look ahead: the state at time T must be reproducible from data at T."""

    def test_future_rows_do_not_change_the_past(self):
        fund, spot, met = _funding(), _spot(), _metrics()
        cut = pd.Timestamp("2022-06-01", tz=UTC)
        full = dv.leverage_state(
            dv.LeverageInputs(funding=fund, metrics=met, spot=spot, perp=spot),
            "BTC/USDT", cut)
        truncated = dv.leverage_state(
            dv.LeverageInputs(
                funding=dv.FundingSeries(fund.frame.loc[fund.frame.index <= cut], "BTCUSDT",
                                         cap=fund.cap, interval_h=8.0, source="test"),
                metrics=met.loc[met.index <= cut],
                spot=spot.loc[spot.index <= cut],
                perp=spot.loc[spot.index <= cut]),
            "BTC/USDT", cut)
        assert full["funding"]["fr_8h"] == truncated["funding"]["fr_8h"]
        assert full["funding"]["funding_ann_3d"] == truncated["funding"]["funding_ann_3d"]
        assert full["open_interest"]["oi_chg_24h"] == truncated["open_interest"]["oi_chg_24h"]

    def test_the_table_only_uses_closed_forward_windows(self):
        """An anchor may not enter the table until its whole forward window has also closed."""
        fund, spot = _funding(), _spot()
        as_of = spot.index[-1]
        table = dv.drawdown_bucket_table(fund, spot, as_of, bootstrap=False)
        newest = max(b["fund_hi"] for b in table["buckets"])
        assert table["usable"]
        # the last DD_HORIZON_D anchors cannot be scored, so n is short of the row count
        assert table["n"] <= len(spot) - dv.DD_HORIZON_D
        assert np.isfinite(newest)

    def test_funding_features_ignore_prints_after_as_of(self):
        fund = _funding()
        early = dv.funding_features(fund, pd.Timestamp("2022-02-01", tz=UTC))
        late = dv.funding_features(fund, fund.frame.index[-1])
        assert early["n_prints"] < late["n_prints"]
        assert early["as_of"] < late["as_of"]


# --------------------------------------------------------------------------- features


class TestFeatures:
    def test_funding_annualiser_comes_from_the_data(self):
        fund = _funding()
        assert fund.per_year == pytest.approx(365 * 3)     # 8h prints
        feats = dv.funding_features(fund)
        assert feats["funding_interval_h"] == 8.0
        assert feats["funding_ann_3d"] is not None

    def test_capped_print_is_flagged_as_censored(self):
        fund = _funding()
        fund.frame.iloc[-1, fund.frame.columns.get_loc("funding_rate")] = 0.003
        assert dv.funding_features(fund)["fr_capped_flag"] is True
        fund.frame.iloc[-1, fund.frame.columns.get_loc("funding_rate")] = 0.0001
        assert dv.funding_features(fund)["fr_capped_flag"] is False

    def test_sign_run_length_is_signed(self):
        fund = _funding()
        fund.frame["funding_rate"] = 0.0002
        assert dv.funding_features(fund)["fr_sign_run_length"] > 0
        fund.frame["funding_rate"] = -0.0002
        assert dv.funding_features(fund)["fr_sign_run_length"] < 0

    def test_the_four_quadrants(self):
        met, spot = _metrics(), _spot()
        out = dv.oi_features(met, spot)
        assert out["quadrant"] in {"price_up_oi_up", "price_up_oi_down",
                                   "price_down_oi_up", "price_down_oi_down"}

    def test_basis_is_z_scored_and_observe_only(self):
        """Mean basis on Binance is −1.38 bps, so a raw-level rule is wrong by construction."""
        spot = _spot()
        perp = spot.copy()
        perp["close"] = perp["close"] * 1.0002
        out = dv.basis_features(spot, perp)
        assert out["observe_only"] is True
        assert out["basis_bps"] == pytest.approx(2.0, abs=0.01)
        assert out["basis_z_180"] is not None

    def test_cascade_is_labelled_a_proxy_and_names_no_liquidation(self):
        out = dv.cascade_features(_metrics(), _spot())
        assert out["source"] == "oi_price_proxy"
        assert "liquidations_usd" not in out
        assert not any("liquidat" in k for k in out)

    def test_crowding_agrees_with_the_bucket_it_is_built_from(self):
        """The label and the number beside it must not contradict each other."""
        table = dv.drawdown_bucket_table(_funding(), _spot(), bootstrap=False)
        assert dv.crowding_state({}, {}, table, 2) == "crowded"
        assert dv.crowding_state({}, {"oi_z_180": 0.0}, table, 0) == "clean"
        assert dv.crowding_state({}, {}, table, 1) == "mixed"
        assert dv.crowding_state({}, {}, None, None) == "unknown"

    def test_crowding_falls_back_without_a_table(self):
        out = dv.crowding_state({"funding_pctile_1y": 0.95},
                                {"quadrant": "price_down_oi_up", "oi_z_180": 2.0}, None, None)
        assert out == "crowded"


# --------------------------------------------------------------------------- the script


class TestScript:
    def test_runs_offline_with_no_data_at_all(self, tmp_path):
        """A source being down must produce a stale-flagged document, never a guess and never
        a crash."""
        doc = compute_leverage.build(["BTC/USDT", "ETH/USDT"], online=False, as_of=None,
                                     archive_root=str(tmp_path / "a"),
                                     cache=str(tmp_path / "c"),
                                     data_root=str(tmp_path / "d"))
        assert set(doc["pairs"]) == {"BTC/USDT", "ETH/USDT"}
        for state in doc["pairs"].values():
            assert state.get("exposure_multiplier", 1.0) == 1.0
            assert not state.get("drawdown_table", {}).get("usable", False)

    def test_writes_only_under_knowledge(self, tmp_path):
        doc = compute_leverage.build(["BTC/USDT"], online=False, as_of=None,
                                     archive_root=str(tmp_path / "a"),
                                     cache=str(tmp_path / "c"), data_root=str(tmp_path / "d"))
        out = compute_leverage.write_state(doc, tmp_path)
        assert out == tmp_path / "knowledge/state/leverage.json"
        assert json.loads(out.read_text())["no_direction"] is True

    def test_flag_needs_both_crowding_and_a_biting_multiplier(self):
        assert compute_leverage.should_flag(
            {"crowding_state": "crowded", "exposure_multiplier": 0.7}) is True
        assert compute_leverage.should_flag(
            {"crowding_state": "crowded", "exposure_multiplier": 1.0}) is False
        assert compute_leverage.should_flag(
            {"crowding_state": "mixed", "exposure_multiplier": 0.7}) is False
        assert compute_leverage.should_flag({}) is False

    def test_json_round_trips_without_nan_or_inf(self, tmp_path):
        fund, spot, met = _funding(), _spot(), _metrics()
        state = dv.leverage_state(
            dv.LeverageInputs(funding=fund, metrics=met, spot=spot, perp=spot), "BTC/USDT")
        text = json.dumps(state)
        assert "NaN" not in text and "Infinity" not in text
        assert json.loads(text) == state

    def test_summary_names_the_base_rate_beside_the_probability(self):
        """A tail probability without its base rate is the easiest way to mislead."""
        doc = compute_leverage.build(["BTC/USDT"], online=False, as_of=None,
                                     archive_root="/tmp/earn-none", cache="/tmp/earn-none",
                                     data_root="/tmp/earn-none")
        summary = compute_leverage.summarise(doc)
        row = summary["pairs"]["BTC/USDT"]
        assert "p_drawdown_7d" in row and "p_drawdown_base_rate" in row

    def test_cli_offline_exits_cleanly(self, tmp_path, capsys):
        code = compute_leverage.main([
            "--offline", "--no-flag", "--pairs", "BTC/USDT",
            "--root", str(tmp_path), "--archive-root", str(tmp_path / "a"),
            "--cache", str(tmp_path / "c"), "--data-root", str(tmp_path / "d")])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "BTC/USDT" in out["pairs"]
        assert (tmp_path / "knowledge/state/leverage.json").is_file()


def test_as_of_parsing_accepts_the_repo_timestamp_format():
    stamp = "2024-03-14T00:00:00Z"
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == stamp
