"""Event studies: what did each corroborated news event class actually do to
prices? Forward 4h / 1d / 7d returns of the affected assets measured from the
first 4h close at/after the item's timestamp. Pure code, no model.

Writes knowledge/state/event_stats.json:
  {event_class: {n, mean_4h_pct, mean_1d_pct, mean_7d_pct, median_1d_pct,
                 worst_1d_pct}}
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from ops import db  # noqa: E402
from ops.config import load_config  # noqa: E402

H4 = 14_400_000  # ms


def closes_4h(kdb, pair: str) -> list[tuple[int, float]]:
    return [(r["close_time"], r["close"]) for r in kdb.execute(
        "SELECT close_time, close FROM candles WHERE pair=? AND tf='4h'"
        " AND is_closed=1 ORDER BY open_time", (pair,))]


def forward_returns(series: list[tuple[int, float]], ts_ms: int) -> dict | None:
    """Anchor = first 4h close at/after ts (no look-ahead); forward returns from it."""
    anchor_i = next((i for i, (t, _) in enumerate(series) if t >= ts_ms), None)
    if anchor_i is None:
        return None
    t0, p0 = series[anchor_i]
    out = {}
    for label, horizon in (("4h", H4), ("1d", 6 * H4), ("7d", 42 * H4)):
        j = next((i for i, (t, _) in enumerate(series) if t >= t0 + horizon), None)
        out[label] = (series[j][1] / p0 - 1) * 100 if j is not None else None
    return out


def compute(kdb, cfg) -> dict:
    series = {a: closes_4h(kdb, f"{a}/{cfg.universe.quote}")
              for a in cfg.universe.assets}
    rows = kdb.execute(
        "SELECT event_class, assets, COALESCE(published_at, fetched_at) AS ts"
        " FROM news_items WHERE corroborated=1 AND event_class IS NOT NULL"
        " AND cluster_id IS NOT NULL GROUP BY cluster_id").fetchall()
    samples: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        try:
            ts_ms = int(datetime.fromisoformat(
                r["ts"].replace("Z", "+00:00")).timestamp() * 1000)
            assets = json.loads(r["assets"] or "[]") or list(cfg.universe.assets)
        except (ValueError, json.JSONDecodeError, AttributeError):
            continue
        for asset in assets:
            if asset not in series or not series[asset]:
                continue
            fr = forward_returns(series[asset], ts_ms)
            if fr is None:
                continue
            bucket = samples.setdefault(r["event_class"], {"4h": [], "1d": [], "7d": []})
            for h, v in fr.items():
                if v is not None:
                    bucket[h].append(v)
    stats = {}
    for ev, b in sorted(samples.items()):
        if not b["1d"]:
            continue
        stats[ev] = {
            "n": len(b["1d"]),
            "mean_4h_pct": round(statistics.mean(b["4h"]), 2) if b["4h"] else None,
            "mean_1d_pct": round(statistics.mean(b["1d"]), 2),
            "mean_7d_pct": round(statistics.mean(b["7d"]), 2) if b["7d"] else None,
            "median_1d_pct": round(statistics.median(b["1d"]), 2),
            "worst_1d_pct": round(min(b["1d"]), 2),
        }
    return {"computed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "events": stats}


def main() -> int:
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db, readonly=True) as kdb:
        stats = compute(kdb, cfg)
    out = REPO_ROOT / "knowledge" / "state" / "event_stats.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=2) + "\n")
    print(f"wrote {out} ({len(stats['events'])} event classes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
