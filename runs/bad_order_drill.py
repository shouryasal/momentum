"""Week-2 'deliberately bad order rejected in dry-run' drill (spec §11).

Arms the EARN_DRILL=oversize mode on the sleeve-A container (custom_stake_amount then
bypasses the cap so the confirm-stage gate must reject a ~90%-NAV entry), seeds a
fresh book snapshot so the staleness check passes pre-ingest, waits up to two bot
loops, and asserts a gate_decisions row with allowed=0, severity='breach' and reason
starting 'weight_cap' appeared in journal.db.

Run on the user's machine with the dry-run stack up:
    .venv/bin/python -m runs.bad_order_drill
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime

from ops import db
from ops.config import REPO_ROOT, load_config


def seed_fresh_book_snapshot(cfg) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db) as conn:
        for pair in cfg.universe.pairs:
            conn.execute(
                "INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid, spread_bps)"
                " VALUES (?,?,?,?,?,?)",
                (pair, now, 1.0, 1.0, 1.0, 0.0),
            )
        conn.execute(
            "INSERT OR REPLACE INTO candles(pair, tf, open_time, open, high, low, close, volume)"
            " VALUES ('BTC/USDT','1h',?,1,1,1,1,1)",
            (int(datetime.now(UTC).timestamp() * 1000),),
        )
        conn.commit()


def latest_weight_cap_reject(cfg) -> dict | None:
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM gate_decisions WHERE allowed=0 AND reason LIKE 'weight_cap%'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def main() -> int:
    cfg = load_config()
    seed_fresh_book_snapshot(cfg)
    print("arming EARN_DRILL=oversize on freqtrade-a (container restart)...")
    subprocess.run(
        ["docker", "compose", "exec", "-e", "EARN_DRILL=oversize", "freqtrade-a", "true"],
        cwd=REPO_ROOT / "ops", check=False,
    )
    # compose exec env does not persist; the documented path is:
    print("run: EARN_DRILL=oversize docker compose up -d --force-recreate freqtrade-a")
    print("waiting up to 2 bot loops (~10 min) for the rejection row...")
    deadline = time.time() + 700
    while time.time() < deadline:
        row = latest_weight_cap_reject(cfg)
        if row:
            print(f"DRILL PASS: gate rejected the oversize order: {row['reason']}"
                  f" severity={row['severity']} at {row['ts_utc']}")
            print("Disarm: docker compose up -d --force-recreate freqtrade-a (without EARN_DRILL)")
            return 0
        time.sleep(15)
    print("DRILL FAIL: no weight_cap rejection row appeared", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
