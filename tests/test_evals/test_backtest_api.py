"""The measurement API: what it refuses, what it copies, and what it computes.

The refusals are tested one tier-2 key at a time because the failure this module exists to
prevent is a *silent* one — a patch that reaches for a risk limit and is quietly dropped
produces a result labelled with a change that never happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import backtest_api as api
from tests.test_evals.conftest import make_result_zip

# ------------------------------------------------------------------ patch validation


def test_flatten_accepts_nested_and_dotted_forms():
    assert api.flatten_patch({"params": {"vol": {"target_annual": 0.4}}}) == {
        "params.vol.target_annual": 0.4}
    assert api.flatten_patch({"params.vol.target_annual": 0.4}) == {
        "params.vol.target_annual": 0.4}


def test_a_dotted_key_keeps_its_mapping_value_whole():
    flat = api.flatten_patch({"trading.take_profit.roi_table": {"0": 0.35, "720": 0.0}})
    assert flat["trading.take_profit.roi_table"] == {"0": 0.35, "720": 0.0}


@pytest.mark.parametrize("key", [
    "risk.max_gross_exposure", "risk.usdt_floor", "bounds.sleeve_a.vol.target_annual",
    "universe.pairs", "phase", "exchange.pair_whitelist", "exchange.key", "dry_run",
    "dry_run_wallet", "stake_amount", "max_open_trades", "timeframe",
    "trading.plan_bounds.stop_pct", "trading.sleeves.b.take_profit",
])
def test_tier2_keys_are_refused_by_name_with_a_reason(key):
    with pytest.raises(api.PatchRefused) as exc:
        api.validate_patch({key: 1})
    message = str(exc.value)
    assert key in message
    assert "REFUSED" in message
    assert len(message.splitlines()) >= 2  # the reason is printed, not just the key


def test_unknown_namespaces_are_refused_rather_than_ignored():
    with pytest.raises(api.PatchRefused, match="unknown namespace"):
        api.validate_patch({"freqai.enabled": True})


def test_a_bare_namespace_is_not_a_key():
    with pytest.raises(api.PatchRefused, match="is a namespace, not a key"):
        api.validate_patch({"params": 1})


def test_allowed_namespaces_pass_through_unchanged():
    flat = api.validate_patch({
        "params.vol.target_annual": 0.35,
        "trading.stoploss.trailing.enabled": True,
        "execution.rebalance_band": 0.04,
    })
    assert set(flat) == {"params.vol.target_annual", "trading.stoploss.trailing.enabled",
                         "execution.rebalance_band"}


def test_every_denied_key_has_a_stated_reason():
    assert all(len(v.split()) >= 4 for v in api.DENIED_KEYS.values())


# ------------------------------------------------------------------------- bounds


def test_out_of_bounds_param_is_refused_not_silently_clamped(repo: Path):
    riskgate = json.loads((repo / "config" / "riskgate.json").read_text())
    with pytest.raises(api.PatchRefused) as exc:
        api.check_bounds({"params.vol.target_annual": 0.9}, riskgate, "a")
    assert "bounds.max 0.5" in str(exc.value)
    assert "clamp" in str(exc.value)


def test_in_bounds_param_is_allowed(repo: Path):
    riskgate = json.loads((repo / "config" / "riskgate.json").read_text())
    api.check_bounds({"params.vol.target_annual": 0.35}, riskgate, "a")


# ---------------------------------------------------------------------- materialise


def _spec(repo: Path, patch: dict, pairs: tuple[str, ...] = ()) -> api.RunSpec:
    return api.RunSpec(patch=api.validate_patch(patch), timerange="20210101-20220101",
                       pairs=pairs, sleeve="a", strategy="SleeveA", fee_bps=10.0,
                       slippage_bps=5.0, config_digest="cfg", data_digest="dat")


def test_patch_is_applied_to_a_copy_and_never_to_the_live_config(repo: Path):
    before = (repo / "config" / "params-sleeve-a.json").read_text()
    run_root = repo / "ft_userdata" / "research" / "x"
    cfg = api.materialise(_spec(repo, {"params.vol.target_annual": 0.45}), run_root,
                          root=repo)
    assert (repo / "config" / "params-sleeve-a.json").read_text() == before
    copied = json.loads((cfg / "params-sleeve-a.json").read_text())
    assert copied["params"]["vol"]["target_annual"] == 0.45
    assert copied["params"]["trend"]["ma_days"] == 200  # untouched keys survive


def test_trading_patch_lands_on_this_sleeve_only(repo: Path):
    run_root = repo / "ft_userdata" / "research" / "y"
    cfg = api.materialise(
        _spec(repo, {"trading.take_profit.roi_table": {"0": 0.35}}), run_root, root=repo)
    doc = json.loads((cfg / "riskgate.json").read_text())
    assert doc["trading"]["sleeves"]["a"]["take_profit"]["roi_table"] == {"0": 0.35}
    assert doc["trading"]["sleeves"]["b"]["take_profit"]["roi_table"] == {"0": 10.0}


def test_pairs_narrow_the_whitelist(repo: Path):
    run_root = repo / "ft_userdata" / "research" / "z"
    cfg = api.materialise(_spec(repo, {}, ("BTC/USDT", "ETH/USDT")), run_root, root=repo)
    doc = json.loads((cfg / "freqtrade-a.json").read_text())
    assert doc["exchange"]["pair_whitelist"] == ["BTC/USDT", "ETH/USDT"]


def test_pairs_can_never_add_to_the_universe(repo: Path):
    run_root = repo / "ft_userdata" / "research" / "w"
    with pytest.raises(api.PatchRefused, match="never add to it"):
        api.materialise(_spec(repo, {}, ("DOGE/USDT",)), run_root, root=repo)


def test_credentials_are_stripped_from_the_copied_bot_config(repo: Path):
    run_root = repo / "ft_userdata" / "research" / "v"
    cfg = api.materialise(_spec(repo, {}), run_root, root=repo)
    doc = json.loads((cfg / "freqtrade-a.json").read_text())
    assert "key" not in doc["exchange"] and "secret" not in doc["exchange"]


def test_container_mounts_the_copy_read_only_and_live_data_read_only(repo: Path, candles):
    run_root = (repo / "ft_userdata" / "research" / "u").resolve()
    argv = api.container_argv(_spec(repo, {}), run_root, root=repo)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert any(m.endswith("/freqtrade/earn-config:ro") for m in mounts)
    assert any(m.endswith("/freqtrade/user_data/data:ro") for m in mounts)
    assert any(m.endswith("/freqtrade/knowledge:ro") for m in mounts)
    assert any(m.endswith("/freqtrade/user_data/strategies:ro") for m in mounts)
    # the live config directory is never mounted
    assert not any(str((repo / "config").resolve()) + ":" in m for m in mounts)
    # the journal the container writes is the throwaway one
    assert any(m == f"{run_root / 'journal'}:/freqtrade/journal" for m in mounts)
    assert "--fee" in argv and argv[argv.index("--fee") + 1] == "0.00150000"


# ---------------------------------------------------------------------- timeranges


@pytest.mark.parametrize("bad", ["", "2021", "20210101", "20220101-20210101", "x-y"])
def test_bad_timeranges_raise(bad):
    with pytest.raises(api.BacktestError):
        api.parse_timerange(bad)


def test_fold_windows_state_both_spans_and_skip_the_warmup():
    folds = api.fold_windows("20210101-20250101", 4, warmup_days=365)
    assert len(folds) == 4
    # every in-sample span starts at the beginning (expanding) and ends where its
    # out-of-sample window begins, so the two never overlap
    for is_, oos in folds:
        assert is_.startswith("20210101-")
        assert is_.split("-")[1] == oos.split("-")[0]
    # the warm-up year is never scored, and the folds tile the rest without overlapping
    assert folds[0][1].startswith("20220101-")
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert earlier[1].split("-")[1] == later[1].split("-")[0]
    assert folds[-1][1].endswith("20250101")


def test_fold_windows_refuse_a_window_shorter_than_the_warmup():
    with pytest.raises(api.BacktestError, match="warm-up"):
        api.fold_windows("20210101-20210601", 2, warmup_days=365)


# -------------------------------------------------------------------------- metrics


def test_metrics_are_recomputed_from_the_archive(tmp_path: Path, repo: Path, candles):
    zip_path = make_result_zip(tmp_path / "backtest-result-1.zip", profit_total=0.25)
    spec = _spec(repo, {})
    m = api.metrics_from_zip(zip_path, spec, run_id="t1")
    assert m.net_return_pct == pytest.approx(25.0, abs=1e-6)
    assert m.trades == 20
    assert 0 < m.max_drawdown_pct < 100
    assert m.cagr_pct > 0
    # fees come from the ORDER costs at the measured rate, not from a headline field
    assert m.fees_paid_quote == pytest.approx(20 * 2010.0 * 0.0015, rel=1e-9)
    assert m.fees_pct_of_start == pytest.approx(m.fees_paid_quote / 100.0, rel=1e-9)
    # freqtrade's own TOTAL row is dropped: it is the sum, not a way out of a trade
    assert {e.reason for e in m.exit_reasons} == {"roi", "trailing_stop_loss"}
    assert sum(e.trades for e in m.exit_reasons) == m.trades
    assert [y.year for y in m.per_year] == ["2021", "2022"]
    assert 0 < m.time_in_market_pct <= 100
    assert len(m.equity_curve) == 400


def test_the_equity_curve_is_the_series_the_drawdown_comes_from(tmp_path, repo, candles):
    zip_path = make_result_zip(tmp_path / "backtest-result-2.zip", profit_total=0.40)
    m = api.metrics_from_zip(zip_path, _spec(repo, {}), run_id="t2")
    equity = [v for _, v in m.equity_curve]
    peak, worst = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100.0)
    assert m.max_drawdown_pct == pytest.approx(worst, abs=1e-3)
    assert equity[-1] == pytest.approx(10000.0 * 1.40, rel=1e-6)


def test_per_regime_splits_by_btc_trend_when_candles_exist(tmp_path, repo, candles):
    zip_path = make_result_zip(tmp_path / "backtest-result-3.zip")
    m = api.metrics_from_zip(zip_path, _spec(repo, {}), run_id="t3")
    names = {r.regime for r in m.per_regime}
    assert names & {"trend_up", "trend_down", "warmup"}
    assert sum(r.days for r in m.per_regime) == len(m.equity_curve)


# ------------------------------------------------------------------- run_backtest


class FakeDocker:
    """Drops a result archive where the real container would, and records the argv."""

    def __init__(self, **zip_kwargs):
        self.calls: list[list[str]] = []
        self.zip_kwargs = zip_kwargs

    def __call__(self, argv, cwd, timeout_s):
        self.calls.append(list(argv))
        mount = next(a for a in argv if a.endswith(":/freqtrade/user_data"))
        results = Path(mount.split(":/freqtrade")[0]) / "backtest_results"
        make_result_zip(results / "backtest-result-fake.zip", **self.zip_kwargs)
        return api.CommandResult(True, 0, "ok")


def test_run_backtest_end_to_end_with_an_injected_runner(repo: Path, candles):
    runner = FakeDocker(profit_total=0.30)
    m = api.run_backtest({}, "20210101-20220206", root=repo, runner=runner)
    assert m.net_return_pct == pytest.approx(30.0)
    assert m.cached is False
    assert len(runner.calls) == 1


def test_an_identical_run_is_served_from_cache(repo: Path, candles):
    runner = FakeDocker(profit_total=0.30)
    first = api.run_backtest({}, "20210101-20220206", root=repo, runner=runner)
    second = api.run_backtest({}, "20210101-20220206", root=repo, runner=runner)
    assert len(runner.calls) == 1, "the second identical run must not start a container"
    assert second.cached is True
    assert second.digest == first.digest
    assert second.net_return_pct == first.net_return_pct


def test_a_different_patch_gets_a_different_digest_and_a_new_run(repo: Path, candles):
    runner = FakeDocker(profit_total=0.30)
    a = api.run_backtest({}, "20210101-20220206", root=repo, runner=runner)
    b = api.run_backtest({"params.vol.target_annual": 0.35}, "20210101-20220206",
                         root=repo, runner=runner)
    assert a.digest != b.digest
    assert len(runner.calls) == 2


def test_a_refused_patch_never_starts_a_container(repo: Path, candles):
    runner = FakeDocker()
    with pytest.raises(api.PatchRefused):
        api.run_backtest({"risk.max_gross_exposure": 0.95}, "20210101-20220206",
                         root=repo, runner=runner)
    assert runner.calls == []


def test_a_failing_container_raises_rather_than_returning_a_number(repo: Path, candles):
    def failing(argv, cwd, timeout_s):
        return api.CommandResult(False, 1, "boom\nstack trace")

    with pytest.raises(api.BacktestError, match="backtest failed"):
        api.run_backtest({}, "20210101-20220206", root=repo, runner=failing)


# ---------------------------------------------------------------------- comparison


def test_compare_reports_both_directions(repo: Path, candles, tmp_path):
    base = api.metrics_from_zip(
        make_result_zip(tmp_path / "b.zip", profit_total=0.20, trades=40),
        _spec(repo, {}), run_id="base")
    variant = api.metrics_from_zip(
        make_result_zip(tmp_path / "v.zip", profit_total=0.40, trades=10),
        _spec(repo, {"params.vol.target_annual": 0.35}), run_id="var")
    cmp = api.compare(variant, base, with_benchmark=False)
    assert "net_return_pct" in cmp.better
    assert "trades" in cmp.better  # fewer trades is better on the declared direction
    assert cmp.deltas["net_return_pct"] == pytest.approx(20.0, abs=1e-6)
    assert set(cmp.better) | set(cmp.worse) | set(cmp.unchanged) == set(cmp.deltas)
    assert "per-year delta" in cmp.describe()


def test_compare_refuses_two_different_windows(repo: Path, candles, tmp_path):
    a = api.metrics_from_zip(make_result_zip(tmp_path / "a.zip"), _spec(repo, {}), run_id="a")
    other = api.RunSpec(patch={}, timerange="20220101-20230101", pairs=(), sleeve="a",
                        strategy="SleeveA", fee_bps=10.0, slippage_bps=5.0,
                        config_digest="c", data_digest="d")
    b = api.metrics_from_zip(make_result_zip(tmp_path / "b2.zip"), other, run_id="b")
    with pytest.raises(api.BacktestError, match="different windows"):
        api.compare(a, b, with_benchmark=False)


def test_an_identical_variant_is_reported_as_noise(repo: Path, candles, tmp_path):
    spec = _spec(repo, {})
    z = make_result_zip(tmp_path / "same.zip")
    a = api.metrics_from_zip(z, spec, run_id="a")
    b = api.metrics_from_zip(z, spec, run_id="b")
    cmp = api.compare(a, b, with_benchmark=False)
    assert cmp.is_noise
    assert "does nothing" in cmp.describe()


# ----------------------------------------------------------------------- benchmark


def test_buy_and_hold_is_costed_on_both_sides(repo: Path, candles):
    costed = api.buy_and_hold("BTC/USDT", "2021-01-01", "2023-01-01", root=repo)
    free = api.buy_and_hold("BTC/USDT", "2021-01-01", "2023-01-01", root=repo,
                            fee_bps=0.0, slippage_bps=0.0)
    assert costed.net_return_pct < free.net_return_pct
    assert costed.max_drawdown_pct > 0


def test_compare_prints_the_benchmark_excess(repo: Path, candles, tmp_path):
    base = api.metrics_from_zip(make_result_zip(tmp_path / "bb.zip", profit_total=0.2),
                                _spec(repo, {}), run_id="base")
    var = api.metrics_from_zip(make_result_zip(tmp_path / "vv.zip", profit_total=0.5),
                               _spec(repo, {}), run_id="var")
    cmp = api.compare(var, base, root=repo)
    assert cmp.benchmark is not None
    assert cmp.variant_excess_vs_benchmark_pct is not None
    assert "buy-and-hold" in cmp.describe()


# --------------------------------------------------------------------- walk forward


def test_walk_forward_runs_both_arms_per_fold_and_states_the_split(repo: Path, candles):
    runner = FakeDocker(profit_total=0.30)
    wf = api.walk_forward({"params.vol.target_annual": 0.35}, 2,
                          timerange="20210101-20240101", root=repo, runner=runner)
    assert len(wf.folds) == 2
    assert all(f.in_sample and f.out_of_sample for f in wf.folds)
    assert "fixed rules" in wf.fitting
    assert 0.0 <= wf.oos_win_rate <= 1.0
    assert "IS " in wf.describe() and "OOS " in wf.describe()
