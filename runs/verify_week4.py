"""Week-4 'done when' checker (spec §11): two valid proposals/day for 5 consecutive
days, costs journaled with pinned models, SleeveB consumption visible, month-to-date
spend printed. Run on the paper machine: .venv/bin/python -m runs.verify_week4"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import yaml

from ops import db
from ops.config import REPO_ROOT, load_config

PINNED = set()


def main(days: int = 5) -> int:
    cfg = load_config()
    models = yaml.safe_load((REPO_ROOT / cfg.models_config).read_text())["models"]
    pinned = set(models.values())
    ok = True
    with db.connect(REPO_ROOT / cfg.paths.journal_db, readonly=True) as jdb:
        today = datetime.now(UTC).date()
        for d in range(1, days + 1):
            day = (today - timedelta(days=d)).isoformat()
            rows = jdb.execute(
                "SELECT run_id, valid, consumed_status FROM proposals"
                " WHERE shadow=0 AND run_id LIKE ?", (day + "%",)).fetchall()
            valid = [r for r in rows if r["valid"]]
            consumed = [r for r in valid if r["consumed_status"] == "consumed"]
            line = f"{day}: {len(valid)}/2 valid, {len(consumed)} consumed by SleeveB"
            if len(valid) < 2:
                ok = False
                line += "  <-- FAIL"
            print(line)
        runs = jdb.execute(
            "SELECT run_id, stage, cost_usd, requested_model, served_model, status"
            " FROM runs WHERE kind='research' AND started_utc >= ?",
            ((today - timedelta(days=days)).isoformat(),)).fetchall()
        bad_cost = [r for r in runs if r["status"] == "success"
                    and (r["cost_usd"] is None or r["cost_usd"] <= 0)]
        bad_model = [r for r in runs if r["requested_model"]
                     and r["requested_model"] not in pinned]
        if bad_cost:
            ok = False
            print(f"FAIL: {len(bad_cost)} successful stages without a journaled cost")
        if bad_model:
            ok = False
            print(f"FAIL: unpinned models requested: {sorted({r['requested_model'] for r in bad_model})}")
        month = datetime.now(UTC).strftime("%Y-%m")
        spend = jdb.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS c FROM runs WHERE started_utc LIKE ?",
            (month + "%",)).fetchone()["c"]
        print(f"month-to-date Claude spend: ${spend:.2f}"
              f" (budget ${cfg.budgets.monthly_usd:.0f})")
    print("WEEK-4 GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(*(int(a) for a in sys.argv[1:2])))
