"""Daily NAV job (00:10 Gulf): one nav_daily row per sleeve from each bot's REST
/balance, plus the buy-and-hold benchmark row (sleeve_c_benchmark). Closes the
"nobody writes sleeve NAV" gap — the digest, Excel NAV sheet, G3 evaluation and the
near-stop hard-case flag all read these rows.

Like ``runs/nav_tick.py`` this reads the bot, and a bot only knows the database it is
running on: ``/balance`` is ``dry_run_wallet`` plus that database's profit. A restart onto
a fresh database (2026-09-23 23:37Z) therefore resets this series to the seed too — the
2026-09-24 rows read 9,994.78 for both sleeves although 69.77 had already been lost the
evening before. The cumulative figure lives in ``console.services.pot_service``, which
reads every run database rather than asking the bot.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import locks
from ops.lib.freqtrade_api import BotApi
from runs import sleeve_c_benchmark


def sleeve_nav(api: BotApi) -> tuple[float, float, dict] | None:
    """(nav_usdt, cash_usdt, positions) from /balance; None when the bot is down."""
    try:
        bal = api.balance()
    except Exception:
        return None
    total = float(bal.get("total", 0.0))
    cash = 0.0
    positions: dict[str, float] = {}
    for c in bal.get("currencies", []):
        if c.get("currency") == "USDT":
            cash = float(c.get("free", 0.0))
        elif float(c.get("balance", 0.0)):
            positions[c["currency"]] = float(c.get("balance", 0.0))
    return total, cash, positions


def active_run_id(jdb, sleeve: str) -> str | None:
    """The sleeve_runs row this day belongs to, so a day can be attributed to a run."""
    try:
        row = jdb.execute(
            "SELECT run_id FROM sleeve_runs WHERE sleeve=? AND status='active'"
            " ORDER BY started_utc DESC LIMIT 1", (sleeve,)).fetchone()
    except Exception:  # noqa: BLE001 - an older journal has no sleeve_runs table
        return None
    return str(row["run_id"]) if row else None


def write_nav(jdb, sleeve: str, date_utc: str, nav: float, cash: float,
              positions: dict, run_id: str | None = None) -> None:
    prev_max = jdb.execute(
        "SELECT MAX(nav_usdt) AS m FROM nav_daily WHERE sleeve=?", (sleeve,)).fetchone()
    dd = None
    if prev_max and prev_max["m"]:
        peak = max(prev_max["m"], nav)
        dd = (nav / peak - 1) * 100
    trades = jdb.execute(
        "SELECT COUNT(*) AS n FROM fills WHERE sleeve=? AND ts_utc LIKE ?",
        (sleeve, f"{date_utc}%")).fetchone()["n"]
    jdb.execute(
        "INSERT INTO nav_daily(date_utc, sleeve, nav_usdt, cash_usdt, positions_json,"
        " drawdown_pct, trades_today, run_id) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(date_utc, sleeve) DO UPDATE SET nav_usdt=excluded.nav_usdt,"
        " cash_usdt=excluded.cash_usdt, positions_json=excluded.positions_json,"
        " drawdown_pct=excluded.drawdown_pct, trades_today=excluded.trades_today,"
        " run_id=COALESCE(excluded.run_id, nav_daily.run_id)",
        (date_utc, sleeve, nav, cash, json.dumps(positions), dd, trades, run_id))
    jdb.commit()


def run(cfg: EarnConfig, jdb, apis: dict[str, BotApi], now: datetime) -> int:
    date_utc = now.strftime("%Y-%m-%d")
    missing = []
    for sleeve, api in apis.items():
        got = sleeve_nav(api)
        if got is None:
            missing.append(sleeve)
            continue
        nav, cash, positions = got
        write_nav(jdb, sleeve, date_utc, nav, cash, positions, active_run_id(jdb, sleeve))
    if missing:
        print(f"nav_job: bot(s) unreachable: {missing}", file=sys.stderr)
    return 1 if missing else 0


def main() -> int:
    cfg = load_config()
    now = datetime.now(UTC)
    with locks.acquire("nav"):
        with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb:
            rc = run(cfg, jdb, {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")}, now)
    try:
        sleeve_c_benchmark.main()
    except Exception as e:  # benchmark needs candles; never fail the sleeve rows on it
        print(f"nav_job: benchmark row failed: {e}", file=sys.stderr)
        rc = rc or 1
    try:  # postflight: rebuild the what-if track (Excel testing mode)
        from runs import whatif

        whatif.main()
    except Exception as e:  # noqa: BLE001 — the simulator never fails NAV rows
        print(f"nav_job: whatif rebuild failed: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
