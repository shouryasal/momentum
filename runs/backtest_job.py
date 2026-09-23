"""Human-driven backtests and walk-forwards from the console (Backtest Lab).

This is the *operator's* backtest runner: it exists so a human can try a parameter or a
config patch against history before touching the live config. The self-improvement loop has
its own evidence runner (``evals/verify_change.py``); the two deliberately do not share a
queue, because a human exploring ideas must never be able to starve or pollute the evidence
that gates an automated change.

Every run is a ``backtest_runs`` row from the moment it is queued, so the UI can show a
queue with progress over SSE and a result table that survives a console restart. The
freqtrade invocation is injected (:data:`Runner`), the result parsing is
``runs.walk_forward.parse_backtest_zip``, and the costs default to the TCA-measured numbers
in ``config/backtest.yaml`` rather than to freqtrade's defaults.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import compose as composelib
from runs import walk_forward

KINDS: tuple[str, ...] = ("backtest", "walk_forward")
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TIMERANGE_RE = re.compile(r"^\d{8}-(\d{8})?$")

#: ``(argv, cwd, timeout_s) -> CommandResult`` — the same shape as ops.lib.compose
Runner = composelib.Runner


class BacktestError(Exception):
    pass


# --------------------------------------------------------------------------- request


@dataclass(frozen=True)
class BacktestRequest:
    kind: str = "backtest"
    sleeve: str = "a"
    strategy: str | None = None
    timerange: str = ""
    config_patch: Mapping[str, Any] = field(default_factory=dict)
    fee_bps: float | None = None
    slippage_bps: float | None = None
    oos_months: int = 6
    note: str | None = None

    def validate(self, cfg: EarnConfig) -> BacktestRequest:
        if self.kind not in KINDS:
            raise BacktestError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.sleeve.lower() not in ("a", "b"):
            raise BacktestError(f"unknown sleeve {self.sleeve!r}")
        if not TIMERANGE_RE.match(self.timerange or ""):
            raise BacktestError(
                f"timerange must look like 20240101-20241231, got {self.timerange!r}"
            )
        return self

    def strategy_name(self, cfg: EarnConfig) -> str:
        return self.strategy or getattr(cfg.sleeves, self.sleeve.lower()).strategy


def default_costs(root: Path | None = None) -> tuple[float, float]:
    """``(fee_bps, slippage_bps)`` from ``config/backtest.yaml`` — the measured numbers."""
    path = (root or REPO_ROOT) / "config" / "backtest.yaml"
    try:
        costs = yaml.safe_load(path.read_text())["costs"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return 10.0, 5.0
    return float(costs.get("fee_bps", 10.0)), float(costs.get("slippage_bps", 5.0))


# --------------------------------------------------------------------------- rows


def new_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S")
    return f"bt-{stamp}-{uuid.uuid4().hex[:6]}"


def create(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    request: BacktestRequest,
    *,
    actor: str,
    now: datetime | None = None,
    root: Path | None = None,
) -> str:
    """Queue a backtest and return its id. The row exists before anything runs."""
    request.validate(cfg)
    fee, slip = default_costs(root)
    bt_id = new_id(now)
    db.write(
        conn,
        "INSERT INTO backtest_runs(id, started_utc, actor, kind, strategy, timerange,"
        " config_patch_json, fee_bps, slippage_bps, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            bt_id,
            (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            actor,
            request.kind,
            request.strategy_name(cfg),
            request.timerange,
            json.dumps(dict(request.config_patch), sort_keys=True),
            request.fee_bps if request.fee_bps is not None else fee,
            request.slippage_bps if request.slippage_bps is not None else slip,
            STATUS_QUEUED,
        ),
    )
    return bt_id


def set_status(
    conn: sqlite3.Connection,
    bt_id: str,
    status: str,
    *,
    metrics: Mapping[str, Any] | None = None,
    report_path: str | None = None,
    error: str | None = None,
    finished: bool = False,
    now: datetime | None = None,
) -> None:
    db.write(
        conn,
        "UPDATE backtest_runs SET status=?, metrics_json=COALESCE(?, metrics_json),"
        " report_path=COALESCE(?, report_path), error=COALESCE(?, error),"
        " finished_utc=COALESCE(?, finished_utc) WHERE id=?",
        (
            status,
            json.dumps(dict(metrics), sort_keys=True, default=str) if metrics else None,
            report_path,
            error,
            (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ") if finished else None,
            bt_id,
        ),
    )


def cancel(conn: sqlite3.Connection, bt_id: str, *, now: datetime | None = None) -> bool:
    row = get(conn, bt_id)
    if row is None or row["status"] in (STATUS_OK, STATUS_FAILED, STATUS_CANCELLED):
        return False
    set_status(conn, bt_id, STATUS_CANCELLED, finished=True, now=now, error="cancelled by operator")
    return True


def get(conn: sqlite3.Connection, bt_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM backtest_runs WHERE id=?", (bt_id,)).fetchone()
    return dict(row) if row else None


def listing(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM backtest_runs ORDER BY started_utc DESC LIMIT ?", (int(limit),)
        )
    ]


# --------------------------------------------------------------------------- execution


def patch_file(
    bt_id: str, patch: Mapping[str, Any], *, root: Path | None = None
) -> Path | None:
    """Write the config patch as a freqtrade overlay config; ``None`` when empty."""
    if not patch:
        return None
    target = (root or REPO_ROOT) / "ft_userdata" / "backtests" / f"{bt_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(patch), indent=2, sort_keys=True) + "\n")
    return target


def backtest_argv(
    cfg: EarnConfig,
    request: BacktestRequest,
    *,
    timerange: str,
    fee: float,
    patch: Path | None,
    root: Path | None = None,
) -> list[str]:
    """The ``docker compose run`` argv for one backtest window."""
    sleeve = request.sleeve.lower()
    service = cfg.ops.bots[sleeve].service  # type: ignore[index]
    argv = [
        "docker", "compose", "-p", cfg.runtime.docker.compose_project,
        "-f", str(composelib.base_path()),
        "run", "--rm", service, "backtesting",
        "--strategy", request.strategy_name(cfg),
        "--config", f"/freqtrade/earn-config/freqtrade-{sleeve}.json",
    ]
    if patch is not None:
        argv += ["--config", f"/freqtrade/user_data/backtests/{patch.name}"]
    argv += [
        "--timerange", timerange,
        "--fee", f"{fee:.8f}",
        "--enable-protections",
        "--export", "trades",
        "--backtest-directory", "/freqtrade/user_data/backtest_results",
    ]
    return argv


def results_dir(sleeve: str, root: Path | None = None) -> Path:
    return (root or REPO_ROOT) / "ft_userdata" / sleeve.lower() / "backtest_results"


def execute(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    bt_id: str,
    request: BacktestRequest,
    *,
    runner: Runner,
    root: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    timeout_s: float = 1800.0,
    newest_zip: Callable[[Path], Path | None] | None = None,
) -> dict[str, Any]:
    """Run every window of a queued backtest and store the metrics. Never raises."""
    root = root or REPO_ROOT
    find_zip = newest_zip or walk_forward.newest_result_zip
    row = get(conn, bt_id)
    if row is None:
        raise BacktestError(f"unknown backtest {bt_id!r}")
    fee = (float(row["fee_bps"] or 0) + float(row["slippage_bps"] or 0)) / 10000.0
    patch = patch_file(bt_id, request.config_patch, root=root)

    if request.kind == "walk_forward":
        start = request.timerange.split("-")[0]
        windows = [
            f"{a}-{b}"
            for a, b in walk_forward.windows(start, request.oos_months, now().date())
        ]
    else:
        windows = [request.timerange]
    if not windows:
        set_status(conn, bt_id, STATUS_FAILED, error="no windows to run", finished=True, now=now())
        return {"status": STATUS_FAILED, "error": "no windows to run"}

    set_status(conn, bt_id, STATUS_RUNNING, now=now())
    if progress is not None:
        progress("started", {"id": bt_id, "windows": len(windows)})

    results: list[dict[str, Any]] = []
    for idx, window in enumerate(windows, start=1):
        current = get(conn, bt_id)
        if current is not None and current["status"] == STATUS_CANCELLED:
            return {"status": STATUS_CANCELLED, "windows": results}
        argv = backtest_argv(
            cfg, request, timerange=window, fee=fee, patch=patch, root=root
        )
        outcome = runner(argv, root / "ops", timeout_s)
        if not outcome.ok:
            message = outcome.output.strip()[:1000] or f"exit {outcome.returncode}"
            set_status(conn, bt_id, STATUS_FAILED, error=message, finished=True, now=now())
            if progress is not None:
                progress("failed", {"id": bt_id, "window": window, "error": message})
            return {"status": STATUS_FAILED, "error": message, "windows": results}
        zip_path = find_zip(results_dir(request.sleeve, root))
        parsed: dict[str, Any] = {"window": window}
        if zip_path is not None:
            try:
                parsed.update(walk_forward.parse_backtest_zip(zip_path))
            except (OSError, KeyError, StopIteration, ValueError) as e:
                parsed["parse_error"] = str(e)
        else:
            parsed["parse_error"] = "no result archive produced"
        results.append(parsed)
        if progress is not None:
            progress("window", {"id": bt_id, "index": idx, "of": len(windows), **parsed})

    metrics = summarise(results)
    set_status(
        conn, bt_id, STATUS_OK, metrics={"windows": results, **metrics},
        report_path=str(results_dir(request.sleeve, root)), finished=True, now=now(),
    )
    if progress is not None:
        progress("finished", {"id": bt_id, **metrics})
    return {"status": STATUS_OK, "windows": results, **metrics}


def summarise(windows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate window results into the headline numbers the UI ranks on."""
    profits = [
        float(w["profit_total_pct"])
        for w in windows
        if w.get("profit_total_pct") is not None
    ]
    drawdowns = [
        float(w["max_drawdown_pct"])
        for w in windows
        if w.get("max_drawdown_pct") is not None
    ]
    trades = [int(w["trades"]) for w in windows if w.get("trades") is not None]
    compounded: float | None = None
    if profits:
        compounded = 1.0
        for p in profits:
            compounded *= 1.0 + p / 100.0
        compounded = (compounded - 1.0) * 100.0
    return {
        "windows_run": len(windows),
        "profit_total_pct": round(compounded, 4) if compounded is not None else None,
        "profit_mean_pct": round(sum(profits) / len(profits), 4) if profits else None,
        "worst_window_pct": round(min(profits), 4) if profits else None,
        "max_drawdown_pct": round(max(drawdowns), 4) if drawdowns else None,
        "trades": sum(trades) if trades else 0,
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator entry point
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("usage: python -m runs.backtest_job <sleeve> <timerange> [walk_forward]")
        return 2
    cfg = load_config()
    request = BacktestRequest(
        kind="walk_forward" if len(args) > 2 and args[2] == "walk_forward" else "backtest",
        sleeve=args[0],
        timerange=args[1],
    ).validate(cfg)
    with db.opened(REPO_ROOT / cfg.paths.journal_db) as conn:
        bt_id = create(conn, cfg, request, actor="human:cli")
        result = execute(conn, cfg, bt_id, request, runner=composelib.subprocess_runner)
    print(json.dumps({"id": bt_id, **result}, indent=2, default=str))
    return 0 if result.get("status") == STATUS_OK else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "KINDS",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "BacktestError",
    "BacktestRequest",
    "Runner",
    "backtest_argv",
    "cancel",
    "create",
    "default_costs",
    "execute",
    "get",
    "listing",
    "new_id",
    "patch_file",
    "results_dir",
    "set_status",
    "summarise",
]
