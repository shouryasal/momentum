"""Run metrics and run comparison — pure functions over the journal.

Everything here reads ``nav_points``, ``fills``, ``gate_decisions``, ``signals``,
``proposals`` and ``llm_calls`` for one ``sleeve_runs`` row and returns numbers. There is
no I/O beyond the connection it is handed and no model anywhere near it, so the Test Lab's
"compare these five runs" view and the final metrics stamped on a closed run come from the
same code path and can be hand-checked in a test.

Two deliberate choices:

* **Round trips are matched FIFO** from the journalled fills rather than read out of
  Freqtrade, because a run's Freqtrade database is per-run and disposable while the journal
  is the permanent record. Win rate, profit factor and average win/loss all derive from
  those round trips.
* **Annualisation is inferred** from the median spacing of the NAV series instead of being
  hard-coded to the 15-minute ``nav_tick`` cadence, so a sparse or backfilled series still
  produces an honest Sharpe rather than a confident wrong one.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_COMPARE_RUNS = 5
MIN_COMPARE_RUNS = 2
SECONDS_PER_YEAR = 365.25 * 24 * 3600
NORMALISED_BASE = 100.0


class MetricsError(Exception):
    pass


# --------------------------------------------------------------------------- primitives


def _parse(ts: str | None) -> datetime:
    if not ts:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def simple_returns(values: Sequence[float]) -> list[float]:
    """Period-over-period returns; a zero or negative NAV contributes nothing."""
    out: list[float] = []
    for prev, cur in zip(values, values[1:], strict=False):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough fall as a negative fraction (``-0.25`` = -25%)."""
    peak = float("-inf")
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def median_spacing_s(timestamps: Sequence[datetime]) -> float:
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(timestamps, timestamps[1:], strict=False)
        if (b - a).total_seconds() > 0
    ]
    return statistics.median(gaps) if gaps else 0.0


def annualisation(spacing_s: float) -> float:
    return SECONDS_PER_YEAR / spacing_s if spacing_s > 0 else 0.0


def sharpe(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 2 or periods_per_year <= 0:
        return None
    sd = statistics.pstdev(returns)
    if sd == 0:
        return None
    return (statistics.fmean(returns) / sd) * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 2 or periods_per_year <= 0:
        return None
    downside = [r for r in returns if r < 0]
    if not downside:
        return None
    dd = math.sqrt(statistics.fmean([r * r for r in downside]))
    if dd == 0:
        return None
    return (statistics.fmean(returns) / dd) * math.sqrt(periods_per_year)


def volatility(returns: Sequence[float], periods_per_year: float) -> float | None:
    if len(returns) < 2 or periods_per_year <= 0:
        return None
    return statistics.pstdev(returns) * math.sqrt(periods_per_year)


def cagr(nav_start: float, nav_end: float, days: float) -> float | None:
    if nav_start <= 0 or nav_end <= 0 or days <= 0:
        return None
    return (nav_end / nav_start) ** (365.25 / days) - 1.0


# --------------------------------------------------------------------------- round trips


@dataclass(frozen=True)
class RoundTrip:
    pair: str
    opened_utc: str
    closed_utc: str
    amount: float
    entry_price: float
    exit_price: float
    fees_usdt: float
    pnl_usdt: float

    @property
    def win(self) -> bool:
        return self.pnl_usdt > 0


def round_trips(fills: Iterable[Mapping[str, Any]], *, quote: str = "USDT") -> list[RoundTrip]:
    """FIFO-match buys against sells, one lot list per pair."""
    lots: dict[str, list[list[float | str]]] = {}
    out: list[RoundTrip] = []
    for row in fills:
        pair = str(row["pair"])
        side = str(row["side"]).lower()
        amount = float(row["fill_amount"] or 0.0)
        price = float(row["fill_price"] or 0.0)
        fee = float(row["fee_amount"] or 0.0)
        currency = str(row["fee_currency"] or quote).upper()
        fee_usdt = fee if currency == quote.upper() else fee * price
        ts = str(row["ts_utc"])
        if amount <= 0:
            continue
        if side == "buy":
            lots.setdefault(pair, []).append([amount, price, fee_usdt, ts])
            continue
        remaining = amount
        queue = lots.setdefault(pair, [])
        while remaining > 1e-12 and queue:
            lot = queue[0]
            take = min(remaining, float(lot[0]))
            entry_price = float(lot[1])
            entry_fee = float(lot[2]) * (take / float(lot[0])) if float(lot[0]) else 0.0
            exit_fee = fee_usdt * (take / amount) if amount else 0.0
            out.append(
                RoundTrip(
                    pair=pair,
                    opened_utc=str(lot[3]),
                    closed_utc=ts,
                    amount=take,
                    entry_price=entry_price,
                    exit_price=price,
                    fees_usdt=entry_fee + exit_fee,
                    pnl_usdt=take * (price - entry_price) - entry_fee - exit_fee,
                )
            )
            lot[0] = float(lot[0]) - take
            lot[2] = float(lot[2]) - entry_fee
            remaining -= take
            if float(lot[0]) <= 1e-12:
                queue.pop(0)
    return out


def win_rate(trips: Sequence[RoundTrip]) -> float | None:
    return (sum(1 for t in trips if t.win) / len(trips)) if trips else None


def profit_factor(trips: Sequence[RoundTrip]) -> float | None:
    gains = sum(t.pnl_usdt for t in trips if t.pnl_usdt > 0)
    losses = -sum(t.pnl_usdt for t in trips if t.pnl_usdt < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return gains / losses


# --------------------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Metrics:
    """Everything the Test Lab shows for one run. ``None`` means "not measurable yet"."""

    run_id: str
    sleeve: str = ""
    mode: str = ""
    submode: str | None = None
    label: str | None = None
    seed_usdt: float = 0.0
    started_utc: str = ""
    ended_utc: str | None = None
    config_sha: str | None = None
    days: float = 0.0
    points: int = 0
    nav_start: float | None = None
    nav_end: float | None = None
    return_pct: float | None = None
    cagr_pct: float | None = None
    max_drawdown_pct: float | None = None
    volatility_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    trades: int = 0
    win_rate: float | None = None
    profit_factor: float | None = None
    avg_win_usdt: float | None = None
    avg_loss_usdt: float | None = None
    exposure_pct: float | None = None
    turnover_usdt: float = 0.0
    fees_usdt: float = 0.0
    gate_rejects: int = 0
    gate_breaches: int = 0
    signals_total: int = 0
    signals_acted: int = 0
    signal_conversion: float | None = None
    decisions: int = 0
    llm_cost_usd: float = 0.0
    cost_per_decision_usd: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))


def nav_series(
    conn: sqlite3.Connection, run_id: str, sleeve: str | None = None
) -> list[sqlite3.Row]:
    if sleeve is None:
        return _rows(
            conn,
            "SELECT * FROM nav_points WHERE run_id=? AND sleeve!='benchmark' ORDER BY ts_utc",
            (run_id,),
        )
    return _rows(
        conn,
        "SELECT * FROM nav_points WHERE run_id=? AND sleeve=? ORDER BY ts_utc",
        (run_id, sleeve),
    )


def benchmark_series(conn: sqlite3.Connection, run: Mapping[str, Any]) -> list[sqlite3.Row]:
    """Benchmark points for this run, falling back to the run's time window.

    ``nav_points`` holds one benchmark row per timestamp (its primary key is
    ``(ts_utc, sleeve)``), so when the two sleeves are on different runs only one of them
    owns the benchmark rows by ``run_id``. The window fallback keeps the other sleeve's
    "vs BTC" column honest instead of blank.
    """
    rows = _rows(
        conn,
        "SELECT * FROM nav_points WHERE run_id=? AND sleeve='benchmark' ORDER BY ts_utc",
        (run["run_id"],),
    )
    if rows:
        return rows
    return _rows(
        conn,
        "SELECT * FROM nav_points WHERE sleeve='benchmark' AND ts_utc >= ? AND ts_utc <= ?"
        " ORDER BY ts_utc",
        (run["started_utc"], run["ended_utc"] or "9999-12-31T23:59:59Z"),
    )


def compute(conn: sqlite3.Connection, run_id: str) -> Metrics:
    """Every metric for one run. Raises :class:`MetricsError` if the run is unknown."""
    run = conn.execute("SELECT * FROM sleeve_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise MetricsError(f"unknown run {run_id!r}")
    sleeve = str(run["sleeve"])

    points = nav_series(conn, run_id, sleeve)
    navs = [float(r["nav_usdt"]) for r in points]
    stamps = [_parse(r["ts_utc"]) for r in points]
    ppy = annualisation(median_spacing_s(stamps))
    rets = simple_returns(navs)

    nav_start = float(run["seed_usdt"]) if navs else None
    nav_end = navs[-1] if navs else None
    if navs:
        nav_start = navs[0]
    days = 0.0
    if stamps:
        days = max(0.0, (stamps[-1] - stamps[0]).total_seconds() / 86400)
    elif run["started_utc"]:
        end = _parse(run["ended_utc"]) if run["ended_utc"] else datetime.now(UTC)
        days = max(0.0, (end - _parse(run["started_utc"])).total_seconds() / 86400)

    fills = _rows(
        conn,
        "SELECT * FROM fills WHERE sleeve=? AND run_id=? ORDER BY ts_utc, id",
        (sleeve, run_id),
    )
    trips = round_trips(fills)
    wins = [t.pnl_usdt for t in trips if t.pnl_usdt > 0]
    losses = [t.pnl_usdt for t in trips if t.pnl_usdt < 0]
    turnover = sum(
        float(f["fill_amount"] or 0.0) * float(f["fill_price"] or 0.0) for f in fills
    )
    fees = sum(t.fees_usdt for t in trips) or sum(
        float(f["fee_amount"] or 0.0)
        * (1.0 if str(f["fee_currency"] or "USDT").upper() == "USDT" else float(f["fill_price"] or 0.0))
        for f in fills
    )

    exposure = None
    exposures = [
        1.0 - (float(r["cash_usdt"]) / float(r["nav_usdt"]))
        for r in points
        if r["cash_usdt"] is not None and float(r["nav_usdt"] or 0) > 0
    ]
    if exposures:
        exposure = statistics.fmean(exposures)

    gate = conn.execute(
        "SELECT SUM(severity='reject') AS rejects, SUM(severity='breach') AS breaches"
        " FROM gate_decisions WHERE sleeve=? AND run_id=?",
        (sleeve, run_id),
    ).fetchone()

    window = (run["started_utc"], run["ended_utc"] or "9999-12-31T23:59:59Z")
    signals = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status='acted') AS acted FROM signals"
        " WHERE ts_utc >= ? AND ts_utc <= ?",
        window,
    ).fetchone()
    decisions = conn.execute(
        "SELECT COUNT(*) AS n FROM proposals WHERE ts_utc >= ? AND ts_utc <= ?", window
    ).fetchone()
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM llm_calls WHERE ts_utc >= ? AND ts_utc <= ?",
        window,
    ).fetchone()

    bench = benchmark_series(conn, run)
    bench_navs = [float(r["nav_usdt"]) for r in bench]
    bench_ret = (
        (bench_navs[-1] / bench_navs[0] - 1.0) if len(bench_navs) >= 2 and bench_navs[0] > 0
        else None
    )
    total_ret = (
        (navs[-1] / navs[0] - 1.0) if len(navs) >= 2 and navs[0] > 0 else None
    )

    total_signals = int(signals["total"] or 0) if signals else 0
    acted = int(signals["acted"] or 0) if signals else 0
    n_decisions = int(decisions["n"] or 0) if decisions else 0
    llm_cost = float(cost["c"] or 0.0) if cost else 0.0

    return Metrics(
        run_id=run_id,
        sleeve=sleeve,
        mode=str(run["mode"]),
        submode=run["submode"],
        label=run["label"],
        seed_usdt=float(run["seed_usdt"]),
        started_utc=str(run["started_utc"]),
        ended_utc=run["ended_utc"],
        config_sha=run["config_sha"],
        days=round(days, 4),
        points=len(points),
        nav_start=nav_start,
        nav_end=nav_end,
        return_pct=_pct(total_ret),
        cagr_pct=_pct(cagr(navs[0], navs[-1], days) if len(navs) >= 2 else None),
        max_drawdown_pct=_pct(max_drawdown(navs)) if navs else None,
        volatility_pct=_pct(volatility(rets, ppy)),
        sharpe=_round(sharpe(rets, ppy)),
        sortino=_round(sortino(rets, ppy)),
        trades=len(trips),
        win_rate=_round(win_rate(trips)),
        profit_factor=_round(profit_factor(trips)),
        avg_win_usdt=_round(statistics.fmean(wins)) if wins else None,
        avg_loss_usdt=_round(statistics.fmean(losses)) if losses else None,
        exposure_pct=_pct(exposure),
        turnover_usdt=round(turnover, 4),
        fees_usdt=round(fees, 6),
        gate_rejects=int((gate["rejects"] if gate else 0) or 0),
        gate_breaches=int((gate["breaches"] if gate else 0) or 0),
        signals_total=total_signals,
        signals_acted=acted,
        signal_conversion=_round(acted / total_signals) if total_signals else None,
        decisions=n_decisions,
        llm_cost_usd=round(llm_cost, 6),
        cost_per_decision_usd=_round(llm_cost / n_decisions) if n_decisions else None,
        benchmark_return_pct=_pct(bench_ret),
        excess_return_pct=(
            _pct(total_ret - bench_ret) if total_ret is not None and bench_ret is not None
            else None
        ),
    )


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100, 4)


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return value
    return round(value, digits)


# --------------------------------------------------------------------------- compare


@dataclass(frozen=True)
class Comparison:
    run_ids: list[str]
    metrics: list[Metrics]
    series: dict[str, list[dict[str, float | str]]] = field(default_factory=dict)
    deltas: dict[str, dict[str, float | None]] = field(default_factory=dict)
    config_diff: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_ids": self.run_ids,
            "metrics": [m.to_json() for m in self.metrics],
            "series": self.series,
            "deltas": self.deltas,
            "config_diff": self.config_diff,
        }


def normalised_series(
    conn: sqlite3.Connection, run_id: str, sleeve: str | None = None
) -> list[dict[str, float | str]]:
    """NAV rebased to 100 at the run's first point, with elapsed days on the x axis."""
    rows = nav_series(conn, run_id, sleeve)
    if not rows:
        return []
    base = float(rows[0]["nav_usdt"]) or 1.0
    t0 = _parse(rows[0]["ts_utc"])
    return [
        {
            "ts_utc": str(r["ts_utc"]),
            "days": round((_parse(r["ts_utc"]) - t0).total_seconds() / 86400, 6),
            "nav": round(float(r["nav_usdt"]), 6),
            "index": round(float(r["nav_usdt"]) / base * NORMALISED_BASE, 6),
        }
        for r in rows
    ]


_DELTA_FIELDS: tuple[str, ...] = (
    "return_pct", "cagr_pct", "max_drawdown_pct", "volatility_pct", "sharpe", "sortino",
    "trades", "win_rate", "profit_factor", "exposure_pct", "turnover_usdt", "fees_usdt",
    "gate_rejects", "gate_breaches", "signal_conversion", "cost_per_decision_usd",
    "excess_return_pct",
)


def compare(conn: sqlite3.Connection, run_ids: Sequence[str]) -> Comparison:
    """Overlay 2–5 runs: normalised NAV, a metric-delta table and the config changes."""
    ids = list(dict.fromkeys(run_ids))
    if not MIN_COMPARE_RUNS <= len(ids) <= MAX_COMPARE_RUNS:
        raise MetricsError(
            f"compare takes {MIN_COMPARE_RUNS}–{MAX_COMPARE_RUNS} runs, got {len(ids)}"
        )
    metrics = [compute(conn, rid) for rid in ids]
    series = {rid: normalised_series(conn, rid) for rid in ids}
    series.update(
        {f"{rid}:benchmark": normalised_series(conn, rid, "benchmark") for rid in ids}
    )
    base = metrics[0]
    deltas: dict[str, dict[str, float | None]] = {}
    for m in metrics[1:]:
        row: dict[str, float | None] = {}
        for name in _DELTA_FIELDS:
            a, b = getattr(base, name), getattr(m, name)
            row[name] = (
                round(float(b) - float(a), 6)
                if isinstance(a, (int, float)) and isinstance(b, (int, float))
                and not math.isinf(float(a)) and not math.isinf(float(b))
                else None
            )
        deltas[m.run_id] = row
    return Comparison(ids, metrics, series, deltas, config_changes(conn, metrics))


def config_changes(
    conn: sqlite3.Connection, metrics: Sequence[Metrics]
) -> list[dict[str, Any]]:
    """Config saves landed between the earliest and latest run start, from ``config_audit``.

    The runs themselves only record a ``config_sha``; the audit trail is what turns two
    different shas into "these keys moved".
    """
    if not metrics:
        return []
    starts = sorted(m.started_utc for m in metrics if m.started_utc)
    if len(starts) < 2:
        return []
    rows = conn.execute(
        "SELECT ts_utc, file, changed_paths_json, reason, actor FROM config_audit"
        " WHERE ts_utc > ? AND ts_utc <= ? ORDER BY ts_utc",
        (starts[0], starts[-1]),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            changed = json.loads(r["changed_paths_json"] or "[]")
        except json.JSONDecodeError:
            changed = []
        out.append(
            {
                "ts_utc": r["ts_utc"],
                "file": r["file"],
                "changed_paths": changed,
                "reason": r["reason"],
                "actor": r["actor"],
            }
        )
    return out


__all__ = [
    "MAX_COMPARE_RUNS",
    "MIN_COMPARE_RUNS",
    "Comparison",
    "Metrics",
    "MetricsError",
    "RoundTrip",
    "annualisation",
    "cagr",
    "compare",
    "compute",
    "config_changes",
    "max_drawdown",
    "median_spacing_s",
    "normalised_series",
    "profit_factor",
    "round_trips",
    "sharpe",
    "simple_returns",
    "sortino",
    "volatility",
    "win_rate",
]
