"""Expanding-window walk-forward driver for Sleeve A.

Sleeve A has fixed rules (no fitting), so each out-of-sample window is an independent
backtest; the in-sample span only provides indicator warm-up. Windows step
`oos_months` at a time from `start` to now. Each window shells out to the freqtrade
docker image; results (zip/json in ft_userdata/a/backtest_results) are parsed into
reports/backtests/walkforward.json for g2_check and the review run.

Pure helpers (window arithmetic, result parsing, summarizing) are unit-tested; the
docker invocation itself runs on the user's machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from ops.config import REPO_ROOT, load_config

RESULTS_DIR = REPO_ROOT / "ft_userdata" / "a" / "backtest_results"
OUT = REPO_ROOT / "reports" / "backtests" / "walkforward.json"


def month_add(d: date, months: int) -> date:
    y, m = divmod((d.year * 12 + d.month - 1) + months, 12)
    return date(y, m + 1, 1)


def windows(start: str, oos_months: int, today: date) -> list[tuple[str, str]]:
    """Expanding windows: [(oos_start, oos_end)] stepping oos_months, warm-up implied."""
    first = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:8]}")
    out = []
    t = month_add(first, 12)  # first year is warm-up only
    while t < today:
        end = min(month_add(t, oos_months), today)
        out.append((t.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        t = end
    return out


def parse_backtest_zip(zip_path: Path, strategy: str | None = None) -> dict:
    """Extract the metrics g2 needs from a freqtrade backtest result zip.

    ``strategy`` names which block to read; the default takes the only one present, so the
    same parser serves Sleeve A's walk-forward and the console's Sleeve B backtests.
    """
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist()
                    if n.endswith(".json") and not n.endswith("_config.json")
                    and "market_change" not in n)
        data = json.loads(z.read(name))
    strategies = data["strategy"]
    if strategy is not None:
        res = strategies[strategy]
    elif len(strategies) == 1:
        res = next(iter(strategies.values()))
    else:
        res = strategies["SleeveA"]
    return {
        "profit_total_pct": res.get("profit_total") * 100 if res.get("profit_total") is not None else None,
        "max_drawdown_pct": (res.get("max_drawdown_account") or res.get("max_drawdown") or 0) * 100,
        "trades": res.get("total_trades"),
        "fees_paid": res.get("total_fees") if "total_fees" in res else None,
        "start": res.get("backtest_start"),
        "end": res.get("backtest_end"),
    }


def newest_result_zip(results_dir: Path = RESULTS_DIR) -> Path | None:
    zips = sorted(results_dir.glob("backtest-result-*.zip"))
    return zips[-1] if zips else None


def run_window(timerange: str, fee: float) -> None:
    subprocess.run(
        ["docker", "compose", "run", "--rm", "freqtrade-a", "backtesting",
         "--strategy", "SleeveA",
         "--config", "/freqtrade/earn-config/freqtrade-a.json",
         "--timerange", timerange, "--fee", str(fee),
         "--enable-protections", "--export", "trades",
         "--backtest-directory", "/freqtrade/user_data/backtest_results"],
        cwd=REPO_ROOT / "ops", check=True,
    )


def main(start: str = "20210101", oos_months: int = 6) -> int:
    import yaml

    load_config()  # validates the repo config before an expensive run
    costs = yaml.safe_load((REPO_ROOT / "config" / "backtest.yaml").read_text())["costs"]
    fee = (costs["fee_bps"] + costs["slippage_bps"]) / 10000
    today = datetime.now(UTC).date()
    results = []
    for oos_start, oos_end in windows(start, oos_months, today):
        run_window(f"{oos_start}-{oos_end}", fee)
        z = newest_result_zip()
        if z is None:
            print(f"no result zip after window {oos_start}-{oos_end}", file=sys.stderr)
            return 1
        results.append({"window": f"{oos_start}-{oos_end}", **parse_backtest_zip(z)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"scheme": "expanding", "oos_months": oos_months, "fee_per_side": fee,
         "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "windows": results},
        indent=2) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2], *(int(a) for a in sys.argv[2:3])))
