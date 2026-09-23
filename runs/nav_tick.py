"""15-minute NAV sampler — one ``nav_points`` row per sleeve plus the BTC benchmark.

**Ledger NAV, not wallet NAV.** The number this job writes is the bot's own capital:

    nav = seed + realised closed profit + P&L of the bot's own open trades

and nothing else on the exchange account counts. That is what the gate sizes against, so
a manual deposit, a second sleeve or an airdrop can neither inflate the bot's limits nor
hide a loss. USDT sitting in a resting entry order is still the bot's money, so it is
reported separately as ``reserved_usdt`` rather than being dropped from cash.

The benchmark row is "the same seed, in BTC, bought at run start". The anchor price is
stamped on the ``sleeve_runs`` row the first time this job sees the run, so a restart or a
backfill can never re-anchor a running comparison — which is the whole point of measuring
Earn against buy-and-hold.

The freqtrade responses and the clock are injected, so the arithmetic is unit-tested
against hand-written payloads.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config, seed_for
from ops.lib import mode_state as ms

BENCHMARK = "benchmark"


@dataclass(frozen=True)
class LedgerNav:
    """One sleeve's ledger NAV and the parts it is made of."""

    nav_usdt: float
    cash_usdt: float
    reserved_usdt: float
    realized_pnl: float
    unrealized_pnl: float
    open_trades: int
    positions: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "nav_usdt": round(self.nav_usdt, 8),
            "cash_usdt": round(self.cash_usdt, 8),
            "reserved_usdt": round(self.reserved_usdt, 8),
            "realized_pnl": round(self.realized_pnl, 8),
            "unrealized_pnl": round(self.unrealized_pnl, 8),
            "open_trades": self.open_trades,
            "positions": {k: round(v, 10) for k, v in sorted(self.positions.items())},
        }


def ledger_nav(
    *, seed_usdt: float, profit: Mapping[str, Any], open_trades: Sequence[Mapping[str, Any]]
) -> LedgerNav:
    """Compute ledger NAV from freqtrade's ``/profit`` and ``/status`` payloads."""
    realized = float(profit.get("profit_closed_coin", 0.0) or 0.0)
    unrealized = 0.0
    invested = 0.0
    reserved = 0.0
    positions: dict[str, float] = {}
    for trade in open_trades:
        stake = float(trade.get("stake_amount", 0.0) or 0.0)
        pnl = float(trade.get("profit_abs", 0.0) or 0.0)
        invested += stake
        unrealized += pnl
        if trade.get("open_order_id") or trade.get("has_open_orders"):
            reserved += stake
        pair = str(trade.get("pair", ""))
        amount = float(trade.get("amount", 0.0) or 0.0)
        if pair and amount:
            base = pair.split("/")[0].upper()
            positions[base] = positions.get(base, 0.0) + amount
    nav = float(seed_usdt) + realized + unrealized
    cash = nav - invested - unrealized
    return LedgerNav(
        nav_usdt=nav,
        cash_usdt=cash,
        reserved_usdt=reserved,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_trades=len(open_trades),
        positions=positions,
    )


# --------------------------------------------------------------------------- benchmark


def latest_price(kdb: sqlite3.Connection, pair: str, tf: str = "1h") -> float | None:
    """Newest closed candle close for ``pair`` — the price both NAV and benchmark use."""
    row = kdb.execute(
        "SELECT close FROM candles WHERE pair=? AND tf=? AND is_closed=1"
        " ORDER BY close_time DESC LIMIT 1",
        (pair, tf),
    ).fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


def benchmark_anchor(
    jdb: sqlite3.Connection, run: Mapping[str, Any], price: float | None
) -> float | None:
    """Return the run's anchor price, stamping it on first sight. Never re-anchors."""
    existing = run.get("benchmark_anchor_price")
    if existing:
        return float(existing)
    if price is None or price <= 0:
        return None
    db.write(
        jdb,
        "UPDATE sleeve_runs SET benchmark_anchor_price=? WHERE run_id=?",
        (float(price), run["run_id"]),
    )
    return float(price)


def benchmark_nav(seed_usdt: float, anchor_price: float, price: float) -> float:
    """What the seed would be worth held in the benchmark asset since the anchor."""
    if anchor_price <= 0:
        return float(seed_usdt)
    return float(seed_usdt) * (price / anchor_price)


# --------------------------------------------------------------------------- writer


def write_point(
    jdb: sqlite3.Connection,
    *,
    ts_utc: str,
    sleeve: str,
    run_id: str | None,
    mode: str,
    nav: LedgerNav,
    btc_price: float | None,
) -> None:
    db.write(
        jdb,
        "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt,"
        " reserved_usdt, positions_json, realized_pnl, unrealized_pnl, open_trades, btc_price)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(ts_utc, sleeve) DO UPDATE SET run_id=excluded.run_id,"
        " mode=excluded.mode, nav_usdt=excluded.nav_usdt, cash_usdt=excluded.cash_usdt,"
        " reserved_usdt=excluded.reserved_usdt, positions_json=excluded.positions_json,"
        " realized_pnl=excluded.realized_pnl, unrealized_pnl=excluded.unrealized_pnl,"
        " open_trades=excluded.open_trades, btc_price=excluded.btc_price",
        (
            ts_utc, sleeve, run_id, mode, nav.nav_usdt, nav.cash_usdt, nav.reserved_usdt,
            json.dumps(nav.positions, sort_keys=True), nav.realized_pnl, nav.unrealized_pnl,
            nav.open_trades, btc_price,
        ),
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def active_run(jdb: sqlite3.Connection, sleeve: str) -> sqlite3.Row | None:
    return jdb.execute(
        "SELECT * FROM sleeve_runs WHERE sleeve=? AND status='active'"
        " ORDER BY started_utc DESC LIMIT 1",
        (sleeve,),
    ).fetchone()


def run(
    cfg: EarnConfig,
    jdb: sqlite3.Connection,
    kdb: sqlite3.Connection,
    apis: Mapping[str, Any],
    *,
    now: datetime | None = None,
    state: ms.ModeState | None = None,
) -> dict[str, Any]:
    """Sample every sleeve plus the benchmark. Returns a small summary for the caller."""
    ts = _iso(now or datetime.now(UTC))
    st = state if state is not None else ms.load()
    bench_pair = cfg.sleeves.benchmark.pair
    btc_price = latest_price(kdb, bench_pair)

    written: list[str] = []
    missing: list[str] = []
    bench_source: tuple[sqlite3.Row, float] | None = None

    for sleeve in ("a", "b"):
        api = apis.get(sleeve)
        run_row = active_run(jdb, sleeve)
        sl = st.sleeve(sleeve)
        mode = "live" if sl.is_live else "test"
        seed = seed_for(cfg, sleeve, state=st)
        if run_row is not None:
            seed = float(run_row["seed_usdt"])
        if api is None:
            missing.append(sleeve)
            continue
        try:
            profit = api.profit() or {}
            trades = api.status() or []
        except Exception:  # noqa: BLE001 - a down bot is a gap, never a crash
            missing.append(sleeve)
            continue
        nav = ledger_nav(seed_usdt=seed, profit=profit, open_trades=trades)
        write_point(
            jdb, ts_utc=ts, sleeve=sleeve, run_id=run_row["run_id"] if run_row else None,
            mode=mode, nav=nav, btc_price=btc_price,
        )
        written.append(sleeve)
        if run_row is not None and (bench_source is None or sleeve == "b"):
            bench_source = (run_row, seed)

    benchmark_written = False
    if bench_source is not None and btc_price:
        run_row, seed = bench_source
        anchor = benchmark_anchor(jdb, dict(run_row), btc_price)
        if anchor:
            value = benchmark_nav(seed, anchor, btc_price)
            write_point(
                jdb, ts_utc=ts, sleeve=BENCHMARK, run_id=str(run_row["run_id"]),
                mode=str(run_row["mode"]),
                nav=LedgerNav(
                    nav_usdt=value, cash_usdt=0.0, reserved_usdt=0.0, realized_pnl=0.0,
                    unrealized_pnl=value - seed, open_trades=0,
                    positions={bench_pair.split("/")[0].upper(): seed / anchor},
                ),
                btc_price=btc_price,
            )
            benchmark_written = True

    return {
        "ts_utc": ts,
        "written": written,
        "missing": missing,
        "benchmark": benchmark_written,
        "btc_price": btc_price,
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - cron entry point
    from ops.lib import locks
    from ops.lib.freqtrade_api import BotApi

    cfg = load_config()
    with locks.acquire("nav_tick"):
        with db.opened(REPO_ROOT / cfg.paths.journal_db) as jdb, db.opened(
            REPO_ROOT / cfg.paths.knowledge_db
        ) as kdb:
            summary = run(cfg, jdb, kdb, {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")})
    if summary["missing"]:
        print(f"nav_tick: bot(s) unreachable: {summary['missing']}", file=sys.stderr)
    print(f"nav_tick {summary['ts_utc']}: wrote {summary['written'] or 'nothing'}")
    return 1 if summary["missing"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "BENCHMARK",
    "LedgerNav",
    "active_run",
    "benchmark_anchor",
    "benchmark_nav",
    "latest_price",
    "ledger_nav",
    "main",
    "run",
    "write_point",
]
