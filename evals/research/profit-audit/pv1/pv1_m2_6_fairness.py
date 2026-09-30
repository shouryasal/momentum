"""PV1 / verify claim M2-6: is the PIT excluded-vs-satellite comparison FAIR?

Reproduces evals/research/profit-audit/pa3/m6_point_in_time.py for the two books the claim
names (PIT satellite_members vs PIT EXCLUDED = listed but not watchlist), then varies ONLY
the two things the claim's framing assumes away:
  (1) cost: flat 15 bps/side (as published) vs a liquidity-tiered cost taken from
      docs/design/wide-universe.md's measured order-book walk ($2,000 clip round trip
      halved per side: >100M 10.2, 20-100M 12.3, 5-20M 18.0, 1-5M 23.9, <1M 27.5 bps).
  (2) delisting: free exit at the last panel close (as published) vs a -7.3% terminal hit
      on the last held day of a name that never trades again (m9 median forced-exit
      ret_last30, docs pa3/out/m9.json).
Also reports WHAT the excluded book actually holds (adv90, listing age, share of held-days
in names that later die) so the reader can see which exclusion rule the profit comes from.
Read-only. Adds 0 selection trials (re-costing of an existing book, same rule, same window).
"""
import json, os, re
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pv1"
os.makedirs(OUT, exist_ok=True)
START = pd.Timestamp("2019-01-01", tz="UTC")
MAXPOS, STAKE = 8, 0.05
DELIST_HIT = -0.0731

cfg = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
R = cfg["rules"]
LEV = tuple(R["leveraged_suffixes"]); EQ = re.compile(R["equity_token_pattern"])
MIN_VOL120, MIN_WE = R["min_ann_vol_120d"], R["min_weekend_volume_ratio"]
MIN_AGE, MIN_ADV_WATCH = R["min_listing_age_days"], R["min_median_quote_volume_usdt"]
SAT_ADV = 5_000_000
CORE = {"BTCUSDT", "ETHUSDT"}; NEVER = {"BNB", "XAUT", "PAXG"}


def name_ok(sym):
    b = sym[:-4] if sym.endswith("USDT") else sym
    return (not b.endswith(LEV)) and (not EQ.match(b)) and b.isascii() and b not in NEVER


def atr(h, l, c, n=14):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).rolling(n, min_periods=n).mean()


p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date", "o", "h", "l", "c", "qv", "symbol"]).sort_values(["symbol", "date"])
held_d, raw_d, mem_d, sat_d, adv_d, age_d = {}, {}, {}, {}, {}, {}
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
    up = ef > es
    entry = (up & (c > bh) & (ap >= 0.0015) & (ap <= 0.070)).to_numpy()
    ex = (~up).to_numpy()
    pos = np.zeros(len(g), bool); on = False
    for i in range(len(g)):
        if on and ex[i]:
            on = False
        elif (not on) and entry[i]:
            on = True
        pos[i] = on
    rr = r.shift(1)
    vol120 = rr.rolling(120, min_periods=60).std() * np.sqrt(365)
    adv90 = qv.shift(1).rolling(90, min_periods=30).median()
    dow = pd.Series(g.index.dayofweek, index=g.index); qs = qv.shift(1)
    we = qs.where(dow >= 5).rolling(120, min_periods=20).median()
    wd = qs.where(dow < 5).rolling(120, min_periods=40).median()
    age = pd.Series((g.index - g.index[0]).days, index=g.index)
    watch = (pd.Series(name_ok(sym), index=g.index) & (vol120 >= MIN_VOL120)
             & ((we / wd.where(wd > 0)) >= MIN_WE) & (age >= MIN_AGE)
             & (adv90 >= MIN_ADV_WATCH)).fillna(False)
    sat = (watch & (adv90 >= SAT_ADV)).fillna(False)
    if sym in CORE:
        watch = watch | True; sat = sat | True
    held_d[sym] = pd.Series(pos, index=g.index).shift(1).fillna(False)
    raw_d[sym] = r; mem_d[sym] = watch; sat_d[sym] = sat; adv_d[sym] = adv90; age_d[sym] = age

syms = sorted(held_d)
idx = pd.DatetimeIndex(sorted(set().union(*[set(held_d[s].index) for s in syms])))
M = lambda d, f=None: (pd.DataFrame({s: d[s] for s in syms}).reindex(idx) if f is None
                       else pd.DataFrame({s: d[s] for s in syms}).reindex(idx).fillna(f))
H = M(held_d, False).astype(bool); RAW = M(raw_d); RR = RAW.fillna(0.0)
LISTED = ~RAW.isna(); W = M(mem_d, False).astype(bool); SA = M(sat_d, False).astype(bool)
Q = M(adv_d); AGE = M(age_d)
LAST = {s: RAW[s].last_valid_index() for s in syms}
alive_end = {s for s in syms if LAST[s] is not None and LAST[s] >= idx[-1] - pd.Timedelta(days=10)}
DEATH = pd.DataFrame(False, index=idx, columns=syms)
for s in syms:
    if s not in alive_end and LAST[s] is not None:
        DEATH.loc[LAST[s], s] = True

TIER = [(100e6, 0.00102), (20e6, 0.00123), (5e6, 0.00180), (1e6, 0.00239)]


def tiered(q):
    c = pd.DataFrame(0.00275, index=q.index, columns=q.columns)
    for lo, bps in reversed(TIER):
        c = c.mask(q >= lo, bps)
    return c


def stats(d, label, **kw):
    d = pd.Series(d).dropna(); eq = (1 + d).cumprod(); yrs = len(d) / 365.0
    return dict(label=label, n=len(d), sharpe=round(float(d.mean() / d.std() * np.sqrt(365)), 3),
                cagr=round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4),
                max_dd=round(float((eq / eq.cummax() - 1).min()), 4), **kw)


def book(mask, label, cost, delist):
    sel = H & mask
    sel = sel & (Q.where(sel).rank(axis=1, ascending=False, na_option="bottom") <= MAXPOS)
    prev = sel.shift(1).fillna(False)
    ent = (sel.astype(int) - prev.astype(int)).clip(lower=0)
    ext = (prev.astype(int) - sel.astype(int)).clip(lower=0)
    cs = tiered(Q) if cost == "tiered" else 0.0015
    gross = (RR * prev.astype(float)).sum(axis=1) * STAKE
    fees = ((ent + ext) * (cs if isinstance(cs, float) else cs.fillna(0.00275))).sum(axis=1) * STAKE
    net = gross - fees
    if delist:
        dyingcol = pd.DataFrame({s: (s not in alive_end) for s in syms}, index=idx)
        lastheld = prev & (~prev.shift(-1).fillna(False)) & dyingcol & (AGE.notna())
        near = pd.DataFrame(False, index=idx, columns=syms)
        for s in syms:
            if s not in alive_end and LAST[s] is not None:
                near.loc[LAST[s] - pd.Timedelta(days=10):LAST[s] + pd.Timedelta(days=2), s] = True
        hit = (lastheld & near)
        print(f"   [{label}] delist-exit events hit: {int(hit.values.sum())}")
        net = net + hit.sum(axis=1) * STAKE * DELIST_HIT
    net = net.loc[START:]
    hd = prev.loc[START:]
    dying = hd & pd.DataFrame({s: (s not in alive_end) for s in syms}, index=idx).loc[START:]
    return stats(net, label, cost=cost, delist=delist,
                 names=round(float(hd.sum(axis=1).mean()), 2),
                 med_adv_musd=round(float(Q.where(hd).stack().median() / 1e6), 2),
                 med_age_d=int(AGE.where(hd).stack().median()),
                 pct_helddays_in_names_that_die=round(100 * dying.values.sum() / max(hd.values.sum(), 1), 1))


rows = []
for cost in ("flat", "tiered"):
    for delist in (False, True):
        rows.append(book(SA, "PIT|satellite (whitelist rule)", cost, delist))
        rows.append(book(LISTED & ~W, "PIT|EXCLUDED", cost, delist))
rows.append(stats(RR["BTCUSDT"].loc[START:], "BASELINE|BTC hold 100%"))
rows.append(stats(RR["BTCUSDT"].loc[START:] * 0.40, "BASELINE|BTC hold at 40% cap"))
B = pd.DataFrame(rows)
print(B.to_string())
B.to_csv(f"{OUT}/pv1_m2_6.csv", index=False)

# which exclusion rule the excluded holdings fail
sel = H & (LISTED & ~W)
sel = sel & (Q.where(sel).rank(axis=1, ascending=False, na_option="bottom") <= MAXPOS)
hd = sel.shift(1).fillna(False).loc[START:]
tot = hd.values.sum()
young = (AGE.loc[START:] < MIN_AGE) & hd
thin = (Q.loc[START:] < MIN_ADV_WATCH) & hd
print(f"\nexcluded held-days: {tot}; share under {MIN_AGE}d listing age: "
      f"{100*young.values.sum()/tot:.1f}%; share under ${MIN_ADV_WATCH/1e6:.0f}M adv90: "
      f"{100*thin.values.sum()/tot:.1f}%")
