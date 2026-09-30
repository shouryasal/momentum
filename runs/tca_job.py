"""Hourly TCA: reconcile fills against decision-time quotes -> cost in bps; rolling
7d/30d medians per sleeve; monthly calibration of config/backtest.yaml costs; the
1.5x/14-day freeze rule that sets the human-cleared tier1_freeze flag.

Arrival-price convention: slippage_bps = side_sign * (fill_price - ref_mid)/ref_mid * 1e4
(side_sign +1 buy, -1 sell); fee_bps = fee_in_usdt / notional * 1e4 (BNB fees
converted at the BNB/USDT 1h close covering the fill).
"""

from __future__ import annotations

import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ruamel.yaml import YAML

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import locks


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bnb_usdt_close(kdb, ts_utc: str) -> float | None:
    ts_ms = int(datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).timestamp() * 1000)
    row = kdb.execute(
        "SELECT close FROM candles WHERE pair='BNB/USDT' AND tf='1h' AND open_time<=?"
        " ORDER BY open_time DESC LIMIT 1", (ts_ms,)).fetchone()
    return float(row["close"]) if row else None


def fee_in_usdt(fill, kdb) -> float | None:
    if fill["fee_amount"] is None:
        return None
    ccy = fill["fee_currency"] or "USDT"
    if ccy == "USDT":
        return float(fill["fee_amount"])
    if ccy == "BNB":
        px = _bnb_usdt_close(kdb, fill["ts_utc"])
        return float(fill["fee_amount"]) * px if px else None
    if ccy == fill["pair"].split("/")[0]:  # fee taken in base currency
        return float(fill["fee_amount"]) * float(fill["fill_price"])
    return None


def _ref_mid(fill, kdb, fallback_max_s: int) -> tuple[float, str] | None:
    if fill["quote_bid"] and fill["quote_ask"]:
        return (fill["quote_bid"] + fill["quote_ask"]) / 2, "fill_row"
    ts = fill["ts_utc"]
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    lo, hi = _iso(t - timedelta(seconds=fallback_max_s)), _iso(t + timedelta(seconds=fallback_max_s))
    row = kdb.execute(
        "SELECT mid, captured_at FROM book_snapshots WHERE pair=? AND captured_at BETWEEN ? AND ?"
        " ORDER BY ABS(julianday(captured_at) - julianday(?)) LIMIT 1",
        (fill["pair"], lo, hi, ts)).fetchone()
    if row:
        return float(row["mid"]), "book_snapshot"
    return None


def reconcile_fills(jdb, kdb, cfg: EarnConfig, now: datetime) -> int:
    fills = jdb.execute(
        "SELECT f.* FROM fills f LEFT JOIN tca_fill_costs t ON t.fill_id = f.id"
        " WHERE t.fill_id IS NULL").fetchall()
    n = 0
    for f in fills:
        notional = float(f["fill_amount"]) * float(f["fill_price"])
        ref = _ref_mid(f, kdb, cfg.tca.quote_fallback_max_s)
        fee = fee_in_usdt(f, kdb)
        if ref is None:
            jdb.execute(
                "INSERT OR REPLACE INTO tca_fill_costs(fill_id, sleeve, pair, side,"
                " notional_usdt, fill_vwap, reconciled_at, status)"
                " VALUES (?,?,?,?,?,?,?,'unreconciled')",
                (f["id"], f["sleeve"], f["pair"], f["side"], notional,
                 f["fill_price"], _iso(now)))
            continue
        mid, source = ref
        sign = 1 if f["side"] == "buy" else -1
        slip = sign * (float(f["fill_price"]) - mid) / mid * 1e4
        fee_bps = (fee / notional * 1e4) if (fee is not None and notional) else None
        total = slip + (fee_bps or 0.0)
        jdb.execute(
            "INSERT OR REPLACE INTO tca_fill_costs(fill_id, sleeve, pair, side,"
            " notional_usdt, fill_vwap, ref_mid, quote_source, fee_bps, slippage_bps,"
            " total_bps, reconciled_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["id"], f["sleeve"], f["pair"], f["side"], notional, f["fill_price"],
             mid, source, fee_bps, slip, total, _iso(now),
             "ok" if source == "fill_row" else "fallback"))
        n += 1
    jdb.commit()
    return n


def update_rolling(jdb, now: datetime) -> None:
    day = now.strftime("%Y-%m-%d")
    for sleeve in ("a", "b"):
        for window, days in (("7d", 7), ("30d", 30)):
            since = _iso(now - timedelta(days=days))
            rows = jdb.execute(
                "SELECT t.fee_bps, t.slippage_bps, t.total_bps FROM tca_fill_costs t"
                " JOIN fills f ON f.id = t.fill_id"
                " WHERE t.sleeve=? AND f.ts_utc >= ? AND t.status != 'unreconciled'",
                (sleeve, since)).fetchall()
            if not rows:
                continue
            fee = [r["fee_bps"] for r in rows if r["fee_bps"] is not None]
            slip = [r["slippage_bps"] for r in rows if r["slippage_bps"] is not None]
            total = [r["total_bps"] for r in rows if r["total_bps"] is not None]
            jdb.execute(
                "INSERT OR REPLACE INTO tca_rolling(day, sleeve, window, n_fills,"
                " fee_bps_med, slip_bps_med, total_bps_med, total_bps_mean)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (day, sleeve, window, len(rows),
                 statistics.median(fee) if fee else None,
                 statistics.median(slip) if slip else None,
                 statistics.median(total) if total else None,
                 statistics.fmean(total) if total else None))
    jdb.commit()


def calibrate_monthly(jdb, cfg: EarnConfig, backtest_yaml: Path, now: datetime) -> bool:
    """First run of a new month: write prior-month measured medians into backtest.yaml
    (ruamel round-trip preserves the rest of the file). Below the fill floor: skip.

    LIVE FILLS ONLY. A dry-run fill is booked at the touch and pays no spread, so paper
    slippage is near zero by construction — calibrating from it would replace a measured
    assumption with an artefact and make every future backtest optimistic in the one
    direction that matters. This system has never traded real money, so on 2026-10-01 this
    job was about to rewrite ``costs.slippage_bps`` from 5.0 to about 2.3 on the strength of
    a week of paper trading, silently, and every strategy measured afterwards would have
    cleared its cost floor more easily than reality allows. ``fills.mode`` has always
    recorded ``test`` or ``live``; nothing was reading it.
    """
    month_first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prior_start = (month_first - timedelta(days=1)).replace(day=1)
    prior = prior_start.strftime("%Y-%m")
    done = jdb.execute("SELECT 1 FROM tca_calibrations WHERE month=?", (prior,)).fetchone()
    if done:
        return False
    rows = jdb.execute(
        "SELECT t.fee_bps, t.slippage_bps FROM tca_fill_costs t JOIN fills f ON f.id=t.fill_id"
        " WHERE f.ts_utc >= ? AND f.ts_utc < ? AND t.status != 'unreconciled'"
        " AND t.fee_bps IS NOT NULL AND f.mode = 'live'",
        (_iso(prior_start), _iso(month_first))).fetchall()
    if len(rows) < cfg.tca.calibration_min_fills:
        jdb.execute(
            "INSERT OR REPLACE INTO tca_calibrations(month, n_fills, written_at, applied)"
            " VALUES (?,?,?,0)", (prior, len(rows), _iso(now)))
        jdb.commit()
        return False
    fee_med = statistics.median(r["fee_bps"] for r in rows)
    slip_med = statistics.median(r["slippage_bps"] for r in rows)
    yaml_rt = YAML()
    data = yaml_rt.load(backtest_yaml.read_text())
    data["costs"]["fee_bps"] = round(fee_med, 2)
    data["costs"]["slippage_bps"] = round(slip_med, 2)
    data["costs"]["measured_month"] = prior
    data["costs"]["n_fills"] = len(rows)
    with backtest_yaml.open("w") as fh:
        yaml_rt.dump(data, fh)
    jdb.execute(
        "INSERT OR REPLACE INTO tca_calibrations(month, fee_bps, slippage_bps, n_fills,"
        " written_at, applied) VALUES (?,?,?,?,?,1)",
        (prior, fee_med, slip_med, len(rows), _iso(now)))
    jdb.commit()
    return True


def check_freeze(jdb, cfg: EarnConfig, backtest_yaml: Path, flags_path: Path,
                 now: datetime) -> None:
    """Measured > freeze_ratio x assumed for freeze_days -> tier1_freeze (human-cleared).
    Recovery clears the breach clock but NEVER the flag."""
    import yaml as pyyaml

    costs = pyyaml.safe_load(backtest_yaml.read_text())["costs"]
    assumed = costs["fee_bps"] + costs["slippage_bps"]
    day = now.strftime("%Y-%m-%d")
    worst = jdb.execute(
        "SELECT MAX(total_bps_med) AS m FROM tca_rolling"
        " WHERE day=? AND window='7d' AND n_fills >= 5", (day,)).fetchone()
    measured = worst["m"] if worst else None
    breach = measured is not None and measured > cfg.tca.freeze_ratio * assumed
    since = jdb.execute("SELECT value FROM tca_state WHERE key='breach_since'").fetchone()
    if breach:
        if since is None:
            jdb.execute("INSERT OR REPLACE INTO tca_state(key, value, updated_utc)"
                        " VALUES ('breach_since', ?, ?)", (_iso(now), _iso(now)))
            jdb.commit()
        else:
            started = datetime.fromisoformat(since["value"].replace("Z", "+00:00"))
            if now - started >= timedelta(days=cfg.tca.freeze_days):
                flagslib.set_flag(flags_path, "tier1_freeze", severity="freeze_tier1",
                                  reason=f"measured {measured:.1f}bps > "
                                         f"{cfg.tca.freeze_ratio}x assumed {assumed:.1f}bps"
                                         f" since {since['value']}",
                                  set_by="tca_job", now=now)
    elif since is not None:
        jdb.execute("DELETE FROM tca_state WHERE key='breach_since'")
        jdb.commit()


def main() -> int:
    cfg = load_config()
    now = datetime.now(UTC)
    backtest_yaml = REPO_ROOT / "config" / "backtest.yaml"
    flags_path = REPO_ROOT / cfg.paths.flags_file
    with locks.acquire("tca"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb, \
                db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
            n = reconcile_fills(jdb, kdb, cfg, now)
            update_rolling(jdb, now)
            calibrate_monthly(jdb, cfg, backtest_yaml, now)
            check_freeze(jdb, cfg, backtest_yaml, flags_path, now)
            # heartbeat for healthcheck's missed-run probe
            jdb.execute("INSERT OR REPLACE INTO tca_state(key, value, updated_utc)"
                        " VALUES ('last_run', ?, ?)", (_iso(now), _iso(now)))
            jdb.commit()
    print(f"reconciled {n} fills")
    return 0


if __name__ == "__main__":
    sys.exit(main())
