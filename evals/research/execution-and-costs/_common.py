"""Shared pieces for the execution-and-costs study (2026-09-29).

Research-only. Nothing here is imported by the product. Reads the feather store through
``runs.features.load_candles`` (honours ``$EARN_DATA_DIR``) and the survivorship-free
daily panel; writes only under ``evals/research/execution-and-costs/results/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

#: The three regimes every table in this study is split by (task brief).
REGIMES: dict[str, tuple[str, str]] = {
    "2019-22": ("2019-01-01", "2022-12-31"),
    "2023-24": ("2023-01-01", "2024-12-31"),
    "2025-26": ("2025-01-01", "2026-09-24"),
}

#: Verified on 2026-09-29 from https://www.binance.com/en/fee/schedule (Regular User /
#: VIP 0: maker 0.100% / taker 0.100%; with BNB 0.075% / 0.075%; VIP 1 needs >= 1,000,000
#: USD 30-day volume AND >= 5 BNB) and the BNB-discount FAQ (25% off spot, must be toggled
#: on, insufficient BNB balance => the original fee is charged, "valid until further
#: notice").
FEE_VIP0 = 0.0010
FEE_VIP0_BNB = 0.00075
BNB_DISCOUNT = 0.25
#: config/backtest.yaml: 10 bps fee + 5 bps slippage per side, the repo's standing cost.
SLIPPAGE = 0.0005
COST_SIDE = FEE_VIP0 + SLIPPAGE          # 15 bps
COST_SIDE_BNB = FEE_VIP0_BNB + SLIPPAGE  # 12.5 bps


def utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def cagr_vol_mdd(net: pd.Series, *, periods_per_year: float) -> dict[str, float]:
    """CAGR, annualised vol, Sharpe = CAGR/vol (the repo's convention), MaxDD, total."""
    r = net.to_numpy(dtype=float)
    n = len(r)
    if n == 0:
        return {"cagr": np.nan, "vol": np.nan, "sharpe": np.nan, "mdd": np.nan,
                "total": np.nan, "n": 0}
    eq = np.cumprod(1.0 + r)
    cagr = float(eq[-1] ** (periods_per_year / n) - 1.0)
    vol = float(np.std(r) * np.sqrt(periods_per_year))
    mdd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    return {"cagr": cagr, "vol": vol, "sharpe": (cagr / vol if vol > 0 else np.nan),
            "mdd": mdd, "total": float(eq[-1] - 1.0), "n": int(n)}


def hold_series(close: pd.Series, start: str, end: str) -> pd.Series:
    """Buy-and-hold daily net returns of one asset over a window (no cost: nothing trades)."""
    c = close[(close.index >= utc(start)) & (close.index <= utc(end))]
    return c.pct_change().fillna(0.0)


def write_json(name: str, payload: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / name
    path.write_text(json.dumps(payload, indent=2, default=_jsonable), encoding="utf-8")
    return path


def _jsonable(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, (np.ndarray, pd.Series)):
        return [float(x) for x in np.asarray(o)]
    return str(o)


def md_table(rows: list[dict], cols: list[str], fmt: dict[str, str] | None = None) -> str:
    fmt = fmt or {}
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c, "")
            f = fmt.get(c)
            if f and isinstance(v, (int, float, np.floating, np.integer)) and not (
                    isinstance(v, float) and np.isnan(v)):
                cells.append(format(v, f))
            else:
                cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)
