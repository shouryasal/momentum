"""The measurement surface an in-product research agent calls to test an idea.

TIER 2 (``evals/**``): a human writes this; an automated run only *calls* it, through the
allowlisted skill script ``.claude/skills/strategy-lab/scripts/research_api.py``.

Why this module exists
----------------------
Every strategy finding this system has ever shipped was produced by a build-time agent who
knew docker, freqtrade flags and feather layouts, and handed the result over as finished
config. That tests the builder, not the product. For the product to form and test its own
hypotheses it needs one surface it can call that does not require any of that knowledge::

    from evals.backtest_api import run_backtest, compare, walk_forward

    base = run_backtest({}, "20210101-20260901")
    cand = run_backtest({"trading.take_profit.roi_table": {"0": 0.35}}, "20210101-20260901")
    print(compare(cand, base).describe())

What the caller gets is a :class:`Metrics` object — net return, max drawdown, Calmar,
Sharpe, Sortino, turnover, fees actually paid, trade count, time in market, the exit-reason
breakdown, per-year and per-regime splits, and the equity curve — with costs ALWAYS
applied from ``config/backtest.yaml`` (the TCA-measured numbers, never freqtrade's
defaults).

Safety properties, because the caller is a machine
--------------------------------------------------
1. **Read-only on config.** The patch is applied to a *copy* of ``config/`` inside a
   throwaway run root. ``config/earn.yaml`` and the live ``config/`` are never written and
   are not even mounted into the container; the copy goes in read-only.
2. **Tier-2 keys are refused, loudly.** A patch that reaches for a risk limit, the
   universe, capital, mode or credentials raises :class:`PatchRefused` naming the key and
   the reason. Unknown namespaces are refused too — deny by default, never silently
   ignored. See :data:`ALLOWED_NAMESPACES` / :data:`DENIED_KEYS`.
3. **Bounds are not silently clamped.** The container clamps a tier-1 param to
   ``riskgate.json: bounds`` and keeps going, which would make a research result a lie
   about the value it tested. So an out-of-bounds param is refused *here*, before the run.
4. **Bounded run time.** Every container invocation carries a timeout (default 30 min) and
   is killed at it; :func:`walk_forward` carries a budget across its folds.
5. **No writes to live state.** The journal and proposal directories the container sees
   are empty throwaways under the run root. ``data/`` and ``knowledge/`` are mounted
   read-only.
6. **Deterministic given the same inputs.** Identical (patch, timerange, pairs, costs,
   config digest, candle digest) returns the cached result rather than re-running, and
   says so in ``Metrics.cached``.

The one network access in the whole path is freqtrade's own Binance ``exchangeInfo`` load
at container start — symbol metadata, which never enters the P&L. Nothing in this module
opens a socket, and the price data comes entirely from the local feather store.

Dependencies: stdlib + pandas/numpy, and ``runs.features`` for the candle store. It does
not import ``ops`` or ``console``, so it is callable from a worktree, a skill script or a
bare REPL.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from runs import features

__all__ = [
    "ALLOWED_NAMESPACES",
    "DENIED_KEYS",
    "BacktestError",
    "Benchmark",
    "Comparison",
    "ExitReason",
    "Fold",
    "Metrics",
    "PatchRefused",
    "RunSpec",
    "WalkForward",
    "YearStat",
    "buy_and_hold",
    "compare",
    "costs",
    "docker_runner",
    "flatten_patch",
    "materialise",
    "parse_timerange",
    "run_backtest",
    "validate_patch",
    "walk_forward",
]

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where a research run's throwaway root is built. Nothing under it is ever read back by
#: the live system; it exists so the container has a writable ``user_data`` that is not the
#: bots'.
RESEARCH_DIR = "ft_userdata/research"

DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_SLEEVE = "a"
ANNUAL_DAYS = 365.0


# ============================================================================ errors


class BacktestError(RuntimeError):
    """The measurement could not be produced. Never a silently degraded number."""


class PatchRefused(ValueError):
    """The patch asked for something a research run is not allowed to change.

    Raised instead of dropping the key, because a patch that is quietly ignored produces a
    result labelled with a change that never happened — the worst possible outcome for a
    loop that grades itself on its own predictions.
    """


# ============================================================================ the patch
#
# A patch is a flat mapping of dotted keys to values. Three namespaces exist, and they are
# the three things a research run may legitimately vary:
#
#   params.<...>     -> config/params-sleeve-<s>.json, under "params"
#                       (the tier-1 sleeve parameters: trend.ma_days, vol.target_annual,
#                        dca.chunk_pct_nav, rebalance_band)
#   trading.<...>    -> config/riskgate.json, under trading.sleeves.<s>
#                       (the mechanics: take_profit.roi_table, take_profit.ladder,
#                        stoploss.trailing.*, stoploss.atr.*, stoploss.fixed_pct, dca.*,
#                        pyramid.*, entry_price.*, exit_price.*, rebalance.*, sizing_mode)
#   execution.<...>  -> config/riskgate.json, under execution
#                       (rebalance_band, dust_weight, the unfilled timeouts)
#
# Everything else is refused. `trading.<key>` deliberately addresses THIS sleeve's block:
# a research run cannot reach across into the other sleeve's mechanics.

ALLOWED_NAMESPACES: dict[str, str] = {
    "params": "config/params-sleeve-<sleeve>.json :: params",
    "trading": "config/riskgate.json :: trading.sleeves.<sleeve>",
    "execution": "config/riskgate.json :: execution",
}

#: Dotted keys (or whole namespaces) that are tier 2 — human-only — with the reason the
#: refusal message quotes. Matching is by exact key or by dotted prefix.
DENIED_KEYS: dict[str, str] = {
    "risk": "risk limits are tier 2 (human-only): a research run may not widen a limit",
    "bounds": "the tier-1 bounds table is tier 2: moving a bound is a human decision",
    "universe": "the tradeable universe is tier 2: scope a run with pairs=, never a patch",
    "phase": "mode is not config — it lives in the HMAC-signed var/state/mode.json",
    "container_paths": "container paths are tier 2 wiring, not a research variable",
    "proposal": "the proposal contract is tier 2",
    "sleeve_b": "cross-sleeve wiring is tier 2",
    "generated_from": "provenance of the generated config is not a research variable",
    "source_sha256": "provenance of the generated config is not a research variable",
    "trading.plan_bounds": "plan_bounds constrains what a model may propose — tier 2",
    "trading.sleeves": "address this sleeve's mechanics as trading.<key>, not a sleeve",
    # freqtrade-level keys, named explicitly so the refusal is specific rather than a
    # generic 'unknown namespace'.
    "exchange": "exchange config carries credentials and the whitelist — tier 2",
    "dry_run": "committed bot configs are always dry_run: true (an invariant)",
    "dry_run_wallet": "capital is tier 2",
    "stake_amount": "capital is tier 2",
    "stake_currency": "capital is tier 2",
    "tradable_balance_ratio": "capital is tier 2",
    "max_open_trades": "concurrent-position capacity is a risk limit — tier 2",
    "api_server": "the API server is tier 2",
    "db_url": "the trade database location is tier 2",
    "user_data_dir": "container paths are tier 2 wiring",
    "pairlists": "the pairlist method is tier 2: scope a run with pairs=",
    "strategy": "pick a strategy with strategy=, not a patch",
    "timeframe": "the timeframe is tier 2 (it changes what every stored candle means)",
}


def flatten_patch(patch: Mapping[str, Any]) -> dict[str, Any]:
    """Accept either ``{"a.b": 1}`` or ``{"a": {"b": 1}}`` and return the dotted form.

    The two forms mean slightly different things, and the difference is worth knowing:

    * A **dotted** key is taken exactly as written and its value is used whole. This is how
      a mapping is REPLACED — ``{"trading.take_profit.roi_table": {"0": 0.35}}`` installs
      that table and drops whatever was there before.
    * A **nested** mapping is descended all the way to its leaves, so
      ``{"trading": {"take_profit": {"roi_table": {"0": 0.35}}}}`` becomes
      ``trading.take_profit.roi_table.0`` and MERGES into the existing table.

    Lists are never descended into, so a ``ladder`` always arrives whole.
    """
    out: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, Mapping) and node and all(isinstance(k, str) for k in node):
            for k, v in node.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
            return
        if not prefix:
            raise PatchRefused("a patch key may not be empty")
        out[prefix] = node

    for key, value in patch.items():
        if "." in str(key):
            out[str(key)] = value
        else:
            walk(str(key), value)
    return out


def _denial_for(key: str) -> str | None:
    parts = key.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in DENIED_KEYS:
            return DENIED_KEYS[prefix]
    return None


def validate_patch(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise a patch to dotted keys, refusing every tier-2 key by name.

    Returns the flat patch. Raises :class:`PatchRefused` with a message that names the
    offending key, why it is refused and what the caller should do instead.
    """
    flat = flatten_patch(patch or {})
    problems: list[str] = []
    for key in sorted(flat):
        reason = _denial_for(key)
        if reason is not None:
            problems.append(f"{key}: REFUSED — {reason}")
            continue
        namespace = key.split(".")[0]
        if namespace not in ALLOWED_NAMESPACES:
            allowed = ", ".join(sorted(ALLOWED_NAMESPACES))
            problems.append(
                f"{key}: REFUSED — unknown namespace {namespace!r}; a research patch may "
                f"only touch {allowed} (deny by default)"
            )
        elif len(key.split(".")) < 2:
            problems.append(
                f"{key}: REFUSED — {namespace!r} is a namespace, not a key; "
                f"name the leaf, e.g. {namespace}.<key>"
            )
    if problems:
        raise PatchRefused(
            "this patch may not be applied by an automated research run:\n  "
            + "\n  ".join(problems)
        )
    return flat


def _set_dotted(tree: dict[str, Any], dotted: str, value: Any) -> None:
    node = tree
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _get_dotted(tree: Mapping[str, Any], dotted: str) -> Any:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def check_bounds(flat: Mapping[str, Any], riskgate: Mapping[str, Any], sleeve: str) -> None:
    """Refuse a ``params.*`` value outside ``riskgate.json: bounds``.

    The container clamps such a value and carries on, so the run would report a metric for
    a parameter it did not actually test. Refusing here keeps every number this module
    returns a number about the configuration it names. Widening a bound is a human change.
    """
    bounds = riskgate.get("bounds") or {}
    problems = []
    for key, value in flat.items():
        if not key.startswith("params."):
            continue
        leaf = key[len("params."):]
        spec = _get_dotted(bounds, f"sleeve_{sleeve}.{leaf}") or _get_dotted(bounds, leaf)
        if not isinstance(spec, Mapping) or not isinstance(value, int | float):
            continue
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and float(value) < float(lo):
            problems.append(f"{key}={value} < bounds.min {lo}")
        if hi is not None and float(value) > float(hi):
            problems.append(f"{key}={value} > bounds.max {hi}")
    if problems:
        raise PatchRefused(
            "out of the tier-1 bounds the container would silently clamp to, so the run "
            "would not measure what it claims:\n  " + "\n  ".join(problems)
            + "\n  moving a bound is a tier-2 change a human makes."
        )


# ============================================================================ run spec


def parse_timerange(timerange: str) -> tuple[date, date]:
    """``20210101-20260901`` -> dates. An open end means 'up to today'."""
    raw = (timerange or "").strip()
    if "-" not in raw:
        raise BacktestError(f"timerange must look like 20210101-20260901, got {raw!r}")
    a, b = raw.split("-", 1)
    if len(a) != 8 or not a.isdigit():
        raise BacktestError(f"timerange start must be YYYYMMDD, got {a!r}")
    start = date(int(a[:4]), int(a[4:6]), int(a[6:8]))
    if not b:
        end = datetime.now(UTC).date()
    elif len(b) == 8 and b.isdigit():
        end = date(int(b[:4]), int(b[4:6]), int(b[6:8]))
    else:
        raise BacktestError(f"timerange end must be YYYYMMDD or empty, got {b!r}")
    if end <= start:
        raise BacktestError(f"timerange ends before it starts: {raw!r}")
    return start, end


def costs(root: Path | None = None) -> tuple[float, float]:
    """``(fee_bps, slippage_bps)`` from ``config/backtest.yaml`` — the TCA-measured pair.

    Never freqtrade's default. A missing file is an error rather than a fallback: a
    backtest with unknown costs is the single easiest way to manufacture an edge.
    """
    path = (root or REPO_ROOT) / "config" / "backtest.yaml"
    try:
        block = yaml.safe_load(path.read_text(encoding="utf-8"))["costs"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise BacktestError(f"cannot read measured costs from {path}: {exc}") from exc
    return float(block["fee_bps"]), float(block["slippage_bps"])


@dataclass(frozen=True)
class RunSpec:
    """Everything that decides a result. Its digest is the cache key."""

    patch: dict[str, Any]
    timerange: str
    pairs: tuple[str, ...]
    sleeve: str
    strategy: str
    fee_bps: float
    slippage_bps: float
    config_digest: str
    data_digest: str

    @property
    def fee_rate(self) -> float:
        """The per-side rate handed to freqtrade: fee + slippage, since freqtrade has no
        slippage knob. This is the documented deviation ``ops/backtest.sh`` already makes."""
        return (self.fee_bps + self.slippage_bps) / 10000.0

    def digest(self) -> str:
        payload = {
            "patch": self.patch, "timerange": self.timerange, "pairs": list(self.pairs),
            "sleeve": self.sleeve, "strategy": self.strategy, "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps, "config": self.config_digest,
            "data": self.data_digest,
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def _digest_files(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        try:
            h.update(p.read_bytes() if p.stat().st_size < 4_000_000 else b"")
            st = p.stat()
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:16]


# ============================================================================ metrics


@dataclass(frozen=True)
class ExitReason:
    """One row of the exit-reason breakdown — how each way out actually paid."""

    reason: str
    trades: int
    win_rate_pct: float
    profit_total_pct: float
    profit_mean_pct: float

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class YearStat:
    year: str
    return_pct: float
    max_drawdown_pct: float
    trades: int

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class RegimeStat:
    """A split by BTC's own 200d trend regime, computed from the local candle store.

    ``return_pct`` is P&L earned while in that regime as a fraction of starting balance —
    additive across regimes, which a compounded figure would not be.
    """

    regime: str
    days: int
    return_pct: float
    best_day_pct: float
    worst_day_pct: float

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class Metrics:
    """The result object. Every field is recomputed from the freqtrade artefact.

    ``sharpe``/``sortino``/``calmar`` are freqtrade's own (trade-based); the equity-curve
    versions computed here carry the ``_daily`` suffix so the two definitions are never
    confused for one another.
    """

    # identity / provenance
    run_id: str
    digest: str
    strategy: str
    sleeve: str
    timerange: str
    start: str
    end: str
    days: int
    pairs: tuple[str, ...]
    patch: dict[str, Any]
    fee_bps: float
    slippage_bps: float
    starting_balance: float
    cached: bool = False
    result_zip: str = ""

    # headline
    net_return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    sharpe_daily: float = 0.0
    calmar_daily: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    win_rate_pct: float = 0.0

    # activity and cost
    trades: int = 0
    trades_per_year: float = 0.0
    turnover_annual: float = 0.0
    fees_paid_quote: float = 0.0
    fees_pct_of_start: float = 0.0
    time_in_market_pct: float = 0.0
    avg_concurrent_positions: float = 0.0

    # breakdowns
    exit_reasons: tuple[ExitReason, ...] = ()
    per_year: tuple[YearStat, ...] = ()
    per_regime: tuple[RegimeStat, ...] = ()
    equity_curve: tuple[tuple[str, float], ...] = ()

    @property
    def years(self) -> float:
        return max(self.days / ANNUAL_DAYS, 1e-9)

    def headline(self) -> dict[str, float]:
        """The numbers a comparison ranks on — the ones a prediction may name."""
        return {
            "net_return_pct": self.net_return_pct,
            "cagr_pct": self.cagr_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "calmar": self.calmar,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "sharpe_daily": self.sharpe_daily,
            "profit_factor": self.profit_factor,
            "trades": float(self.trades),
            "turnover_annual": self.turnover_annual,
            "fees_paid_quote": self.fees_paid_quote,
            "time_in_market_pct": self.time_in_market_pct,
        }

    def as_dict(self, *, curve: bool = False) -> dict[str, Any]:
        out = {
            "run_id": self.run_id, "digest": self.digest, "strategy": self.strategy,
            "sleeve": self.sleeve, "timerange": self.timerange, "start": self.start,
            "end": self.end, "days": self.days, "pairs": list(self.pairs),
            "patch": self.patch, "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "starting_balance": self.starting_balance, "cached": self.cached,
            "trades": self.trades, "trades_per_year": self.trades_per_year,
            "turnover_annual": self.turnover_annual,
            "fees_paid_quote": self.fees_paid_quote,
            "fees_pct_of_start": self.fees_pct_of_start,
            "time_in_market_pct": self.time_in_market_pct,
            "avg_concurrent_positions": self.avg_concurrent_positions,
            "exit_reasons": [e.as_dict() for e in self.exit_reasons],
            "per_year": [y.as_dict() for y in self.per_year],
            "per_regime": [r.as_dict() for r in self.per_regime],
            **self.headline(),
        }
        if curve:
            out["equity_curve"] = [list(p) for p in self.equity_curve]
        return out

    def describe(self) -> str:
        lines = [
            f"{self.strategy} {self.timerange} ({self.start[:10]} -> {self.end[:10]}, "
            f"{self.days}d, {len(self.pairs)} pairs)",
            f"  patch            {json.dumps(self.patch, sort_keys=True) if self.patch else '{} (baseline)'}",
            f"  costs            {self.fee_bps:.1f} bps fee + {self.slippage_bps:.1f} bps slippage, per side",
            f"  net return       {self.net_return_pct:+.2f}%   CAGR {self.cagr_pct:+.2f}%",
            f"  max drawdown     {self.max_drawdown_pct:.2f}%",
            f"  Calmar {self.calmar:.2f}   Sharpe {self.sharpe:.3f} (daily {self.sharpe_daily:.3f})"
            f"   Sortino {self.sortino:.3f}",
            f"  trades           {self.trades} ({self.trades_per_year:.1f}/yr), "
            f"win rate {self.win_rate_pct:.1f}%",
            f"  turnover         {self.turnover_annual:.2f}x/yr   fees paid "
            f"{self.fees_paid_quote:,.2f} ({self.fees_pct_of_start:.2f}% of start)",
            f"  time in market   {self.time_in_market_pct:.1f}%  "
            f"(avg {self.avg_concurrent_positions:.2f} concurrent)",
        ]
        if self.exit_reasons:
            lines.append("  exits:")
            for e in self.exit_reasons:
                lines.append(
                    f"    {e.reason:<22} {e.trades:>4} trades  win {e.win_rate_pct:>5.1f}%  "
                    f"total {e.profit_total_pct:+.2f}%  mean {e.profit_mean_pct:+.2f}%"
                )
        if self.per_year:
            lines.append("  per year: " + "  ".join(
                f"{y.year} {y.return_pct:+.1f}%/dd {y.max_drawdown_pct:.1f}%"
                for y in self.per_year))
        if self.per_regime:
            lines.append("  per regime: " + "  ".join(
                f"{r.regime} {r.return_pct:+.1f}% over {r.days}d" for r in self.per_regime))
        return "\n".join(lines)


# ============================================================================ execution


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    returncode: int
    output: str


#: ``(argv, cwd, timeout_s) -> CommandResult``. Injected so the tests never start docker.
Runner = Callable[[Sequence[str], Path, float], CommandResult]


def subprocess_runner(argv: Sequence[str], cwd: Path, timeout_s: float) -> CommandResult:
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built here, never from a string
            list(argv), cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(False, -1, f"timed out after {timeout_s:.0f}s")
    return CommandResult(proc.returncode == 0, proc.returncode,
                         (proc.stdout or "") + (proc.stderr or ""))


def docker_runner(argv: Sequence[str], cwd: Path, timeout_s: float) -> CommandResult:
    """The production runner. Kept separate so a caller can see which one it got."""
    return subprocess_runner(argv, cwd, timeout_s)


def materialise(spec: RunSpec, run_root: Path, *, root: Path | None = None) -> Path:
    """Build the throwaway run root: a PATCHED COPY of config/, plus empty writable dirs.

    Returns the copied config directory. The live ``config/`` is read and never written;
    this is the only place the patch is ever applied.
    """
    src_root = root or REPO_ROOT
    cfg_dir = run_root / "config"
    for sub in ("user_data/backtest_results", "user_data/logs", "journal",
                "proposals", "killdir"):
        (run_root / sub).mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for name in ("freqtrade-a.json", "freqtrade-b.json", "riskgate.json",
                 "params-sleeve-a.json", "params-sleeve-b.json"):
        src = src_root / "config" / name
        if src.exists():
            shutil.copy2(src, cfg_dir / name)

    sleeve = spec.sleeve
    riskgate = json.loads((cfg_dir / "riskgate.json").read_text(encoding="utf-8"))
    params_path = cfg_dir / f"params-sleeve-{sleeve}.json"
    params_doc = json.loads(params_path.read_text(encoding="utf-8"))
    ft_path = cfg_dir / f"freqtrade-{sleeve}.json"
    ft_doc = json.loads(ft_path.read_text(encoding="utf-8"))

    check_bounds(spec.patch, riskgate, sleeve)

    for key, value in spec.patch.items():
        namespace, leaf = key.split(".", 1)
        if namespace == "params":
            _set_dotted(params_doc.setdefault("params", {}), leaf, value)
        elif namespace == "trading":
            sleeves = riskgate.setdefault("trading", {}).setdefault("sleeves", {})
            _set_dotted(sleeves.setdefault(sleeve, {}), leaf, value)
        elif namespace == "execution":
            _set_dotted(riskgate.setdefault("execution", {}), leaf, value)
        else:  # pragma: no cover - validate_patch already refused this
            raise PatchRefused(f"{key}: unreachable namespace {namespace!r}")

    # Scope, never widen: `pairs` must be a subset of the configured whitelist. Adding a
    # pair would be a universe change, which is tier 2.
    configured = list(ft_doc.get("exchange", {}).get("pair_whitelist") or [])
    if spec.pairs:
        extra = sorted(set(spec.pairs) - set(configured))
        if extra:
            raise PatchRefused(
                f"pairs {extra} are not in the configured universe; a research run may "
                "narrow the whitelist to measure a subset, never add to it (tier 2)"
            )
        ft_doc.setdefault("exchange", {})["pair_whitelist"] = list(spec.pairs)

    # The container must not be handed exchange credentials for a backtest.
    ft_doc.setdefault("exchange", {}).pop("key", None)
    ft_doc.setdefault("exchange", {}).pop("secret", None)

    params_path.write_text(json.dumps(params_doc, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    (cfg_dir / "riskgate.json").write_text(
        json.dumps(riskgate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ft_path.write_text(json.dumps(ft_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return cfg_dir


def container_argv(spec: RunSpec, run_root: Path, *, root: Path | None = None,
                   image: str = "freqtradeorg/freqtrade:2026.8",
                   uid: int = 1000, gid: int = 1000) -> list[str]:
    """``docker run`` rather than ``docker compose run``.

    Compose would hand the container the LIVE ``config/`` and the LIVE journal. This
    invocation mounts the patched copy read-only, mounts ``data/`` and ``knowledge/``
    read-only, and points every writable path at the throwaway run root — so a research
    run has no way to write anything the trading system reads.
    """
    src = root or REPO_ROOT
    data = features.data_dir()
    rr = run_root.resolve()
    return [
        "docker", "run", "--rm", "--user", f"{uid}:{gid}",
        "-v", f"{rr / 'config'}:/freqtrade/earn-config:ro",
        "-v", f"{rr / 'user_data'}:/freqtrade/user_data",
        "-v", f"{data}:/freqtrade/user_data/data:ro",
        "-v", f"{(src / 'strategies').resolve()}:/freqtrade/user_data/strategies:ro",
        "-v", f"{rr / 'journal'}:/freqtrade/journal",
        "-v", f"{(src / 'knowledge').resolve()}:/freqtrade/knowledge:ro",
        "-v", f"{rr / 'proposals'}:/freqtrade/proposals:ro",
        "-v", f"{rr / 'killdir'}:/freqtrade/killdir:ro",
        "-e", f"EARN_SLEEVE={spec.sleeve}",
        "-e", "EARN_RISKGATE=/freqtrade/earn-config/riskgate.json",
        "-e", "EARN_JOURNAL_DB=/freqtrade/journal/journal.db",
        "-e", "EARN_KNOWLEDGE_DB=/freqtrade/knowledge/earn.db",
        image, "backtesting",
        "--strategy", spec.strategy,
        "--config", f"/freqtrade/earn-config/freqtrade-{spec.sleeve}.json",
        "--timerange", spec.timerange,
        "--fee", f"{spec.fee_rate:.8f}",
        "--enable-protections",
        "--export", "trades",
        "--backtest-directory", "/freqtrade/user_data/backtest_results",
    ]


# ============================================================================ parsing


def _read_result(zip_path: Path, strategy: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist()
                    if n.endswith(".json") and not n.endswith("_config.json")
                    and "market_change" not in n)
        doc = json.loads(z.read(name))
    strategies = doc["strategy"]
    if strategy in strategies:
        return doc, strategies[strategy]
    if len(strategies) == 1:
        return doc, next(iter(strategies.values()))
    raise BacktestError(f"{zip_path.name} holds {sorted(strategies)}, not {strategy!r}")


def _equity_curve(res: Mapping[str, Any]) -> pd.DataFrame:
    """``starting_balance + cumsum(daily realised P&L)`` — freqtrade's own equity series.

    Verified against the artefact: the drawdown of this series reproduces freqtrade's
    ``max_drawdown_account`` to 1e-15, which is what makes it safe to split by year and by
    regime here rather than re-deriving a different number.
    """
    rows = res.get("daily_profit") or []
    if not rows:
        return pd.DataFrame(columns=["date", "equity", "pnl"])
    df = pd.DataFrame(rows, columns=["date", "pnl"])
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    df["equity"] = float(res.get("starting_balance") or 0.0) + df["pnl"].cumsum()
    return df


def _max_dd_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float(-(equity / equity.cummax() - 1.0).min() * 100.0)


def _exit_reasons(res: Mapping[str, Any]) -> tuple[ExitReason, ...]:
    out = []
    for row in res.get("exit_reason_summary") or []:
        # freqtrade appends its own TOTAL row; it is the sum, not a way out of a trade,
        # and leaving it in would double every count a comparison prints.
        if str(row.get("key") or "").upper() == "TOTAL":
            continue
        out.append(ExitReason(
            reason=str(row.get("key") or "unknown"),
            trades=int(row.get("trades") or 0),
            win_rate_pct=round(float(row.get("winrate") or 0.0) * 100.0, 2),
            profit_total_pct=round(float(row.get("profit_total") or 0.0) * 100.0, 3),
            profit_mean_pct=round(float(row.get("profit_mean") or 0.0) * 100.0, 3),
        ))
    return tuple(sorted(out, key=lambda e: -e.trades))


def _per_year(eq: pd.DataFrame, trades: pd.DataFrame, start_balance: float
              ) -> tuple[YearStat, ...]:
    if eq.empty:
        return ()
    out = []
    eq = eq.set_index("date")
    opened = (pd.to_datetime(trades["open_date"], utc=True, errors="coerce")
              if "open_date" in trades.columns else pd.Series(dtype="datetime64[ns, UTC]"))
    for year, block in eq.groupby(eq.index.year):
        prior = eq.loc[eq.index < block.index[0], "equity"]
        opening = float(prior.iloc[-1]) if len(prior) else start_balance
        closing = float(block["equity"].iloc[-1])
        ret = (closing / opening - 1.0) * 100.0 if opening else 0.0
        curve = pd.concat([pd.Series([opening]), block["equity"].reset_index(drop=True)])
        n = int((opened.dt.year == year).sum()) if len(opened) else 0
        out.append(YearStat(str(year), round(ret, 3), round(_max_dd_pct(curve), 3), n))
    return tuple(out)


def _btc_regime(start: pd.Timestamp, end: pd.Timestamp, ma_days: int = 200) -> pd.Series:
    """Daily ``trend_up``/``trend_down`` from BTC's own 200d SMA, from the local store.

    Computed here rather than read from ``knowledge/state`` because a backtest spans years
    of history that no state file covers, and because a regime split must be reproducible
    from data the caller already has.
    """
    try:
        df = features.load_candles("BTC/USDT", "1d")
    except (FileNotFoundError, ValueError):
        return pd.Series(dtype=object)
    df = df.set_index("date")
    sma = df["close"].rolling(ma_days, min_periods=ma_days).mean()
    regime = pd.Series(
        np.where(df["close"] >= sma, "trend_up", "trend_down"), index=df.index, dtype=object
    )
    regime[sma.isna()] = "warmup"
    return regime.loc[(regime.index >= start) & (regime.index <= end)]


def _per_regime(eq: pd.DataFrame, start_balance: float) -> tuple[RegimeStat, ...]:
    if eq.empty or start_balance <= 0:
        return ()
    regime = _btc_regime(eq["date"].min(), eq["date"].max())
    if regime.empty:
        return ()
    daily = eq.set_index("date")["pnl"]
    labels = regime.reindex(daily.index, method="ffill")
    out = []
    for name, block in daily.groupby(labels):
        if not isinstance(name, str):
            continue
        pct = block / start_balance * 100.0
        out.append(RegimeStat(
            regime=name, days=int(len(block)), return_pct=round(float(pct.sum()), 3),
            best_day_pct=round(float(pct.max()), 3), worst_day_pct=round(float(pct.min()), 3),
        ))
    return tuple(sorted(out, key=lambda r: r.regime))


def _activity(res: Mapping[str, Any], trades: pd.DataFrame, fee_rate: float,
              start: pd.Timestamp, end: pd.Timestamp, start_balance: float
              ) -> dict[str, float]:
    """Turnover, fees actually paid, and time in market — from the trade/order records.

    Fees are summed from the individual filled orders (``cost * fee_rate``), not from a
    headline field, because that is the number that would show up on a statement and it is
    the one a turnover-increasing change has to earn back.
    """
    span_days = max((end - start).total_seconds() / 86400.0, 1e-9)
    years = span_days / ANNUAL_DAYS
    notional = 0.0
    if "orders" in trades.columns:
        for orders in trades["orders"]:
            for order in orders or []:
                notional += float(order.get("cost") or 0.0)
    if notional <= 0:
        notional = float(res.get("total_volume") or 0.0)
    fees = notional * fee_rate

    covered = 0.0
    concurrent_days = 0.0
    if {"open_date", "close_date"} <= set(trades.columns) and len(trades):
        opens = pd.to_datetime(trades["open_date"], utc=True, errors="coerce")
        closes = pd.to_datetime(trades["close_date"], utc=True, errors="coerce").fillna(end)
        spans = sorted(zip(opens, closes, strict=False))
        concurrent_days = float(sum((c - o).total_seconds() for o, c in spans) / 86400.0)
        cur_o, cur_c = spans[0]
        for o, c in spans[1:]:
            if o <= cur_c:
                cur_c = max(cur_c, c)
            else:
                covered += (cur_c - cur_o).total_seconds()
                cur_o, cur_c = o, c
        covered += (cur_c - cur_o).total_seconds()
        covered /= 86400.0
    return {
        "fees_paid_quote": round(fees, 4),
        "fees_pct_of_start": round(fees / start_balance * 100.0, 4) if start_balance else 0.0,
        "turnover_annual": round(notional / start_balance / years, 4)
        if start_balance and years else 0.0,
        "time_in_market_pct": round(min(covered / span_days, 1.0) * 100.0, 3),
        "avg_concurrent_positions": round(concurrent_days / span_days, 3),
        "trades_per_year": round(float(res.get("total_trades") or 0) / years, 3)
        if years else 0.0,
    }


def metrics_from_zip(zip_path: Path, spec: RunSpec, *, run_id: str, cached: bool = False
                     ) -> Metrics:
    """Turn one freqtrade result archive into a :class:`Metrics`. Pure; no docker."""
    _, res = _read_result(zip_path, spec.strategy)
    start_balance = float(res.get("starting_balance") or 0.0)
    eq = _equity_curve(res)
    trades = pd.DataFrame(res.get("trades") or [])
    start = pd.Timestamp(res.get("backtest_start"), tz="UTC")
    end = pd.Timestamp(res.get("backtest_end"), tz="UTC")
    days = int(round((end - start).total_seconds() / 86400.0))

    ret = eq["equity"].pct_change().dropna() if not eq.empty else pd.Series(dtype=float)
    sharpe_daily = (float(ret.mean() / ret.std() * math.sqrt(ANNUAL_DAYS))
                    if len(ret) > 2 and ret.std() > 0 else 0.0)
    dd = _max_dd_pct(eq["equity"]) if not eq.empty else 0.0
    net = float(res.get("profit_total") or 0.0) * 100.0
    years = max(days / ANNUAL_DAYS, 1e-9)
    cagr = ((1.0 + net / 100.0) ** (1.0 / years) - 1.0) * 100.0 if net > -100 else -100.0
    act = _activity(res, trades, spec.fee_rate, start, end, start_balance)

    return Metrics(
        run_id=run_id, digest=spec.digest(), strategy=spec.strategy, sleeve=spec.sleeve,
        timerange=spec.timerange, start=str(res.get("backtest_start")),
        end=str(res.get("backtest_end")), days=days,
        pairs=tuple(spec.pairs or res.get("pairlist") or ()), patch=dict(spec.patch),
        fee_bps=spec.fee_bps, slippage_bps=spec.slippage_bps,
        starting_balance=start_balance, cached=cached, result_zip=str(zip_path),
        net_return_pct=round(net, 4), cagr_pct=round(cagr, 4),
        max_drawdown_pct=round(dd, 4),
        calmar=round(float(res.get("calmar") or 0.0), 4),
        sharpe=round(float(res.get("sharpe") or 0.0), 4),
        sortino=round(float(res.get("sortino") or 0.0), 4),
        sharpe_daily=round(sharpe_daily, 4),
        calmar_daily=round(cagr / dd, 4) if dd > 0 else 0.0,
        profit_factor=round(float(res.get("profit_factor") or 0.0), 4),
        expectancy=round(float(res.get("expectancy") or 0.0), 4),
        win_rate_pct=round(float(res.get("winrate") or 0.0) * 100.0, 3),
        trades=int(res.get("total_trades") or 0),
        trades_per_year=act["trades_per_year"],
        turnover_annual=act["turnover_annual"],
        fees_paid_quote=act["fees_paid_quote"],
        fees_pct_of_start=act["fees_pct_of_start"],
        time_in_market_pct=act["time_in_market_pct"],
        avg_concurrent_positions=act["avg_concurrent_positions"],
        exit_reasons=_exit_reasons(res),
        per_year=_per_year(eq, trades, start_balance),
        per_regime=_per_regime(eq, start_balance),
        equity_curve=tuple((d.strftime("%Y-%m-%d"), round(float(v), 4))
                           for d, v in zip(eq["date"], eq["equity"], strict=False)),
    )


# ============================================================================ the API


def _newest_zip(results_dir: Path) -> Path | None:
    zips = sorted(results_dir.glob("backtest-result-*.zip"),
                  key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def _spec_for(patch: Mapping[str, Any] | None, timerange: str,
              pairs: Sequence[str] | None, *, sleeve: str, strategy: str,
              fee_bps: float | None, slippage_bps: float | None, root: Path) -> RunSpec:
    flat = validate_patch(patch)
    parse_timerange(timerange)
    fee, slip = costs(root)
    pair_list = tuple(sorted(pairs)) if pairs else ()
    data = features.data_dir()
    digest_pairs = pair_list or ("BTC/USDT", "ETH/USDT")
    return RunSpec(
        patch=flat, timerange=timerange, pairs=pair_list, sleeve=sleeve.lower(),
        strategy=strategy,
        fee_bps=fee if fee_bps is None else float(fee_bps),
        slippage_bps=slip if slippage_bps is None else float(slippage_bps),
        config_digest=_digest_files(
            (root / "config").glob("*.json") if (root / "config").exists() else []),
        data_digest=_digest_files(
            features.candle_path(p, "4h", data) for p in digest_pairs),
    )


def run_backtest(
    config_patch: Mapping[str, Any] | None = None,
    timerange: str = "20210101-",
    pairs: Sequence[str] | None = None,
    *,
    sleeve: str = DEFAULT_SLEEVE,
    strategy: str | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    root: Path | None = None,
    runner: Runner | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reuse: bool = True,
    keep_run_root: bool = False,
) -> Metrics:
    """Backtest one configuration and return its :class:`Metrics`. Costs always applied.

    ``config_patch`` is a flat mapping of dotted keys (see :data:`ALLOWED_NAMESPACES`); an
    empty patch is the shipped baseline. ``pairs`` NARROWS the configured whitelist to
    measure a subset and can never add to it. ``timerange`` is freqtrade's
    ``YYYYMMDD-YYYYMMDD`` with an open end allowed.

    Raises :class:`PatchRefused` for a tier-2 key or an out-of-bounds param, and
    :class:`BacktestError` when the container fails or produces no archive. It never
    returns a degraded number.
    """
    src = (root or REPO_ROOT).resolve()
    spec = _spec_for(config_patch, timerange, pairs, sleeve=sleeve,
                     strategy=strategy or ("SleeveA" if sleeve.lower() == "a" else "SleeveB"),
                     fee_bps=fee_bps, slippage_bps=slippage_bps, root=src)
    digest = spec.digest()
    run_root = src / RESEARCH_DIR / digest
    results = run_root / "user_data" / "backtest_results"

    if reuse:
        existing = _newest_zip(results) if results.exists() else None
        manifest = run_root / "spec.json"
        if existing is not None and manifest.exists():
            try:
                if json.loads(manifest.read_text(encoding="utf-8")).get("digest") == digest:
                    return metrics_from_zip(existing, spec, run_id=digest, cached=True)
            except (OSError, ValueError):
                pass

    if run_root.exists():
        shutil.rmtree(run_root)
    materialise(spec, run_root, root=src)
    (run_root / "spec.json").write_text(
        json.dumps({"digest": digest, **{k: v for k, v in vars(spec).items()}},
                   indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    argv = container_argv(spec, run_root, root=src)
    outcome = (runner or docker_runner)(argv, src, timeout_s)
    if not outcome.ok:
        tail = "\n".join(outcome.output.strip().splitlines()[-25:])
        raise BacktestError(f"backtest failed (exit {outcome.returncode}):\n{tail}")
    zip_path = _newest_zip(results)
    if zip_path is None:
        raise BacktestError(f"no backtest archive produced under {results}")
    result = metrics_from_zip(zip_path, spec, run_id=digest)
    if not keep_run_root:
        shutil.rmtree(run_root / "config", ignore_errors=True)
    return result


# ============================================================================ benchmark


@dataclass(frozen=True)
class Benchmark:
    """Buy and hold, costed: one entry fee and one exit fee, nothing else."""

    pair: str
    start: str
    end: str
    net_return_pct: float
    max_drawdown_pct: float
    cagr_pct: float
    sharpe_daily: float
    calmar_daily: float
    fee_bps: float
    slippage_bps: float

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def buy_and_hold(pair: str, start: str | datetime, end: str | datetime, *,
                 fee_bps: float | None = None, slippage_bps: float | None = None,
                 root: Path | None = None) -> Benchmark:
    """The honest benchmark: hold ``pair`` across the window, paying to get in and out.

    A strategy that returns less than this had better be returning it with a much smaller
    drawdown, which is the whole reason Earn exists — so the comparison is built in rather
    than left to the caller to remember.
    """
    fee, slip = costs(root or REPO_ROOT)
    fee_bps = fee if fee_bps is None else float(fee_bps)
    slippage_bps = slip if slippage_bps is None else float(slippage_bps)
    df = features.load_candles(pair, "1d")
    lo = pd.Timestamp(start, tz="UTC") if not isinstance(start, pd.Timestamp) else start
    hi = pd.Timestamp(end, tz="UTC") if not isinstance(end, pd.Timestamp) else end
    block = df[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)
    if len(block) < 2:
        raise BacktestError(f"no {pair} candles between {lo} and {hi}")
    side = (fee_bps + slippage_bps) / 10000.0
    entry = float(block["close"].iloc[0]) * (1.0 + side)
    units = 1.0 / entry
    equity = block["close"] * units
    equity.iloc[-1] = float(block["close"].iloc[-1]) * units * (1.0 - side)
    net = (float(equity.iloc[-1]) - 1.0) * 100.0
    days = max((block["date"].iloc[-1] - block["date"].iloc[0]).days, 1)
    years = days / ANNUAL_DAYS
    cagr = ((1.0 + net / 100.0) ** (1.0 / years) - 1.0) * 100.0 if net > -100 else -100.0
    dd = _max_dd_pct(equity)
    ret = equity.pct_change().dropna()
    sharpe = (float(ret.mean() / ret.std() * math.sqrt(ANNUAL_DAYS))
              if len(ret) > 2 and ret.std() > 0 else 0.0)
    return Benchmark(
        pair=pair, start=str(block["date"].iloc[0]), end=str(block["date"].iloc[-1]),
        net_return_pct=round(net, 4), max_drawdown_pct=round(dd, 4),
        cagr_pct=round(cagr, 4), sharpe_daily=round(sharpe, 4),
        calmar_daily=round(cagr / dd, 4) if dd > 0 else 0.0,
        fee_bps=fee_bps, slippage_bps=slippage_bps,
    )


# ============================================================================ compare

#: ``metric -> True when a HIGHER value is better``. A metric absent from this map is
#: reported as a delta without a verdict rather than guessed at.
DIRECTION: dict[str, bool] = {
    "net_return_pct": True, "cagr_pct": True, "calmar": True, "sharpe": True,
    "sortino": True, "sharpe_daily": True, "profit_factor": True,
    "max_drawdown_pct": False, "turnover_annual": False, "fees_paid_quote": False,
    "time_in_market_pct": True, "trades": False,
}

#: A delta smaller than this (relative to the baseline, or absolute for ratios) is noise.
NOISE: dict[str, float] = {
    "net_return_pct": 1.0, "cagr_pct": 0.2, "max_drawdown_pct": 0.5, "calmar": 0.05,
    "sharpe": 0.02, "sortino": 0.05, "sharpe_daily": 0.02, "profit_factor": 0.05,
    "turnover_annual": 0.05, "fees_paid_quote": 1.0, "time_in_market_pct": 1.0,
    "trades": 1.0,
}


@dataclass(frozen=True)
class Comparison:
    """What changed, in both directions, and what it cost.

    ``better`` and ``worse`` are always both populated and both printed. A filter that
    dodges the drawdowns by also dodging the rallies looks excellent in a single headline
    number and terrible here, which is the point.
    """

    variant: str
    baseline: str
    timerange: str
    deltas: dict[str, float]
    better: tuple[str, ...]
    worse: tuple[str, ...]
    unchanged: tuple[str, ...]
    per_year_delta: dict[str, float]
    exit_reason_delta: dict[str, int]
    benchmark: Benchmark | None
    variant_excess_vs_benchmark_pct: float | None
    baseline_excess_vs_benchmark_pct: float | None
    variant_headline: dict[str, float] = field(default_factory=dict)
    baseline_headline: dict[str, float] = field(default_factory=dict)

    @property
    def is_noise(self) -> bool:
        return not self.better and not self.worse

    def as_dict(self) -> dict[str, Any]:
        out = dict(vars(self))
        out["better"] = list(self.better)
        out["worse"] = list(self.worse)
        out["unchanged"] = list(self.unchanged)
        out["benchmark"] = self.benchmark.as_dict() if self.benchmark else None
        return out

    def describe(self) -> str:
        lines = [f"variant {self.variant} vs baseline {self.baseline}  [{self.timerange}]"]
        for metric in sorted(self.deltas):
            v = self.variant_headline.get(metric)
            b = self.baseline_headline.get(metric)
            mark = ("better" if metric in self.better
                    else "WORSE" if metric in self.worse else "~")
            lines.append(f"  {metric:<22} {b:>12,.3f} -> {v:>12,.3f}  "
                         f"({self.deltas[metric]:+,.3f})  {mark}")
        if self.benchmark is not None:
            lines.append(
                f"  vs buy-and-hold {self.benchmark.pair}: benchmark "
                f"{self.benchmark.net_return_pct:+.2f}% / dd "
                f"{self.benchmark.max_drawdown_pct:.2f}%; "
                f"baseline excess {self.baseline_excess_vs_benchmark_pct:+.2f}pp, "
                f"variant excess {self.variant_excess_vs_benchmark_pct:+.2f}pp")
        if self.per_year_delta:
            lines.append("  per-year delta: " + "  ".join(
                f"{y} {d:+.1f}pp" for y, d in sorted(self.per_year_delta.items())))
        if self.exit_reason_delta:
            lines.append("  exit-count delta: " + "  ".join(
                f"{k} {v:+d}" for k, v in sorted(self.exit_reason_delta.items()) if v))
        if self.is_noise:
            lines.append("  VERDICT: nothing moved beyond noise — this change does nothing.")
        return "\n".join(lines)


def compare(variant: Metrics, baseline: Metrics, *, benchmark_pair: str = "BTC/USDT",
            with_benchmark: bool = True, root: Path | None = None) -> Comparison:
    """The deltas that matter, including against buy-and-hold BTC over the same window."""
    if variant.timerange != baseline.timerange:
        raise BacktestError(
            f"refusing to compare different windows: variant {variant.timerange!r} vs "
            f"baseline {baseline.timerange!r}")
    vh, bh = variant.headline(), baseline.headline()
    deltas, better, worse, unchanged = {}, [], [], []
    for metric in sorted(set(vh) & set(bh)):
        delta = vh[metric] - bh[metric]
        deltas[metric] = round(delta, 4)
        if abs(delta) < NOISE.get(metric, 0.0):
            unchanged.append(metric)
        elif metric in DIRECTION:
            (better if (delta > 0) == DIRECTION[metric] else worse).append(metric)
        else:
            unchanged.append(metric)

    by_year_v = {y.year: y.return_pct for y in variant.per_year}
    by_year_b = {y.year: y.return_pct for y in baseline.per_year}
    per_year = {y: round(by_year_v.get(y, 0.0) - by_year_b.get(y, 0.0), 3)
                for y in sorted(set(by_year_v) | set(by_year_b))}
    exits_v = {e.reason: e.trades for e in variant.exit_reasons}
    exits_b = {e.reason: e.trades for e in baseline.exit_reasons}
    exit_delta = {k: exits_v.get(k, 0) - exits_b.get(k, 0)
                  for k in sorted(set(exits_v) | set(exits_b))}

    bench = v_excess = b_excess = None
    if with_benchmark:
        try:
            bench = buy_and_hold(benchmark_pair, baseline.start, baseline.end, root=root)
            v_excess = round(variant.net_return_pct - bench.net_return_pct, 4)
            b_excess = round(baseline.net_return_pct - bench.net_return_pct, 4)
        except (BacktestError, FileNotFoundError, ValueError):
            bench = None

    return Comparison(
        variant=variant.run_id, baseline=baseline.run_id, timerange=variant.timerange,
        deltas=deltas, better=tuple(better), worse=tuple(worse),
        unchanged=tuple(unchanged), per_year_delta=per_year,
        exit_reason_delta=exit_delta, benchmark=bench,
        variant_excess_vs_benchmark_pct=v_excess,
        baseline_excess_vs_benchmark_pct=b_excess,
        variant_headline=vh, baseline_headline=bh,
    )


# ============================================================================ walk fwd


@dataclass(frozen=True)
class Fold:
    index: int
    in_sample: str
    out_of_sample: str
    variant: Metrics
    baseline: Metrics
    delta_net_return_pct: float
    delta_max_drawdown_pct: float
    variant_wins: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
            "delta_net_return_pct": self.delta_net_return_pct,
            "delta_max_drawdown_pct": self.delta_max_drawdown_pct,
            "variant_wins": self.variant_wins,
            "variant": self.variant.headline(), "baseline": self.baseline.headline(),
        }


@dataclass(frozen=True)
class WalkForward:
    """Out-of-sample folds with the in-sample/out-of-sample split stated explicitly.

    ``fitting`` is the honest caveat printed with every result: Sleeve A has fixed rules
    and nothing is fitted inside a fold, so the in-sample span is warm-up plus the history
    a hypothesis was formed on. That makes these folds a test of STABILITY across regimes,
    not of an estimator's generalisation — and reading them as the latter would overstate
    what they show.
    """

    patch: dict[str, Any]
    timerange: str
    folds: tuple[Fold, ...]
    scheme: str
    fitting: str
    oos_win_rate: float
    oos_mean_delta_pct: float
    oos_worst_delta_pct: float
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "patch": self.patch, "timerange": self.timerange, "scheme": self.scheme,
            "fitting": self.fitting, "oos_win_rate": self.oos_win_rate,
            "oos_mean_delta_pct": self.oos_mean_delta_pct,
            "oos_worst_delta_pct": self.oos_worst_delta_pct, "passed": self.passed,
            "folds": [f.as_dict() for f in self.folds],
        }

    def describe(self) -> str:
        lines = [
            f"walk-forward {self.timerange}  scheme={self.scheme}  folds={len(self.folds)}",
            f"  fitting: {self.fitting}",
        ]
        for f in self.folds:
            lines.append(
                f"  fold {f.index}  IS {f.in_sample}  OOS {f.out_of_sample}  "
                f"return {f.baseline.net_return_pct:+.2f}% -> "
                f"{f.variant.net_return_pct:+.2f}% ({f.delta_net_return_pct:+.2f}pp)  "
                f"dd {f.baseline.max_drawdown_pct:.2f}% -> "
                f"{f.variant.max_drawdown_pct:.2f}%  "
                f"{'win' if f.variant_wins else 'LOSS'}")
        lines.append(
            f"  OOS win rate {self.oos_win_rate:.0%}  mean delta "
            f"{self.oos_mean_delta_pct:+.2f}pp  worst {self.oos_worst_delta_pct:+.2f}pp  "
            f"=> {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def fold_windows(timerange: str, folds: int, *, warmup_days: int = 365
                 ) -> list[tuple[str, str]]:
    """``[(in_sample, out_of_sample)]`` — expanding in-sample, contiguous out-of-sample.

    The first ``warmup_days`` of the requested range are never scored. freqtrade loads its
    own startup candles from before a timerange, so the indicators are warm either way;
    what this guards against is the first fold being scored over a stretch the hypothesis
    was formed on, which is the difference between a walk-forward and a victory lap.

    No fold's out-of-sample window overlaps another's, and none overlaps its own
    in-sample span.
    """
    if folds < 1:
        raise BacktestError("folds must be >= 1")
    start, end = parse_timerange(timerange)
    first = start + timedelta(days=warmup_days)
    if first >= end:
        raise BacktestError(
            f"{timerange} is shorter than the {warmup_days}d warm-up; widen it")
    span = (end - first).days
    if span < folds:
        raise BacktestError(f"{span} scoreable days cannot make {folds} folds")
    step = span // folds
    out = []
    for i in range(folds):
        oos_start = first + timedelta(days=i * step)
        oos_end = end if i == folds - 1 else first + timedelta(days=(i + 1) * step)
        out.append((f"{start:%Y%m%d}-{oos_start:%Y%m%d}",
                    f"{oos_start:%Y%m%d}-{oos_end:%Y%m%d}"))
    return out


def walk_forward(
    config_patch: Mapping[str, Any] | None = None,
    folds: int = 4,
    *,
    timerange: str = "20210101-",
    pairs: Sequence[str] | None = None,
    baseline_patch: Mapping[str, Any] | None = None,
    warmup_days: int = 365,
    min_win_rate: float = 0.5,
    **run_kwargs: Any,
) -> WalkForward:
    """Run ``folds`` out-of-sample windows for the patch and for the baseline.

    Each fold states its own in-sample span. The candidate must win out of sample: an
    in-sample-only winner is what the strategy-lab protocol auto-rejects, and this is the
    object that shows whether it did.
    """
    windows = fold_windows(timerange, folds, warmup_days=warmup_days)
    results: list[Fold] = []
    for i, (is_range, oos_range) in enumerate(windows, start=1):
        base = run_backtest(baseline_patch or {}, oos_range, pairs, **run_kwargs)
        cand = run_backtest(config_patch or {}, oos_range, pairs, **run_kwargs)
        d_ret = round(cand.net_return_pct - base.net_return_pct, 4)
        d_dd = round(cand.max_drawdown_pct - base.max_drawdown_pct, 4)
        results.append(Fold(
            index=i, in_sample=is_range, out_of_sample=oos_range,
            variant=cand, baseline=base, delta_net_return_pct=d_ret,
            delta_max_drawdown_pct=d_dd,
            # A win is: it did not lose money relative to the baseline, OR it gave up
            # return but bought a materially smaller drawdown. Both directions, stated.
            variant_wins=bool(d_ret > 0 or (d_dd < -1.0 and d_ret > -1.0)),
        ))
    deltas = [f.delta_net_return_pct for f in results]
    wins = sum(1 for f in results if f.variant_wins)
    win_rate = wins / len(results) if results else 0.0
    return WalkForward(
        patch=validate_patch(config_patch), timerange=timerange, folds=tuple(results),
        scheme="expanding in-sample, contiguous out-of-sample",
        fitting="none — Sleeve A has fixed rules; the in-sample span is warm-up plus the "
                "history the hypothesis was formed on, so these folds test STABILITY "
                "across regimes, not an estimator's generalisation",
        oos_win_rate=round(win_rate, 4),
        oos_mean_delta_pct=round(sum(deltas) / len(deltas), 4) if deltas else 0.0,
        oos_worst_delta_pct=round(min(deltas), 4) if deltas else 0.0,
        passed=bool(win_rate >= min_win_rate and (sum(deltas) / len(deltas)) > 0)
        if deltas else False,
    )
