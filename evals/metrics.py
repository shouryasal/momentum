"""Pure replay metrics (spec §8): constraint violations, validity, agreement with
the rules sleeve on unambiguous states, confidence calibration, implied turnover,
determinism. Shared by evals/replay.py and the tests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class Scores:
    constraint_violations: int
    schema_validity_rate: float
    agreement_rate: float | None      # None when no unambiguous snapshots
    calibration_error: float | None   # None when outcomes unresolved / too few
    implied_turnover: float
    determinism: float

    def as_dict(self) -> dict:
        return dict(vars(self))


def violates_limits(targets: dict[str, float], limits_yaml_text: str) -> list[str]:
    lim = yaml.safe_load(limits_yaml_text)
    caps = lim["max_weight"]
    problems = []
    crypto = 0.0
    for asset, w in targets.items():
        if asset == "USDT":
            continue
        crypto += w
        cap = caps.get(asset, caps.get("default", 0.0))
        if w > cap + 1e-9:
            problems.append(f"weight {asset} {w} > cap {cap}")
    if crypto > lim["max_gross_exposure"] + 1e-9:
        problems.append(f"gross {crypto} > {lim['max_gross_exposure']}")
    if targets.get("USDT", 0.0) < lim["usdt_floor"] - 1e-9:
        problems.append(f"USDT {targets.get('USDT')} < floor {lim['usdt_floor']}")
    return problems


def is_unambiguous(state_json_text: str, flags_json_text: str) -> bool:
    try:
        state = json.loads(state_json_text)
        regime = state.get("portfolio", {}).get("regime")
    except json.JSONDecodeError:
        return False
    try:
        flags = json.loads(flags_json_text)
        active = [f for f in flags.get("flags", {}).values() if f.get("active")]
    except (json.JSONDecodeError, AttributeError):
        return False
    return regime in ("trend_up", "trend_down") and not active


def targets_agree(a: dict[str, float], b: dict[str, float], tolerance: float) -> bool:
    keys = set(a) | set(b)
    return all(abs(a.get(k, 0.0) - b.get(k, 0.0)) <= tolerance for k in keys)


def proposals_identical(p1: dict, p2: dict, tolerance: float) -> bool:
    return (p1["module"] == p2["module"] and p1["abstain"] == p2["abstain"]
            and targets_agree(p1["targets"], p2["targets"], tolerance))


def implied_turnover(target_series: list[dict[str, float]],
                     decisions_per_day: float = 2.0) -> float:
    """Mean per-decision sum(|delta target|) annualized."""
    if len(target_series) < 2:
        return 0.0
    deltas = []
    for prev, cur in zip(target_series, target_series[1:], strict=False):
        keys = set(prev) | set(cur)
        deltas.append(sum(abs(cur.get(k, 0.0) - prev.get(k, 0.0)) for k in keys))
    return (sum(deltas) / len(deltas)) * decisions_per_day * 365


def calibration_error(confidences: list[float], beat_benchmark: list[bool],
                      min_n: int = 10) -> float | None:
    """3-bin max |mean confidence - realized beat rate|; None under min_n."""
    pairs = [(c, b) for c, b in zip(confidences, beat_benchmark, strict=False) if b is not None]
    if len(pairs) < min_n:
        return None
    bins: dict[int, list[tuple[float, bool]]] = {0: [], 1: [], 2: []}
    for c, b in pairs:
        bins[min(int(c * 3), 2)].append((c, b))
    worst = 0.0
    for members in bins.values():
        if len(members) >= 3:
            mean_c = sum(c for c, _ in members) / len(members)
            rate = sum(1 for _, b in members if b) / len(members)
            worst = max(worst, abs(mean_c - rate))
    return worst


def score_replay(results: list[dict], *, limits_by_snapshot: dict[str, str],
                 states: dict[str, str], flags: dict[str, str],
                 rules_targets: dict[str, dict[str, float]],
                 tolerance: float) -> Scores:
    """results: [{run_id, attempts: [proposal-dict|None, ...]}] (2 attempts each)."""
    total = len(results) * max(len(r["attempts"]) for r in results) if results else 0
    valid = [a for r in results for a in r["attempts"] if a is not None]
    violations = 0
    agree_hits, agree_total = 0, 0
    ident = 0
    series = []
    for r in results:
        first = r["attempts"][0]
        if first is not None:
            series.append(first["targets"])
            violations += bool(violates_limits(first["targets"],
                                               limits_by_snapshot[r["run_id"]]))
            if is_unambiguous(states[r["run_id"]], flags[r["run_id"]]):
                agree_total += 1
                rt = rules_targets.get(r["run_id"], {})
                crypto_targets = {k: v for k, v in first["targets"].items() if k != "USDT"}
                if targets_agree(crypto_targets, rt, tolerance):
                    agree_hits += 1
        pair = [a for a in r["attempts"][:2]]
        if all(a is not None for a in pair) and len(pair) == 2:
            ident += proposals_identical(pair[0], pair[1], tolerance)
    n = len(results) or 1
    return Scores(
        constraint_violations=violations,
        schema_validity_rate=len(valid) / total if total else 0.0,
        agreement_rate=(agree_hits / agree_total) if agree_total else None,
        calibration_error=None,  # filled by the caller when outcomes are resolvable
        implied_turnover=implied_turnover(series),
        determinism=ident / n,
    )


def compare(baseline: Scores, candidate: Scores) -> dict[str, str]:
    """better/equal/worse per comparable metric. Hard gates (violations=0,
    determinism=1.0) are checked separately by the ship rule."""
    out: dict[str, str] = {}

    def cmp(name: str, b, c, higher_is_better: bool, eps: float = 1e-9):
        if b is None or c is None:
            out[name] = "equal"
            return
        if abs(b - c) <= eps:
            out[name] = "equal"
        elif (c > b) == higher_is_better:
            out[name] = "better"
        else:
            out[name] = "worse"

    cmp("schema_validity_rate", baseline.schema_validity_rate,
        candidate.schema_validity_rate, True, 1e-6)
    cmp("agreement_rate", baseline.agreement_rate, candidate.agreement_rate, True, 1e-6)
    cmp("calibration_error", baseline.calibration_error, candidate.calibration_error,
        False, 1e-6)
    cmp("implied_turnover", baseline.implied_turnover, candidate.implied_turnover,
        False, 1e-6)
    return out


def ship_rule(candidate: Scores, comparison: dict[str, str],
              determinism_min: float = 1.0) -> tuple[bool, str]:
    """Spec §8: zero violations, full determinism, no metric worse, >=1 better."""
    if candidate.constraint_violations != 0:
        return False, "constraint violations"
    if candidate.determinism < determinism_min - 1e-9:
        return False, f"determinism {candidate.determinism} < {determinism_min}"
    if any(v == "worse" for v in comparison.values()):
        worse = [k for k, v in comparison.items() if v == "worse"]
        return False, f"worse on {worse}"
    if not any(v == "better" for v in comparison.values()):
        return False, "no metric better"
    return True, "ok"
