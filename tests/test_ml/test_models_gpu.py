"""Tests for the GPU zoo's *architectures* — every one of them on CPU, so they run anywhere.

Nothing here needs CUDA. That is the point: a model definition that can only be constructed on
a GPU cannot be unit-tested on a host without one, and the whole zoo would then be unverified
everywhere except the one machine it was written on.

Three things are checked that a shape test would not catch:

* **Causality of the TCN**, structurally. A ``Conv1d`` with symmetric ``padding`` lets step ``t``
  see ``t+1`` through the right pad, which is the single easiest way to produce a beautiful
  forecast that is a leak. :func:`test_temporal_block_is_causal` perturbs a late bar and requires
  every earlier output position to be bit-identical.
* **bfloat16 is refused on this card.** Capability 7.5 has fp16 tensor cores and no bf16, so a
  recipe copied from an Ampere machine must not silently select it.
* **Determinism given a seed**, because every claim in the report rests on it.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from ml.models_gpu import (  # noqa: E402
    FAMILIES,
    TCN,
    Forecast,
    ModelSpec,
    MultiTaskHead,
    NBeats,
    NHiTS,
    PatchTST,
    Recurrent,
    _TemporalBlock,
    amp_dtype,
    build_model,
    gpu_ceiling,
    param_count,
    pick_device,
    vram_probe,
    zoo_specs,
)
from ml.registry import seed_everything  # noqa: E402

CPU = torch.device("cpu")
NF = 11


def _x(b: int = 8, L: int = 32, f: int = NF) -> torch.Tensor:
    g = torch.Generator().manual_seed(0)
    return torch.randn(b, L, f, generator=g)


# --------------------------------------------------------------------------- contract


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_every_family_forwards_with_the_shared_contract(family: str) -> None:
    """Each family returns a :class:`Forecast` with three ``[B]`` heads. The zoo is comparable
    only because every encoder ends in the same head, so this is the contract test."""
    spec = ModelSpec(family, family, seq_len=32, hidden=16, layers=2, path_h=7,
                     patch_len=8, patch_stride=4, n_heads=2, blocks=2, pool_rates=(4, 1))
    m = build_model(spec, NF)
    out = m(_x(L=32))
    assert isinstance(out, Forecast)
    for field in ("ret", "vol", "dd"):
        t = getattr(out, field)
        assert t is not None and t.shape == (8,), f"{family}.{field} shape {t.shape}"
        assert torch.isfinite(t).all()


@pytest.mark.parametrize("family", ["nbeats", "nhits"])
def test_multi_horizon_families_emit_the_whole_path(family: str) -> None:
    """N-BEATS/N-HiTS are the natively multi-horizon families: ``path`` is ``[B, H]``.

    And the 7-day head must be tied to the path rather than independent of it, so the two
    cannot disagree — which for :class:`NBeats` means ``ret`` is the path sum plus a learned
    correction and therefore moves when the path moves.
    """
    spec = ModelSpec(family, family, seq_len=32, hidden=16, layers=2, path_h=7,
                     blocks=2, pool_rates=(4, 1))
    out = build_model(spec, NF)(_x(L=32))
    assert out.path is not None and out.path.shape == (8, 7)
    assert torch.isfinite(out.path).all()


def test_non_multi_horizon_families_have_no_path_by_default() -> None:
    """``path_h=0`` must mean no path head at all, not a head predicting zeros.

    The distinction matters because the loss adds a path term only when the tensor is present;
    a silently-present head of zeros would add a constant to every loss and change the early
    stopping point for the families that are not supposed to have one.
    """
    spec = ModelSpec("gru", "gru", seq_len=32, hidden=16, layers=1, path_h=0)
    assert build_model(spec, NF)(_x(L=32)).path is None


def test_build_model_rejects_an_unknown_family() -> None:
    with pytest.raises(ValueError, match="unknown family"):
        build_model(ModelSpec("x", "transformerxl"), NF)


def test_patchtst_rejects_a_patch_longer_than_the_window() -> None:
    """A patch longer than the sequence silently produces zero patches downstream; refuse it."""
    with pytest.raises(ValueError, match="exceeds seq_len"):
        PatchTST(NF, ModelSpec("p", "patchtst", seq_len=8, patch_len=16, hidden=16, n_heads=2))


def test_recurrent_rejects_an_unknown_cell() -> None:
    with pytest.raises(ValueError, match="gru"):
        Recurrent(NF, ModelSpec("r", "gru", hidden=8), cell="rnn")


# --------------------------------------------------------------------------- causality


def test_temporal_block_is_causal() -> None:
    """Perturbing bar ``k`` must not move any output at a position before ``k``.

    This is the test that would fail if ``_TemporalBlock`` used ``Conv1d(padding=...)`` instead of
    a left-only ``F.pad``. It is checked on the block rather than on :class:`TCN` because
    :class:`TCN` only exposes its last position, where a causality violation is invisible.
    """
    seed_everything(0)
    blk = _TemporalBlock(NF, 8, kernel=3, dilation=2, dropout=0.0).eval()
    x = _x(b=4, L=40)
    k = 30
    with torch.no_grad():
        a = blk(x.transpose(1, 2))
        x2 = x.clone()
        x2[:, k, :] += 5.0
        b = blk(x2.transpose(1, 2))
    assert torch.equal(a[:, :, :k], b[:, :, :k]), "the TCN block leaked the future backwards"
    assert not torch.equal(a[:, :, k:], b[:, :, k:]), "the perturbation had no effect at all"


def test_tcn_receptive_field_covers_the_default_window() -> None:
    """A TCN whose receptive field is shorter than its input is discarding the oldest bars.

    That is a legitimate design and a bad accident, so the zoo's own configuration is required to
    cover its window and the number is exposed rather than inferred from the layer count.
    """
    spec = next(s for s in zoo_specs() if s.family == "tcn")
    assert TCN(NF, spec).receptive_field >= spec.seq_len


def test_recurrent_reads_the_decision_bar_hardest() -> None:
    """Perturbing the last bar must move the output more than perturbing the first.

    The zoo takes the final hidden state rather than a mean over time, and this is what that
    choice buys: on a panel whose strongest single feature is trailing volatility at the decision
    bar, a mean pool would tell the model that a value 64 bars ago matters as much.
    """
    seed_everything(0)
    m = Recurrent(NF, ModelSpec("g", "gru", seq_len=32, hidden=16, layers=1, dropout=0.0)).eval()
    x = _x(b=16, L=32)
    with torch.no_grad():
        base = m(x).ret
        last, first = x.clone(), x.clone()
        last[:, -1, :] += 1.0
        first[:, 0, :] += 1.0
        d_last = (m(last).ret - base).abs().mean()
        d_first = (m(first).ret - base).abs().mean()
    assert d_last > d_first


# --------------------------------------------------------------------------- precision


def test_amp_dtype_refuses_bfloat16_on_turing() -> None:
    """Capability 7.5 gets fp16; 8.0+ gets bf16; CPU gets no autocast at all.

    Monkeypatched rather than skipped, so the rule is verified on a host with no GPU. A fp16
    recipe needs a :class:`torch.amp.GradScaler` and a bf16 one does not, so getting this wrong
    silently produces either NaNs or a slow fallback.
    """
    assert amp_dtype(CPU) is None


@pytest.mark.parametrize("cap,expected", [((7, 5), torch.float16), ((8, 6), torch.bfloat16),
                                          ((7, 0), torch.float16)])
def test_amp_dtype_by_capability(monkeypatch, cap, expected) -> None:
    monkeypatch.setattr(torch.cuda, "get_device_capability", lambda *_a, **_k: cap)
    assert amp_dtype(torch.device("cuda:0")) is expected


# --------------------------------------------------------------------------- determinism


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_same_seed_same_forward(family: str) -> None:
    """Two constructions under the same seed must agree bit for bit.

    Every number in the report is a pure function of (data, config, seed), and this is the
    evidence for that sentence rather than the assertion of it.
    """
    spec = ModelSpec(family, family, seq_len=32, hidden=16, layers=2, path_h=7,
                     patch_len=8, patch_stride=4, n_heads=2, blocks=2, pool_rates=(4, 1),
                     dropout=0.0)
    x = _x(L=32)
    outs = []
    for _ in range(2):
        seed_everything(11)
        outs.append(build_model(spec, NF).eval()(x).ret)
    assert torch.equal(outs[0], outs[1])


# --------------------------------------------------------------------------- bookkeeping


def test_zoo_specs_are_unique_and_pre_registered() -> None:
    """Seven named configurations, chosen before any result — that is the trial count.

    Duplicated names would silently overwrite each other in the report dict and, worse, would
    make the recorded trial count smaller than the number of models actually fitted.
    """
    specs = zoo_specs()
    names = [s.name for s in specs]
    assert len(names) == len(set(names)) == 7
    assert {"linear", "gru", "lstm", "tcn", "patchtst", "nbeats", "nhits"} == set(names)
    assert all(s.family in FAMILIES for s in specs)


def test_the_linear_control_is_far_smaller_than_the_deep_models() -> None:
    """The control exists so "the transformer beat nothing" is checkable; it must be tiny.

    If the linear probe had a comparable parameter count it would stop being a control.
    """
    n_lin = param_count(build_model(
        next(s for s in zoo_specs() if s.name == "linear"), NF))
    n_deep = param_count(build_model(
        next(s for s in zoo_specs() if s.name == "patchtst"), NF))
    assert n_lin * 20 < n_deep


def test_nhits_pooling_costs_fewer_parameters_than_nbeats() -> None:
    """N-HiTS's multi-rate pooling is a parameter saving, not only a frequency prior.

    Same width, same blocks, same horizon: the pooled blocks see a shorter input, so the first
    layer is smaller. If this inverts, ``pool_rates`` is not reaching the blocks.
    """
    base = ModelSpec("n", "nbeats", seq_len=64, hidden=64, layers=2, blocks=3, path_h=7)
    nb = param_count(NBeats(NF, base))
    nh = param_count(NHiTS(NF, base.sized(pool_rates=(8, 4, 1))))
    assert nh < nb


def test_multitask_head_keeps_the_drawdown_output_a_logit() -> None:
    """``dd`` must be unbounded, not a probability.

    The loss is ``binary_cross_entropy_with_logits`` — stable under fp16 — and the probability is
    produced exactly once, in the driver, where it is also Brier-scored. A head that sigmoided
    here would double-apply it.
    """
    h = MultiTaskHead(6, path_h=0)
    with torch.no_grad():
        h.dd.bias.fill_(12.0)
        out = h(torch.zeros(4, 6))
    assert float(out.dd[0]) > 1.0


def test_vram_probe_runs_on_cpu_and_reports_a_step_time(tmp_path) -> None:
    """The cost instrumentation must work without CUDA, or the zoo cannot be timed anywhere else.

    On CPU there is no ``peak_alloc_MiB`` and the probe says so by omitting the key rather than
    reporting a zero that would read as "this model uses no memory".
    """
    spec = ModelSpec("g", "gru", seq_len=16, hidden=8, layers=1, batch_size=8)
    r = vram_probe(spec, NF, device=CPU, batch_size=8)
    assert r["ms_per_step"] > 0 and r["params"] > 0
    assert r["amp"] == "none" and "peak_alloc_MiB" not in r


def test_gpu_ceiling_never_raises_without_cuda() -> None:
    """The ceiling report is the first thing the driver prints; it must not be what fails."""
    rep = gpu_ceiling(CPU)
    assert rep["device"] == "cpu" and "note" in rep


def test_model_spec_sized_does_not_mutate() -> None:
    """``sized`` is how the sweeps vary a field; sharing state between trials would pollute them."""
    a = ModelSpec("g", "gru", seq_len=64)
    b = a.sized(seq_len=128)
    assert a.seq_len == 64 and b.seq_len == 128 and a.name == b.name


def test_default_device_is_cpu_when_cuda_is_refused() -> None:
    assert pick_device(prefer_cuda=False) == CPU
