"""Pre-registration and close-out audit for one hypothesis. Deterministic, no model.

TIER 2. A human writes this file, because it is the half of the discipline a model must not
be able to relax: the seal, the parameter cap, the required baselines and the refusal to
record `supported` on a study that failed its own checks.

    python3 hypothesis.py open  --id 2026-09-23-funding-stop-width \
        --statement "forecast 7d drawdown predicts the stop width that survives" \
        --falsifier "top-bucket forward drawdown within 3pp of the base rate" \
        --params 2 --regimes "bull,bear" --horizon 7d --costs-bps 10 \
        --surface local-candles+derivatives
    python3 hypothesis.py show  --id <id>
    python3 hypothesis.py list
    python3 hypothesis.py close --id <id> --outcome refuted --result result.json
    python3 hypothesis.py --self-test

Everything that decides an outcome is a pure function over dicts
(:func:`preregister`, :func:`seal_of`, :func:`audit_result`) so ``--self-test`` exercises
the real rules without touching the filesystem.

The statistics themselves are NOT here. Effective sample size, purged cross-validation and
the deflated-Sharpe hurdle belong to the edge-audit skill, and this script only checks that
its numbers were supplied and respected. See ../references/protocol.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: At most this many free parameters in one hypothesis. Four is already generous: the
#: expected best in-sample Sharpe from a wide search is what the deflated hurdle exists to
#: punish, and every extra knob multiplies the search.
MAX_PARAMS = 4

#: Both of them, every time. Quoting one is how a candidate that loses to buy-and-hold gets
#: sold on beating the shipped strategy.
REQUIRED_BASELINES = ("btc_buy_and_hold", "strategy_baseline")

#: The minimum regime split. One number over one regime is a regime bet sold as an edge.
MIN_REGIMES = 2

OUTCOMES = ("supported", "refuted", "inconclusive")

#: The fields the seal covers. Changing any of them after measurement invalidates the study.
SEALED_FIELDS = ("id", "statement", "falsifier", "horizon", "n_params", "regimes",
                 "costs_bps", "surface", "baselines", "opened_utc")

STATE_DIRNAME = "hypotheses"


# --------------------------------------------------------------------------- seal


def seal_of(pre: dict) -> str:
    """SHA-256 over the canonical pre-registration block.

    The seal is the mechanism the whole skill rests on. A falsifier that can be edited once
    the result is known is not a falsifier, and no amount of procedure text prevents the
    edit — only recomputing this hash does.
    """
    payload = {k: pre.get(k) for k in SEALED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_seal(record: dict) -> bool:
    return bool(record.get("seal")) and seal_of(record) == record["seal"]


# --------------------------------------------------------------------------- open


class PreRegistrationError(ValueError):
    """The pre-registration itself is not admissible."""


def preregister(*, hid: str, statement: str, falsifier: str, horizon: str,
                n_params: int, regimes: list[str], costs_bps: float, surface: str,
                baselines: list[str] | None = None, opened_utc: str = "") -> dict:
    """Build and seal one pre-registration. Raises rather than returning a bad one."""
    problems = []
    if not str(hid).strip():
        problems.append("id is empty")
    if len(str(statement).strip()) < 20:
        problems.append("statement must say '<observable> predicts <target> at <horizon>'")
    falsifier = str(falsifier).strip()
    if len(falsifier) < 20:
        problems.append("no falsifier: write the specific result that would make you "
                        "abandon this, as a number")
    elif not any(ch.isdigit() for ch in falsifier):
        problems.append("the falsifier names no number — 'if it does not work' is not "
                        "falsifiable and cannot be checked from the same data")
    if int(n_params) > MAX_PARAMS:
        problems.append(f"{n_params} free parameters exceeds the cap of {MAX_PARAMS}")
    if int(n_params) < 0:
        problems.append("n_params cannot be negative")
    regimes = [r.strip() for r in (regimes or []) if str(r).strip()]
    if len(regimes) < MIN_REGIMES:
        problems.append(f"declare at least {MIN_REGIMES} regimes to split by; one number "
                        "over one regime is a regime bet sold as an edge")
    if float(costs_bps) <= 0:
        problems.append("costs must be on and measured: a zero-cost result is not a "
                        "preliminary result, it is not a result")
    if not str(surface).strip():
        problems.append("name the surface the data comes from (research-scout: surfaces)")
    if problems:
        raise PreRegistrationError("; ".join(problems))

    pre = {
        "id": str(hid).strip(),
        "statement": str(statement).strip(),
        "falsifier": falsifier,
        "horizon": str(horizon).strip(),
        "n_params": int(n_params),
        "regimes": regimes,
        "costs_bps": float(costs_bps),
        "surface": str(surface).strip(),
        "baselines": list(baselines or REQUIRED_BASELINES),
        "opened_utc": str(opened_utc),
        "status": "preregistered",
    }
    pre["seal"] = seal_of(pre)
    return pre


# --------------------------------------------------------------------------- close


def audit_result(record: dict, result: dict) -> list[str]:
    """Every reason this result may not be recorded as ``supported``.

    Returns the problems in the order they are worth fixing. An empty list means the study
    followed the procedure it pre-registered — not that the hypothesis is true, which is
    what the numbers say.
    """
    problems: list[str] = []

    if not verify_seal(record):
        problems.append(
            "SEAL MISMATCH: the pre-registration was edited after the result was known. "
            "The falsifier is no longer a falsifier; re-run from a fresh pre-registration.")

    costs = result.get("costs_bps")
    if costs is None:
        problems.append("the result does not say what costs were applied")
    elif float(costs) <= 0:
        problems.append("costs were off; a zero-cost result is not a result")
    elif record.get("costs_bps") and float(costs) < float(record["costs_bps"]):
        problems.append(
            f"costs were lowered after pre-registration ({record['costs_bps']} -> {costs} "
            "bps)")

    baselines = result.get("baselines") or {}
    missing = [b for b in REQUIRED_BASELINES if b not in baselines]
    if missing:
        problems.append(
            f"missing baseline(s) {missing}: beating the shipped strategy while losing to "
            "buy-and-hold is the most common shape of a bad proposal here, and one number "
            "hides it")

    oos = result.get("out_of_sample") or {}
    if not oos:
        problems.append("no out-of-sample block: the in-sample number is a diagnostic, "
                        "never the headline")
    elif result.get("headline_is_in_sample"):
        problems.append("the headline is an in-sample number; walk it forward")

    regimes = result.get("regimes") or {}
    if len(regimes) < MIN_REGIMES:
        problems.append(f"the result is not split by regime (need >= {MIN_REGIMES})")
    else:
        thin = [name for name, blk in regimes.items()
                if int((blk or {}).get("n", 0)) < 30]
        if thin:
            problems.append(f"regime(s) {sorted(thin)} have fewer than 30 observations; "
                            "report n beside the number rather than the number alone")

    used = result.get("n_params")
    if used is not None and int(used) > int(record.get("n_params", MAX_PARAMS)):
        problems.append(
            f"{used} parameters were used against {record.get('n_params')} pre-registered: "
            "knobs added after the fact are search, and search is what the hurdle punishes")

    audit = result.get("edge_audit") or {}
    if "effective_n" not in audit:
        problems.append("no effective_n from edge-audit: a row count is not a sample size")
    if "deflated_hurdle" not in audit:
        problems.append("no deflated_hurdle from edge-audit: beating the baseline is not "
                        "the bar, and the trial counter only grows")

    if not result.get("both_directions"):
        problems.append("report BOTH directions of the rule: what it avoided and what it "
                        "gave up. A filter that dodges the crashes by also dodging the "
                        "rallies is worthless and one headline number hides it")

    trig = result.get("falsifier_triggered")
    if trig is None:
        problems.append("the result does not say whether the falsifier triggered")
    if result.get("falsifier_value") is None:
        problems.append("the result does not carry the measured value the falsifier was "
                        "written against")

    return problems


def close(record: dict, result: dict, outcome: str) -> dict:
    """Apply the audit and return the closed record. ``supported`` must earn itself."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    problems = audit_result(record, result)
    headline = result.get("headline")
    hurdle = (result.get("edge_audit") or {}).get("deflated_hurdle")
    final = outcome

    if outcome == "supported":
        if problems:
            final = "inconclusive"
        elif result.get("falsifier_triggered"):
            final = "refuted"
        elif headline is None or hurdle is None:
            final = "inconclusive"
            problems.append("no headline or no hurdle to compare it against")
        elif float(headline) <= float(hurdle):
            final = "inconclusive"
            problems.append(
                f"headline {headline} does not clear the deflated hurdle {hurdle}; "
                "beating the baseline is not the bar")

    closed = dict(record)
    closed["result"] = result
    closed["audit_problems"] = problems
    closed["claimed_outcome"] = outcome
    closed["outcome"] = final
    closed["status"] = "closed"
    closed["seal_ok"] = verify_seal(record)
    return closed


# --------------------------------------------------------------------------- storage


def _store_dir():
    from runs.features import knowledge_dir

    return knowledge_dir() / "state" / STATE_DIRNAME


def _read(hid: str) -> dict:
    from runs.features import read_json

    path = _store_dir() / f"{hid}.json"
    record = read_json(path, {})
    if not record:
        raise FileNotFoundError(f"no pre-registration for {hid!r} at {path}")
    return record


def _write(record: dict):
    from runs.features import write_json_atomic

    return write_json_atomic(_store_dir() / f"{record['id']}.json", record)


def _now_iso() -> str:
    """UTC ISO-8601 with `Z`, stdlib only.

    Deliberately not routed through ``runs.features``: a REFUSED pre-registration must not
    depend on pandas being importable, or the refusal path stops being testable in the
    contained eval sandbox.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- self-test


def _good_result() -> dict:
    return {
        "costs_bps": 10.0,
        "headline": 1.42,
        "headline_is_in_sample": False,
        "baselines": {"btc_buy_and_hold": 0.83, "strategy_baseline": 0.61},
        "out_of_sample": {"folds": 5, "sharpe": 1.42},
        "regimes": {"bull": {"n": 420, "sharpe": 1.5}, "bear": {"n": 310, "sharpe": 1.2}},
        "n_params": 2,
        "edge_audit": {"effective_n": 3020, "deflated_hurdle": 1.19},
        "both_directions": {"avoided": "-8% tail days", "gave_up": "12% of up days"},
        "falsifier_triggered": False,
        "falsifier_value": 9.4,
    }


def _good_record() -> dict:
    return preregister(
        hid="2026-09-23-selftest",
        statement="forecast 7d drawdown predicts the stop width that survives",
        falsifier="top-bucket forward drawdown lands within 3pp of the base rate",
        horizon="7d", n_params=2, regimes=["bull", "bear"], costs_bps=10.0,
        surface="local-candles", opened_utc="2026-09-23T00:00:00Z")


def self_test() -> int:
    """Pinned behaviour that needs no candle store, no network and no config."""
    ok = True

    rec = _good_record()
    print(f"preregistered id={rec['id']} seal_len={len(rec['seal'])}")
    ok &= verify_seal(rec)
    print(f"seal_verifies={str(verify_seal(rec)).lower()}")

    # A falsifier with no number is not a falsifier.
    for bad, label in (
        (dict(falsifier="if it does not work we drop it"), "vague_falsifier"),
        (dict(n_params=9), "too_many_params"),
        (dict(regimes=["bull"]), "one_regime"),
        (dict(costs_bps=0.0), "costs_off"),
    ):
        kw = {"hid": "x", "statement": "a predicts b at a 7 day horizon",
              "falsifier": "the top bucket lands within 3pp of the base rate",
              "horizon": "7d", "n_params": 2, "regimes": ["bull", "bear"],
              "costs_bps": 10.0, "surface": "local-candles"}
        kw.update(bad)
        try:
            preregister(**kw)
        except PreRegistrationError as exc:
            print(f"refused_{label}=True ({str(exc)[:60]}...)")
        else:
            print(f"refused_{label}=False")
            ok = False

    clean = audit_result(rec, _good_result())
    print(f"audit_problems_on_a_clean_study={len(clean)}")
    ok &= clean == []

    closed = close(rec, _good_result(), "supported")
    print(f"clean_study_outcome={closed['outcome']}")
    ok &= closed["outcome"] == "supported"

    # The seal: edit the falsifier after the fact and the study dies.
    tampered = dict(rec)
    tampered["falsifier"] = "the top bucket lands within 30pp of the base rate"
    problems = audit_result(tampered, _good_result())
    print(f"seal_mismatch_detected={str(any('SEAL MISMATCH' in p for p in problems)).lower()}")
    ok &= any("SEAL MISMATCH" in p for p in problems)
    ok &= close(tampered, _good_result(), "supported")["outcome"] == "inconclusive"

    # One baseline is the most common way a bad proposal gets sold.
    one_baseline = _good_result()
    one_baseline["baselines"] = {"strategy_baseline": 0.61}
    problems = audit_result(rec, one_baseline)
    print(f"one_baseline_refused={str(any('missing baseline' in p for p in problems)).lower()}")
    ok &= any("missing baseline" in p for p in problems)

    # Costs off.
    free_lunch = _good_result()
    free_lunch["costs_bps"] = 0.0
    ok &= any("costs were off" in p for p in audit_result(rec, free_lunch))
    print("costs_off_refused=true")

    # In-sample headline.
    in_sample = _good_result()
    in_sample["headline_is_in_sample"] = True
    ok &= any("in-sample" in p for p in audit_result(rec, in_sample))
    print("in_sample_headline_refused=true")

    # Beating the baseline is not the bar.
    weak = _good_result()
    weak["headline"] = 0.95
    weak_closed = close(rec, weak, "supported")
    print(f"below_hurdle_outcome={weak_closed['outcome']}")
    ok &= weak_closed["outcome"] == "inconclusive"

    # A triggered falsifier means refuted, whatever the author claimed.
    triggered = _good_result()
    triggered["falsifier_triggered"] = True
    triggered["falsifier_value"] = 1.2
    fired = close(rec, triggered, "supported")
    print(f"triggered_falsifier_outcome={fired['outcome']}")
    ok &= fired["outcome"] == "refuted"

    # One direction only.
    one_way = _good_result()
    one_way["both_directions"] = None
    ok &= any("BOTH directions" in p for p in audit_result(rec, one_way))
    print("one_direction_refused=true")

    # A row count is not a sample size.
    no_n = _good_result()
    no_n["edge_audit"] = {"deflated_hurdle": 1.19}
    ok &= any("effective_n" in p for p in audit_result(rec, no_n))
    print("row_count_refused=true")

    # An honest negative closes cleanly.
    negative = close(rec, triggered, "refuted")
    print(f"honest_negative_outcome={negative['outcome']}")
    ok &= negative["outcome"] == "refuted"

    print("self_test: PASS" if ok else "self_test: FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-register a hypothesis, then close it honestly.")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    op = sub.add_parser("open", help="seal a pre-registration before measuring anything")
    op.add_argument("--id", required=True)
    op.add_argument("--statement", required=True)
    op.add_argument("--falsifier", required=True)
    op.add_argument("--horizon", default="")
    op.add_argument("--params", type=int, required=True)
    op.add_argument("--regimes", default="")
    op.add_argument("--costs-bps", type=float, required=True)
    op.add_argument("--surface", required=True)

    sh = sub.add_parser("show", help="print one hypothesis and whether its seal still holds")
    sh.add_argument("--id", required=True)

    sub.add_parser("list", help="every hypothesis on record")

    cl = sub.add_parser("close", help="audit a result and record the outcome")
    cl.add_argument("--id", required=True)
    cl.add_argument("--outcome", required=True, choices=list(OUTCOMES))
    cl.add_argument("--result", required=True, help="path to the result JSON")

    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.cmd == "open":
        try:
            rec = preregister(
                hid=args.id, statement=args.statement, falsifier=args.falsifier,
                horizon=args.horizon, n_params=args.params,
                regimes=[r for r in args.regimes.split(",") if r.strip()],
                costs_bps=args.costs_bps, surface=args.surface,
                opened_utc=_now_iso())
        except PreRegistrationError as exc:
            print("REFUSED: this pre-registration is not admissible")
            for problem in str(exc).split("; "):
                print(f"  - {problem}")
            return 1
        path = _write(rec)
        print(f"preregistered id={rec['id']} seal={rec['seal'][:16]}...")
        print(f"written {path}")
        print("Now measure. The seal is recomputed at close, so the falsifier above is "
              "the one you are held to.")
        return 0

    if args.cmd == "show":
        rec = _read(args.id)
        if args.json:
            print(json.dumps(rec, indent=2))
            return 0
        print(f"id={rec.get('id')} status={rec.get('status')} "
              f"seal_ok={str(verify_seal(rec)).lower()}")
        print(f"statement: {rec.get('statement')}")
        print(f"falsifier: {rec.get('falsifier')}")
        print(f"regimes={rec.get('regimes')} n_params={rec.get('n_params')} "
              f"costs_bps={rec.get('costs_bps')}")
        if rec.get("outcome"):
            print(f"outcome={rec['outcome']} (claimed {rec.get('claimed_outcome')})")
            for problem in rec.get("audit_problems") or []:
                print(f"  - {problem}")
        return 0

    if args.cmd == "list":
        store = _store_dir()
        rows = sorted(store.glob("*.json")) if store.is_dir() else []
        print(f"hypotheses={len(rows)}")
        for path in rows:
            print(f"  {path.stem}")
        return 0

    if args.cmd == "close":
        from runs.features import read_json

        rec = _read(args.id)
        result = read_json(Path(args.result), {})
        if not result:
            print(f"REFUSED: no result JSON at {args.result}")
            return 1
        closed = close(rec, result, args.outcome)
        closed["closed_utc"] = _now_iso()
        path = _write(closed)
        print(f"id={closed['id']} claimed={closed['claimed_outcome']} "
              f"outcome={closed['outcome']} seal_ok={str(closed['seal_ok']).lower()}")
        for problem in closed["audit_problems"]:
            print(f"  - {problem}")
        print(f"written {path}")
        if closed["outcome"] != closed["claimed_outcome"]:
            print("The audit changed the outcome. Fix the study or publish the negative — "
                  "a refuted hypothesis is a finished piece of work.")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
