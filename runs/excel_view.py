"""reports/earn.xlsx — the human view over journal.db (+ g2_summary.json).

Sheets: NAV (per sleeve vs benchmark, indexed to 100, line chart), Trades (fills
joined to decision quotes, slippage bps), Costs (rolling TCA + turnover), Gate
(G1-G7 with live values), Limits (every §9 control: configured vs current).
Atomic write (tmp + os.replace). Invoked by the run wrappers' postflight, not cron.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config

G2_SUMMARY = REPO_ROOT / "reports" / "backtests" / "g2_summary.json"


def _nav_sheet(wb: Workbook, conn) -> None:
    ws = wb.active
    ws.title = "NAV"
    ws.append(["date", "a", "b", "benchmark", "a_idx", "b_idx", "bench_idx"])
    rows = conn.execute(
        "SELECT date_utc, sleeve, nav_usdt FROM nav_daily ORDER BY date_utc"
    ).fetchall()
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        by_date.setdefault(r["date_utc"], {})[r["sleeve"]] = r["nav_usdt"]
    base: dict[str, float] = {}
    for d in sorted(by_date):
        vals = by_date[d]
        for s, v in vals.items():
            base.setdefault(s, v)
        ws.append([
            d, vals.get("a"), vals.get("b"), vals.get("benchmark"),
            *(100 * vals[s] / base[s] if s in vals and base.get(s) else None
              for s in ("a", "b", "benchmark")),
        ])
    if ws.max_row > 1:
        chart = LineChart()
        chart.title = "NAV indexed to 100"
        data = Reference(ws, min_col=5, max_col=7, min_row=1, max_row=ws.max_row)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
        ws.add_chart(chart, "I2")


def _trades_sheet(wb: Workbook, conn) -> None:
    ws = wb.create_sheet("Trades")
    ws.append(["ts_utc", "sleeve", "pair", "side", "amount", "fill_price",
               "quote_bid", "quote_ask", "slippage_bps", "fee", "fee_ccy"])
    for r in conn.execute("SELECT * FROM fills ORDER BY ts_utc").fetchall():
        slip = None
        if r["quote_bid"] and r["quote_ask"]:
            mid = (r["quote_bid"] + r["quote_ask"]) / 2
            sign = 1 if r["side"] == "buy" else -1
            slip = sign * (r["fill_price"] - mid) / mid * 1e4
        ws.append([r["ts_utc"], r["sleeve"], r["pair"], r["side"], r["fill_amount"],
                   r["fill_price"], r["quote_bid"], r["quote_ask"], slip,
                   r["fee_amount"], r["fee_currency"]])


def _costs_sheet(wb: Workbook, conn) -> None:
    ws = wb.create_sheet("Costs")
    ws.append(["day", "sleeve", "window", "n_fills", "fee_bps_med", "slip_bps_med",
               "total_bps_med", "total_bps_mean"])
    for r in conn.execute("SELECT * FROM tca_rolling ORDER BY day, sleeve, window"):
        ws.append([r["day"], r["sleeve"], r["window"], r["n_fills"], r["fee_bps_med"],
                   r["slip_bps_med"], r["total_bps_med"], r["total_bps_mean"]])
    ws.append([])
    ws.append(["turnover (sum |fills| / NAV, by sleeve, whole period)"])
    for r in conn.execute(
        "SELECT f.sleeve, SUM(f.fill_amount * f.fill_price) AS traded,"
        " (SELECT AVG(nav_usdt) FROM nav_daily n WHERE n.sleeve = f.sleeve) AS avg_nav"
        " FROM fills f GROUP BY f.sleeve"
    ):
        turnover = (r["traded"] / r["avg_nav"]) if r["avg_nav"] else None
        ws.append([None, r["sleeve"], turnover])


def _gate_sheet(wb: Workbook, conn) -> None:
    ws = wb.create_sheet("Gate")
    ws.append(["gate", "metric", "threshold", "live value", "pass"])
    g2 = None
    if G2_SUMMARY.exists():
        g2 = json.loads(G2_SUMMARY.read_text())
    ws.append(["G1 plumbing", "rehearsals passed twice", "manual", None, None])
    if g2:
        ws.append(["G2 backtest", "dd better / ret>=0.6xBTC / cost<1%mo",
                   "see g2_summary.json",
                   f"A {g2['sleeve_a']['profit_total_pct']:.1f}% dd {g2['sleeve_a']['max_drawdown_pct']:.1f}%"
                   f" vs BTC {g2['btc_hold']['return_pct']:.1f}% dd {g2['btc_hold']['max_drawdown_pct']:.1f}%",
                   g2["pass"]])
    else:
        ws.append(["G2 backtest", "run runs/walk_forward.py + runs/g2_check.py", None, None, None])
    validity = conn.execute(
        "SELECT COUNT(*) AS n, SUM(valid) AS ok FROM proposals WHERE shadow=0"
    ).fetchone()
    rate = (validity["ok"] / validity["n"] * 100) if validity["n"] else None
    breaches = conn.execute(
        "SELECT COUNT(*) AS n FROM gate_decisions WHERE severity='breach'").fetchone()["n"]
    ws.append(["G3 paper 90d", "B>=A and B>=hold-BTC; dd; costs", "day 90", None, None])
    ws.append(["G4 process", "validity>=95%; zero breaches", ">=95% / 0",
               f"{rate:.0f}% / {breaches}" if rate is not None else f"- / {breaches}",
               (rate or 0) >= 95 and breaches == 0 if validity["n"] else None])
    incidents = conn.execute(
        "SELECT COUNT(*) AS n FROM incidents WHERE opened_utc >= date('now','-30 days')"
    ).fetchone()["n"]
    ws.append(["G5 ops", "<=2 incidents/month", "<=2", incidents, incidents <= 2])
    ws.append(["G6 live propose 30d", "cost within 50% of paper; >=80% approvals", "live", None, None])
    ws.append(["G7 scale", "allocation x2 max per 90 days", "live", None, None])


def _limits_sheet(wb: Workbook, conn, cfg: EarnConfig) -> None:
    ws = wb.create_sheet("Limits")
    ws.append(["control", "configured", "current", "source"])
    r = cfg.risk
    state = {(row["sleeve"], row["key"]): row["value"]
             for row in conn.execute("SELECT * FROM risk_state")}
    ws.append(["max weight BTC", r.max_weight["BTC"], None, "custom_stake/confirm_entry"])
    ws.append(["max weight other", r.max_weight["default"], None, "gate"])
    ws.append(["max gross exposure", r.max_gross_exposure, None, "gate"])
    ws.append(["usdt floor", r.usdt_floor, None, "gate"])
    ws.append(["daily loss stop", r.daily_loss_stop,
               state.get(("a", "daily_stop_fired_date")), "gate + MaxDrawdown"])
    ws.append(["monthly loss stop", r.monthly_loss_stop,
               state.get(("a", "monthly_locked")), "gate; human resume"])
    ws.append(["max trades/day", r.max_trades_per_day,
               state.get(("a", "trades_today")), "gate"])
    ws.append(["stoploss guard", f"{r.stoploss_guard.count} in {r.stoploss_guard.window_hours}h",
               None, "StoplossGuard"])
    ws.append(["cooldown", f"{r.cooldown_candles} candles", None, "CooldownPeriod"])
    ws.append(["staleness", f"{r.staleness_minutes} min", None, "gate + healthcheck"])
    ws.append(["kill switch", r.kill_file,
               "ENGAGED" if (REPO_ROOT / r.kill_file).exists() else "clear", "everything"])


def write_workbook(cfg: EarnConfig, out: Path | None = None) -> Path:
    out = out or (REPO_ROOT / "reports" / "earn.xlsx")
    wb = Workbook()
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as conn:
        _nav_sheet(wb, conn)
        _trades_sheet(wb, conn)
        _costs_sheet(wb, conn)
        _gate_sheet(wb, conn)
        _limits_sheet(wb, conn, cfg)
    ws_meta = wb.create_sheet("_meta")
    ws_meta.append(["generated_at", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")])
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".xlsx")
    os.close(fd)
    wb.save(tmp)
    os.replace(tmp, out)
    return out


def main() -> int:
    cfg = load_config()
    out = write_workbook(cfg)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
