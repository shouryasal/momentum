"""Effective sample size, purged CV, the trial counter and the deflated hurdle.

Deterministic: every number comes from `runs.features.sampling`, computed on the local
feather candles. No model is involved and nothing here forecasts anything — this script
only answers whether a claim is supportable at the sample size that actually exists.

    python3 audit_stats.py labels  --pair BTC/USDT --tf 4h
    python3 audit_stats.py cv      --pair BTC/USDT --tf 4h --splits 5
    python3 audit_stats.py hurdle  --baseline 0.83 --trials 200 --years 9.1
    python3 audit_stats.py power   --sharpe-a 1.14 --sharpe-b 0.83
    python3 audit_stats.py trials  --add "ma lookback sweep 50..250"
    python3 audit_stats.py trials  --add "nightly screen: vol target 0.25" --screen
    python3 audit_stats.py --self-test

`labels`, `cv` and `hurdle` write their result into `knowledge/state/edge_audit.json`; the
trial counter lives in `knowledge/state/trial_counter.json`, every total in it only ever
grows, and the hurdle's N is the OPEN selection family rather than the all-time total —
see `trial_counter` and `../references/method.md` §3.

Definitions and the measured numbers behind the thresholds: `../references/method.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from runs.features import (  # noqa: E402
    iso,
    knowledge_dir,
    load_candles,
    read_json,
    utcnow,
    write_json_atomic,
)
from runs.features import sampling as smp  # noqa: E402


def _state_path(name: str) -> Path:
    return knowledge_dir() / "state" / name


def _merge_state(section: str, payload: dict) -> Path:
    path = _state_path("edge_audit.json")
    state = read_json(path, {})
    state.setdefault("sections", {})[section] = payload
    state["updated_utc"] = iso(utcnow())
    return write_json_atomic(path, state)


# --------------------------------------------------------------------------- labels


def labels_report(pair: str, timeframe: str, *, pt: float, sl: float,
                  max_bars: int) -> dict:
    """Triple-barrier labels and the effective sample size they actually carry."""
    candles = load_candles(pair, timeframe)
    lab = smp.triple_barrier(candles["close"], pt_mult=pt, sl_mult=sl, max_bars=max_bars)
    uniq = smp.average_uniqueness(lab)
    eff = smp.effective_n(lab)
    values, freq = np.unique(lab.label, return_counts=True)
    counts = {int(k): int(v) for k, v in zip(values, freq, strict=True)}
    return {
        "pair": pair, "timeframe": timeframe,
        "params": {"pt_mult": pt, "sl_mult": sl, "max_bars": max_bars},
        "bars": int(lab.n_bars), "rows": int(len(lab)),
        "mean_uniqueness": round(float(np.nanmean(uniq)), 4),
        "effective_n": round(float(eff), 1),
        "rows_per_effective_sample": round(len(lab) / eff, 2) if eff else None,
        "mean_span_bars": round(float(lab.span.mean()), 2),
        "max_concurrency": int(smp.concurrency(lab).max()),
        "label_mix": counts,
        "first_bar": candles["date"].iloc[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_bar": candles["date"].iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def cv_report(pair: str, timeframe: str, *, splits: int, embargo_pct: float,
              pt: float, sl: float, max_bars: int) -> dict:
    """Purged, embargoed folds — with a leakage assertion, not a leakage promise."""
    candles = load_candles(pair, timeframe)
    lab = smp.triple_barrier(candles["close"], pt_mult=pt, sl_mult=sl, max_bars=max_bars)
    folds = list(smp.purged_splits(lab, n_splits=splits, embargo_pct=embargo_pct))
    eff = smp.effective_n(lab)
    leaks = 0
    rows = []
    for s in folds:
        t_start = int(lab.t0[s.test].min())
        t_end = int(lab.t1[s.test].max())
        overlap = int(np.count_nonzero((lab.t1[s.train] >= t_start)
                                       & (lab.t0[s.train] <= t_end)))
        leaks += overlap
        rows.append({"fold": s.fold, "train": int(len(s.train)), "test": int(len(s.test)),
                     "purged": int(s.purged), "embargoed": int(s.embargoed),
                     "leaks": overlap})
    return {
        "pair": pair, "timeframe": timeframe, "splits": splits,
        "embargo_pct": embargo_pct, "folds": rows, "total_leaks": leaks,
        "effective_n": round(float(eff), 1),
        "effective_n_per_fold": round(float(eff) / max(splits, 1), 1),
        "verdict": "purged" if leaks == 0 else "LEAKING",
    }


# --------------------------------------------------------------------------- hurdle


def trial_counter(add: str | None = None, *, selection: bool = True) -> dict:
    """The persistent count of search trials. Every total in it only ever grows.

    The file is shared with `runs/discovery.py: TrialCounter`, which owns the shape and the
    reasoning; `../references/method.md` §3 states the rule. Three numbers, because they
    answer different questions:

    * `n_trials` — every measurement ever. The audit trail. Never a hurdle on its own.
    * `n_selection_trials` — all-time trials that *could* have produced a change.
    * `family.n_selection_trials` — **the hurdle's N**: the selection trials spent since
      the last change of the loop's own that the gate merged.

    `selection=False` records a screening trial — a measurement made by a pass that cannot
    propose. It still lands in `n_trials` for ever; it does not raise the hurdle, because
    the loop never took a maximum over trials no change could come out of.
    """
    path = _state_path("trial_counter.json")
    state = read_json(path, {"n_trials": 0, "history": []})
    n = max(int(state.get("n_trials", 0)), 0)
    state["n_trials"] = n
    # A file written before the split has no selection count: read it as equal to n_trials,
    # so the migration can only leave the hurdle where it was or above it.
    state["n_selection_trials"] = (max(int(state.get("n_selection_trials") or 0), 0)
                                   if "n_selection_trials" in state else n)
    fam = state.get("family")
    if not isinstance(fam, dict):
        fam = {"opened_utc": None, "closed_by": None,
               "n_selection_trials": state["n_selection_trials"]}
    fam["n_selection_trials"] = max(int(fam.get("n_selection_trials") or 0), 0)
    state["family"] = fam
    if add:
        state["n_trials"] = max(state["n_trials"] + 1, 1)
        if selection:
            state["n_selection_trials"] = max(state["n_selection_trials"] + 1, 1)
            fam["n_selection_trials"] += 1
            if not fam.get("opened_utc"):
                fam["opened_utc"] = iso(utcnow())
        state.setdefault("history", []).append(
            {"utc": iso(utcnow()), "what": add, "selection": bool(selection),
             "hypothesis": "", "pass": "edge-audit"})
        write_json_atomic(path, state)
    return state


def hurdle_trials(state: dict | None = None) -> int:
    """The N the hurdle is formed from: the OPEN selection family, never the all-time total."""
    st = state if state is not None else trial_counter()
    return int((st.get("family") or {}).get("n_selection_trials") or 0)


def hurdle_report(baseline: float, trials: int, years: float,
                  state: dict | None = None) -> dict:
    expected = smp.expected_max_sharpe(trials, years)
    st = state or {}
    return {
        "baseline_sharpe": round(float(baseline), 4),
        "n_trials": int(trials), "years": float(years),
        "n_selection_trials_all_time": int(st.get("n_selection_trials") or trials),
        "n_measurements_all_time": int(st.get("n_trials") or trials),
        "expected_max_sharpe": round(float(expected), 4),
        "deflated_hurdle": round(float(baseline + expected), 4),
        "note": ("a candidate must beat the deflated hurdle, not the baseline. N is the "
                 "OPEN selection family — the trials that could actually have produced a "
                 "change, spent since the last merged change of the loop's own. No total "
                 "in the counter ever decreases, and a family closes only behind a change "
                 "the gate recomputed and merged"),
    }


def power_report(sr_a: float, sr_b: float, power: float) -> dict:
    two = smp.years_to_detect(sr_a, sr_b, power=power, two_sided=True)
    one = smp.years_to_detect(sr_a, sr_b, power=power, two_sided=False)
    return {
        "sharpe_a": sr_a, "sharpe_b": sr_b, "power": power,
        "years_to_detect_two_sided": round(two, 1),
        "years_to_detect_one_sided": round(one, 1),
        "note": ("live testing proves the plumbing, not the edge; quote this beside any "
                 "claim that a good quarter validated a change"),
    }


# --------------------------------------------------------------------------- refusals


#: What a claim must carry before this skill will report it.
REQUIRED_CLAIM_KEYS = ("effective_n", "purged", "embargo_bars", "n_trials")


def check_claim(claim: dict) -> list[str]:
    """Return the reasons this claim may not be reported. Empty list = reportable.

    Three refusals, each earned by a specific way results go wrong here:

    * **a row count instead of an effective-N** — overlapping labels make ``n`` a lie, and
      on this repo's own 4h panel ``n`` overstates the independent sample by 6.6x;
    * **a CV score with no purge or embargo** — a training label that overlaps the test
      window has already seen it;
    * **beating the baseline instead of the deflated hurdle** — the expected best Sharpe
      from 200 zero-skill trials over 9.1 years is 1.19, so "better than baseline" is a
      coin flip with a decimal point.
    """
    problems: list[str] = []
    if "effective_n" not in claim:
        problems.append(
            "quotes no effective_n" + (f" (only n={claim['n']} rows)" if "n" in claim else "")
            + " — a row count is not a sample size when labels overlap")
    if not claim.get("purged"):
        problems.append("CV score was not purged — training labels may overlap the test window")
    if not claim.get("embargo_bars"):
        problems.append("CV score has no embargo — serial correlation leaks across the boundary")
    if "sharpe" in claim:
        # A claim written under the split accounting says which N it means. Prefer it: the
        # hurdle is formed from the trials that could actually have produced a change, and
        # scoring such a claim against its all-time measurement count over-corrects it.
        trials = int(claim.get("n_selection_trials") or claim.get("n_trials") or 0)
        years = float(claim.get("years") or 0)
        if trials < 1 or years <= 0:
            problems.append("no trial count or sample length, so no deflated hurdle can be formed")
        else:
            hurdle = float(claim.get("baseline_sharpe", 0.0)) + smp.expected_max_sharpe(
                trials, years)
            if float(claim["sharpe"]) <= hurdle:
                problems.append(
                    f"sharpe {float(claim['sharpe']):.3f} does not clear the deflated hurdle "
                    f"{hurdle:.3f} (N={trials}, T={years})")
    return problems


# --------------------------------------------------------------------------- self-test


def self_test() -> int:
    """Pinned values that need no candle store, no network and no config."""
    ok = True
    hurdles = {n: smp.expected_max_sharpe(n, 9.1) for n in (10, 50, 200, 1000)}
    for n, want in ((10, 0.862), (50, 1.050), (200, 1.187), (1000, 1.329)):
        got = hurdles[n]
        hit = abs(got - want) < 0.005
        ok &= hit
        print(f"expected_max_sharpe(N={n}, T=9.1) = {got:.3f} (want {want}) "
              f"{'OK' if hit else 'FAIL'}")
    years = smp.years_to_detect(1.14, 0.83)
    print(f"years_to_detect(1.14, 0.83) = {years:.1f}")
    ok &= years > 100

    # A deterministic panel with a known overlap: 200 bars, every label 10 bars long.
    closes = np.exp(np.cumsum(np.full(400, 0.001)))
    lab = smp.triple_barrier(closes, pt_mult=2.0, sl_mult=1.0, max_bars=10,
                             sigma=np.full(400, 0.02))
    eff = smp.effective_n(lab)
    uniq = float(np.nanmean(smp.average_uniqueness(lab)))
    print(f"synthetic labels rows={len(lab)} mean_uniqueness={uniq:.4f} "
          f"effective_n={eff:.1f}")
    # 10-bar labels on consecutive bars overlap heavily, so the row count must overstate
    ok = bool(ok) and len(lab) > eff * 5

    folds = list(smp.purged_splits(lab, n_splits=5, embargo_bars=5))
    leaks = 0
    for s in folds:
        t_start, t_end = lab.t0[s.test].min(), lab.t1[s.test].max()
        leaks += int(np.count_nonzero((lab.t1[s.train] >= t_start)
                                      & (lab.t0[s.train] <= t_end)))
    print(f"purged 5-fold: folds={len(folds)} leaks={leaks} "
          f"purged={sum(s.purged for s in folds)} embargoed={sum(s.embargoed for s in folds)}")
    ok = ok and leaks == 0 and sum(s.purged for s in folds) > 0
    print("no_leakage=True" if leaks == 0 else "no_leakage=False")

    # The refusals, exercised rather than described.
    bad = check_claim({"n": 19879, "sharpe": 1.15, "baseline_sharpe": 0.83,
                       "n_trials": 200, "years": 9.1})
    good = check_claim({"effective_n": 3020.5, "purged": True, "embargo_bars": 199,
                        "sharpe": 2.40, "baseline_sharpe": 0.83, "n_trials": 200,
                        "years": 9.1})
    print(f"refusals_on_a_bad_claim={len(bad)} refusals_on_a_good_claim={len(good)}")
    ok = ok and len(bad) >= 3 and len(good) == 0
    print("self_test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("labels", help="triple-barrier labels and EFFECTIVE_N")
    p.add_argument("--pair", default="BTC/USDT")
    p.add_argument("--tf", default="4h")
    p.add_argument("--pt", type=float, default=2.0)
    p.add_argument("--sl", type=float, default=1.0)
    p.add_argument("--bars", type=int, default=30)

    p = sub.add_parser("cv", help="purged + embargoed folds, with a leakage check")
    p.add_argument("--pair", default="BTC/USDT")
    p.add_argument("--tf", default="4h")
    p.add_argument("--splits", type=int, default=5)
    p.add_argument("--embargo-pct", type=float, default=0.01)
    p.add_argument("--pt", type=float, default=2.0)
    p.add_argument("--sl", type=float, default=1.0)
    p.add_argument("--bars", type=int, default=30)

    p = sub.add_parser("hurdle", help="the deflated Sharpe hurdle")
    p.add_argument("--baseline", type=float, required=True)
    p.add_argument("--trials", type=int)
    p.add_argument("--years", type=float, default=9.1)

    p = sub.add_parser("power", help="years of live data needed to tell two Sharpes apart")
    p.add_argument("--sharpe-a", type=float, required=True)
    p.add_argument("--sharpe-b", type=float, required=True)
    p.add_argument("--power", type=float, default=0.80)

    p = sub.add_parser("trials", help="read or increment the persistent trial counter")
    p.add_argument("--add", help="describe the search that was run")
    p.add_argument("--screen", action="store_true",
                   help="this search could not have produced a change (a screening pass): "
                        "count it in n_trials for ever, but not in the hurdle's N")

    p = sub.add_parser("check", help="may this claim be reported at all?")
    p.add_argument("--claim", required=True, help="path to a JSON claim")

    args = ap.parse_args(argv)
    if args.self_test or args.cmd is None:
        return self_test()

    if args.cmd == "labels":
        out = labels_report(args.pair, args.tf, pt=args.pt, sl=args.sl,
                            max_bars=args.bars)
        _merge_state("labels", out)
        print(f"rows={out['rows']} mean_uniqueness={out['mean_uniqueness']} "
              f"EFFECTIVE_N={out['effective_n']} "
              f"({out['rows_per_effective_sample']} rows per independent sample)")
    elif args.cmd == "cv":
        out = cv_report(args.pair, args.tf, splits=args.splits,
                        embargo_pct=args.embargo_pct, pt=args.pt, sl=args.sl,
                        max_bars=args.bars)
        _merge_state("cv", out)
        print(f"verdict={out['verdict']} total_leaks={out['total_leaks']} "
              f"EFFECTIVE_N={out['effective_n']} per_fold={out['effective_n_per_fold']}")
    elif args.cmd == "hurdle":
        state = trial_counter()
        trials = args.trials if args.trials is not None else hurdle_trials(state) or 1
        out = hurdle_report(args.baseline, trials, args.years, state)
        _merge_state("hurdle", out)
        print(f"n_trials={out['n_trials']} (open selection family; "
              f"{out['n_selection_trials_all_time']} selection trials all time, "
              f"{out['n_measurements_all_time']} measurements all time) "
              f"expected_max_sharpe={out['expected_max_sharpe']} "
              f"deflated_hurdle={out['deflated_hurdle']}")
    elif args.cmd == "power":
        out = power_report(args.sharpe_a, args.sharpe_b, args.power)
        _merge_state("power", out)
        print(f"years_to_detect={out['years_to_detect_two_sided']} (two-sided), "
              f"{out['years_to_detect_one_sided']} (one-sided)")
    elif args.cmd == "trials":
        out = trial_counter(args.add, selection=not args.screen)
        print(f"hurdle_N={hurdle_trials(out)} "
              f"n_selection_trials={out.get('n_selection_trials', 0)} "
              f"n_trials={out.get('n_trials', 0)}")
    elif args.cmd == "check":
        claim = read_json(Path(args.claim), {})
        problems = check_claim(claim)
        out = {"claim": claim, "problems": problems,
               "verdict": "reportable" if not problems else "REFUSED"}
        for line in problems:
            print(f"REFUSED: {line}")
        print(f"verdict={out['verdict']}")
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if not problems else 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
