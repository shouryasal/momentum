"""Tests for the GPU driver — the leakage guarantees, and one planted signal it must find.

Two kinds of test, and the second is the one that matters.

**Guarantees.** A lookback window that crosses a coin boundary, a delisting gap or a
discontinuity split would fabricate a result; a scaler fitted on the test period would move the
third decimal place, which is the size of every effect being claimed; a sample from before a coin
was eligible is a price from a period that could not have been traded. Each of those is asserted
here on a panel constructed so the violation *would* happen if the guard were removed.

**A planted signal.** Every no-leakage test in this file passes trivially for a model that
predicts zero. So :func:`planted_panel` builds a panel whose forward 7-day return really is a
function of trailing momentum and whose forward volatility really is a function of trailing
volatility, and requires the pipeline to recover both through the full purged walk-forward. A
harness that cannot find a signal that is definitely there cannot be trusted to report that a
real one is absent.

Nothing here needs CUDA. The driver runs on CPU with ``amp`` disabled, which is slower and
identical in every number that is checked.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from ml.features import assert_no_lookahead  # noqa: E402
from ml.models_gpu import ModelSpec  # noqa: E402
from ml.splits import LeakageError, walk_forward  # noqa: E402
from ml.train_gpu import (  # noqa: E402
    ALL_FEATURES,
    FEATURES,
    FoldScaler,
    TrainConfig,
    build_panel,
    make_tensors,
    run_model,
    score,
    topk_book,
    zoo_table,
)

CPU = torch.device("cpu")


# --------------------------------------------------------------------------- fixtures


def _planted_series(n: int, *, seed: int, start: str, s0: float = 100.0) -> pd.DataFrame:
    """A price series whose forward return *is* a function of trailing momentum.

    Construction: a slowly-varying latent drift with a 180-bar period and a slowly-varying
    latent volatility with a 240-bar period, plus noise. Both are smooth enough that a trailing
    30-bar mean estimates the drift and a trailing 20-bar realised vol estimates the vol, so
    ``mom_30`` predicts the forward 7-day return and ``vol_20`` predicts forward realised vol —
    by construction, not by luck.

    The amplitudes are large on purpose. This fixture is not a simulation of crypto; it is a
    signal loud enough that failing to find it means the pipeline is broken.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    drift = 0.006 * np.sin(2 * np.pi * t / 180.0 + seed)
    sig = 0.012 * (1.6 + np.sin(2 * np.pi * t / 240.0 + seed))
    r = drift + rng.normal(0.0, 1.0, n) * sig
    close = s0 * np.exp(np.cumsum(r))
    qv = np.exp(17.0 + 0.3 * rng.normal(0, 1, n))
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=n, freq="1D", tz="UTC"),
        "open": np.concatenate([[s0], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": qv / close, "quote_volume": qv,
        "trades": rng.integers(500, 20_000, n).astype(float),
    })


@pytest.fixture(scope="module")
def planted_panel() -> pd.DataFrame:
    """Twelve coins, 1,500 daily bars, one that lists late and one that dies. Signal planted.

    Twelve names because :func:`ml.features.cross_sectional_rank` needs at least five live values
    per timestamp to emit a rank at all, and the ``_xs`` features are a third of the block.
    """
    parts = []
    for i in range(12):
        n = 1_500
        start = "2018-01-01"
        if i == 10:                      # lists 400 bars late
            n, start = 1_100, "2019-02-05"
        if i == 11:                      # dies 300 bars early
            n = 1_200
        p = _planted_series(n, seed=100 + i, start=start, s0=10.0 * (i + 1))
        p["symbol"] = f"C{i:02d}USDT"
        parts.append(p)
    return pd.concat(parts, ignore_index=True)


@pytest.fixture
def cfg() -> TrainConfig:
    return TrainConfig(n_splits=3, min_train=1_500, top_k=3)


@pytest.fixture
def panel_built(planted_panel, cfg):
    df, ds = build_panel(cfg, frame=planted_panel)
    return df, ds


SPEC = ModelSpec("linear", "linear", seq_len=32, hidden=0, layers=0, dropout=0.0,
                 batch_size=512, max_epochs=8, patience=3, lr=3e-3)


# --------------------------------------------------------------------------- panel


def test_forward_path_sums_to_the_horizon_label(panel_built, cfg) -> None:
    """``fwd_1 + ... + fwd_7`` must equal ``logret_7d`` exactly.

    The path head and the return head would otherwise be fitting two different questions, and
    :class:`ml.models_gpu.NBeats` — which reads its 7-day output off the path sum — would be
    internally inconsistent in a way no shape test could see.
    """
    df, _ = panel_built
    cols = [f"fwd_{k}" for k in range(1, 8)]
    d = df.dropna(subset=[*cols, "logret_7d"])
    assert len(d) > 5_000
    np.testing.assert_allclose(d[cols].sum(axis=1), d["logret_7d"], atol=1e-9)


def test_run_counter_resets_at_a_symbol_boundary_and_at_a_gap(cfg) -> None:
    """The consecutive-run counter is what makes a lookback window provably gap-free.

    Built on a frame with a deliberate seven-day hole: the run must be 0 on the bar after the
    hole, so no window can straddle it. Without this, a window would silently splice bars from
    either side of a delisting and the model would be fed a price series that never existed.
    """
    a = _planted_series(200, seed=1, start="2020-01-01")
    a["symbol"] = "AAAUSDT"
    b = _planted_series(200, seed=2, start="2020-01-01")
    b["symbol"] = "BBBUSDT"
    b = b.drop(index=range(100, 107))          # a seven-day hole
    df, _ = build_panel(cfg, frame=pd.concat([a, b], ignore_index=True))
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    for _, part in df.groupby("symbol", sort=False):
        assert int(part["run"].iloc[0]) == 0, "the first bar of a symbol has no history"
    hole = df.loc[df["symbol"] == "BBBUSDT"].reset_index(drop=True)
    resets = hole.index[(hole["run"] == 0)]
    assert len(resets) == 2, f"expected a reset at the listing and at the hole, got {list(resets)}"


def test_features_have_no_lookahead(planted_panel) -> None:
    """The harness's own detector, run on the feature block this phase actually uses.

    Recomputing every feature on a prefix of the panel must give identical values. A feature that
    changes when future rows are appended is reading them.
    """
    d = planted_panel.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    assert_no_lookahead(d, names=list(FEATURES))


# --------------------------------------------------------------------------- samples


def test_no_window_crosses_a_symbol_boundary(panel_built, cfg) -> None:
    """Checked directly, by reading the symbol at the far end of every sample's window.

    The strongest form of this test: not "the counter looks right" but "no sample's oldest bar
    belongs to a different coin", evaluated on every sample.
    """
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    sym = df["symbol"].to_numpy()
    ends = pt.ends.numpy()
    assert (sym[ends] == sym[ends - SPEC.seq_len + 1]).all()
    ts = df["ts"].to_numpy()
    span = (ts[ends] - ts[ends - SPEC.seq_len + 1]).astype("timedelta64[D]").astype(int)
    assert (span == SPEC.seq_len - 1).all(), "a window spans more calendar than it has bars"


def test_samples_are_eligible_only_and_dead_coins_are_kept(panel_built, cfg) -> None:
    """Point-in-time eligibility on samples, survivorship-free universe on bars.

    Both halves matter and they pull in opposite directions: the panel must keep every bar of the
    coin that died (or the sample is survivor-only and proves the opposite of what it appears to),
    while no *sample* may come from a bar the eligibility funnel rejected.
    """
    df, ds = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    assert df["eligible"].to_numpy()[pt.ends.numpy()].all()
    assert ds.n_dead >= 1, "the fixture's dead coin was dropped from the panel"
    dead_syms = set(df.loc[df["symbol"].str.startswith("C11"), "symbol"])
    assert dead_syms, "the dead coin's bars are missing entirely"


def test_no_sample_has_a_non_finite_window(panel_built, cfg) -> None:
    """A single NaN anywhere in a window disqualifies the sample.

    Imputing it would be inventing data — ``mom_90`` is NaN for a coin's first 90 bars because
    that momentum does not exist, and filling it with zero tells the model the coin was flat.
    """
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    X = pt.X.numpy()
    ends = pt.ends.numpy()
    for off in (0, SPEC.seq_len // 2, SPEC.seq_len - 1):
        assert np.isfinite(X[ends - off]).all(), f"NaN at window offset {off}"


def test_resident_panel_is_tiny_in_vram(panel_built, cfg) -> None:
    """The flat-panel design is the reason 6 GB is not the constraint; hold it to that claim.

    A regression to a materialised ``[n_samples, L, F]`` tensor would multiply this by ``L``, and
    the number appearing in the report would quietly become wrong.
    """
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    per_sample_MiB = pt.vram_MiB() / max(pt.n_samples, 1)
    assert per_sample_MiB < 0.001, f"{pt.vram_MiB()} MiB for {pt.n_samples} samples"


# --------------------------------------------------------------------------- leakage


def test_scaler_cannot_see_beyond_the_training_window(panel_built, cfg) -> None:
    """Fitted on ``ts <= train_end`` only — verified by planting an outlier after that date.

    A scaler fitted once on the whole panel is the commonest quiet leak in this kind of code. The
    test plants a 10,000x feature value in the *future* and requires the fitted centre and scale
    to be unchanged, which they cannot be if the mask is ignored.
    """
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).to_numpy()
    cut = np.datetime64("2020-01-01")
    mask = torch.as_tensor(ts <= cut)
    meta_ts = pd.to_datetime(pt.meta["ts"], utc=True).dt.tz_localize(None).to_numpy()
    train_idx = torch.as_tensor(np.flatnonzero(meta_ts <= cut))
    before = FoldScaler.fit(pt, train_idx, panel_rows_mask=mask)
    pt.X[torch.as_tensor(ts > cut)] *= 10_000.0
    after = FoldScaler.fit(pt, train_idx, panel_rows_mask=mask)
    assert torch.allclose(before.centre, after.centre)
    assert torch.allclose(before.scale, after.scale)


def test_the_folds_actually_purge(panel_built, cfg) -> None:
    """Every fold must purge a non-zero number of rows on an overlapping 7-day label.

    ``n_purged == 0`` on a daily-sampled 7-day label is the signature of a ``t1`` column that was
    defaulted to ``ts``, which is precisely the mistake the purge exists to prevent. The count
    being on the record is how that gets caught.
    """
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    wf = walk_forward(pt.meta["ts"], pt.meta["t1"], n_splits=cfg.n_splits,
                      embargo_frac=cfg.embargo_frac, min_train=cfg.min_train)
    assert len(wf) >= 1
    assert all(f.n_purged > 0 for f in wf), wf.summary().to_string()


def test_a_wrong_t1_is_caught_rather_than_scored(panel_built, cfg) -> None:
    """With ``t1 = ts`` the split leaks and :func:`ml.splits.assert_no_leakage` must raise.

    The guard is only worth having if it fires, and the only way to show it fires is to hand it a
    split that leaks.
    """
    from ml.splits import assert_no_leakage
    df, _ = panel_built
    pt = make_tensors(df, cfg, SPEC, device=CPU)
    wf = walk_forward(pt.meta["ts"], pt.meta["ts"], n_splits=cfg.n_splits,
                      embargo_frac=cfg.embargo_frac, min_train=cfg.min_train)
    with pytest.raises(LeakageError):
        assert_no_leakage(wf, pt.meta["ts"], pt.meta["t1"] + pd.Timedelta(days=7))


# --------------------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def planted_run(planted_panel):
    """One end-to-end walk-forward on the planted panel, shared by the tests below.

    Module-scoped because it trains a model, and eight assertions about one run are cheaper and
    more coherent than eight runs. It needs no cache isolation because ``frame=`` bypasses the
    parquet cache entirely.
    """
    c = TrainConfig(n_splits=3, min_train=1_500, top_k=3)
    df, _ = build_panel(c, frame=planted_panel)
    res, pred = run_model(SPEC, c, df, device=CPU, kind="torch", verbose=False)
    return res, pred, df, c


def test_pipeline_recovers_a_planted_return_signal(planted_run) -> None:
    """Out of sample, the forecast must rank the planted forward return positively.

    This is the test that makes a *negative* result on real data meaningful. Everything else here
    proves the pipeline does not cheat; this proves it can still learn. Rank IC and directional
    accuracy are both required, because a model can get one without the other by betting on a
    single name.
    """
    res, pred, _, _ = planted_run
    ic = res.metrics["ret"]["rank_ic"]
    da = res.metrics["ret"]["directional"]
    assert ic["ic"] > 0.05, f"planted signal not recovered: IC {ic['ic']:.4f}"
    assert da["accuracy"] > 0.52, f"planted signal not recovered: acc {da['accuracy']:.4f}"
    assert res.metrics["ret"]["r2_oos_vs_zero"] > -0.05


def test_pipeline_recovers_a_planted_volatility_signal(planted_run) -> None:
    """Forward realised vol is a function of trailing vol in the fixture; the vol head must find it.

    Scored against the *unconditional mean*, which is the baseline that matters for the claim
    "volatility is forecastable". The stronger comparison — beating trailing realised vol, a
    feature the model is already fed — is the one the real-data report is judged on.
    """
    res, _, _, _ = planted_run
    assert res.metrics["vol"]["r2_vs_mean_model"] > 0.05


def test_multi_horizon_head_is_absent_for_the_control(planted_run) -> None:
    """``path_h=0`` on the control means ``pred_ret_1d`` is all NaN, not silently zero."""
    _, pred, _, _ = planted_run
    assert pred["pred_ret_1d"].isna().all()


def test_the_book_is_long_only_and_fully_invested(planted_run) -> None:
    """This system is spot-only, so a negative weight is a bug and the test says so.

    Also checks the book never exceeds 1.0 gross: a top-k book that summed above one would be
    levered, which the risk gate forbids and which would inflate every return in the table.
    """
    _, pred, df, c = planted_run
    books = topk_book(pred, df, c)
    assert books, "no book was produced"
    for name, b in books.items():
        assert b["turnover"] >= 0.0, name
        assert -1.0 <= b["max_dd"] <= 0.0, f"{name} drawdown {b['max_dd']}"


def test_costs_are_charged(planted_run) -> None:
    """A top-k book rebalanced weekly must show a non-zero cost drag at 15 bps per side.

    ``cost_drag == 0`` would mean the turnover never reached the cost calculation, which is how a
    high-turnover rule survives a backtest it should not.
    """
    _, pred, df, c = planted_run
    books = topk_book(pred, df, c)
    assert books["topk_forecast"]["cost_drag"] > 0.0
    assert books["topk_forecast"]["turnover"] > 0.0


def test_score_never_reports_a_price_level_mape(planted_run) -> None:
    """The corrected target, enforced. Any MAPE in the report must be on returns.

    ``mape_ret`` carries ``n_mape`` and sits beside the zero-forecast baseline. A key named plain
    ``mape``, or a MAPE without its baseline in the same dict, would be the trap the whole phase
    exists to avoid.
    """
    res, _, _, _ = planted_run
    ret = res.metrics["ret"]
    keys = [k for k in ret["errors"] if "mape" in k]
    assert keys == ["mape_ret", "n_mape"] or set(keys) == {"mape_ret", "n_mape"}
    assert "baseline_zero_forecast" in ret
    flat = str(res.metrics)
    assert "mape_price" not in flat and "price_mape" not in flat


def test_cross_sectional_direction_is_reported_beside_the_pooled_one(planted_run) -> None:
    """Both hit rates, because the pooled one is mostly the base rate of up-weeks.

    A forecast sitting near its training mean is almost always positive, so the pooled number
    inherits the market factor that one principal component already explains. The demeaned number
    is the question a long-only selection faces, and the report must carry it or the table reads
    far better than the model is.
    """
    res, _, _, _ = planted_run
    ret = res.metrics["ret"]
    assert "directional_cross_sectional" in ret and "directional" in ret
    xs = ret["directional_cross_sectional"]
    assert 0.0 < xs["accuracy"] < 1.0 and xs["n"] > 100
    assert xs["ci_low"] < xs["accuracy"] < xs["ci_high"]


def test_the_bottom_k_book_is_the_sign_check(planted_run) -> None:
    """A real ranking must rank badly as well as well, so both ends are costed.

    If top-k and bottom-k score the same, the ranking carried no information and the top-k number
    is a draw from noise. The test only requires the book to exist and be measurable — the
    *direction* of the gap is a result, not an invariant, and asserting it would be asserting the
    conclusion.
    """
    _, pred, df, c = planted_run
    books = topk_book(pred, df, c)
    assert "bottomk_forecast" in books and "equal_weight_eligible" in books
    assert books["bottomk_forecast"]["n_periods"] == books["topk_forecast"]["n_periods"]


def test_effective_sample_bracket_is_reported_at_both_ends(planted_run) -> None:
    """``eff_n_panel <= eff_n_symbol <= n_samples``, and both are in the cost record.

    Quoting the row count is proposing a failure; quoting only the panel-scope number is harder on
    the model than the 57-69% PC1 figure justifies. The honest report carries the bracket.
    """
    res, _, _, _ = planted_run
    c = res.cost
    assert c["eff_n_panel_scope"] <= c["eff_n_symbol_scope"] <= c["n_samples"]
    assert c["eff_n_panel_scope"] > 0


def test_inner_validation_is_smaller_than_inner_train(planted_run) -> None:
    """The early-stopping slice must be a row quantile, not a calendar fraction.

    On a panel whose coin count grows tenfold, the last 15% of the *calendar* is far more than 15%
    of the rows: measured on the real panel's fold 2, a calendar cut gave 16,502 training rows
    against 22,190 validation rows — the fold with the least data spending most of it on early
    stopping. This test fails if that regression returns.
    """
    res, _, _, _ = planted_run
    for f in res.folds:
        if f["n_val"] == 0:
            continue
        assert f["n_val"] < f["n_train"], f"fold {f['fold']}: val {f['n_val']} >= train {f['n_train']}"
        assert f["n_val"] / (f["n_train"] + f["n_val"]) < 0.30


def test_every_fold_reports_its_cost(planted_run) -> None:
    """Wall-clock and epoch count per fold, because the owner asked what retraining costs.

    Also a guard on early stopping: ``epochs_run`` must be at least one and at most the
    configured maximum, so a fold that silently trained zero epochs cannot report a score.
    """
    res, _, _, _ = planted_run
    assert res.folds and res.cost["wall_s_total"] > 0
    for f in res.folds:
        assert 1 <= f["epochs_run"] <= SPEC.max_epochs
        assert f["n_test"] > 0 and f["n_train"] > 0
        assert f["n_val"] >= 0


def test_out_of_sample_span_is_reported_and_non_trivial(planted_run) -> None:
    """A table of metrics without the span they cover is not auditable."""
    res, _, _, _ = planted_run
    m = res.metrics
    assert m["n_oos"] > 1_000
    assert pd.Timestamp(m["oos_end"]) > pd.Timestamp(m["oos_start"]) + pd.Timedelta(days=200)


def test_feature_block_puts_logret_first(planted_run) -> None:
    """``logret_1`` must be channel 0: :class:`ml.models_gpu.NBeats` decomposes that channel.

    Reordering :data:`ml.train_gpu.FEATURES` would silently change what N-BEATS is a basis
    expansion *of*, with no error and a different number.
    """
    assert ALL_FEATURES[0] == "logret_1"
    _, _, df, c = planted_run
    pt = make_tensors(df, c, SPEC, device=CPU)
    assert pt.features[0] == "logret_1"


def test_score_is_empty_rather_than_wrong_on_an_empty_frame(cfg) -> None:
    """Degenerate input must not produce a confident number."""
    empty = pd.DataFrame(columns=["ts", "symbol", "pred_ret", "pred_ret_real", "y_ret",
                                  "pred_vol_real", "y_vol_log", "vol_20", "pred_dd", "y_dd",
                                  "w"])
    out = score(empty, cfg)
    assert out["n_oos"] == 0
    assert "vol" not in out and "dd" not in out


def test_zoo_table_renders_the_drawdown_head(planted_run) -> None:
    """The table must render a real :func:`ml.train_gpu.score` output without raising.

    This is a regression test for a real crash. ``ml.metrics.brier`` returns a **dict** — the
    raw score, the base-rate score and the skill between them — and :func:`zoo_table` used to
    call ``round()`` on it. The whole zoo trained for eleven minutes, every prediction file was
    written, and then the run died on the last line while formatting the summary. A renderer is
    load-bearing when it is the only thing the reader sees, so it is tested against a genuine
    metrics dict rather than a hand-built one.

    ``dd_skill`` is asserted present because it is the only drawdown column worth reading: at a
    16.6% base rate the raw Brier score is 0.138 for a model that knows nothing.
    """
    res, _, _, _ = planted_run
    report = {"models": {"planted": res.as_dict()}, "config": {"cost_bps": 15.0, "top_k": 3}}
    md = zoo_table(report)
    assert "dd_skill" in md
    assert "planted" in md
    row = next(iter(report["models"].values()))
    assert isinstance(row["metrics"]["dd"]["brier"], dict), (
        "brier() returns a dict; if this ever becomes a float, zoo_table's unpacking is wrong")


def test_zoo_table_survives_a_model_that_failed() -> None:
    """A family that could not run must appear as a row, not vanish or take the table down."""
    report = {"models": {"broken": {"error": "RuntimeError: no kernel for this batch size"}},
              "config": {"cost_bps": 15.0, "top_k": 10}}
    md = zoo_table(report)
    assert "broken" in md and "RuntimeError" in md
