"""Deterministic market-state computation — "code, not estimates" by construction.

Reads knowledge/earn.db (1d candles, funding, book snapshots), computes per-asset
trend / realized vol / drawdown / funding and the portfolio regime, and writes
knowledge/state/latest.json (+ a state_snapshots audit row). Callable standalone
(python3 compute_state.py) and imported by runs/research_run.py before any model call.

Definitions are pinned in ../references/definitions.md; tests assert golden values.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

VOL_LOOKBACK_D = 20
MA_SHORT, MA_LONG = 50, 200
HYSTERESIS = 0.01
DD_WINDOW_D = 90
VOL_LOW, VOL_HIGH = 0.30, 0.60  # annualized bounds: low < 0.30 <= med < 0.60 <= high


def _candles_1d(kdb, pair: str) -> pd.DataFrame:
    rows = kdb.execute(
        "SELECT open_time, close FROM candles WHERE pair=? AND tf='1d' AND is_closed=1"
        " ORDER BY open_time", (pair,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def asset_state(kdb, pair: str) -> dict | None:
    df = _candles_1d(kdb, pair)
    if len(df) < MA_LONG + 1:
        return None
    close = df["close"]
    ma50 = close.rolling(MA_SHORT).mean().iloc[-1]
    ma200 = close.rolling(MA_LONG).mean().iloc[-1]
    last = close.iloc[-1]
    if last > ma200 * (1 + HYSTERESIS):
        trend = "up"
    elif last < ma200 * (1 - HYSTERESIS):
        trend = "down"
    else:
        trend = "flat"
    log_ret = np.log(close / close.shift(1))
    rvol = float(log_ret.iloc[-VOL_LOOKBACK_D:].std() * np.sqrt(365))
    peak = close.iloc[-DD_WINDOW_D:].max()
    dd = float(last / peak - 1)
    frow = kdb.execute("SELECT last_rate FROM funding_current WHERE symbol=?",
                       (pair.replace("/", ""),)).fetchone()
    return {
        "close": float(last), "ma50": float(ma50), "ma200": float(ma200),
        "trend": trend, "rvol_20d": round(rvol, 4),
        "drawdown_from_90d_high": round(dd, 4),
        "funding_8h": float(frow["last_rate"]) if frow else None,
        "asof_candle_utc": df["date"].iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def portfolio_state(assets: dict[str, dict], prev: dict | None,
                    now: datetime) -> dict:
    ups = [a for a, s in assets.items() if s["trend"] == "up"]
    btc = assets.get("BTC")
    vols = [s["rvol_20d"] for s in assets.values()]
    vmax = max(vols) if vols else 0.0
    vol_regime = "low" if vmax < VOL_LOW else ("med" if vmax < VOL_HIGH else "high")
    if vol_regime == "high":
        regime = "high_vol"
    elif btc and btc["trend"] == "up":
        regime = "trend_up"
    elif btc and btc["trend"] == "down":
        regime = "trend_down"
    else:
        regime = "range"
    prev_regime = (prev or {}).get("portfolio", {}).get("regime")
    prev_changed = (prev or {}).get("portfolio", {}).get("regime_changed_utc")
    changed_utc = (prev_changed if regime == prev_regime and prev_changed
                   else now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    trend_signal = "long" if (btc and btc["trend"] == "up") else "flat"
    return {
        "regime": regime,
        "regime_changed_utc": changed_utc,
        "breadth_above_200d": round(len(ups) / max(len(assets), 1), 2),
        "modules": {
            "trend_signal": trend_signal,
            "vol_regime": vol_regime,
            "disagreement": trend_signal == "long" and vol_regime == "high",
        },
    }


def data_freshness(kdb, now: datetime) -> tuple[bool, int]:
    row = kdb.execute("SELECT MAX(captured_at) AS m FROM book_snapshots").fetchone()
    candle = kdb.execute("SELECT MAX(open_time) AS m FROM candles WHERE tf='1h'").fetchone()
    ages = []
    if row and row["m"]:
        book_dt = datetime.fromisoformat(row["m"].replace("Z", "+00:00"))
        ages.append((now - book_dt).total_seconds() / 60)
    if candle and candle["m"]:
        cdt = datetime.fromtimestamp(candle["m"] / 1000, tz=UTC)
        ages.append(max((now - cdt).total_seconds() / 60 - 60, 0.0))
    if not ages:
        return False, 10**6
    age = int(max(ages))
    return age <= 30, age


def compute_and_write(cfg, kdb, root: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    out_path = root / cfg.paths.state_latest
    prev = None
    try:
        prev = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    assets = {}
    for pair in cfg.universe.pairs:
        s = asset_state(kdb, pair)
        if s is not None:
            assets[pair.split("/")[0]] = s
    fresh, age = data_freshness(kdb, now)
    state = {
        "computed_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asof_candle_utc": max((s["asof_candle_utc"] for s in assets.values()),
                               default=None),
        "assets": assets,
        "portfolio": portfolio_state(assets, prev, now) if assets else
                     {"regime": "unknown", "regime_changed_utc": None,
                      "breadth_above_200d": None,
                      "modules": {"trend_signal": "flat", "vol_regime": "unknown",
                                  "disagreement": False}},
        "data_fresh": fresh,
        "newest_data_age_min": age,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    kdb.execute(
        "INSERT INTO state_snapshots(ts_utc, asof_candle_utc, state_json, regime)"
        " VALUES (?,?,?,?)",
        (state["computed_utc"], state["asof_candle_utc"] or "",
         json.dumps(state), state["portfolio"]["regime"]))
    kdb.commit()
    return state


def main() -> int:
    from ops import db
    from ops.config import load_config

    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.knowledge_db) as kdb:
        state = compute_and_write(cfg, kdb, REPO_ROOT)
    print(json.dumps({"regime": state["portfolio"]["regime"],
                      "data_fresh": state["data_fresh"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
