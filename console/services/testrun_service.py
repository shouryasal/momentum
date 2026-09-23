"""Test-run lifecycle for the Test Lab: history, one run's detail, and comparison.

A *run* is a row in ``sleeve_runs``: one seed, one Freqtrade database, one set of
run-scoped risk anchors. Resetting does not erase anything — it closes the current row with
its final metrics and a snapshot of the run-scoped risk state and opens a new one, so a
six-week-old experiment stays fully inspectable while the new run starts from clean anchors.

Metrics come from ``runs.test_metrics`` (pure functions over the journal), so what the Test
Lab shows for a live run and what was stamped on a closed run are computed by the same code.

The **dry-run caveat** travels with the numbers rather than living in the frontend: TEST
fills are simulated and therefore optimistic, and :func:`tca_caveat` measures by how much,
comparing the TCA-observed cost with the assumption in ``config/backtest.yaml``. A P&L view
that cannot tell you that is a P&L view that misleads you.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from ops.lib import mode_state as ms
from runs import test_metrics

MAX_TRADES = 500
MAX_POINTS = 4000


class TestRunError(RuntimeError):
    pass


# --------------------------------------------------------------------------- listing


def listing(
    conn: sqlite3.Connection, *, sleeve: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sleeve_runs"
    params: list[Any] = []
    if sleeve:
        sql += " WHERE sleeve=?"
        params.append(sleeve.lower())
    sql += " ORDER BY started_utc DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


def active(conn: sqlite3.Connection, sleeve: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sleeve_runs WHERE sleeve=? AND status='active'"
        " ORDER BY started_utc DESC LIMIT 1",
        (sleeve.lower(),),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- detail


def detail(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    run_id: str,
    *,
    root: Path | None = None,
    include_series: bool = True,
) -> dict[str, Any]:
    """Metrics, NAV series, trades and the caveat banner for one run."""
    row = conn.execute("SELECT * FROM sleeve_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise TestRunError(f"unknown run {run_id!r}")
    run = dict(row)
    metrics = test_metrics.compute(conn, run_id)
    out: dict[str, Any] = {
        "run": run,
        "metrics": metrics.to_json(),
        "caveat": tca_caveat(conn, cfg, run["sleeve"], mode=str(run["mode"]), root=root),
    }
    if include_series:
        out["series"] = test_metrics.normalised_series(conn, run_id, str(run["sleeve"]))[
            :MAX_POINTS
        ]
        out["benchmark"] = test_metrics.normalised_series(conn, run_id, "benchmark")[:MAX_POINTS]
        out["trades"] = [
            {
                "pair": t.pair,
                "opened_utc": t.opened_utc,
                "closed_utc": t.closed_utc,
                "amount": t.amount,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "fees_usdt": round(t.fees_usdt, 6),
                "pnl_usdt": round(t.pnl_usdt, 6),
                "win": t.win,
            }
            for t in test_metrics.round_trips(
                conn.execute(
                    "SELECT * FROM fills WHERE sleeve=? AND run_id=? ORDER BY ts_utc, id",
                    (run["sleeve"], run_id),
                )
            )
        ][-MAX_TRADES:]
    return out


def compare(
    conn: sqlite3.Connection, run_ids: Sequence[str], *, cfg: EarnConfig | None = None
) -> dict[str, Any]:
    """Overlay 2–5 runs. Raises :class:`TestRunError` outside that range."""
    try:
        return test_metrics.compare(conn, run_ids).to_json()
    except test_metrics.MetricsError as e:
        raise TestRunError(str(e)) from e


# --------------------------------------------------------------------------- caveat


def tca_caveat(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    sleeve: str,
    *,
    mode: str = "test",
    root: Path | None = None,
    window: str = "30d",
) -> dict[str, Any]:
    """How optimistic this run's fills are, measured rather than asserted.

    ``assumed_bps`` is what the backtests and the what-if curve charge; ``measured_bps`` is
    the median total cost TCA has observed on real fills. In TEST the difference is the
    discount to apply to every P&L number on the page.
    """
    assumed = _assumed_bps(root)
    measured = None
    try:
        row = conn.execute(
            "SELECT total_bps_med FROM tca_rolling WHERE sleeve=? AND window=?"
            " ORDER BY day DESC LIMIT 1",
            (sleeve.lower(), window),
        ).fetchone()
        measured = float(row["total_bps_med"]) if row and row["total_bps_med"] is not None else None
    except sqlite3.Error:
        measured = None
    gap = None if measured is None else round(measured - assumed, 2)
    return {
        "simulated": mode == "test",
        "assumed_bps": assumed,
        "measured_bps": measured,
        "gap_bps": gap,
        "text": (
            "Dry-run fills are optimistic: orders fill at the book, never partially and never"
            " with a queue. "
            + (
                f"Measured execution cost is {measured:.1f} bps against a {assumed:.1f} bps"
                f" assumption ({gap:+.1f} bps)."
                if measured is not None
                else f"No measured TCA yet; costs are assumed at {assumed:.1f} bps."
            )
        )
        if mode == "test"
        else "Live fills; costs are measured, not assumed.",
    }


def _assumed_bps(root: Path | None = None) -> float:
    import yaml

    path = (root or REPO_ROOT) / "config" / "backtest.yaml"
    try:
        costs = yaml.safe_load(path.read_text())["costs"]
        return float(costs["fee_bps"]) + float(costs["slippage_bps"])
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        return 15.0


# --------------------------------------------------------------------------- pending seed


def pending_reset(
    cfg: EarnConfig, conn: sqlite3.Connection, sleeve: str, *, state: ms.ModeState | None = None
) -> dict[str, Any]:
    """Whether ``modes.test.seed_usdt`` has moved away from the running run's seed.

    Freqtrade's wallet is bound to the run, so a seed change cannot be applied in place —
    the Test Lab offers "Reset run to apply" and this is what tells it to.
    """
    run = active(conn, sleeve)
    configured = float(cfg.modes.test.seed_usdt.get(sleeve.lower(), 0.0))
    current = float(run["seed_usdt"]) if run else None
    return {
        "sleeve": sleeve.lower(),
        "run_id": run["run_id"] if run else None,
        "current_seed_usdt": current,
        "configured_seed_usdt": configured,
        "reset_required": current is not None and abs(current - configured) > 1e-9,
    }


def summary(
    cfg: EarnConfig, conn: sqlite3.Connection, sleeve: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """The Test Lab's active-run card: seed, age, headline metrics, pending seed change."""
    run = active(conn, sleeve)
    if run is None:
        return {"sleeve": sleeve.lower(), "run": None, "metrics": None,
                "pending": pending_reset(cfg, conn, sleeve)}
    metrics = test_metrics.compute(conn, str(run["run_id"]))
    started = run["started_utc"]
    days = None
    try:
        days = round(
            max(
                0.0,
                (
                    (now or datetime.now(UTC))
                    - datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                ).total_seconds()
                / 86400,
            ),
            2,
        )
    except ValueError:
        days = None
    return {
        "sleeve": sleeve.lower(),
        "run": run,
        "days": days,
        "metrics": metrics.to_json(),
        "pending": pending_reset(cfg, conn, sleeve),
        "caveat": tca_caveat(conn, cfg, sleeve, mode=str(run["mode"])),
    }


__all__ = [
    "MAX_POINTS",
    "MAX_TRADES",
    "TestRunError",
    "active",
    "compare",
    "detail",
    "listing",
    "pending_reset",
    "summary",
    "tca_caveat",
]
