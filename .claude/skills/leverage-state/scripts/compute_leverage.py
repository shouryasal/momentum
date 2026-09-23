"""Deterministic leverage-state computation — "code, not estimates" by construction.

Loads funding (REST, full history since 2019-09-10), open interest and positioning (the
``data.binance.vision`` bulk archive), the perp–spot basis and the local spot candles, then
writes ``knowledge/state/leverage.json``. Every number is computed by
:mod:`runs.features.derivatives`; nothing in here is estimated and nothing is a direction.

Runs standalone (``python3 compute_leverage.py``), offline (``--offline``, what the backtest
and the replay use) and point-in-time (``--as-of 2024-03-14T00:00:00Z``).

Definitions, sources and the evidence are pinned in ``../references/leverage.md``; the tests
assert golden values and the two invariants that matter — the multiplier can never exceed 1.0,
and no direction field is ever emitted.
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

from runs.features import derivatives as dv  # noqa: E402

#: Where the state lands. ``knowledge/state/**`` is tier 2 and this script is tier 2, which is
#: the same arrangement market-state's compute_state.py uses for latest.json.
OUT_REL = "knowledge/state/leverage.json"

#: The flag this skill is allowed to raise. ``info`` on purpose: crowding informs the gate's
#: operators, it does not block entries. Only a deterministic gate check blocks.
FLAG_NAME = "derisk"
FLAG_SEVERITY = "info"


def compute_pair(pair: str, *, online: bool, as_of: datetime | None,
                 archive_root: str | None = None, cache: str | None = None,
                 data_root: str | None = None) -> dict:
    """Gather the inputs and return the full leverage state for one pair."""
    inputs = dv.gather(pair, online=online, as_of=as_of, archive_root=archive_root,
                       cache=cache, data_root=data_root)
    return dv.leverage_state(inputs, pair, as_of)


def should_flag(state: dict) -> bool:
    """Raise ``derisk`` only when crowding is real **and** the scalar is actually biting.

    Both conditions on purpose. Crowding with a multiplier of 1.0 means the tail ratio stood
    itself down, so there is nothing to act on and a flag would be noise. A multiplier below
    1.0 without the crowded label means one leg nudged — also not worth a flag.
    """
    return bool(state.get("crowding_state") == "crowded"
                and (state.get("exposure_multiplier") or 1.0) < 1.0)


def build(pairs: list[str], *, online: bool, as_of: datetime | None,
          archive_root: str | None = None, cache: str | None = None,
          data_root: str | None = None) -> dict:
    """The whole document: one entry per pair, plus the run's own metadata."""
    now = datetime.now(UTC)
    pairs_out: dict[str, dict] = {}
    for pair in pairs:
        try:
            pairs_out[pair] = compute_pair(pair, online=online, as_of=as_of,
                                           archive_root=archive_root, cache=cache,
                                           data_root=data_root)
        except (OSError, ValueError, KeyError) as exc:
            # A pair failing must not take the document down: the other pair's numbers are
            # still good, and an absent block is more honest than a partial one.
            pairs_out[pair] = {"pair": pair, "error": f"{type(exc).__name__}: {exc}",
                               "no_direction": True}
    stalenesses = [p.get("worst_staleness_min") for p in pairs_out.values()
                   if isinstance(p.get("worst_staleness_min"), int)]
    return {
        "computed_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_requested": as_of.strftime("%Y-%m-%dT%H:%M:%SZ") if as_of else None,
        "online": bool(online),
        "pairs": pairs_out,
        "worst_staleness_min": max(stalenesses) if stalenesses else None,
        "no_direction": True,
    }


def write_state(doc: dict, root: Path, out_rel: str = OUT_REL) -> Path:
    """Atomic write of the state document. Destination is fixed under ``knowledge/``."""
    out_path = root / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out_path)
    return out_path


def _maybe_flag(doc: dict, root: Path, now: datetime) -> str | None:
    """Set or clear ``derisk`` through the one sanctioned writer, or do nothing if unavailable.

    Flag writing is best effort by design: this is a risk *input*, and a flags file that will
    not open must never stop the state document from being written.
    """
    try:
        from ops.config import load_config
        from ops.lib import flags as flagslib
    except ImportError:
        return None
    try:
        cfg = load_config()
        flags_path = root / cfg.paths.flags_file
        hits = [p for p, s in doc.get("pairs", {}).items() if should_flag(s)]
        current = flagslib.active_flags(flags_path, now)
        if hits:
            flagslib.set_flag(flags_path, FLAG_NAME, severity=FLAG_SEVERITY,
                              reason="crowded leverage: " + ", ".join(sorted(hits)),
                              set_by="leverage-state", now=now)
            return f"set {FLAG_NAME}"
        existing = current.get(FLAG_NAME)
        if existing and existing.get("set_by") == "leverage-state":
            flagslib.clear_flag(flags_path, FLAG_NAME, by="leverage-state", now=now)
            return f"cleared {FLAG_NAME}"
    except Exception as exc:  # noqa: BLE001 - a flag failure never vetoes the state write
        return f"flag skipped: {type(exc).__name__}: {exc}"
    return None


def summarise(doc: dict) -> dict:
    """The compact stdout summary: enough to see the run worked, no wall of JSON."""
    out = {"computed_utc": doc.get("computed_utc"), "pairs": {}}
    for pair, s in doc.get("pairs", {}).items():
        if s.get("error"):
            out["pairs"][pair] = {"error": s["error"]}
            continue
        table = s.get("drawdown_table", {})
        out["pairs"][pair] = {
            "crowding_state": s.get("crowding_state"),
            "funding_ann_3d": (s.get("funding") or {}).get("funding_ann_3d"),
            "quadrant": (s.get("open_interest") or {}).get("quadrant"),
            "p_drawdown_7d": s.get("p_drawdown_7d"),
            "p_drawdown_base_rate": s.get("p_drawdown_base_rate"),
            "exposure_multiplier": s.get("exposure_multiplier"),
            "edge_live": table.get("edge_live"),
            "staleness_min": s.get("worst_staleness_min"),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compute Earn's leverage state.")
    ap.add_argument("--offline", action="store_true",
                    help="never reach the network; use only cached data (backtest/replay mode)")
    ap.add_argument("--as-of", dest="as_of",
                    help="reproduce the state as of this UTC instant, e.g. 2024-03-14T00:00:00Z")
    ap.add_argument("--pairs", nargs="*", help="override the universe pairs")
    ap.add_argument("--no-write", action="store_true", help="print only; write nothing")
    ap.add_argument("--no-flag", action="store_true", help="do not touch the flags file")
    ap.add_argument("--archive-root", help="override the bulk-archive cache location")
    ap.add_argument("--cache", help="override the derivatives cache location")
    ap.add_argument("--data-root", help="override the feather candle directory")
    ap.add_argument("--root", help="override the repo root the state is written under")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root) if args.root else REPO_ROOT
    as_of = None
    if args.as_of:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))

    pairs = args.pairs
    if not pairs:
        try:
            from ops.config import load_config

            pairs = list(load_config().universe.pairs)
        except (ImportError, AttributeError, OSError):
            pairs = ["BTC/USDT", "ETH/USDT"]

    doc = build(pairs, online=not args.offline, as_of=as_of,
                archive_root=args.archive_root, cache=args.cache, data_root=args.data_root)

    if not args.no_write:
        write_state(doc, root)
    if not args.no_flag and not args.no_write:
        note = _maybe_flag(doc, root, datetime.now(UTC))
        if note:
            doc["flag_action"] = note

    summary = summarise(doc)
    if doc.get("flag_action"):
        summary["flag_action"] = doc["flag_action"]
    print(json.dumps(summary, indent=2, sort_keys=True))
    ok = any(not s.get("error") for s in doc.get("pairs", {}).values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
