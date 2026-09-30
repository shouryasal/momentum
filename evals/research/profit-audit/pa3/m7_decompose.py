"""MEASURE 3 / step (b) follow-up -- WHICH exclusion rule carries the excluded set's edge,
and does that edge survive a microcap cost?

m6 found the point-in-time EXCLUDED set (listed, not on the watchlist) at Sharpe 1.118 /
24.4% net CAGR against the point-in-time satellite book at 0.688 / 14.0%. That result is
only actionable if (i) it is not concentrated in names that were untradeable, and (ii) it
survives a cost appropriate to a $1M-a-day coin rather than the 15 bps a BTC fill pays.

PRE-REGISTERED (written before any number was computed)
  H1: the excluded set's advantage is concentrated in the min_listing_age_days=180 rule --
      i.e. it is the young-coin breakout, not the illiquid-coin breakout.
      Falsifier: refuted if the age-only excluded sub-book is at or below the satellite book
      on Sharpe AND net CAGR.
  H2: the excluded set's advantage does not survive a microcap round trip.
      Falsifier: refuted if the excluded book still beats BOTH the satellite book and BTC
      hold at the 40% cap on Sharpe at 100 bps per side.
  Baselines always reported: BTC hold 100%, BTC hold at the 40% cap, PIT satellite book.
  Sharpe = ARITHMETIC mean(daily)/std(daily)*sqrt(365).
  Trials added: 5 exclusion-reason buckets + 4 cost levels = 9.

Read-only. Writes ~/pa3/out/m7*.
"""
import json, os, re
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pa3/out"
START = pd.Timestamp("2019-01-01", tz="UTC")
MAXPOS, STAKE = 8, 0.05

cfgsnap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
R = cfgsnap["rules"]
LEV = tuple(R["leveraged_suffixes"])
EQ = re.compile(R["equity_token_pattern"])
CORE = {"BTCUSDT", "ETHUSDT"}
NEVER = {"BNB", "XAUT", "PAXG"}


def name_ok(sym):
    b = sym[:-4] if sym.endswith("USDT") else sym
    return (not b.endswith(LEV)) and (not EQ.match(b)) and b.isascii() and b not in NEVER


p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date", "o", "h", "l", "c", "qv", "symbol"]
                    ).sort_values(["symbol", "date"])


def atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


D = {k: {} for k in ("held", "raw", "adv", "ageok", "volok", "weok", "advok", "nameok")}
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 200 or not sym.endswith("USDT"):
        continue
    g = g.set_index("date")
    c, h, l, qv = g["c"], g["h"], g["l"], g["qv"]
    r = c.pct_change().fillna(0.0)
    ef = c.ewm(span=6, adjust=False, min_periods=6).mean()
    es = c.ewm(span=18, adjust=False, min_periods=18).mean()
    ap = atr(h, l, c) / c.where(c > 0)
    bh = h.rolling(3, min_periods=3).max().shift(1)
    up = (ef > es)
    entry = (up & (c > bh) & (ap >= 0.0015) & (ap <= 0.070)).to_numpy()
    ex = (~up).to_numpy()
    pos = np.zeros(len(g), bool)
    on = False
    for i in range(len(g)):
        if on and ex[i]:
            on = False
        elif (not on) and entry[i]:
            on = True
        pos[i] = on
    rr = r.shift(1)
    vol120 = rr.rolling(120, min_periods=60).std() * np.sqrt(365)
    adv90 = qv.shift(1).rolling(90, min_periods=30).median()
    dow = pd.Series(g.index.dayofweek, index=g.index)
    qs = qv.shift(1)
    we = qs.where(dow >= 5).rolling(120, min_periods=20).median()
    wd = qs.where(dow < 5).rolling(120, min_periods=40).median()
    wratio = we / wd.where(wd > 0)
    age = pd.Series((g.index - g.index[0]).days, index=g.index)
    D["held"][sym] = pd.Series(pos, index=g.index).shift(1).fillna(False)
    D["raw"][sym] = r
    D["adv"][sym] = adv90
    D["ageok"][sym] = (age >= R["min_listing_age_days"])
    D["volok"][sym] = (vol120 >= R["min_ann_vol_120d"]).fillna(False)
    D["weok"][sym] = (wratio >= R["min_weekend_volume_ratio"]).fillna(False)
    D["advok"][sym] = (adv90 >= R["min_median_quote_volume_usdt"]).fillna(False)
    D["nameok"][sym] = pd.Series(name_ok(sym), index=g.index)

syms = sorted(D["held"])
idx = pd.DatetimeIndex(sorted(set().union(*[set(D["held"][s].index) for s in syms])))


def M(key, fill=None):
    out = pd.DataFrame({s: D[key][s] for s in syms}).reindex(idx)
    return out if fill is None else out.fillna(fill)


H = M("held", False).astype(bool)
RAW = M("raw")
RR = RAW.fillna(0.0)
LISTED = ~RAW.isna()
Q = M("adv")
AGE = M("ageok", False).astype(bool)
VOL = M("volok", False).astype(bool)
WE = M("weok", False).astype(bool)
ADV = M("advok", False).astype(bool)
NM = M("nameok", False).astype(bool)
for s in CORE:
    if s in syms:
        for X in (AGE, VOL, WE, ADV, NM):
            X[s] = True
WATCH = AGE & VOL & WE & ADV & NM
SAT = WATCH & (Q >= 5_000_000).fillna(False)

btc = RR["BTCUSDT"].loc[START:]


def stats(d, label, **kw):
    d = pd.Series(d).dropna()
    eq = (1 + d).cumprod()
    yrs = len(d) / 365.0
    return dict(label=label, n_days=len(d),
                sharpe_arith=round(float(d.mean() / d.std() * np.sqrt(365)), 3),
                cagr=round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4),
                max_dd=round(float((eq / eq.cummax() - 1).min()), 4),
                total_x=round(float(eq.iloc[-1]), 3), **kw)


def book(mask, label, cost_side=0.0015, **kw):
    sel = H & mask
    rank = Q.where(sel).rank(axis=1, ascending=False, na_option="bottom")
    sel = sel & (rank <= MAXPOS)
    prev = sel.shift(1).fillna(False)
    ent = (sel.astype(int) - prev.astype(int)).clip(lower=0).sum(axis=1)
    ext = (prev.astype(int) - sel.astype(int)).clip(lower=0).sum(axis=1)
    gross = (RR * prev.astype(float)).sum(axis=1) * STAKE
    net = (gross - (ent + ext) * STAKE * cost_side).loc[START:]
    # median ADV of the names actually held, in $ -- the capacity check
    advheld = Q.where(prev).stack()
    return stats(net, label, cost_bps_side=int(cost_side * 1e4),
                 round_trips=int(ent.loc[START:].sum()),
                 mean_names_held=round(float(prev.loc[START:].sum(axis=1).mean()), 2),
                 mean_members=round(float(mask.loc[START:].sum(axis=1).mean()), 1),
                 median_adv_of_held_musd=(round(float(advheld.median()) / 1e6, 2)
                                          if len(advheld) else None), **kw)


EXCL = LISTED & ~WATCH
rows = []
# ---- (1) which single rule is binding on the excluded names the rule wants to buy?
rows.append(book(SAT, "PIT satellite book (what Earn trades)"))
rows.append(book(EXCL, "PIT excluded, all reasons"))
rows.append(book(EXCL & ~AGE & VOL & WE & ADV & NM,
                 "PIT excluded ONLY by age<180d (liquid, real, young)"))
rows.append(book(EXCL & ~ADV & AGE & VOL & WE & NM,
                 "PIT excluded ONLY by ADV<$1M (illiquid, old enough)"))
rows.append(book(EXCL & ~VOL & AGE & WE & ADV & NM,
                 "PIT excluded ONLY by vol120<0.20 (the pegs)"))
rows.append(book(EXCL & ~WE & AGE & VOL & ADV & NM,
                 "PIT excluded ONLY by weekend ratio (not 24/7)"))
rows.append(book(EXCL & ~NM & AGE & VOL & WE & ADV,
                 "PIT excluded ONLY by the name rules (lev/equity/RWA)"))
rows.append(book(EXCL & ADV, "PIT excluded but ADV>=$1M anyway"))
rows.append(book(WATCH | EXCL, "PIT everything listed (union)"))

# ---- (2) cost stress on the two that matter
for cb in (0.0015, 0.0030, 0.0050, 0.0100):
    rows.append(book(EXCL, f"COST STRESS | PIT excluded @ {int(cb*1e4)}bps/side", cost_side=cb))
    rows.append(book(SAT, f"COST STRESS | PIT satellite @ {int(cb*1e4)}bps/side", cost_side=cb))
    rows.append(book(EXCL & ~AGE & VOL & WE & ADV & NM,
                     f"COST STRESS | excluded-young-only @ {int(cb*1e4)}bps/side", cost_side=cb))

rows.append(stats(btc, "BASELINE|BTC_HOLD_100pct"))
rows.append(stats(btc * 0.40, "BASELINE|BTC_HOLD_at_40pct_cap"))
B = pd.DataFrame(rows)
B.to_csv(f"{OUT}/m7_decompose.csv", index=False)
cols = ["label", "sharpe_arith", "cagr", "max_dd", "total_x", "cost_bps_side",
        "round_trips", "mean_names_held", "mean_members", "median_adv_of_held_musd"]
txt = B.reindex(columns=cols).to_string()
print(txt)
open(f"{OUT}/m7.txt", "w").write(txt + "\n")
