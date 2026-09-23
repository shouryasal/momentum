"""The one command a research run uses to measure something. TIER 2 (scripts are human-only).

An in-product agent cannot be expected to know docker invocations, freqtrade flags or
feather layouts, and it should not have to. This wraps :mod:`evals.backtest_api`,
:mod:`evals.hypothesis` and :mod:`runs.features.series` behind subcommands that print JSON
(``--json``) or a readable block, and it is the only entry point the strategy-lab skill's
``allowed-tools`` permits.

    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py namespaces
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py backtest \
        --timerange 20210101-20260901
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py compare \
        --patch '{"trading.take_profit.roi_table": {"0": 0.35}}' \
        --timerange 20210101-20260901
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py walk-forward \
        --patch @plan.json --folds 4 --timerange 20210101-20260901
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py hypothesis record --file h.json
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py hypothesis grade <id> \
        --baseline base.json --measured cand.json
    python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py study \
        --pairs BTC/USDT,ETH/USDT,SOL/USDT --feature run_up:30 --target fwd_return:30

The order the loop is meant to run in, and which this script enforces by what it refuses:
``hypothesis record`` FIRST, then ``compare`` or ``walk-forward``, then
``hypothesis grade``. ``grade`` will not touch a hypothesis that was not recorded, and
``record`` will not overwrite one that was.

This script starts docker (that is its job) but opens no socket of its own, writes nothing
outside ``ft_userdata/research/`` and ``knowledge/research/``, and never touches
``config/``: a patch is applied to a copy. A patch naming a risk limit, the universe,
capital, mode or credentials is refused with an error rather than ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import backtest_api as api  # noqa: E402
from evals import hypothesis as hyp  # noqa: E402


def _json_arg(raw: str | None) -> Any:
    """Inline JSON, or ``@path`` / a plain path to a JSON file. ``None`` -> ``{}``."""
    if not raw:
        return {}
    text = raw[1:] if raw.startswith("@") else raw
    p = Path(text)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(raw)


def _pairs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _emit(payload: dict[str, Any], text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str) if as_json else text)


# --------------------------------------------------------------------------- commands


def cmd_namespaces(args: argparse.Namespace) -> int:
    payload = {
        "allowed": api.ALLOWED_NAMESPACES,
        "denied": api.DENIED_KEYS,
        "pairs": "narrows the configured whitelist to measure a subset; never adds to it",
        "costs": "always applied, from config/backtest.yaml (fee_bps + slippage_bps)",
    }
    lines = ["a research patch may touch ONLY these namespaces:"]
    for ns, where in sorted(api.ALLOWED_NAMESPACES.items()):
        lines.append(f"  {ns + '.<key>':<20} -> {where}")
    lines.append("")
    lines.append("refused, with the reason printed on the error:")
    for key, why in sorted(api.DENIED_KEYS.items()):
        lines.append(f"  {key:<24} {why}")
    _emit(payload, "\n".join(lines), args.json)
    return 0


def _run(args: argparse.Namespace, patch: Any) -> api.Metrics:
    return api.run_backtest(
        patch, args.timerange, _pairs(args.pairs), sleeve=args.sleeve,
        strategy=args.strategy, timeout_s=args.timeout,
        reuse=not args.no_cache, root=REPO_ROOT,
    )


def cmd_backtest(args: argparse.Namespace) -> int:
    result = _run(args, _json_arg(args.patch))
    _emit(result.as_dict(curve=args.curve), result.describe(), args.json)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    baseline = _run(args, _json_arg(args.baseline_patch))
    variant = _run(args, _json_arg(args.patch))
    cmp = api.compare(variant, baseline, benchmark_pair=args.benchmark, root=REPO_ROOT)
    payload = {"baseline": baseline.as_dict(), "variant": variant.as_dict(),
               "comparison": cmp.as_dict()}
    text = "\n\n".join([baseline.describe(), variant.describe(), cmp.describe()])
    _emit(payload, text, args.json)
    return 0


def cmd_walk_forward(args: argparse.Namespace) -> int:
    wf = api.walk_forward(
        _json_arg(args.patch), args.folds, timerange=args.timerange,
        pairs=_pairs(args.pairs), baseline_patch=_json_arg(args.baseline_patch),
        warmup_days=args.warmup_days, sleeve=args.sleeve, strategy=args.strategy,
        timeout_s=args.timeout, reuse=not args.no_cache, root=REPO_ROOT,
    )
    _emit(wf.as_dict(), wf.describe(), args.json)
    return 0 if wf.passed else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    b = api.buy_and_hold(args.pair, args.start, args.end, root=REPO_ROOT)
    text = (f"buy and hold {b.pair} {b.start[:10]} -> {b.end[:10]}: "
            f"{b.net_return_pct:+.2f}% (CAGR {b.cagr_pct:+.2f}%), "
            f"max dd {b.max_drawdown_pct:.2f}%, Sharpe {b.sharpe_daily:.3f} "
            f"[costed: {b.fee_bps}+{b.slippage_bps} bps in and out]")
    _emit(b.as_dict(), text, args.json)
    return 0


def cmd_hypothesis(args: argparse.Namespace) -> int:
    ledger = hyp.Ledger(REPO_ROOT)
    if args.sub == "record":
        h = hyp.Hypothesis.from_dict(_json_arg(args.file))
        path = ledger.record(h)
        _emit({"recorded": str(path), "id": h.id, "digest": h.digest()},
              f"recorded {h.id} -> {path}\n{h.describe()}", args.json)
        return 0
    if args.sub == "show":
        h = ledger.load(args.id)
        _emit(h.as_dict(), h.describe(), args.json)
        return 0
    if args.sub == "list":
        rows = ledger.listing()
        text = "\n".join(f"{r['verdict']:<13}{r['id']}  {r['statement']}" for r in rows) \
            or "no hypotheses recorded yet"
        _emit({"hypotheses": rows}, text, args.json)
        return 0
    if args.sub == "grade":
        baseline = _json_arg(args.baseline)
        measured = _json_arg(args.measured)
        grade = ledger.grade(args.id, _headline(baseline), _headline(measured),
                             evidence=_evidence(baseline, measured), note=args.note)
        _emit(grade.as_dict(), grade.describe(), args.json)
        return 0 if grade.verdict in ("supported", "mixed") else 1
    raise SystemExit(f"unknown hypothesis subcommand {args.sub!r}")


def _headline(doc: Any) -> dict[str, float]:
    """Accept a full ``Metrics.as_dict()`` or a bare metric map."""
    if not isinstance(doc, dict):
        raise SystemExit("expected a JSON object of metrics")
    src = doc.get("variant") if "variant" in doc and isinstance(doc["variant"], dict) else doc
    return {k: float(v) for k, v in src.items() if isinstance(v, int | float)}


def _evidence(baseline: Any, measured: Any) -> dict[str, Any]:
    keep = ("run_id", "digest", "timerange", "start", "end", "fee_bps", "slippage_bps")
    return {
        "baseline": {k: baseline.get(k) for k in keep if isinstance(baseline, dict)},
        "measured": {k: measured.get(k) for k in keep if isinstance(measured, dict)},
    }


def cmd_study(args: argparse.Namespace) -> int:
    from runs.features import series  # noqa: PLC0415 - heavy import, only this path needs it

    pairs = _pairs(args.pairs) or []
    p = series.panel(pairs, args.timeframe, start=args.start, end=args.end)
    feature = _resolve(series, p, args.feature)
    target = _resolve(series, p, args.target)
    blocks = [p.describe()]
    payload: dict[str, Any] = {"coverage": [c.as_dict() for c in p.coverage],
                               "missing": list(p.missing)}
    table = series.conditional_table(feature, target, buckets=args.buckets,
                                     feature_name=args.feature, target_name=args.target)
    blocks.append(table.describe())
    payload["table"] = table.as_dict()
    if args.cut is not None:
        effect = series.cut_effect(feature, target, threshold=args.cut, side=args.side,
                                   rule_name=f"{args.feature} {args.side} {args.cut}")
        blocks.append(effect.describe())
        payload["cut"] = effect.as_dict()
    _emit(payload, "\n\n".join(blocks), args.json)
    return 0


def _resolve(series: Any, p: Any, spec: str) -> Any:
    """``run_up:30`` / ``fwd_return:30`` / ``dd_from_ath`` / ``rs_vs_btc:56`` -> a frame."""
    name, _, arg = spec.partition(":")
    n = int(arg) if arg else 0
    if name == "run_up":
        return series.trailing_return(p.close, n)
    if name == "fwd_return":
        return series.forward_return(p.close, n)
    if name == "fwd_drawdown":
        return series.forward_drawdown(p.close, n)
    if name == "dd_from_ath":
        return series.drawdown_from_ath(p.close)
    if name == "days_since_ath":
        return series.days_since_ath(p.close, bars_per_day=p.bars_per_day)
    if name == "rs_vs_btc":
        return series.relative_strength(p.close, "BTC/USDT", n)
    if name == "vol":
        return series.rolling_vol(p.close, n)
    if name == "volume_trend":
        return series.volume_trend(p.volume, short=max(n // 10, 2) or 7, long=n or 90)
    if name == "listing_age":
        return series.listing_age_days(p.close)
    raise SystemExit(f"unknown series {spec!r}; see --help for the list")


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="research_api", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    # `--json` is accepted on either side of the subcommand: an automated caller should not
    # have to remember argparse's ordering rule to get machine-readable output. SUPPRESS
    # keeps the subparser from resetting a flag the top level already set.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    def add_run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--timerange", default="20210101-")
        p.add_argument("--pairs", default=None, help="comma-separated SUBSET of the universe")
        p.add_argument("--sleeve", default="a", choices=["a", "b"])
        p.add_argument("--strategy", default=None)
        p.add_argument("--timeout", type=float, default=api.DEFAULT_TIMEOUT_S)
        p.add_argument("--no-cache", action="store_true",
                       help="re-run even when an identical run is on disk")

    add("namespaces", "what a patch may and may not touch")

    bt = add("backtest", "one costed backtest")
    bt.add_argument("--patch", default=None, help="inline JSON or @file; empty = baseline")
    bt.add_argument("--curve", action="store_true", help="include the equity curve")
    add_run_args(bt)

    cp = add("compare", "variant vs baseline, both directions, vs BTC")
    cp.add_argument("--patch", required=True)
    cp.add_argument("--baseline-patch", default=None)
    cp.add_argument("--benchmark", default="BTC/USDT")
    add_run_args(cp)

    wf = add("walk-forward", "out-of-sample folds, split stated")
    wf.add_argument("--patch", required=True)
    wf.add_argument("--baseline-patch", default=None)
    wf.add_argument("--folds", type=int, default=4)
    wf.add_argument("--warmup-days", type=int, default=365)
    add_run_args(wf)

    bm = add("benchmark", "costed buy-and-hold")
    bm.add_argument("--pair", default="BTC/USDT")
    bm.add_argument("--start", required=True)
    bm.add_argument("--end", required=True)

    hp = add("hypothesis", "record a prediction, then grade it")
    hp.add_argument("sub", choices=["record", "show", "list", "grade"])
    hp.add_argument("id", nargs="?", default=None)
    hp.add_argument("--file", default=None, help="record: the hypothesis JSON")
    hp.add_argument("--baseline", default=None, help="grade: baseline metrics JSON")
    hp.add_argument("--measured", default=None, help="grade: measured metrics JSON")
    hp.add_argument("--note", default="")

    st = add("study", "condition a target on a feature over real series")
    st.add_argument("--pairs", required=True)
    st.add_argument("--timeframe", default="1d")
    st.add_argument("--start", default=None)
    st.add_argument("--end", default=None)
    st.add_argument("--feature", default="run_up:30",
                    help="run_up:N | dd_from_ath | days_since_ath | rs_vs_btc:N | vol:N "
                         "| volume_trend:N | listing_age")
    st.add_argument("--target", default="fwd_return:30",
                    help="fwd_return:N | fwd_drawdown:N")
    st.add_argument("--buckets", type=int, default=5)
    st.add_argument("--cut", type=float, default=None, help="also score this threshold")
    st.add_argument("--side", default="below", choices=["below", "above"])
    return ap


HANDLERS = {
    "namespaces": cmd_namespaces, "backtest": cmd_backtest, "compare": cmd_compare,
    "walk-forward": cmd_walk_forward, "benchmark": cmd_benchmark,
    "hypothesis": cmd_hypothesis, "study": cmd_study,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return HANDLERS[args.cmd](args)
    except api.PatchRefused as exc:
        print(f"PATCH REFUSED\n{exc}", file=sys.stderr)
        return 3
    except hyp.HypothesisError as exc:
        print(f"HYPOTHESIS ERROR\n{exc}", file=sys.stderr)
        return 4
    except api.BacktestError as exc:
        print(f"MEASUREMENT FAILED\n{exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
