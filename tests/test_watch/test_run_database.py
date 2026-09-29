"""The watcher must read the database the bot is ACTUALLY writing trades to.

``tradesv3.sqlite`` is only Freqtrade's default. A test or live run is minted with its own
``runs/<run_id>.sqlite`` and the container is pointed at it through ``db_url`` in
``var/runtime/freqtrade-<s>.mode.json``. ``runs/watch/positions.py`` read the default
unconditionally, so from the 2026-09-23 profile transition onward every cycle found an
empty table: six days of ``holdings: 0`` in ``logs/watch.log`` with positions open in both
sleeves, ``run_once`` returning before its loop, and the numeric invalidation, stop-proximity
and weight checks never running. The between-decisions safety layer was not watching.
"""

from __future__ import annotations

import json
import sqlite3

from runs.watch import positions as positionslib

from .conftest import NOW, TRADES_DDL, seed_trade


def _overlay(root, sleeve, run_id):
    d = root / "var" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"freqtrade-{sleeve}.mode.json").write_text(json.dumps({
        "dry_run": True,
        "db_url": f"sqlite:////freqtrade/user_data/runs/{run_id}.sqlite",
    }), encoding="utf-8")


def _seed_run_db(root, sleeve, run_id, *, pair="SOL/USDT", amount=2.5, open_rate=200.0):
    path = root / "ft_userdata" / sleeve / "runs" / f"{run_id}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(TRADES_DDL)
    conn.execute(
        "INSERT INTO trades(id, pair, is_open, open_rate, amount, open_date, stop_loss,"
        " stop_loss_pct, initial_stop_loss, max_rate, min_rate, close_profit_abs,"
        " stake_amount) VALUES (1,?,1,?,?,?,?,?,?,?,?,0.0,?)",
        (pair, open_rate, amount, NOW.strftime("%Y-%m-%d %H:%M:%S.%f"),
         open_rate * 0.94, -0.06, open_rate * 0.94, open_rate, open_rate,
         amount * open_rate))
    conn.commit()
    conn.close()
    return path


def test_the_overlays_run_database_wins_over_the_default(root):
    _seed_run_db(root, "a", "test-a-000")
    _overlay(root, "a", "test-a-000")
    assert positionslib._ft_db("a", root).name == "test-a-000.sqlite"


def test_the_open_position_in_the_run_database_is_seen(root):
    """The exact shape of the 09-23..09-29 blindness: default empty, run database open."""
    seed_trade(root, "a", is_open=0)                      # the old ledger, nothing open
    _seed_run_db(root, "a", "test-a-000")                 # where the bot really trades
    _overlay(root, "a", "test-a-000")
    db = positionslib._ft_db("a", root)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT pair FROM trades WHERE is_open=1").fetchall()
    assert [r[0] for r in rows] == ["SOL/USDT"]


def test_no_overlay_keeps_the_default(root):
    seed_trade(root, "a")
    assert positionslib._ft_db("a", root).name == "tradesv3.sqlite"


def test_an_overlay_naming_a_file_that_does_not_exist_falls_back(root):
    """Fail soft, never fail blind: a stale overlay must not point the watcher at nothing."""
    seed_trade(root, "a")
    _overlay(root, "a", "test-a-999")                     # never created
    assert positionslib._ft_db("a", root).name == "tradesv3.sqlite"


def test_an_unreadable_overlay_keeps_the_default(root):
    seed_trade(root, "a")
    d = root / "var" / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    (d / "freqtrade-a.mode.json").write_text("{not json", encoding="utf-8")
    assert positionslib._ft_db("a", root).name == "tradesv3.sqlite"
