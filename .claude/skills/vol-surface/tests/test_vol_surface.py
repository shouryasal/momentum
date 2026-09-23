"""vol-surface: the body matches the script, and the script keeps its promises.

The golden values below come from the frozen fixture in ``tests/fixtures/`` — 1,100 days of
real BTC 4h closes and the matching real Deribit DVOL series. They are pinned so that a
change to an estimator has to be argued for rather than absorbed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]
FIXTURES = SKILL_DIR / "tests" / "fixtures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runs.features import volatility as vol  # noqa: E402
from runs.features.sampling import ols  # noqa: E402

MIN_TRAIN = 365
TARGET = 30.0


def load_script():
    path = SKILL_DIR / "scripts" / "compute_volsurface.py"
    spec = importlib.util.spec_from_file_location("volsurface_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixture_data():
    candles = pd.read_csv(FIXTURES / "btc_4h_close.csv")
    candles["date"] = pd.to_datetime(candles["date"], utc=True)
    dvol = pd.read_csv(FIXTURES / "btc_dvol_1d.csv")
    dvol["date"] = pd.to_datetime(dvol["date"], utc=True)
    return candles, dvol


# --------------------------------------------------------------------------- the body


def read_body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_body_references_exist():
    body = read_body()
    for rel in re.findall(r"references/[\w.-]+\.md", body):
        assert (SKILL_DIR / rel).exists(), f"{rel} referenced but missing"
    for rel in re.findall(r"scripts/([\w.-]+\.py)", body):
        assert (SKILL_DIR / "scripts" / rel).exists(), f"scripts/{rel} missing"


def test_body_states_its_hard_stops_and_trigger():
    body = read_body().lower()
    assert "hard stop" in body
    assert "when this runs" in body


def test_body_refuses_direction_in_writing():
    """The single most important sentence in this skill's contract."""
    body = read_body().lower()
    assert "no direction" in body
    assert "raw dvol" in body


# --------------------------------------------------------------------------- goldens


def test_blend_is_used_when_dvol_is_present(fixture_data):
    candles, dvol = fixture_data
    f = vol.forecast("BTC/USDT", candles, dvol, target_annual=TARGET, min_train=MIN_TRAIN)
    assert f.source == "blend"
    assert f.sigma_hat == pytest.approx(33.389, abs=0.05)
    assert f.vol_target_scalar == pytest.approx(0.8985, abs=0.002)
    assert f.dvol_last == pytest.approx(37.74, abs=0.01)
    assert f.as_of == "2025-07-01T00:00:00Z"


def test_har_fallback_when_dvol_is_absent(fixture_data):
    candles, _ = fixture_data
    f = vol.forecast("BTC/USDT", candles, None, target_annual=TARGET, min_train=MIN_TRAIN)
    assert f.source == "har"
    assert f.sigma_hat == pytest.approx(31.741, abs=0.05)
    assert f.dvol_last is None


def test_a_stale_dvol_series_falls_back_and_says_so(fixture_data):
    candles, dvol = fixture_data
    stale = dvol.loc[dvol["date"] < pd.Timestamp("2025-06-01", tz="UTC")]
    f = vol.forecast("BTC/USDT", candles, stale, target_annual=TARGET, min_train=MIN_TRAIN)
    assert f.stale is True
    assert f.source == "har"
    assert f.sigma_hat == pytest.approx(31.741, abs=0.05)


def test_raw_dvol_is_never_the_forecast(fixture_data):
    """The negative test: at DVOL 60 the emitted sigma must be the fitted level, not 60."""
    candles, dvol = fixture_data
    rv = vol.realized_variance_daily(candles)
    panel = rv[["date"]].copy()
    panel["fwd"] = vol.forward_vol(rv, 7)
    panel = panel.merge(dvol[["date", "close"]].set_axis(["date", "dvol"], axis=1),
                        on="date", how="inner").dropna()
    fitted = vol.fit_dvol_map(panel["dvol"], panel["fwd"])
    assert fitted.b < 0.9, "a slope near 1 would mean the map is doing nothing"
    assert fitted.apply(60) == pytest.approx(47.2, abs=1.5)
    assert fitted.apply(60) < 55.0


def test_dvol_beats_trailing_realised_on_the_frozen_panel(fixture_data):
    """The regression test the whole skill rests on."""
    candles, dvol = fixture_data
    rv = vol.realized_variance_daily(candles)
    panel = rv[["date"]].copy()
    panel["fwd"] = vol.forward_vol(rv, 7)
    panel["trail"] = vol.trailing_vol(rv, 30)
    panel = panel.merge(dvol[["date", "close"]].set_axis(["date", "dvol"], axis=1),
                        on="date", how="inner").dropna()
    trail_fit = ols(panel[["trail"]].to_numpy(), panel["fwd"].to_numpy(),
                    names=["trail"], nw_lag=7)
    dvol_fit = ols(panel[["dvol"]].to_numpy(), panel["fwd"].to_numpy(),
                   names=["dvol"], nw_lag=7)
    assert dvol_fit.r2 > trail_fit.r2
    assert dvol_fit.r2 == pytest.approx(0.126, abs=0.02)
    assert trail_fit.r2 == pytest.approx(0.077, abs=0.02)
    assert dvol_fit.tstat[1] > 3.0


# --------------------------------------------------------------------------- refusals


def test_a_broken_model_emits_no_forecast(fixture_data):
    """Health below the floor must withhold the forecast, not publish a wide number."""
    candles, dvol = fixture_data
    f = vol.forecast("BTC/USDT", candles, dvol, target_annual=TARGET,
                     min_train=MIN_TRAIN, min_oos_r2=0.99)
    assert f.sigma_hat is None
    assert f.source == "none"
    assert "broken" in f.refused


def test_a_withheld_forecast_still_leaves_a_cautious_scalar(fixture_data):
    """Withheld is not absent.

    The health floor bites on 11.7% of BTC days over 2019-2026, clustered in March 2020 and
    Jan-Aug 2022 — so a rule that emitted no scalar there would drop the volatility cap in
    exactly the regimes it exists for.
    """
    candles, dvol = fixture_data
    f = vol.forecast("BTC/USDT", candles, dvol, target_annual=TARGET,
                     min_train=MIN_TRAIN, min_oos_r2=0.99)
    assert f.degraded is True
    assert f.scalar_source == "trailing_30d_fallback"
    assert f.vol_target_scalar is not None
    assert 0 < f.vol_target_scalar <= 1.0
    # The fallback uses the MORE cautious of the two, so it can never size larger than
    # trailing realised vol alone would.
    assert f.vol_target_scalar <= vol.vol_target_scalar(TARGET, f.trailing_vol_30d) + 1e-9


def test_a_healthy_forecast_is_not_marked_degraded(fixture_data):
    candles, dvol = fixture_data
    f = vol.forecast("BTC/USDT", candles, dvol, target_annual=TARGET, min_train=MIN_TRAIN)
    assert f.degraded is False
    assert f.scalar_source == "sigma_hat"


def test_too_little_history_refuses(fixture_data):
    candles, dvol = fixture_data
    short = candles.tail(600)
    f = vol.forecast("BTC/USDT", short, dvol, target_annual=TARGET, min_train=MIN_TRAIN)
    assert f.sigma_hat is None
    assert "daily observations" in f.refused


def test_the_scalar_can_never_exceed_one():
    assert vol.vol_target_scalar(30.0, 5.0) == 1.0
    assert vol.vol_target_scalar(30.0, 60.0) == pytest.approx(0.5)
    assert vol.vol_target_scalar(30.0, 0.0) is None
    assert vol.vol_target_scalar(30.0, float("nan")) is None


def test_the_payload_carries_no_direction_field(fixture_data):
    candles, dvol = fixture_data
    payload = vol.forecast("BTC/USDT", candles, dvol, target_annual=TARGET,
                           min_train=MIN_TRAIN).as_dict()
    forbidden = {"direction", "signal", "side", "entry", "target_weight", "buy", "sell"}
    assert not forbidden & set(payload), "a vol forecast must not carry a view"


# --------------------------------------------------------------------------- no leakage


def test_forward_vol_is_strictly_forward(fixture_data):
    """Truncating the history must not change a forecast that was already made."""
    candles, dvol = fixture_data
    cut = pd.Timestamp("2025-05-01", tz="UTC")
    full = vol.forecast("BTC/USDT", candles.loc[candles["date"] < cut],
                        dvol, target_annual=TARGET, min_train=MIN_TRAIN)
    trimmed = vol.forecast("BTC/USDT", candles.loc[candles["date"] < cut],
                           dvol.loc[dvol["date"] < cut], target_annual=TARGET,
                           min_train=MIN_TRAIN)
    assert full.sigma_hat == pytest.approx(trimmed.sigma_hat, abs=1e-6), \
        "future DVOL rows changed a past forecast — that is look-ahead"


def test_a_past_forecast_is_reproducible_as_of_that_date(fixture_data):
    """Point-in-time replay: truncating the history must reproduce the same number.

    This is the property the backtest and the decision replay depend on. Verified on the
    live store too: forecasts for 2023-03-12, 2024-03-06, 2024-08-06, 2025-02-26,
    2025-10-15 and 2026-05-20 are identical to the emitted precision whether computed from
    data truncated at that date or from the full nine-year history.
    """
    candles, dvol = fixture_data
    cut = pd.Timestamp("2025-04-01", tz="UTC")
    past = vol.forecast("BTC/USDT", candles.loc[candles["date"] < cut],
                        dvol.loc[dvol["date"] < cut], target_annual=TARGET,
                        min_train=MIN_TRAIN)
    assert past.sigma_hat is not None

    # the same date, recomputed from the WHOLE fixture
    rv = vol.realized_variance_daily(candles)
    har = vol.har_walk_forward(rv, horizon=7, min_train=MIN_TRAIN, refit_every=30)
    joined = rv[["date"]].copy()
    joined["fwd_vol"] = vol.forward_vol(rv, 7)
    joined = joined.merge(dvol[["date", "close"]].set_axis(["date", "dvol"], axis=1),
                          on="date", how="left")
    wf = vol.dvol_walk_forward(joined, horizon=7, min_train=MIN_TRAIN, refit_every=30)
    merged = har.set_axis(["date", "actual", "har"], axis=1).merge(
        wf[["date", "pred"]].set_axis(["date", "dvolmap"], axis=1), on="date")
    row = merged.loc[merged["date"] == cut - pd.Timedelta(days=1)]
    assert not row.empty
    full_history = float((row["har"].iloc[0] + row["dvolmap"].iloc[0]) / 2)
    assert past.sigma_hat == pytest.approx(full_history, abs=5e-4), \
        "the forecast changed once later data existed — that is look-ahead"


def test_realized_variance_drops_only_a_partial_final_day(fixture_data):
    candles, _ = fixture_data
    rv_full = vol.realized_variance_daily(candles)
    partial = candles.loc[candles["date"] <= candles["date"].iloc[-1] + pd.Timedelta(hours=4)]
    partial = pd.concat([partial, partial.tail(1).assign(
        date=partial["date"].iloc[-1] + pd.Timedelta(hours=4))], ignore_index=True)
    rv_partial = vol.realized_variance_daily(partial)
    assert len(rv_partial) == len(rv_full)


# --------------------------------------------------------------------------- the script


def test_the_scripts_self_test_passes():
    mod = load_script()
    assert mod.self_test() == 0


def test_the_script_summarises_a_refusal_rather_than_hiding_it():
    mod = load_script()
    text = mod.summarise({"pairs": {"BTC/USDT": {"sigma_hat": None, "refused": "broken"}}})
    assert "NO FORECAST" in text and "broken" in text
