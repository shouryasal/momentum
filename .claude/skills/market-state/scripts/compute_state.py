"""Deterministic market-state computation — "code, not estimates" by construction.

Reads CLOSED daily candles through the one feather-∪-DB path the trend ensemble uses
(``runs.features.trend.daily_closes``: ``data/binance/<PAIR>-1d.feather`` for the years of
history, ``knowledge/earn.db: candles`` for the bars ingest refreshed in the last
``ingest.cold_start_days``), plus funding and book snapshots from the DB. Computes per-asset
trend / realized vol / drawdown / funding for the CORE assets, a compact breadth block for the
rest of the tradeable universe, and the portfolio regime, then writes
``knowledge/state/latest.json`` (+ a ``state_snapshots`` audit row). Callable standalone
(``python3 compute_state.py``) and imported by ``runs/research_run.py`` before any model call.

Why the union (docs/design/paper-trading-review-2026-09-29.md §2 #7, §6 item 4): the DB alone
holds ~13 daily bars against the 201 the 200d MA needs, so every research proposal of the
first paper week was handed ``assets: {}`` and abstained — 11.86 USD of model spend for zero
output. Why core only (not every tradeable pair): ``runs/build_prompt.py: INPUT_BUDGETS``
gives the state 1000 tokens and keeps the TAIL on overflow; 31 asset blocks would have cut BTC
and ETH out of the prompt. Why the file can never be silently empty: an ``assets`` block that
is missing a core asset carries ``reason`` (one readable line per missing asset), ``status``
is ``partial`` or ``empty``, and the same line goes to stderr so the research log shows it.

Definitions are pinned in ../references/definitions.md; tests assert golden values.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# The feather ∪ DB daily series — imported, not copied: one definition of "the closed daily
# closes" for the ensemble's trend.json and this file, so the two can never disagree on a bar.
from runs.features.trend import daily_closes  # noqa: E402

VOL_LOOKBACK_D = 20
MA_SHORT, MA_LONG = 50, 200
#: Closed daily bars an asset needs before its block is written: the 200d MA plus one return.
MIN_BARS = MA_LONG + 1
HYSTERESIS = 0.01
DD_WINDOW_D = 90
VOL_LOW, VOL_HIGH = 0.30, 0.60  # annualized bounds: low < 0.30 <= med < 0.60 <= high
_DAY = timedelta(days=1)
_ISO = "%Y-%m-%dT%H:%M:%SZ"

#: ``status`` values of the written file. ``ok``: every core asset computed. ``partial``: some
#: core asset is missing (``reason`` says which and why). ``empty``: ``assets`` is ``{}``.
STATUS_OK, STATUS_PARTIAL, STATUS_EMPTY = "ok", "partial", "empty"


# --------------------------------------------------------------------------- inputs


def _data_root(cfg, root: Path) -> Path:
    """Where the feather store lives: ``$EARN_DATA_DIR``, else ``<root>/<paths.data_dir>``.

    Mirrors ``runs.features.trend._data_root`` so ``trend.json`` and ``latest.json`` read the
    same candles; it is not imported because it is that module's private helper.
    """
    override = os.environ.get("EARN_DATA_DIR")
    if override:
        return Path(override).expanduser()
    rel = getattr(getattr(cfg, "paths", None), "data_dir", None) or "data"
    p = Path(rel)
    return p if p.is_absolute() else Path(root) / p


def _core_pairs(cfg) -> list[str]:
    uni = cfg.universe
    core = list(getattr(uni, "core", None) or ("BTC", "ETH"))
    quote = str(getattr(uni, "quote", None) or "USDT")
    return [f"{a}/{quote}" for a in core]


def _watchlist_pairs(cfg) -> list[str]:
    """Tradeable pairs that are not core — breadth context, never a per-name story."""
    core = set(_core_pairs(cfg))
    return [p for p in (getattr(cfg.universe, "pairs", None) or []) if p not in core]


def _window_gap(closes: pd.Series, n: int) -> tuple[int, str] | None:
    """Calendar days missing inside the last ``n`` bars, and where the largest hole sits."""
    window = closes.iloc[-n:]
    span = int((window.index[-1] - window.index[0]) / _DAY) + 1
    if span == n:
        return None
    steps = window.index.to_series().diff()
    after = steps.idxmax()
    before = window.index[window.index.get_loc(after) - 1]
    return span - n, f"{before.date()} -> {after.date()}"


def _feather_name(pair: str) -> str:
    return f"binance/{pair.replace('/', '_')}-1d.feather"


def asset_state(kdb, pair: str, *, data_root: Path | None = None,
                now: datetime | None = None) -> tuple[dict | None, str | None]:
    """``(indicators, None)`` for the last CLOSED daily bar, or ``(None, reason)``.

    The reason is one line a human can act on: how many bars each source contributed, what
    the computation needs, and which store to refresh. It never returns ``(None, None)`` —
    a missing asset always says why.
    """
    asset = pair.split("/")[0]
    closes, source = daily_closes(pair, kdb=kdb, data_root=data_root, now=now)
    where = (f"{_feather_name(pair)} under {data_root}" if data_root is not None
             else "no feather store given")
    if len(closes) == 0:
        return None, (f"{asset}: no closed daily bars: neither the feather store ({where})"
                      f" nor knowledge/earn.db candles(tf='1d') has any")
    first, last = closes.index[0].date(), closes.index[-1].date()
    if len(closes) < MIN_BARS:
        if source == "kdb":
            hint = (f"the feather store has no {_feather_name(pair)} under {data_root}; refresh"
                    " data/binance so history reaches back at least 201 days")
        elif source == "feather":
            hint = "knowledge/earn.db has no closed 1d bars for it (has ingest run?)"
        else:
            hint = "both stores together are too short; refresh data/binance"
        return None, (f"{asset}: {len(closes)} closed daily bars ({source}, {first} -> {last})"
                      f" < {MIN_BARS} needed for the 200d MA; {hint}")
    gap = _window_gap(closes, MIN_BARS)
    if gap is not None:
        missing, hole = gap
        return None, (f"{asset}: {missing} calendar day(s) missing ({hole}) inside the last"
                      f" {MIN_BARS} bars ({source}); refresh data/binance so the feather store"
                      " overlaps the DB's window")
    close = closes
    ma50 = close.rolling(MA_SHORT).mean().iloc[-1]
    ma200 = close.rolling(MA_LONG).mean().iloc[-1]
    last_close = close.iloc[-1]
    if last_close > ma200 * (1 + HYSTERESIS):
        trend = "up"
    elif last_close < ma200 * (1 - HYSTERESIS):
        trend = "down"
    else:
        trend = "flat"
    log_ret = np.log(close / close.shift(1))
    rvol = float(log_ret.iloc[-VOL_LOOKBACK_D:].std() * np.sqrt(365))
    peak = close.iloc[-DD_WINDOW_D:].max()
    dd = float(last_close / peak - 1)
    frow = None
    if kdb is not None:
        try:
            frow = kdb.execute("SELECT last_rate FROM funding_current WHERE symbol=?",
                               (pair.replace("/", ""),)).fetchone()
        except Exception:  # noqa: BLE001 — funding is context; its absence is not a refusal
            frow = None
    bar_open = closes.index[-1].to_pydatetime()
    return {
        "close": float(last_close), "ma50": float(ma50), "ma200": float(ma200),
        "trend": trend, "rvol_20d": round(rvol, 4),
        "drawdown_from_90d_high": round(dd, 4),
        "ret_1d": round(float(close.iloc[-1] / close.iloc[-2] - 1), 4),
        "funding_8h": float(frow["last_rate"]) if frow else None,
        "asof_candle_utc": bar_open.strftime(_ISO),
        "asof_candle_close_utc": (bar_open + _DAY).strftime(_ISO),
        "bars": int(len(closes)),
        "source": source,
    }, None


# --------------------------------------------------------------------------- aggregates


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
                   else now.strftime(_ISO))
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


def watchlist_breadth(kdb, pairs: list[str], *, data_root: Path | None,
                      now: datetime) -> dict:
    """Breadth across the non-core tradeable pairs, as counts — context, not a signal.

    The measured cross-section of alts loses money (SKILL.md), so the file carries how many
    names are above their own 200d MA and the median 1d return, never a block per name: a
    per-name block for 29 pairs would push BTC and ETH out of the prompt's state budget.
    """
    computed: list[dict] = []
    for pair in pairs:
        s, _ = asset_state(kdb, pair, data_root=data_root, now=now)
        if s is not None:
            computed.append(s)
    above = sum(1 for s in computed if s["trend"] == "up")
    rets = [s["ret_1d"] for s in computed]
    return {
        "n": len(pairs),
        "computed": len(computed),
        "missing": len(pairs) - len(computed),
        "above_200d": above,
        "share_above_200d": round(above / len(computed), 2) if computed else None,
        "median_ret_1d": round(float(np.median(rets)), 4) if rets else None,
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


_UNKNOWN_PORTFOLIO = {
    "regime": "unknown", "regime_changed_utc": None, "breadth_above_200d": None,
    "modules": {"trend_signal": "flat", "vol_regime": "unknown", "disagreement": False},
}


def compute_state(cfg, kdb, root: Path, now: datetime | None = None,
                  prev: dict | None = None) -> dict:
    """The payload :func:`compute_and_write` writes. Pure given its inputs and ``now``."""
    now = now or datetime.now(UTC)
    data_root = _data_root(cfg, root)
    assets: dict[str, dict] = {}
    missing: dict[str, str] = {}
    for pair in _core_pairs(cfg):
        s, why = asset_state(kdb, pair, data_root=data_root, now=now)
        if s is not None:
            assets[pair.split("/")[0]] = s
        else:
            missing[pair.split("/")[0]] = why or f"{pair}: no reason recorded (bug)"
    if not assets:
        status = STATUS_EMPTY
    elif missing:
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK
    reason = None
    if missing:
        reason = ("assets block incomplete - the decision stage has no indicators for: "
                  + "; ".join(missing.values()))
    fresh, age = data_freshness(kdb, now)
    return {
        "computed_utc": now.strftime(_ISO),
        "asof_candle_utc": max((s["asof_candle_utc"] for s in assets.values()),
                               default=None),
        "status": status,
        "reason": reason,
        "missing": missing,
        "assets": assets,
        "watchlist": watchlist_breadth(kdb, _watchlist_pairs(cfg), data_root=data_root,
                                       now=now),
        "portfolio": (portfolio_state(assets, prev, now) if assets
                      else json.loads(json.dumps(_UNKNOWN_PORTFOLIO))),
        "data_fresh": fresh,
        "newest_data_age_min": age,
        "inputs": {
            "daily_bars_needed": MIN_BARS,
            "feather_store": str(data_root),
            "candles_db": "knowledge/earn.db",
        },
    }


def compute_and_write(cfg, kdb, root: Path, now: datetime | None = None) -> dict:
    """Compute, write ``paths.state_latest`` atomically, journal a ``state_snapshots`` row.

    Returns the written state. When ``state["reason"]`` is set the same line is printed to
    stderr, so an empty or partial ``assets`` block is loud in the research log as well as
    in the file the decision stage reads.
    """
    now = now or datetime.now(UTC)
    out_path = root / cfg.paths.state_latest
    prev = None
    try:
        prev = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    state = compute_state(cfg, kdb, root, now, prev)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    if state["reason"]:
        print(f"compute_state: status={state['status']}: {state['reason']}", file=sys.stderr)
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
                      "data_fresh": state["data_fresh"],
                      "status": state["status"],
                      "assets": sorted(state["assets"]),
                      "asof_candle_utc": state["asof_candle_utc"],
                      "reason": state["reason"]}, indent=2))
    return 0 if state["status"] == STATUS_OK else 1


if __name__ == "__main__":
    sys.exit(main())
