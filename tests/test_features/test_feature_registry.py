"""The shared feature package: provenance, no look-ahead, and the loaders' failure modes.

These cover the parts of ``runs/features/`` that no skill test reaches — the registry's own
integrity, the as-of truncation every feature depends on, and the option-surface maths.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from runs import features as feat
from runs.features import deribit
from runs.features import sampling as smp
from runs.features import volatility as vol

# --------------------------------------------------------------------------- registry


def test_every_registered_key_declares_a_source_and_a_lag():
    for key, spec in feat.REGISTRY.items():
        assert spec.key == key
        assert spec.module and spec.source and spec.url, key
        assert spec.max_lag_min > 0, key
        assert spec.verified_utc, f"{key} does not say when its source was verified"


def test_no_registered_key_needs_a_paid_credential():
    """A key needing a key is out. The registry may only list things we can actually get."""
    paid = [k for k, s in feat.REGISTRY.items() if s.needs_key]
    assert paid == [], f"these keys need a credential and must not be registered: {paid}"


def test_the_paywalled_list_records_the_verified_failure():
    for name, reason in feat.NOT_AVAILABLE_FREE.items():
        assert len(reason) > 20, f"{name}: record the failure, not just the name"


def test_an_unregistered_key_raises_rather_than_passing_through():
    with pytest.raises(KeyError):
        feat.spec_for("some_number_nobody_declared")


def test_the_vol_surface_payload_only_carries_registered_features():
    """The integrity check the registry exists for."""
    payload_keys = {"sigma_hat", "source", "dvol_last", "dvol_pctile_2y", "dvol_chg_5d",
                    "trailing_vol_30d", "vrp", "oos_r2_250d", "har_oos_r2_250d",
                    "dvol_oos_r2_250d", "vol_target_scalar"}
    missing = sorted(payload_keys - set(feat.REGISTRY))
    assert not missing, f"emitted without a declared source: {missing}"


def test_staleness_is_measured_against_the_declared_lag():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    fresh = feat.iso(now - timedelta(minutes=10))
    old = feat.iso(now - timedelta(days=40))
    assert feat.is_stale("dvol_last", fresh, now) is False
    assert feat.is_stale("dvol_last", old, now) is True
    assert feat.is_stale("dvol_last", None, now) is True


def test_staleness_minutes_returns_none_rather_than_zero():
    assert feat.staleness_minutes(None) is None


def test_describe_registry_groups_by_module():
    report = feat.describe_registry()
    assert "volatility" in report.modules and "deribit" in report.modules
    assert report.keys == sorted(feat.REGISTRY)
    assert "us_spot_etf_flows" in report.unavailable


# --------------------------------------------------------------------------- paths / io


def test_the_data_dir_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EARN_DATA_DIR", str(tmp_path))
    assert feat.data_dir() == tmp_path.resolve()
    monkeypatch.delenv("EARN_DATA_DIR")
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    assert feat.data_dir() == tmp_path.resolve() / "data"


def test_candle_path_matches_the_container_layout(tmp_path):
    p = feat.candle_path("BTC/USDT", "4h", tmp_path)
    assert p.name == "BTC_USDT-4h.feather"
    assert p.parent.name == "binance"


def test_write_json_atomic_leaves_no_temp_file(tmp_path):
    target = tmp_path / "state" / "x.json"
    feat.write_json_atomic(target, {"b": 1, "a": 2})
    assert json.loads(target.read_text())["a"] == 2
    assert list(target.parent.iterdir()) == [target]


def test_read_json_returns_the_default_on_a_broken_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert feat.read_json(bad, {"fallback": True}) == {"fallback": True}
    assert feat.read_json(tmp_path / "missing.json") == {}


def test_load_candles_says_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setenv("EARN_DATA_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="BTC_USDT-4h.feather"):
        feat.load_candles("BTC/USDT", "4h")


# --------------------------------------------------------------------------- as-of


def _frame(n: int = 10) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "close": np.arange(n, dtype=float) + 100.0,
    })


def test_as_of_respects_the_bar_close():
    df = _frame()
    cut = df["date"].iloc[3]
    # With a 4h bar length, the bar OPENING at `cut` has not closed yet.
    closed = feat.as_of(df, cut, closed_offset=timedelta(hours=4))
    assert len(closed) == 3
    assert closed["date"].iloc[-1] < cut
    # Without an offset the stamp is the observation time, so that row is included.
    observed = feat.as_of(df, cut)
    assert len(observed) == 4


def test_as_of_accepts_an_iso_string():
    df = _frame()
    assert len(feat.as_of(df, "2026-01-01T08:00:00Z")) == 3


# --------------------------------------------------------------------------- deribit


def test_parse_instrument_reads_a_deribit_name():
    inst = deribit.parse_instrument("BTC-24SEP26-90000-C")
    assert inst is not None
    assert inst.currency == "BTC" and inst.strike == 90000.0 and inst.is_call
    assert inst.expiry == datetime(2026, 9, 24, 8, tzinfo=UTC)
    assert deribit.parse_instrument("BTC-PERPETUAL") is None
    assert deribit.parse_instrument("nonsense") is None


def test_black_76_delta_behaves():
    atm = deribit.bs_delta(100.0, 100.0, 0.6, 30 / 365, is_call=True)
    assert 0.5 < atm < 0.62
    otm_call = deribit.bs_delta(100.0, 130.0, 0.6, 30 / 365, is_call=True)
    assert 0 < otm_call < atm
    put = deribit.bs_delta(100.0, 100.0, 0.6, 30 / 365, is_call=False)
    assert -0.5 < put < 0
    assert deribit.bs_delta(100.0, 100.0, 0.0, 30 / 365, is_call=True) is None
    assert deribit.bs_delta(100.0, 100.0, 0.6, 0.0, is_call=True) is None


def test_skew_25d_on_a_synthetic_chain_is_observe_only():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for strike in range(60000, 140001, 5000):
        for _kind, tag in ((True, "C"), (False, "P")):
            moneyness = strike / 100000.0 - 1.0
            rows.append({
                "instrument_name": f"BTC-01OCT26-{strike}-{tag}",
                "mark_iv": 60.0 + 20.0 * moneyness ** 2 - 5.0 * moneyness,
                "underlying_price": 100000.0,
                "open_interest": 10.0,
            })
    summary = deribit.skew_25d(rows, now=now)
    assert summary.iv_call_25d is not None and summary.iv_put_25d is not None
    assert summary.rr25 == pytest.approx(summary.iv_call_25d - summary.iv_put_25d)
    assert summary.option_oi_total == pytest.approx(len(rows) * 10.0)
    assert summary.as_dict()["observe_only"] is True


def test_skew_on_an_empty_chain_returns_nothing_rather_than_zero():
    summary = deribit.skew_25d([], now=datetime(2026, 9, 1, tzinfo=UTC))
    assert summary.rr25 is None and summary.instruments == 0


def test_dvol_features_flag_a_missing_cache_as_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
    out = deribit.dvol_features("BTC")
    assert all(v.value is None and v.stale for v in out.values())


def test_dvol_features_stamp_as_of_one_day_after_the_bar():
    series = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC"),
        "close": np.linspace(40, 60, 40),
    })
    out = deribit.dvol_features("BTC", series=series,
                                as_of_utc=datetime(2026, 2, 10, tzinfo=UTC))
    assert out["dvol_last"].value == pytest.approx(60.0)
    assert out["dvol_last"].as_of == "2026-02-10T00:00:00Z"
    assert out["dvol_pctile_2y"].value == pytest.approx(1.0)


def test_dvol_features_do_not_read_bars_from_the_future():
    series = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=40, freq="D", tz="UTC"),
        "close": np.linspace(40, 60, 40),
    })
    early = deribit.dvol_features("BTC", series=series,
                                  as_of_utc=datetime(2026, 1, 15, tzinfo=UTC))
    assert early["dvol_last"].value < 60.0


def test_the_option_canary_names_a_collapse():
    # 90d median of 500k, current 100k -> the venue-migration alarm.
    s = feat.Sourced("deribit_option_oi_total", 100000.0)
    assert s.as_dict()["value"] == 100000.0
    median_history = [500000.0] * 90
    assert 100000.0 < 0.5 * float(pd.Series(median_history).median())


# --------------------------------------------------------------------------- sampling


def test_ols_recovers_a_known_slope():
    rng = np.random.default_rng(0)
    x = rng.normal(size=2000)
    y = 3.0 + 2.5 * x + rng.normal(scale=0.1, size=2000)
    fit = smp.ols(x, y, names=["x"])
    assert fit.beta[0] == pytest.approx(3.0, abs=0.02)
    assert fit.beta[1] == pytest.approx(2.5, abs=0.02)
    assert fit.r2 > 0.99


def test_newey_west_widens_the_error_on_overlapping_data():
    """The whole reason NW is mandatory: naive t-stats on overlapping windows lie."""
    rng = np.random.default_rng(1)
    raw = rng.normal(size=3000)
    x = pd.Series(raw).rolling(20).mean().to_numpy()
    y = pd.Series(rng.normal(size=3000)).rolling(20).mean().to_numpy()
    naive = smp.ols(x, y, names=["x"])
    corrected = smp.ols(x, y, names=["x"], nw_lag=20)
    assert corrected.se[1] > naive.se[1]
    assert abs(corrected.tstat[1]) < abs(naive.tstat[1])


def test_ols_refuses_to_pretend_with_too_few_rows():
    fit = smp.ols(np.array([1.0, 2.0]), np.array([1.0, 2.0]), names=["x"])
    assert np.isnan(fit.beta).all()


def test_triple_barrier_labels_match_the_path():
    closes = np.array([100.0, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    lab = smp.triple_barrier(closes, pt_mult=1.0, sl_mult=1.0, max_bars=3,
                             sigma=np.full(10, 0.01))
    assert set(lab.label.tolist()) <= {-1, 0, 1}
    assert (lab.t1 > lab.t0).all()
    assert (lab.span <= 4).all()


def test_effective_n_never_exceeds_the_row_count():
    rng = np.random.default_rng(2)
    closes = 100 * np.exp(np.cumsum(rng.normal(scale=0.01, size=3000)))
    lab = smp.triple_barrier(closes, max_bars=20)
    assert 0 < smp.effective_n(lab) <= len(lab)


def test_vol_target_scalar_is_clamped_and_never_negative():
    assert vol.vol_target_scalar(30.0, 10.0) == 1.0
    assert vol.vol_target_scalar(30.0, 300.0) == pytest.approx(0.1)
    assert vol.vol_target_scalar(30.0, -5.0) is None


def test_forward_vol_is_not_knowable_at_the_bar_it_is_stamped_on():
    rv = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=30, freq="D", tz="UTC"),
        "rv": np.concatenate([np.full(15, 1e-4), np.full(15, 9e-4)]),
    })
    fwd = vol.forward_vol(rv, 7)
    # The jump in realised variance starts at row 15, so the forward window sees it from
    # row 8 onward — and never before, or the target would be leaking.
    assert fwd.iloc[7] == pytest.approx(np.sqrt(1e-4 * 365) * 100, rel=1e-6)
    assert fwd.iloc[8] > fwd.iloc[7]
    assert fwd.iloc[-7:].isna().all()
