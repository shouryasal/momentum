"""What-if simulator — the Excel "testing mode": what would the proposals ALONE
have earned, followed exactly, with measured costs?

Deterministic full recompute from paper.start_date on every run (idempotent by
construction — the table is rebuilt, never patched):

- every VALID primary proposal is applied at the FIRST closed 4h candle whose
  close time is at/after its ts_utc (no look-ahead);
- an abstaining proposal holds current weights (it still marks the day);
- crypto weights drift with 4h closes between rebalances;
- rebalance cost = nav * sum(|delta crypto weight|) * (fee+slippage bps from
  config/backtest.yaml — the TCA-measured numbers) / 1e4;
- one whatif_nav row per UTC day (the day's last 4h close).

Wired into nav_job's postflight; runs/trace.py and the Excel WhatIf sheet read
the table. No model anywhere near this file.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config


def measured_cost_bps(root: Path) -> float:
    costs = yaml.safe_load((root / "config" / "backtest.yaml").read_text())["costs"]
    return float(costs["fee_bps"]) + float(costs["slippage_bps"])


def load_proposals(jdb: sqlite3.Connection, start_iso: str) -> list[dict]:
    return [dict(r) for r in jdb.execute(
        "SELECT run_id, ts_utc, targets_json, exposure_scale, abstain"
        " FROM proposals WHERE shadow=0 AND valid=1 AND ts_utc >= ?"
        " ORDER BY ts_utc", (start_iso,))]


def load_grid(kdb: sqlite3.Connection, cfg: EarnConfig,
              start_ms: int) -> list[tuple[int, dict[str, float]]]:
    """[(close_time_ms, {asset: close})] on the shared closed-4h grid."""
    series: dict[int, dict[str, float]] = {}
    for asset in cfg.universe.assets:
        pair = f"{asset}/{cfg.universe.quote}"
        for r in kdb.execute(
                "SELECT close_time, close FROM candles WHERE pair=? AND tf='4h'"
                " AND is_closed=1 AND close_time >= ? ORDER BY open_time",
                (pair, start_ms)):
            series.setdefault(r["close_time"], {})[asset] = r["close"]
    # keep only grid points where EVERY asset has a close (no partial drift)
    return sorted((t, p) for t, p in series.items()
                  if len(p) == len(cfg.universe.assets))


def effective(targets: dict, scale: float, assets: list[str]) -> dict[str, float]:
    w = {a: targets.get(a, 0.0) * scale for a in assets}
    w["USDT"] = max(0.0, 1.0 - sum(w.values()))
    return w


def run_whatif(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection,
               root: Path | None = None) -> int:
    root = root or REPO_ROOT
    assets = list(cfg.universe.assets)
    start = datetime.strptime(cfg.paper.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    bps = measured_cost_bps(root)
    props = load_proposals(jdb, start_iso)
    grid = load_grid(kdb, cfg, int(start.timestamp() * 1000))
    jdb.execute("DELETE FROM whatif_nav")
    if not grid:
        jdb.commit()
        return 0

    nav = float(cfg.sleeves.b.capital_usdt)
    weights: dict[str, float] = {a: 0.0 for a in assets} | {"USDT": 1.0}
    last_rid: str | None = None
    prev_prices: dict[str, float] | None = None
    pi = 0
    days: dict[str, dict] = {}
    for t_ms, prices in grid:
        # drift with prices since the previous grid point
        if prev_prices is not None:
            growth = sum(weights[a] * (prices[a] / prev_prices[a]) for a in assets) \
                + weights["USDT"]
            nav *= growth
            weights = {a: weights[a] * (prices[a] / prev_prices[a]) / growth
                       for a in assets} | {"USDT": weights["USDT"] / growth}
        prev_prices = prices
        t_iso = datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        day_turnover = day_cost = 0.0
        # apply every proposal whose FIRST eligible close this is
        while pi < len(props) and props[pi]["ts_utc"] <= t_iso:
            p = props[pi]
            pi += 1
            last_rid = p["run_id"]
            if p["abstain"]:
                continue  # hold current weights
            new_w = effective(json.loads(p["targets_json"] or "{}"),
                              float(p["exposure_scale"] or 1.0), assets)
            turnover = sum(abs(new_w[a] - weights[a]) for a in assets)
            cost = nav * turnover * bps / 1e4
            nav -= cost
            weights = new_w
            day_turnover += turnover
            day_cost += cost
        day = t_iso[:10]
        cur = days.setdefault(day, {"turnover": 0.0, "cost": 0.0})
        cur.update({"nav": nav, "weights": dict(weights), "rid": last_rid})
        cur["turnover"] += day_turnover
        cur["cost"] += day_cost
    for day, d in sorted(days.items()):
        jdb.execute(
            "INSERT OR REPLACE INTO whatif_nav(date_utc, nav_usdt, weights_json,"
            " last_proposal_run_id, turnover, cost_usdt) VALUES (?,?,?,?,?,?)",
            (day, round(d["nav"], 4),
             json.dumps({k: round(v, 6) for k, v in d["weights"].items()}),
             d["rid"], round(d["turnover"], 6), round(d["cost"], 4)))
    jdb.commit()
    return len(days)


def main() -> int:
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
            db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
        n = run_whatif(cfg, jdb, kdb)
    print(f"whatif_nav: {n} day(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
