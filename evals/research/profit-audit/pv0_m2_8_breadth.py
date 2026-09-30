"""pv0 independent re-derivation of claim M2-8 (breadth, not per-name edge).

Claim under test:
  combined point-in-time EXCLUDED 8-slot book Sharpe 1.118 beats every single-reason
  sub-bucket (best = ADV<$1M at 0.761); excluded fills 5.76 of 8 slots from 128 members,
  satellite book fills 5.02 from 70.

Written from the config rules + panel only. Arithmetic Sharpe mean/std*sqrt(365).
Read-only; no writes outside this workspace. 0 new selection trials (descriptive re-check).
"""
import json, os, re
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
START = pd.Timestamp("2019-01-01", tz="UTC")
SLOTS, STAKE, CB = 8, 0.05, 0.0015

snap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
R = snap["rules"]
LEV = tuple(R["leveraged_suffixes"])
EQ = re.compile(R["equity_token_pattern"])
BAD_BASE = set(R["excluded_bases"]) | set(R.get("data_only", []))
MIN_AGE = R["min_listing_age_days"]
MIN_VOL = R["min_ann_vol_120d"]
MIN_WE = R["min_weekend_volume_ratio"]
MIN_ADV = R["min_median_quote_volume_usdt"]
SAT_ADV = snap["tiers"]["satellite_min_volume_usdt"]

pan = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                      columns=["date", "h", "l", "c", "qv", "symbol"])
pan = pan[pan["symbol"].str.endswith("USDT")].sort_values(["symbol", "date"])

cols = {}


def base_ok(sym):
    b = sym[:-4]
    if b.endswith(LEV) or EQ.match(b) or not b.isascii() or b in BAD_BASE:
        return False
    return True


for sym, g in pan.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 200:
        continue
    g = g.set_index("date")
    c, h, l, qv = g["c"], g["h"], g["l"], g["qv"]
    ret = c.pct_change().fillna(0.0)

    # --- shipped entry rule on 1d: ema6>ema18, close above prior 3d high, atr% band
    e_f = c.ewm(span=6, adjust=False, min_periods=6).mean()
    e_s = c.ewm(span=18, adjust=False, min_periods=18).mean()
    prev_c = c.shift(1)
    tr = np.maximum.reduce([(h - l).to_numpy(),
                            (h - prev_c).abs().to_numpy(),
                            (l - prev_c).abs().to_numpy()])
    atrp = pd.Series(tr, index=g.index).rolling(14, min_periods=14).mean() / c.where(c > 0)
    trend = (e_f > e_s).to_numpy()
    fire = (trend & (c > h.rolling(3, min_periods=3).max().shift(1)).to_numpy()
            & (atrp >= 0.0015).to_numpy() & (atrp <= 0.070).to_numpy())
    # state machine: latch on fire, drop when trend breaks
    state = np.empty(len(c), dtype=bool)
    live = False
    for i in range(len(c)):
        live = (live and trend[i]) or ((not live) and fire[i])
        state[i] = live
    held = pd.Series(state, index=g.index).shift(1).fillna(False)

    # --- point-in-time eligibility, all inputs lagged one day
    qs = qv.shift(1)
    adv = qs.rolling(90, min_periods=30).median()
    v120 = ret.shift(1).rolling(120, min_periods=60).std() * np.sqrt(365)
    dow = pd.Series(g.index.dayofweek, index=g.index)
    we = qs.where(dow >= 5).rolling(120, min_periods=20).median()
    wd = qs.where(dow < 5).rolling(120, min_periods=40).median()
    age = pd.Series((g.index - g.index[0]).days, index=g.index)

    cols.setdefault("held", {})[sym] = held
    cols.setdefault("ret", {})[sym] = ret
    cols.setdefault("adv", {})[sym] = adv
    cols.setdefault("c_age", {})[sym] = age >= MIN_AGE
    cols.setdefault("c_vol", {})[sym] = (v120 >= MIN_VOL).fillna(False)
    cols.setdefault("c_we", {})[sym] = ((we / wd.where(wd > 0)) >= MIN_WE).fillna(False)
    cols.setdefault("c_adv", {})[sym] = (adv >= MIN_ADV).fillna(False)
    cols.setdefault("c_nm", {})[sym] = pd.Series(base_ok(sym), index=g.index)

syms = sorted(cols["held"])
idx = pd.DatetimeIndex(sorted(set().union(*(set(v.index) for v in cols["held"].values()))))
F = {k: pd.DataFrame(v).reindex(idx) for k, v in cols.items()}
RET = F["ret"]
LISTED = ~RET.isna()
RR = RET.fillna(0.0)
Q = F["adv"]
H = F["held"].fillna(False).astype(bool)
C = {k: F[k].fillna(False).astype(bool) for k in ("c_age", "c_vol", "c_we", "c_adv", "c_nm")}
for core in ("BTCUSDT", "ETHUSDT"):
    if core in syms:
        for k in C:
            C[k][core] = True
WATCH = C["c_age"] & C["c_vol"] & C["c_we"] & C["c_adv"] & C["c_nm"]
SAT = WATCH & (Q >= SAT_ADV).fillna(False)
EXCL = LISTED & ~WATCH


def book(mask, label):
    sel = H & mask
    sel = sel & (Q.where(sel).rank(axis=1, ascending=False, na_option="bottom") <= SLOTS)
    prev = sel.shift(1).fillna(False)
    ent = (sel.astype(int) - prev.astype(int)).clip(lower=0).sum(axis=1)
    ext = (prev.astype(int) - sel.astype(int)).clip(lower=0).sum(axis=1)
    net = ((RR * prev.astype(float)).sum(axis=1) * STAKE
           - (ent + ext) * STAKE * CB).loc[START:]
    eq = (1 + net).cumprod()
    yrs = len(net) / 365.0
    return dict(book=label, days=len(net),
                sharpe=round(float(net.mean() / net.std() * np.sqrt(365)), 3),
                cagr=round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4),
                slots_filled=round(float(prev.loc[START:].sum(axis=1).mean()), 2),
                members=round(float(mask.loc[START:].sum(axis=1).mean()), 1),
                round_trips=int(ent.loc[START:].sum()))


rows = [book(SAT, "satellite (what Earn trades)"),
        book(EXCL, "excluded, all reasons combined"),
        book(EXCL & ~C["c_adv"] & C["c_age"] & C["c_vol"] & C["c_we"] & C["c_nm"],
             "excluded ONLY by ADV<$1M"),
        book(EXCL & ~C["c_age"] & C["c_vol"] & C["c_we"] & C["c_adv"] & C["c_nm"],
             "excluded ONLY by age<180d"),
        book(EXCL & ~C["c_vol"] & C["c_age"] & C["c_we"] & C["c_adv"] & C["c_nm"],
             "excluded ONLY by vol120<0.20"),
        book(EXCL & ~C["c_we"] & C["c_age"] & C["c_vol"] & C["c_adv"] & C["c_nm"],
             "excluded ONLY by weekend ratio"),
        book(EXCL & ~C["c_nm"] & C["c_age"] & C["c_vol"] & C["c_we"] & C["c_adv"],
             "excluded ONLY by name rules")]
btc = RR["BTCUSDT"].loc[START:]
rows.append(dict(book="BASELINE BTC hold", days=len(btc),
                 sharpe=round(float(btc.mean() / btc.std() * np.sqrt(365)), 3),
                 cagr=round(float((1 + btc).prod() ** (365 / len(btc)) - 1), 4),
                 slots_filled=None, members=None, round_trips=0))
out = pd.DataFrame(rows)
print(out.to_string(index=False))
