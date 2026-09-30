"""MEASURE 3 / step (b), CORRECTED -- the shipped rule on a POINT-IN-TIME universe.

WHY: m2 compared "the 31 pairs the gate authorises TODAY" against "the 383 it does not"
using 2026 membership applied backwards to 2019. That is hindsight: today's whitelist is by
construction the names that stayed liquid and did not die. This script re-runs the same
question with membership recomputed EVERY DAY from data available on that day only, using
the literal thresholds in config/earn.yaml universe.rules / tiers / satellite_eligibility.

PRE-REGISTERED (written before any number was computed)
  H  : the included-beats-excluded result in m2 is an artifact of hindsight membership and
       will shrink or reverse once membership is point-in-time.
  Falsifier: refuted if the point-in-time included book still beats the point-in-time
       excluded book on BOTH arithmetic Sharpe and net CAGR by a margin of the same order
       as the frozen comparison (1.154 vs 0.747 Sharpe, 17.1% vs 5.7% CAGR).
  Baselines always reported: BTC buy-and-hold 100%, and BTC hold at the 40% cap.
  Sharpe = ARITHMETIC mean(daily)/std(daily)*sqrt(365).
  Costs  : 15 bps per side, always on.
  Trials added: 1 parameterisation (the SHIPPED one only) x 8 universes = 8.

Read-only on ~/earn-run and ~/earn-panels. Writes ~/pa3/out/m6*.
"""
import json, os, re
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pa3/out"
os.makedirs(OUT, exist_ok=True)
COST_SIDE = 0.0015
START = pd.Timestamp("2019-01-01", tz="UTC")
MAXPOS = 8
STAKE = 0.05

cfgsnap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
R = cfgsnap["rules"]
LEV = tuple(R["leveraged_suffixes"])
EQ = re.compile(R["equity_token_pattern"])
MIN_VOL120 = R["min_ann_vol_120d"]
MIN_WE = R["min_weekend_volume_ratio"]
MIN_AGE = R["min_listing_age_days"]
MIN_ADV_WATCH = R["min_median_quote_volume_usdt"]
SAT_ADV = 5_000_000
EL = dict(max_ann_vol=1.00, min_age=1095, min_adv=10_000_000)   # satellite_eligibility
CORE = {"BTCUSDT", "ETHUSDT"}
NEVER = {"BNB", "XAUT", "PAXG"}

ei = json.load(open("/tmp/ei_fresh.json"))
usdt_status = {s["symbol"]: s["status"] for s in ei["symbols"] if s["quoteAsset"] == "USDT"}
listed_today = {k for k, v in usdt_status.items() if v == "TRADING"}
watch_today = {q.replace("/", "") for q in cfgsnap["pairs"]}
rg = json.load(open(f"{HOME}/earn-run/config/riskgate.json"))
trade_today = {q.replace("/", "") for q in rg["universe"]["pairs"]}


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


held_d, raw_d, mem_d, sat_d, el_d, adv_d = {}, {}, {}, {}, {}, {}
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 200 or not sym.endswith("USDT"):
        continue
    g = g.set_index("date")
    c, h, l, qv = g["c"], g["h"], g["l"], g["qv"]
    r = c.pct_change().fillna(0.0)
    # --- shipped entry rule 6/18/3, ATR band, exit on EMA cross down
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
    held = pd.Series(pos, index=g.index).shift(1).fillna(False)
    # --- POINT-IN-TIME membership, recomputed every day, all windows ending yesterday
    rr = r.shift(1)
    vol120 = rr.rolling(120, min_periods=60).std() * np.sqrt(365)
    vol60 = rr.rolling(60, min_periods=30).std() * np.sqrt(365)
    adv90 = qv.shift(1).rolling(90, min_periods=30).median()
    dow = pd.Series(g.index.dayofweek, index=g.index)
    qs = qv.shift(1)
    we = qs.where(dow >= 5).rolling(120, min_periods=20).median()
    wd = qs.where(dow < 5).rolling(120, min_periods=40).median()
    wratio = we / wd.where(wd > 0)
    age = pd.Series((g.index - g.index[0]).days, index=g.index)
    ok = name_ok(sym)
    watch = (pd.Series(ok, index=g.index) & (vol120 >= MIN_VOL120) & (wratio >= MIN_WE)
             & (age >= MIN_AGE) & (adv90 >= MIN_ADV_WATCH)).fillna(False)
    sat = (watch & (adv90 >= SAT_ADV)).fillna(False)
    elg = (sat & (vol60 <= EL["max_ann_vol"]) & (age >= EL["min_age"])
           & (adv90 >= EL["min_adv"])).fillna(False)
    if sym in CORE:            # core is never ranked out
        watch = watch | True
        sat = sat | True
        elg = elg | True
    held_d[sym] = held
    raw_d[sym] = r
    mem_d[sym] = watch
    sat_d[sym] = sat
    el_d[sym] = elg
    adv_d[sym] = adv90

syms = sorted(held_d)
idx = pd.DatetimeIndex(sorted(set().union(*[set(held_d[s].index) for s in syms])))


def M(d, fill=None):
    out = pd.DataFrame({s: d[s] for s in syms}).reindex(idx)
    return out if fill is None else out.fillna(fill)


H = M(held_d, False).astype(bool)
RAW = M(raw_d)
RR = RAW.fillna(0.0)
LISTED = (~RAW.isna())
W = M(mem_d, False).astype(bool)
SA = M(sat_d, False).astype(bool)
EE = M(el_d, False).astype(bool)
Q = M(adv_d)

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


def book(mask, label):
    sel = (H & mask)
    rank = Q.where(sel).rank(axis=1, ascending=False, na_option="bottom")
    sel = sel & (rank <= MAXPOS)
    prev = sel.shift(1).fillna(False)
    ent = (sel.astype(int) - prev.astype(int)).clip(lower=0).sum(axis=1)
    ext = (prev.astype(int) - sel.astype(int)).clip(lower=0).sum(axis=1)
    gross = (RR * prev.astype(float)).sum(axis=1) * STAKE
    net = (gross - (ent + ext) * STAKE * COST_SIDE).loc[START:]
    return stats(net, label, round_trips=int(ent.loc[START:].sum()),
                 mean_names_held=round(float(prev.loc[START:].sum(axis=1).mean()), 2),
                 mean_members=round(float(mask.loc[START:].sum(axis=1).mean()), 1))


rows = []
rows.append(book(SA, "PIT|satellite_members (the whitelist rule)"))
rows.append(book(EE, "PIT|enterable (satellite_eligibility)"))
rows.append(book(W, "PIT|watchlist_members"))
rows.append(book(LISTED & ~W, "PIT|EXCLUDED = listed but not watchlist"))
rows.append(book(LISTED, "PIT|EVERYTHING listed, no filter at all"))

FT = pd.DataFrame({s: (s in trade_today) for s in syms}, index=idx).astype(bool)
FW = pd.DataFrame({s: (s in watch_today) for s in syms}, index=idx).astype(bool)
FX = pd.DataFrame({s: (s in listed_today and s not in watch_today) for s in syms},
                  index=idx).astype(bool)
rows.append(book(FT & LISTED, "FROZEN2026|the 31 whitelist pairs"))
rows.append(book(FW & LISTED, "FROZEN2026|the 107 watchlist pairs"))
rows.append(book(FX & LISTED, "FROZEN2026|the 383 not looked at"))
rows.append(stats(btc, "BASELINE|BTC_HOLD_100pct"))
rows.append(stats(btc * 0.40, "BASELINE|BTC_HOLD_at_40pct_cap"))

B = pd.DataFrame(rows)
B.to_csv(f"{OUT}/m6_books.csv", index=False)

width = pd.DataFrame({"watchlist": W.sum(axis=1), "satellite": SA.sum(axis=1),
                      "enterable": EE.sum(axis=1), "listed": LISTED.sum(axis=1)}).loc[START:]
wy = width.resample("YE").mean().round(1)
wy.to_csv(f"{OUT}/m6_universe_width.csv")
print(B.to_string())
print()
print("=== mean point-in-time universe width, by year ===")
print(wy.to_string())
with open(f"{OUT}/m6.txt", "w") as f:
    f.write(B.to_string() + "\n\n=== mean point-in-time universe width by year ===\n"
            + wy.to_string() + "\n")
