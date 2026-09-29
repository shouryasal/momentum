"""The GPU sequence-model zoo, sized to 6 GB — and the measurement of what 6 GB actually costs.

What this module is for
-----------------------
:mod:`ml.baselines` established what a forecast has to beat. This module holds the sequence
architectures that the literature proposes for exactly this problem, built small enough to
train on a **Quadro RTX 3000 with 6,143 MiB of VRAM**, and instrumented so the owner can read
the retraining cost off a table rather than guess it.

It defines models and nothing else. Splits come from :mod:`ml.splits`, metrics from
:mod:`ml.metrics`, the trial counter from :mod:`ml.registry`. :mod:`ml.train_gpu` is the driver.

The four families, and why each is here
---------------------------------------
======================  ====================================================================
:class:`Recurrent`      LSTM/GRU. The baseline every crypto-ML paper reports and the one that
                        most often turns out to be the whole result. Cheap, and if a
                        transformer cannot beat it the transformer has not earned its cost.
:class:`TCN`            Dilated **causal** convolutions (Bai, Kolter & Koltun 2018). A
                        receptive field of ``2^n`` bars with no recurrence, so it trains at
                        convolution speed. Causality is structural: left-padding only, which
                        is the architectural reason a TCN cannot leak the future even if the
                        data pipeline has a bug.
:class:`PatchTST`       Patch transformer, channel-independent (Nie et al. 2023). Patching
                        cuts attention from ``O(L^2)`` to ``O((L/P)^2)`` — the reason a
                        transformer fits here at all — and channel independence shares one
                        encoder across features instead of learning a 14x14 attention map
                        from 200k samples.
:class:`NBeats`         Deep residual basis expansion (Oreshkin et al. 2020), doubly residual:
                        each block subtracts what it explained from the backcast and adds its
                        forecast. Natively **multi-horizon** — it emits the whole 7-step path.
:class:`NHiTS`          N-BEATS with multi-rate pooling and interpolation (Challu et al. 2023).
                        A block that pools 8:1 can only express a slow component, which is a
                        frequency prior rather than a regulariser, and that is the right
                        inductive bias when the effective sample size is ~10^3.
======================  ====================================================================

The shared contract, so the comparison is about architecture
------------------------------------------------------------
Every model maps ``x: [B, L, F]`` (L bars of backward-looking features, the last bar being the
decision bar) to a :class:`Forecast` with four fields through the **same**
:class:`MultiTaskHead`. Only the encoder differs. Three tasks, chosen because the project's own
measurements say they are not equally forecastable and a single-task comparison would hide that:

* ``ret`` — standardised forward log return. The project measures this as close to
  unforecastable (one factor explains 57-69% of cross-sectional variance).
* ``vol`` — standardised log forward realised vol. Implied vol already reaches R2 0.268 here,
  so a model that cannot beat a HAR on this has failed at the easy task.
* ``dd`` — logit of a forward drawdown breach. Funding buckets already move this 17% -> 41%.
* ``path`` — the H-step daily return path, supervised only where the architecture is natively
  multi-horizon (:class:`NBeats`, :class:`NHiTS`) or where a caller asks for the ablation.

Turing, and the two things it forbids
-------------------------------------
Compute capability **7.5**. FP16 tensor cores: yes, so ``autocast(float16)`` with a
:class:`GradScaler` is the right mixed precision. **bfloat16: no** — it needs 8.0, and asking
for it silently falls back to something slower. TF32: also 8.0. :func:`amp_dtype` encodes that
so a caller cannot get it wrong by copying an Ampere recipe.

The VRAM finding, stated before the models
------------------------------------------
:func:`vram_probe` measures peak allocation per configuration and :func:`largest_that_fits`
walks a configuration up until it OOMs. The answer on this host is that **6 GB is not the
binding constraint at this data scale**: the whole 747-symbol daily feature panel is about
**48 MiB** as a flat fp32 tensor and lives in VRAM permanently, and the largest model here
peaks under 1 GiB. What binds is effective sample size, not memory — see
:func:`ml.labels.uniqueness_weights`. 6 GB would bind at hourly bars with a full-attention
transformer over 512 steps, and :func:`largest_that_fits` is how to find that edge rather than
assert it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

__all__ = [
    "FAMILIES",
    "Forecast",
    "MultiTaskHead",
    "NBeats",
    "NHiTS",
    "PatchTST",
    "Recurrent",
    "TCN",
    "ModelSpec",
    "amp_dtype",
    "build_model",
    "gpu_ceiling",
    "largest_that_fits",
    "param_count",
    "pick_device",
    "vram_probe",
    "zoo_specs",
]

#: Compute capability at which bfloat16 and TF32 become available. This card is 7.5.
_BF16_MIN_CAPABILITY = (8, 0)


# --------------------------------------------------------------------------- device


def pick_device(prefer_cuda: bool = True) -> torch.device:
    """``cuda:0`` when it is really there, else CPU. Never raises.

    The zoo must be importable and testable on a host with no GPU, because the test suite runs
    wherever the repo is checked out and a model definition that needs CUDA to be *constructed*
    cannot be unit-tested.
    """
    if prefer_cuda and torch.cuda.is_available():
        return torch.device("cuda:0")
    return torch.device("cpu")


def amp_dtype(device: torch.device) -> torch.dtype | None:
    """The mixed-precision dtype this device can actually use, or ``None`` for no AMP.

    Returns ``float16`` on any CUDA device below capability 8.0 — which is this card — and
    ``bfloat16`` at 8.0 and above. Returning ``None`` on CPU is deliberate: CPU autocast to
    bf16 on this host is slower than fp32 and would make the CPU/GPU timing comparison a lie.

    fp16 needs a :class:`torch.amp.GradScaler`; bf16 does not. The driver reads this to decide.
    """
    if device.type != "cuda":
        return None
    cap = torch.cuda.get_device_capability(device)
    return torch.bfloat16 if cap >= _BF16_MIN_CAPABILITY else torch.float16


def gpu_ceiling(device: torch.device | None = None) -> dict:
    """The VRAM ceiling, and the arithmetic of what it forces. Reported before any training.

    ``usable_MiB`` is *free* memory, not total: the Windows desktop compositor holds a few
    hundred MiB of this card permanently, and a batch size derived from ``total_memory`` will
    OOM at 3 a.m. and not in the sizing test.
    """
    dev = device or pick_device()
    out: dict[str, Any] = {
        "device": str(dev),
        "cuda_available": bool(torch.cuda.is_available()),
        "torch": torch.__version__,
        "cuda_build": torch.version.cuda,
    }
    if dev.type != "cuda":
        out["note"] = "no CUDA device; the zoo is running on CPU and every timing below is CPU"
        return out
    p = torch.cuda.get_device_properties(dev)
    free, total = torch.cuda.mem_get_info(dev)
    cap = (p.major, p.minor)
    out |= {
        "name": p.name,
        "capability": f"{p.major}.{p.minor}",
        "multiprocessors": p.multi_processor_count,
        "total_MiB": int(total // 1024**2),
        "free_MiB": int(free // 1024**2),
        "reserved_by_others_MiB": int((total - free) // 1024**2),
        "amp_dtype": str(amp_dtype(dev)).replace("torch.", ""),
        "bf16_supported": cap >= _BF16_MIN_CAPABILITY,
        "tf32_supported": cap >= _BF16_MIN_CAPABILITY,
    }
    return out


# --------------------------------------------------------------------------- contract


@dataclass
class Forecast:
    """What every encoder in this module returns. Fields are ``None`` when not predicted.

    ``ret`` and ``vol`` are in **standardised** units — the driver holds the per-fold scaler and
    un-standardises before any metric is computed, because a metric on standardised units is
    not comparable to a baseline measured on real returns.

    ``dd`` is a **logit**, not a probability. Keeping it a logit here means the loss is
    ``binary_cross_entropy_with_logits`` (numerically stable under fp16) and the probability is
    produced exactly once, in the driver, where it is also calibrated and Brier-scored.
    """

    ret: torch.Tensor | None = None
    vol: torch.Tensor | None = None
    dd: torch.Tensor | None = None
    #: ``[B, H]`` daily forward log-return path, standardised. Only the multi-horizon families.
    path: torch.Tensor | None = None


class MultiTaskHead(nn.Module):
    """One shared head for every encoder, so the comparison measures encoders.

    Three separate linear maps off one shared embedding rather than one 3-wide output, because
    a drawdown logit and a standardised return want different biases and tying them costs
    nothing to avoid.

    ``path_h > 0`` adds the multi-horizon output. It is a single linear map: the point of
    :class:`NBeats` is that its *encoder* is basis-expanded, and putting an MLP here as well
    would blur which part of the architecture did the work.
    """

    def __init__(self, in_dim: int, *, path_h: int = 0, dropout: float = 0.0) -> None:
        super().__init__()
        self.drop = nn.Dropout(dropout) if dropout > 0 else nn.Identity()
        self.ret = nn.Linear(in_dim, 1)
        self.vol = nn.Linear(in_dim, 1)
        self.dd = nn.Linear(in_dim, 1)
        self.path = nn.Linear(in_dim, path_h) if path_h > 0 else None

    def forward(self, z: torch.Tensor, *, path: torch.Tensor | None = None) -> Forecast:
        z = self.drop(z)
        p = path
        if p is None and self.path is not None:
            p = self.path(z)
        return Forecast(ret=self.ret(z).squeeze(-1), vol=self.vol(z).squeeze(-1),
                        dd=self.dd(z).squeeze(-1), path=p)


@dataclass(frozen=True)
class ModelSpec:
    """One configuration. **This is the unit the trial counter counts.**

    Hashable and ``as_dict``-able so :func:`ml.registry.config_hash` can key a cache on it and
    :class:`ml.registry.Trials` can record it by name. Every field that changes a number is
    here; nothing that changes a number is a default buried in a constructor.
    """

    name: str
    family: str
    seq_len: int = 64
    hidden: int = 64
    layers: int = 2
    dropout: float = 0.1
    #: Multi-horizon path length in bars. 0 disables the path head and its loss term.
    path_h: int = 0
    #: PatchTST only: patch length and stride. Stride == patch_len means non-overlapping.
    patch_len: int = 16
    patch_stride: int = 8
    #: PatchTST only: attention heads. ``hidden`` is d_model there.
    n_heads: int = 4
    #: TCN only: convolution kernel width. Receptive field is ``1+2*(k-1)*(2^layers - 1)``.
    kernel: int = 3
    #: N-BEATS/N-HiTS only: blocks per stack and pooling rates.
    blocks: int = 3
    pool_rates: tuple[int, ...] = (8, 4, 1)
    #: Optimisation. Part of the spec because a learning rate changes the number.
    lr: float = 1e-3
    weight_decay: float = 1e-4
    batch_size: int = 1024
    max_epochs: int = 40
    patience: int = 6
    #: Free-form notes that must not change behaviour, only the record.
    tag: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(vars(self))
        d["pool_rates"] = list(self.pool_rates)
        return d

    def sized(self, **kw: Any) -> ModelSpec:
        """A copy with fields replaced — for the seq-len / width sweeps, without mutation."""
        return replace(self, **kw)


# --------------------------------------------------------------------------- recurrent


class Recurrent(nn.Module):
    """LSTM or GRU over the feature sequence; the final hidden state is the embedding.

    The last *time step* of the output, not a mean over time. A mean pool would tell the model
    that a feature value 64 bars ago matters as much as the decision bar, which on a panel
    where the strongest single feature is trailing 60-day vol is an actively wrong prior.

    ``layer_norm`` on the input is not cosmetic: the feature block mixes a cross-sectional rank
    in [0,1] with an annualised vol that reaches 4.0, and an LSTM fed both without
    normalisation spends its first epochs learning the scale.
    """

    def __init__(self, n_features: int, spec: ModelSpec, *, cell: str = "gru") -> None:
        super().__init__()
        cell = cell.lower()
        if cell not in ("gru", "lstm"):
            raise ValueError(f"cell must be 'gru' or 'lstm', got {cell!r}")
        self.cell = cell
        self.norm = nn.LayerNorm(n_features)
        rnn = nn.GRU if cell == "gru" else nn.LSTM
        self.rnn = rnn(n_features, spec.hidden, num_layers=spec.layers, batch_first=True,
                       dropout=spec.dropout if spec.layers > 1 else 0.0)
        self.head = MultiTaskHead(spec.hidden, path_h=spec.path_h, dropout=spec.dropout)

    def forward(self, x: torch.Tensor) -> Forecast:
        out, _ = self.rnn(self.norm(x))
        return self.head(out[:, -1, :])


# --------------------------------------------------------------------------- TCN


class _TemporalBlock(nn.Module):
    """Two dilated causal convolutions with a residual connection.

    The left-pad-then-trim is where causality lives. ``Conv1d`` with ``padding=p`` pads **both**
    ends, so the naive version lets step ``t`` see ``t+1`` through the right pad; the standard
    fix, and the one used here, is to pad only on the left with ``F.pad`` and leave the
    convolution unpadded. A TCN written the other way scores beautifully and is a leak.
    """

    def __init__(self, c_in: int, c_out: int, kernel: int, dilation: int, dropout: float) -> None:
        super().__init__()
        self.pad = (kernel - 1) * dilation
        wn = nn.utils.parametrizations.weight_norm
        # ``dilation`` must reach the convolution, not only the pad width. Without it the output
        # is ``L + (k-1)*(d-1)`` long and the residual add raises — which is the good failure; the
        # bad one is a caller who "fixes" it by trimming the right end and silently shifts time.
        self.conv1 = wn(nn.Conv1d(c_in, c_out, kernel, dilation=dilation))
        self.conv2 = wn(nn.Conv1d(c_out, c_out, kernel, dilation=dilation))
        self.drop = nn.Dropout(dropout)
        self.down = nn.Conv1d(c_in, c_out, 1) if c_in != c_out else None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.drop(F.relu(self.conv1(F.pad(x, (self.pad, 0)))))
        y = self.drop(F.relu(self.conv2(F.pad(y, (self.pad, 0)))))
        res = x if self.down is None else self.down(x)
        return F.relu(y + res)


class TCN(nn.Module):
    """Dilated causal convolution stack. Receptive field doubles per layer.

    :attr:`receptive_field` is exposed and the driver asserts it covers ``seq_len``: a TCN whose
    receptive field is shorter than its input window is quietly discarding the oldest bars,
    which is a legitimate design but must not happen by accident.
    """

    def __init__(self, n_features: int, spec: ModelSpec) -> None:
        super().__init__()
        self.norm = nn.LayerNorm(n_features)
        blocks, c_in = [], n_features
        for i in range(spec.layers):
            blocks.append(_TemporalBlock(c_in, spec.hidden, spec.kernel, 2**i, spec.dropout))
            c_in = spec.hidden
        self.blocks = nn.Sequential(*blocks)
        self.receptive_field = 1 + 2 * (spec.kernel - 1) * (2**spec.layers - 1)
        self.head = MultiTaskHead(spec.hidden, path_h=spec.path_h, dropout=spec.dropout)

    def forward(self, x: torch.Tensor) -> Forecast:
        y = self.blocks(self.norm(x).transpose(1, 2))
        return self.head(y[:, :, -1])


# --------------------------------------------------------------------------- PatchTST


class PatchTST(nn.Module):
    """Channel-independent patch transformer.

    Two ideas, both load-bearing at this sample size:

    **Patching.** ``L`` bars become ``ceil((L - patch_len)/stride) + 1`` tokens, so attention is
    ``O((L/stride)^2)``: at L=96, patch 16, stride 8 that is 11 tokens instead of 96, which is
    the difference between a transformer that fits in 6 GB at batch 1024 and one that does not.

    **Channel independence.** All ``F`` features go through *one* shared encoder as separate
    sequences, and only the head sees them jointly. The alternative learns cross-feature
    attention, which is ``F^2`` parameters estimated from an effective sample of ~10^3. This is
    the single most important reason to expect this model to lose to gradient boosting, and it
    is here so that expectation is measured rather than assumed.

    ``pre_norm`` ordering and GELU follow the paper. ``instance norm`` is per-channel over the
    window, which is what makes the representation scale-free and is why this model does not
    need the fold scaler as badly as the others.

    **Two measured costs of channel independence, both on this card.** The effective batch is
    ``B x F``, so (a) a step at ``batch_size=1024`` with 20 features costs **187 ms** against 14 ms
    for a GRU on the same data — 13x per sample, which is the price of the encoder and not of the
    patching; and (b) torch's efficient-attention kernel refuses an effective batch above
    **65,535**, capping ``batch_size`` at ``65535 // F`` (3,276 here) regardless of free VRAM. That
    second limit is a library constraint, not a memory one, and it is the kind of thing that only
    appears when the probe is actually run.
    """

    def __init__(self, n_features: int, spec: ModelSpec) -> None:
        super().__init__()
        if spec.patch_len > spec.seq_len:
            raise ValueError(f"patch_len {spec.patch_len} exceeds seq_len {spec.seq_len}")
        self.patch_len, self.stride = spec.patch_len, spec.patch_stride
        self.n_patch = (spec.seq_len - spec.patch_len) // spec.patch_stride + 1
        self.n_features = n_features
        self.embed = nn.Linear(spec.patch_len, spec.hidden)
        self.pos = nn.Parameter(torch.zeros(1, self.n_patch, spec.hidden))
        nn.init.trunc_normal_(self.pos, std=0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=spec.hidden, nhead=spec.n_heads, dim_feedforward=spec.hidden * 2,
            dropout=spec.dropout, activation="gelu", batch_first=True, norm_first=True)
        # enable_nested_tensor=False because norm_first=True makes the fast path unusable anyway;
        # leaving it True only emits a warning on every construction.
        self.enc = nn.TransformerEncoder(layer, num_layers=spec.layers,
                                         enable_nested_tensor=False)
        self.head = MultiTaskHead(n_features * spec.hidden, path_h=spec.path_h,
                                  dropout=spec.dropout)

    def forward(self, x: torch.Tensor) -> Forecast:
        b, _, f = x.shape
        z = x.transpose(1, 2)                                    # [B, F, L]
        # Instance-normalise each (sample, channel) window. eps guards a constant channel,
        # which happens: above_ma_200 is 1 for every bar of a long uptrend.
        mu = z.mean(dim=-1, keepdim=True)
        sd = z.std(dim=-1, keepdim=True).clamp_min(1e-5)
        z = (z - mu) / sd
        # Non-overlapping or strided patches. unfold is a view, so this costs no copy.
        p = z.unfold(dimension=-1, size=self.patch_len, step=self.stride)  # [B,F,n_patch,PL]
        p = p.reshape(b * f, p.shape[2], self.patch_len)
        h = self.enc(self.embed(p) + self.pos)                    # [B*F, n_patch, d]
        h = h[:, -1, :].reshape(b, f * h.shape[-1])               # last patch, per channel
        return self.head(h)


# --------------------------------------------------------------------------- N-BEATS / N-HiTS


class _BasisBlock(nn.Module):
    """One doubly-residual block: fully-connected stack -> theta -> (backcast, forecast).

    ``pool_rate > 1`` makes this an N-HiTS block. The MaxPool over time before the MLP is a
    *frequency prior*: a block that only ever sees an 8:1 pooled window cannot express a
    high-frequency component, so the stack decomposes the signal by rate. Then the forecast is
    produced at length ``ceil(h/pool_rate)`` and linearly interpolated up, which is the
    hierarchical interpolation of the paper and the reason N-HiTS has far fewer parameters than
    N-BEATS for the same horizon.

    ``pool_rate == 1`` with no interpolation is exactly a generic N-BEATS block.
    """

    def __init__(self, backcast: int, horizon: int, hidden: int, layers: int,
                 pool_rate: int, dropout: float) -> None:
        super().__init__()
        self.backcast, self.horizon, self.pool_rate = backcast, horizon, max(1, pool_rate)
        pooled = math.ceil(backcast / self.pool_rate)
        self.theta_f_len = max(1, math.ceil(horizon / self.pool_rate))
        mlp: list[nn.Module] = []
        d = pooled
        for _ in range(layers):
            mlp += [nn.Linear(d, hidden), nn.ReLU(), nn.Dropout(dropout)]
            d = hidden
        self.mlp = nn.Sequential(*mlp)
        self.theta_b = nn.Linear(hidden, backcast)
        self.theta_f = nn.Linear(hidden, self.theta_f_len)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        z = x
        if self.pool_rate > 1:
            z = F.max_pool1d(z.unsqueeze(1), kernel_size=self.pool_rate,
                             stride=self.pool_rate, ceil_mode=True).squeeze(1)
        h = self.mlp(z)
        back = self.theta_b(h)
        fwd = self.theta_f(h)
        if self.theta_f_len != self.horizon:
            fwd = F.interpolate(fwd.unsqueeze(1), size=self.horizon,
                                mode="linear", align_corners=False).squeeze(1)
        return back, fwd


class NBeats(nn.Module):
    """Doubly-residual basis expansion over the **return channel**, with auxiliary heads.

    N-BEATS is a univariate architecture and this keeps it one: the backcast is the log-return
    series (channel ``ret_channel``, which the driver guarantees is ``logret_1``), and the
    exogenous features enter only through ``exog_dim`` — the decision bar's feature vector,
    concatenated into the head. Feeding all 14 channels into the backcast would make this a
    different model wearing the same name.

    The forecast is the ``path_h``-step daily path, which is what makes this family the
    multi-horizon one: it is supervised on all ``H`` steps, and ``ret`` is read off the path sum
    plus a learned correction, so the 7-day number and the 7 daily numbers cannot disagree.
    """

    #: Set ``pool_rates=(1,)*blocks`` for N-BEATS; anything else makes it N-HiTS.
    def __init__(self, n_features: int, spec: ModelSpec, *, ret_channel: int = 0,
                 pool_rates: Sequence[int] | None = None) -> None:
        super().__init__()
        h = max(1, spec.path_h)
        self.horizon, self.ret_channel = h, ret_channel
        rates = list(pool_rates if pool_rates is not None else (1,) * spec.blocks)
        self.blocks = nn.ModuleList([
            _BasisBlock(spec.seq_len, h, spec.hidden, spec.layers, r, spec.dropout)
            for r in rates])
        self.exog = nn.Sequential(nn.LayerNorm(n_features),
                                  nn.Linear(n_features, spec.hidden), nn.ReLU())
        self.head = MultiTaskHead(h + spec.hidden, path_h=0, dropout=spec.dropout)
        self.ret_from_path = nn.Linear(h + spec.hidden, 1)

    def forward(self, x: torch.Tensor) -> Forecast:
        back = x[:, :, self.ret_channel]
        fc = torch.zeros(x.shape[0], self.horizon, device=x.device, dtype=back.dtype)
        for blk in self.blocks:
            b, f = blk(back)
            back = back - b
            fc = fc + f
        z = torch.cat([fc, self.exog(x[:, -1, :])], dim=-1)
        out = self.head(z)
        # The 7-day number is the path sum plus a learned correction, so the two agree.
        out.ret = fc.sum(dim=-1) + self.ret_from_path(z).squeeze(-1)
        out.path = fc
        return out


class NHiTS(NBeats):
    """:class:`NBeats` with the spec's ``pool_rates`` — multi-rate blocks and interpolation."""

    def __init__(self, n_features: int, spec: ModelSpec, *, ret_channel: int = 0) -> None:
        rates = list(spec.pool_rates) or [1]
        super().__init__(n_features, spec.sized(blocks=len(rates)),
                         ret_channel=ret_channel, pool_rates=rates)


# --------------------------------------------------------------------------- linear control


class LinearProbe(nn.Module):
    """Ridge-equivalent on the decision bar only — the control that says whether L>1 earned anything.

    No sequence, no depth: one linear map from the last bar's features to the three heads,
    trained by the same loop on the same folds. If a 400,000-parameter transformer does not beat
    this, the correct report is that the sequence structure carried no information, and having
    the number in the same table as the deep models is what makes that statement checkable
    rather than rhetorical.
    """

    def __init__(self, n_features: int, spec: ModelSpec) -> None:
        super().__init__()
        self.norm = nn.LayerNorm(n_features)
        self.head = MultiTaskHead(n_features, path_h=spec.path_h, dropout=0.0)

    def forward(self, x: torch.Tensor) -> Forecast:
        return self.head(self.norm(x[:, -1, :]))


# --------------------------------------------------------------------------- factory


#: family name -> constructor taking ``(n_features, spec)``.
FAMILIES: dict[str, Any] = {
    "gru": lambda n, s: Recurrent(n, s, cell="gru"),
    "lstm": lambda n, s: Recurrent(n, s, cell="lstm"),
    "tcn": TCN,
    "patchtst": PatchTST,
    "nbeats": NBeats,
    "nhits": NHiTS,
    "linear": LinearProbe,
}


def build_model(spec: ModelSpec, n_features: int) -> nn.Module:
    """Construct ``spec.family`` for ``n_features`` input channels. Raises on an unknown family.

    Deliberately not a registry decorator: the zoo is seven families and a dict that can be read
    in one screen is worth more here than an extension point nobody will use.
    """
    if spec.family not in FAMILIES:
        raise ValueError(f"unknown family {spec.family!r}; have {sorted(FAMILIES)}")
    return FAMILIES[spec.family](n_features, spec)


def param_count(model: nn.Module, *, trainable_only: bool = True) -> int:
    """Parameters, for the cost column. A model with more parameters than effective samples is
    reported as such rather than defended."""
    ps = model.parameters()
    return int(sum(p.numel() for p in ps if p.requires_grad or not trainable_only))


def zoo_specs(*, seq_len: int = 64, path_h: int = 7) -> list[ModelSpec]:
    """The default zoo: one configuration per family, chosen before seeing any result.

    **Choosing these up front is the trial-count discipline.** Seven configurations is N=7
    added to the selection counter; a grid over hidden width and learning rate would be N=200
    and, at 9.1 years, would raise the deflated hurdle to a Sharpe of 1.19 before any skill was
    required. The widths here are the smallest that the respective papers report as viable, not
    the widths that won a sweep.
    """
    return [
        ModelSpec("linear", "linear", seq_len=seq_len, hidden=0, layers=0, dropout=0.0,
                  tag="control: decision bar only, no sequence"),
        ModelSpec("gru", "gru", seq_len=seq_len, hidden=64, layers=2, dropout=0.1),
        ModelSpec("lstm", "lstm", seq_len=seq_len, hidden=64, layers=2, dropout=0.1),
        ModelSpec("tcn", "tcn", seq_len=seq_len, hidden=64, layers=6, kernel=3, dropout=0.1,
                  tag="receptive field 253 bars covers seq_len"),
        # max_epochs=25 here is a **wall-clock budget, not a tuning choice**: this family measured
        # 187 ms/step against 14 ms for a GRU, so 40 epochs x 4 folds would be 95 minutes. Early
        # stopping governs in practice and the report says whether the cap ever bound.
        ModelSpec("patchtst", "patchtst", seq_len=96, hidden=64, layers=3, n_heads=4,
                  patch_len=16, patch_stride=8, dropout=0.1, max_epochs=25, patience=4,
                  tag="11 tokens per channel; channel-independent; 13x GRU cost per sample"),
        ModelSpec("nbeats", "nbeats", seq_len=seq_len, hidden=128, layers=2, blocks=3,
                  path_h=path_h, dropout=0.1, tag="generic basis, multi-horizon"),
        ModelSpec("nhits", "nhits", seq_len=seq_len, hidden=128, layers=2,
                  pool_rates=(8, 4, 1), path_h=path_h, dropout=0.1,
                  tag="multi-rate pooling, multi-horizon"),
    ]


# --------------------------------------------------------------------------- VRAM


def vram_probe(spec: ModelSpec, n_features: int, *, device: torch.device | None = None,
               batch_size: int | None = None, backward: bool = True) -> dict:
    """Peak VRAM and per-step wall-clock for one configuration. Measured, never estimated.

    Runs three warm-up steps before timing, because the first CUDA kernel launch and the
    caching allocator's first growth both land in a cold measurement and inflate it by an order
    of magnitude. ``torch.cuda.reset_peak_memory_stats`` is called after the warm-up for the
    same reason.

    ``backward=True`` measures the training footprint (activations retained for the gradient),
    which is the number that decides batch size. Inference peak is typically 3-5x smaller and
    is not the constraint.
    """
    dev = device or pick_device()
    bs = batch_size or spec.batch_size
    model = build_model(spec, n_features).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=spec.lr)
    dtype = amp_dtype(dev)
    scaler = torch.amp.GradScaler(dev.type, enabled=(dtype == torch.float16))
    x = torch.randn(bs, spec.seq_len, n_features, device=dev)
    y = torch.randn(bs, device=dev)

    # Taken as ARGUMENTS rather than closed over, because `del model, opt, x, y` below is
    # what actually releases the VRAM this function exists to measure. A closure would keep
    # those cells alive until the frame died — and worse, it made the code look broken: after
    # the `del` the captured cells are empty, so a later `step()` would raise NameError, which
    # is what ruff's F821 was pointing at. Explicit parameters make the lifetime obvious and
    # let the `del` do its job.
    def step(model: nn.Module, opt: Any, x: torch.Tensor, y: torch.Tensor) -> None:
        opt.zero_grad(set_to_none=True)
        with torch.autocast(dev.type, dtype=dtype, enabled=dtype is not None):
            out = model(x)
            loss = F.mse_loss(out.ret.float(), y)
            if out.vol is not None:
                loss = loss + F.mse_loss(out.vol.float(), y)
        if not backward:
            return
        scaler.scale(loss).backward()
        scaler.step(opt)
        scaler.update()

    for _ in range(3):
        step(model, opt, x, y)
    if dev.type == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats(dev)
    t0 = time.perf_counter()
    n_steps = 10
    for _ in range(n_steps):
        step(model, opt, x, y)
    if dev.type == "cuda":
        torch.cuda.synchronize()
    ms = (time.perf_counter() - t0) / n_steps * 1000.0

    out = {"model": spec.name, "family": spec.family, "batch": bs, "seq_len": spec.seq_len,
           "n_features": n_features, "params": param_count(model),
           "ms_per_step": round(ms, 3), "device": str(dev),
           "amp": str(dtype).replace("torch.", "") if dtype else "none"}
    if dev.type == "cuda":
        out |= {"peak_alloc_MiB": round(torch.cuda.max_memory_allocated(dev) / 1024**2, 1),
                "peak_reserved_MiB": round(torch.cuda.max_memory_reserved(dev) / 1024**2, 1)}
    del model, opt, x, y
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    return out


def largest_that_fits(spec: ModelSpec, n_features: int, *, axis: str = "batch_size",
                      values: Sequence[int] = (256, 1024, 4096, 16384),
                      device: torch.device | None = None) -> dict:
    """Walk ``axis`` up through ``values`` and report the last size that fit — and read the caveat.

    **On this host the card does not OOM, and that is the measurement.** The Windows/WSL2 driver
    permits *system-memory fallback*, so an allocation past 6 GB is served from host RAM over PCIe
    instead of raising. Measured: PatchTST at ``seq_len=512`` reported ``peak_alloc 9,090 MiB`` on a
    6,143 MiB card and its step time went from 188 ms to **4,639 ms** — 25x slower, no error. So
    ``largest_that_fit`` here means "the largest size that was not catastrophically slow", and the
    real ceiling on this machine is a **time** ceiling that arrives before the memory one. A sizing
    rule written from ``OutOfMemoryError`` would never fire and the run would simply take a day.

    The second thing this probe found is not a memory limit at all: PatchTST is
    channel-independent, so its effective batch is ``B x F``, and torch's efficient-attention
    kernel refuses a batch above **65,535**. At 20 features that caps ``batch_size`` at 3,276
    whatever the VRAM says. It surfaces as a ``RuntimeError``, which is why this function reports
    non-OOM runtime errors with their message rather than swallowing them into "did not fit".

    Every probe empties the allocator cache afterwards, because a reserved-but-free block from the
    previous size otherwise makes the next size look larger than it is.
    """
    if axis not in ("batch_size", "seq_len", "hidden"):
        raise ValueError("axis must be batch_size, seq_len or hidden")
    rows, last_ok = [], None
    for v in values:
        s = spec.sized(**{axis: int(v)})
        try:
            r = vram_probe(s, n_features, device=device, batch_size=s.batch_size)
            r["fit"] = True
            last_ok = r
        except torch.cuda.OutOfMemoryError:
            rows.append({axis: int(v), "fit": False, "error": "CUDA OOM"})
            if device is None or device.type == "cuda":
                torch.cuda.empty_cache()
            break
        except RuntimeError as exc:                       # non-OOM runtime error: report it
            rows.append({axis: int(v), "fit": False, "error": f"{type(exc).__name__}: {exc}"})
            break
        rows.append(r)
    return {"axis": axis, "model": spec.name, "largest_that_fit": last_ok, "probes": rows}
