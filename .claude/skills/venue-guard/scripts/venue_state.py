"""Venue and quote-currency hazard state — code, not estimates.

Writes ``knowledge/state/venue.json``: symbol status and filters from Binance
``exchangeInfo``, the stablecoin peg with its persistence rule, the USDT/USD basis, the
USDT-corrected cross-venue dispersion, and the delisting-announcement scan behind its canary.

All fetching, caching and feature maths live in ``runs/features/venue.py`` so nothing is
computed twice and the numbers a skill reports are the numbers a backtest would see.

Modes
-----
``(no flags)``        live snapshot, cached per source, degrading to a stale-flagged value
``--offline``         read the cache only; never touch a source
``--write-flags``     also move ``depeg`` / ``symbol_halted`` / ``delist_notice``
``--replay F --as-of T``  recompute the peg verdict over a fixture as of an instant
``--selftest``        run the three historical fixtures offline and assert their verdicts

``--selftest`` is the deterministic eval case: no network, no clock, and it fails if the
2024-01-03 wick ever starts firing or the 2022-05-12 Terra episode ever stops.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_DIR / "tests" / "fixtures"

from runs.features import venue as V  # noqa: E402

STATE_REL = "knowledge/state/venue.json"

#: What each fixture must produce. The whole point of the persistence rule is these three
#: rows disagreeing with each other, so they are asserted rather than described.
SELFTEST_CASES = (
    ("peg_terra_2022-05-12.json", "2022-05-12T10:00:00Z", "usdt_depeg", True,
     "Terra contagion: USDT below par on Coinbase AND Bitstamp for 8 consecutive closes"),
    ("peg_wick_2024-01-03.json", "2024-01-03T14:00:00Z", "ok", False,
     "liquidation wick: USDCUSDT low 0.7600, close 0.9995, zero hourly closes off par"),
    ("peg_usdc_2023-03-11.json", "2023-03-11T20:00:00Z", "usdt_premium", False,
     "SVB/USDC depeg: the PEER broke and USDT was the refuge - warn, never block"),
)


def load_fixture(path: Path) -> list[V.PegSeries]:
    doc = json.loads(path.read_text())
    return [V.PegSeries(s["source"], s["group"], [(t, float(c)) for t, c in s["bars"]])
            for s in doc["series"]]


def state_path(root: Path) -> Path:
    return root / "knowledge" / "state" / "venue.json"


def write_state(root: Path, state: dict) -> Path:
    out = state_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(out)
    return out


def apply_flags(root: Path, state: dict, now: datetime) -> list[str]:
    """Move the three flags this skill owns, through the one sanctioned writer.

    Only ``exchangeInfo`` and the peg can set a blocking flag. ``delist_notice`` is ``info``
    because its source is undocumented and may only warn. A flag this skill set is cleared
    by this skill when its condition lifts; a human-set flag is never touched (``ops.lib.flags``
    refuses that on its own).
    """
    from ops.config import load_config
    from ops.lib import flags as flagslib

    cfg = load_config()
    fpath = root / cfg.paths.flags_file
    moved: list[str] = []
    peg = state["peg"]
    wanted = {}
    if peg["depeg_flag"]:
        wanted["depeg"] = ("block_entries", peg["reason"])
    if state["symbols_not_tradable"]:
        wanted["symbol_halted"] = ("block_entries",
                                   "not TRADING: " + ",".join(state["symbols_not_tradable"]))
    hits = state["announcements"]["delist_hits_24h"]
    if hits:
        wanted["delist_notice"] = ("info", "; ".join(h["title"][:120] for h in hits[:3]))

    try:
        current = flagslib.active_flags(fpath, now)
    except flagslib.FlagsError:
        current = {}
    for name, (severity, reason) in wanted.items():
        flagslib.set_flag(fpath, name, severity=severity, reason=reason,
                          set_by="venue-guard", now=now)
        moved.append(f"set:{name}")
    for name in ("depeg", "symbol_halted", "delist_notice"):
        f = current.get(name)
        if name not in wanted and f and f.get("set_by") == "venue-guard":
            flagslib.clear_flag(fpath, name, by="venue-guard", now=now)
            moved.append(f"clear:{name}")
    if not moved:
        flagslib.touch(fpath, now=now)
    return moved


def summarise(state: dict) -> str:
    peg = state["peg"]
    bits = [f"peg={peg['state']}", f"usdt={peg['usdt_state']}", f"peer={peg['peer_state']}",
            f"confirmable={peg['confirmable']}"]
    if state.get("usdt_basis_bps") is not None:
        bits.append(f"usdt_basis={state['usdt_basis_bps']}bps")
    bits.append("tradable=" + ("ALL" if not state["symbols_not_tradable"]
                               else "NOT:" + ",".join(state["symbols_not_tradable"])))
    bits.append(f"blocking={len(state['blocking'])}")
    bits.append(f"warnings={len(state['warnings'])}")
    return " ".join(bits)


def run_selftest() -> int:
    """Assert the three historical fixtures. Deterministic, offline, no clock."""
    failures = 0
    for fixture, as_of, want_state, want_block, why in SELFTEST_CASES:
        path = FIXTURES / fixture
        if not path.is_file():
            print(f"FAIL {fixture}: fixture missing")
            failures += 1
            continue
        series = load_fixture(path)
        reads = [V.peg_read(s, as_of=as_of) for s in series]
        v = V.peg_verdict(reads)
        ok = v["state"] == want_state and v["depeg_flag"] is want_block
        failures += 0 if ok else 1
        devs = {r.source: r.dev_bps for r in reads}
        print(f"{'PASS' if ok else 'FAIL'} {fixture} @ {as_of} "
              f"state={v['state']} (want {want_state}) block={v['depeg_flag']} "
              f"(want {want_block}) dev_bps={devs}")
        print(f"     {why}")
    print(f"selftest cases={len(SELFTEST_CASES)} failures={failures} "
          f"verdict={'OK' if not failures else 'BROKEN'}")
    return 1 if failures else 0


def run_replay(fixture: Path, as_of: str) -> int:
    series = load_fixture(fixture)
    reads = [V.peg_read(s, as_of=as_of) for s in series]
    v = V.peg_verdict(reads)
    print(json.dumps({"as_of": as_of, "fixture": fixture.name, **v}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Venue and quote-currency hazard state.")
    ap.add_argument("--offline", action="store_true", help="use cached values only")
    ap.add_argument("--write-flags", action="store_true",
                    help="move depeg/symbol_halted/delist_notice through ops.lib.flags")
    ap.add_argument("--replay", metavar="FIXTURE",
                    help="peg fixture to recompute over (name under tests/fixtures/ or a path)")
    ap.add_argument("--as-of", metavar="ISO", help="the instant to evaluate --replay at")
    ap.add_argument("--selftest", action="store_true",
                    help="assert the historical peg fixtures and exit")
    ap.add_argument("--json", action="store_true", help="print the whole state document")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return run_selftest()
    if args.replay:
        p = Path(args.replay)
        if not p.is_file():
            p = FIXTURES / args.replay
        if not p.is_file():
            print(f"fixture not found: {args.replay}")
            return 2
        if not args.as_of:
            print("--replay needs --as-of")
            return 2
        return run_replay(p, args.as_of)

    now = datetime.now(UTC)
    state = V.snapshot(REPO_ROOT, now=now, offline=args.offline)
    out = write_state(REPO_ROOT, state)
    if args.write_flags:
        state["flags_moved"] = apply_flags(REPO_ROOT, state, now)
        write_state(REPO_ROOT, state)
    print(summarise(state))
    for w in state["warnings"]:
        print(f"  warn: {w}")
    for b in state["blocking"]:
        print(f"  BLOCK: {b}")
    print(f"  wrote {STATE_REL}" if out else "")
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
