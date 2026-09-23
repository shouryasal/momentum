"""The FOMC event study that re-argues the blackout width from data.

Writes ``knowledge/state/macro_event_study.json``: the hour-by-hour volatility profile
around a statement release, the cumulative 4h window statistics, the direction blocks (which
exist so their absence stays visible), and the asymmetric ``suggested_window`` that follows.

Volatility only. There is no direction output, no surprise estimate and no place to add one.

Candles come from the local archive, in this order: an explicit ``--candles`` path, then the
feather files the containers share, then the knowledge DB. Whichever is used is named in the
output, because a study whose input is ambiguous is not a study.
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

from runs.features import macro_calendar as MC  # noqa: E402
from runs.features.venue import SourceDown  # noqa: E402

STATE_REL = "knowledge/state/macro_event_study.json"
DEFAULT_PAIR = "BTC_USDT"


def load_fixture_bars(path: Path) -> tuple[list[tuple[str, float]], str]:
    """A contiguous hourly close series stored as a start stamp plus the closes."""
    doc = json.loads(path.read_text())
    start = datetime.fromisoformat(doc["start_utc"].replace("Z", "+00:00")).astimezone(UTC)
    step = int(doc.get("step_hours", 1)) * 3600
    bars = [((start.timestamp() + i * step), float(c)) for i, c in enumerate(doc["closes"])]
    return ([(datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), c)
             for t, c in bars], f"fixture:{path.name}")


def load_local_bars(root: Path, pair: str, explicit: str | None
                    ) -> tuple[list[tuple[str, float]], str]:
    """Hourly closes from the local archive. Feather first, knowledge DB as the fallback."""
    import pandas as pd

    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(root / "data" / "binance" / f"{pair}-1h.feather")
    home = Path.home() / "earn-run" / "data" / "binance" / f"{pair}-1h.feather"
    candidates.append(home)
    for p in candidates:
        if p.is_file() and p.suffix == ".feather":
            df = pd.read_feather(p).sort_values("date")
            return ([(t.strftime("%Y-%m-%dT%H:%M:%SZ"), float(c))
                     for t, c in zip(df["date"], df["close"], strict=False)], str(p))

    from ops import db
    from ops.config import load_config

    cfg = load_config()
    with db.opened(root / cfg.paths.knowledge_db, readonly=True) as kdb:
        rows = kdb.execute(
            "SELECT open_time, close FROM candles WHERE pair=? AND tf='1h' AND is_closed=1"
            " ORDER BY open_time", (pair.replace("_", "/"),)).fetchall()
    return ([(datetime.fromtimestamp(r["open_time"] / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
              float(r["close"])) for r in rows], "knowledge_db")


def event_times(root: Path, *, offline: bool, fetcher=None) -> tuple[list[datetime], str]:
    """Release instants, from the scrape when allowed and from the fixture otherwise."""
    if not offline:
        try:
            dates, status = MC.fetch_fomc_dates(years=range(2015, 2021), fetcher=fetcher)
            if dates:
                return MC.fomc_release_times(dates), f"federalreserve.gov ({len(dates)} dates)"
        except SourceDown:
            pass
    doc = json.loads((FIXTURES / "fomc_dates.json").read_text())
    return (MC.fomc_release_times(doc["dates"]),
            f"fixture:fomc_dates.json ({len(doc['dates'])} dates)")


def build(root: Path, *, bars, bars_source: str, events, events_source: str,
          pre_h: int, post_h: int, horizon_h: int, threshold: float, now: datetime) -> dict:
    study = MC.event_study(bars, events, pre_h=pre_h, post_h=post_h, horizon_h=horizon_h)
    window = MC.suggested_window(study, elevated_multiple=threshold)
    rolling = MC.vol_multiplier(bars, events, horizon_h=horizon_h, last_n=20)
    shipped = None
    try:
        from ops.config import load_config

        shipped = float(load_config().risk.blackout.window_minutes)
    except Exception:  # noqa: BLE001 - the study must run without a readable config
        pass
    return {
        "computed_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bars_source": bars_source,
        "events_source": events_source,
        **study,
        "suggested_window": window,
        "rolling_vol_multiplier_20ev": rolling,
        "shipped_window_minutes_symmetric": shipped,
        "disagrees_with_shipped": (
            None if shipped is None or window.get("pre_minutes") is None
            else bool(abs(window["pre_minutes"] - shipped) > 60
                      or abs(window["post_minutes"] - shipped) > 60)),
        "note": ("volatility only. The direction blocks are reported so that their absence "
                 "stays visible; nothing here forecasts a print, a surprise or a sign."),
    }


def write_state(root: Path, state: dict) -> Path:
    out = root / "knowledge" / "state" / "macro_event_study.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(out)
    return out


def summarise(s: dict) -> str:
    w = s["suggested_window"]
    return (f"events={s['n_events']} bars={s['n_bars']} uncond|1h|={s['unconditional_abs_ret_pct']}% "
            f"peak={w.get('peak_multiple')}x@T{w.get('peak_hour'):+d}h "
            f"ratio{s['horizon_h']}h={s['window_abs_ratio']} "
            f"suggested=-{w.get('pre_minutes')}/+{w.get('post_minutes')}min "
            f"shipped={s['shipped_window_minutes_symmetric']}min(symmetric) "
            f"disagrees={s['disagrees_with_shipped']}")


def run_selftest() -> int:
    """Golden values on the bundled REAL slice of BTC 1h, plus a planted-profile control."""
    failures = []

    def check(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'} {name}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    bars, src = load_fixture_bars(FIXTURES / "btc_1h_2024-08_2026-09.json")
    events, esrc = event_times(REPO_ROOT, offline=True)
    s = build(REPO_ROOT, bars=bars, bars_source=src, events=events, events_source=esrc,
              pre_h=12, post_h=12, horizon_h=4, threshold=1.20, now=datetime.now(UTC))
    check("fixture is the real 18,805-bar slice", s["n_bars"] == 18805, f"got {s['n_bars']}")
    check("18 FOMC releases fall inside it", s["n_events"] == 18, f"got {s['n_events']}")
    check("unconditional |1h| = 0.2058%",
          abs(s["unconditional_abs_ret_pct"] - 0.2058) < 1e-4,
          str(s["unconditional_abs_ret_pct"]))
    check("the release bar is the peak hour", s["suggested_window"]["peak_hour"] == 0,
          f"peak at T{s['suggested_window']['peak_hour']:+d}h, "
          f"{s['suggested_window']['peak_multiple']}x")
    check("4h window ratio = 1.618", abs(s["window_abs_ratio"] - 1.618) < 1e-3,
          str(s["window_abs_ratio"]))
    check("direction is NOT significant at 24h", abs(s["direction_t24h"]["t"]) < 2.0,
          f"t={s['direction_t24h']['t']}")
    check("no direction field leaks into the window recommendation",
          not ({"direction", "side", "bullish", "bearish", "forecast"}
               & set(s["suggested_window"])))

    # Planted control: a flat series with a single elevated hour at T+0 must recommend a
    # window of exactly that hour, which is what proves the walk is not inventing width.
    import math

    base = [(datetime.fromtimestamp(1_700_000_000 + i * 3600, UTC)
             .strftime("%Y-%m-%dT%H:%M:%SZ"), 100.0 * math.exp(0.001 * (i % 2 * 2 - 1)))
            for i in range(4000)]
    ev = [datetime.fromtimestamp(1_700_000_000 + i * 3600, UTC) for i in range(200, 4000, 400)]
    flat = MC.event_study(base, ev, pre_h=6, post_h=6)
    w = MC.suggested_window(flat)
    check("a series with no event effect recommends no extra width",
          w["pre_minutes"] == 0 and w["post_minutes"] == 60,
          f"{w['pre_minutes']}/{w['post_minutes']}")

    print(f"selftest failures={len(failures)} verdict={'OK' if not failures else 'BROKEN'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FOMC event study and blackout width.")
    ap.add_argument("--candles", help="path to an hourly feather file")
    ap.add_argument("--pair", default=DEFAULT_PAIR)
    ap.add_argument("--pre-h", type=int, default=MC.STUDY_PRE_H)
    ap.add_argument("--post-h", type=int, default=MC.STUDY_POST_H)
    ap.add_argument("--horizon-h", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=MC.ELEVATED_MULTIPLE,
                    help="multiple of the unconditional |1h| that counts as elevated")
    ap.add_argument("--offline", action="store_true",
                    help="use the bundled FOMC date fixture instead of scraping")
    ap.add_argument("--fixture", action="store_true",
                    help="use the bundled candle slice instead of the local archive")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return run_selftest()

    if args.fixture:
        bars, bsrc = load_fixture_bars(FIXTURES / "btc_1h_2024-08_2026-09.json")
    else:
        try:
            bars, bsrc = load_local_bars(REPO_ROOT, args.pair, args.candles)
        except Exception as exc:  # noqa: BLE001 - say what is missing, do not guess numbers
            print(f"no hourly candles available ({type(exc).__name__}: {exc}); "
                  f"re-run with --fixture to use the bundled slice")
            return 2
    if not bars:
        print("no hourly candles available; refusing to publish a study with no input")
        return 2
    events, esrc = event_times(REPO_ROOT, offline=args.offline)
    state = build(REPO_ROOT, bars=bars, bars_source=bsrc, events=events, events_source=esrc,
                  pre_h=args.pre_h, post_h=args.post_h, horizon_h=args.horizon_h,
                  threshold=args.threshold, now=datetime.now(UTC))
    write_state(REPO_ROOT, state)
    print(summarise(state))
    if state["disagrees_with_shipped"]:
        print("  the shipped symmetric window disagrees with the measurement - "
              "raise a change proposal, never widen it here")
    print(f"  wrote {STATE_REL}")
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
