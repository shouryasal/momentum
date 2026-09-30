"""MEASURE 3 / steps (c) and (d).

(c) SURVIVORSHIP: of every USDT ticker that ever existed, how many are dead, and would Earn
    membership rules have held one into its delisting? Cost the ones it would.
(d) STRUCTURAL exclusions priced where the panel allows: no shorting, no leverage,
    no perps/funding, no listing in the first 180 days, no non-USDT quote.

Costs 15 bps/side throughout. Read-only. Trials added: 0 new selection trials -- every
number here is a descriptive measurement of the SHIPPED rules, not a search over variants.
"""
import json, os, re
import numpy as np, pandas as pd

HOME = os.path.expanduser("~"); OUT = f"{HOME}/pa3/out"; os.makedirs(OUT, exist_ok=True)
COST_SIDE = 0.0015
snap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
rules = snap["rules"]
live = {s["symbol"] for s in json.load(open(f"{HOME}/pa3/binance_usdt_spot.json"))}
watch_sym = {p.replace("/", "") for p in snap["pairs"]}

p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date","o","h","l","c","qv","symbol"]).sort_values(["symbol","date"])
END = p["date"].max()
res = {}

# ------------------------------------------------------------------ (c) survivorship
LEV = tuple(rules["leveraged_suffixes"]); EQPAT = re.compile(rules["equity_token_pattern"])
rows = []
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 30: continue
    base = sym[:-4] if sym.endswith("USDT") else sym
    alive = sym in live
    last, first = g["date"].iloc[-1], g["date"].iloc[0]
    # membership state measured on the LAST 7 candles before the tape stops
    cut = g.iloc[:-7] if len(g) > 40 else g
    r = cut["c"].pct_change().dropna()
    vol120 = float(r.tail(120).std()*np.sqrt(365)) if len(r) >= 60 else np.nan
    vol60  = float(r.tail(60).std()*np.sqrt(365))  if len(r) >= 30 else np.nan
    adv90  = float(cut["qv"].tail(90).median())    if len(cut) >= 30 else np.nan
    w = cut.tail(120).copy(); w["dow"] = w["date"].dt.dayofweek
    we, wd = w[w.dow >= 5]["qv"].median(), w[w.dow < 5]["qv"].median()
    wratio = float(we/wd) if wd and wd > 0 else np.nan
    age = (cut["date"].iloc[-1] - first).days
    passes_funnel = bool(
        (not base.endswith(LEV)) and (not EQPAT.match(base)) and base.isascii()
        and base not in ("BNB","XAUT","PAXG")
        and (vol120 == vol120 and vol120 >= rules["min_ann_vol_120d"])
        and (wratio == wratio and wratio >= rules["min_weekend_volume_ratio"])
        and age >= rules["min_listing_age_days"]
        and (adv90 == adv90 and adv90 >= rules["min_median_quote_volume_usdt"]))
    satellite = bool(passes_funnel and adv90 == adv90 and adv90 >= 5_000_000)
    enterable = bool(satellite and vol60 == vol60 and vol60 <= 1.00
                     and age >= 1095 and adv90 >= 10_000_000)
    d30 = float(g["c"].iloc[-1]/g["c"].iloc[-31]-1) if len(g) > 31 else np.nan
    d90 = float(g["c"].iloc[-1]/g["c"].iloc[-91]-1) if len(g) > 91 else np.nan
    rows.append(dict(symbol=sym, alive=alive, first=first, last=last,
                     days_since_last=(END-last).days, age_days=age, vol120=vol120,
                     vol60=vol60, adv90=adv90, wratio=wratio,
                     passes_funnel=passes_funnel, satellite=satellite, enterable=enterable,
                     ret_last30=d30, ret_last90=d90,
                     life_total=float(g["c"].iloc[-1]/g["c"].iloc[0]-1)))
S = pd.DataFrame(rows).set_index("symbol")
S.to_csv(f"{OUT}/m3_survivorship.csv")
dead = S[~S.alive]
res["c_survivorship"] = dict(
  panel_tickers=int(S.shape[0]), alive_TRADING_today=int(S.alive.sum()),
  dead_not_trading_today=int((~S.alive).sum()),
  dead_that_passed_the_WATCHLIST_funnel_at_the_end=int(dead.passes_funnel.sum()),
  dead_that_were_SATELLITE_members_at_the_end=int(dead.satellite.sum()),
  dead_that_were_ENTERABLE_at_the_end=int(dead.enterable.sum()),
  enterable_dead_names=sorted(dead[dead.enterable].index.tolist()),
  satellite_dead_names=sorted(dead[dead.satellite].index.tolist()),
  cost_if_held_5pct_NAV=dict(
     median_last30_ret=round(float(dead[dead.satellite].ret_last30.median()),4)
       if dead.satellite.any() else None,
     median_last90_ret=round(float(dead[dead.satellite].ret_last90.median()),4)
       if dead.satellite.any() else None,
     worst_last90=round(float(dead[dead.satellite].ret_last90.min()),4)
       if dead.satellite.any() else None,
     nav_hit_at_5pct_median30=round(float(dead[dead.satellite].ret_last30.median())*0.05,5)
       if dead.satellite.any() else None),
  all_dead_median_last30=round(float(dead.ret_last30.median()),4),
  all_dead_median_life_total=round(float(dead.life_total.median()),4),
  delistings_per_year=dead.groupby(dead.last.dt.year).size().to_dict())

# ------------------------------------------------------------------ (d) structure
d = {}
# d1. no shorting -- what the long-only book gives up. Same shipped rule, inverted, on the
#     31 authorised pairs: what a SHORT of the same signal (EMA down + breakdown) earns.
trade_sym = {q.replace("/", "") for q in json.load(
    open(f"{HOME}/earn-run/config/riskgate.json"))["universe"]["pairs"]} \
    if False else {q.replace("/","") for q in json.load(
    open(f"{HOME}/earn-run/config/riskgate.json"))["universe"]["pairs"]}
START = pd.Timestamp("2019-01-01", tz="UTC")
def atr(h,l,c,n=14):
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()
def sig(g, fast=6, slow=18, lb=3, mn=0.0015, mx=0.070, short=False):
    c,h,l=g["c"],g["h"],g["l"]
    ef=c.ewm(span=fast,adjust=False,min_periods=fast).mean()
    es=c.ewm(span=slow,adjust=False,min_periods=slow).mean()
    ap=atr(h,l,c)/c.where(c>0)
    if short:
        bl=l.rolling(lb,min_periods=lb).min().shift(1)
        up=(ef<es); e=up&(c<bl)&(ap>=mn)&(ap<=mx)
    else:
        bh=h.rolling(lb,min_periods=lb).max().shift(1)
        up=(ef>es); e=up&(c>bh)&(ap>=mn)&(ap<=mx)
    x=~up; pos=np.zeros(len(g),bool); on=False
    E,X=e.to_numpy(),x.to_numpy()
    for i in range(len(g)):
        if on and X[i]: on=False
        elif (not on) and E[i]: on=True
        pos[i]=on
    held=pd.Series(pos,index=g.index).shift(1).fillna(False)
    r=c.pct_change().fillna(0.0); r = -r if short else r
    turn=held.astype(int).diff().fillna(0).abs()
    return (r.where(held,0.0)-turn*COST_SIDE), held

longs, shorts, flatdays = [], [], []
for s in sorted(trade_sym):
    g = p[p.symbol==s].dropna(subset=["c"]).set_index("date")
    if len(g) < 250: continue
    nl, hl = sig(g); ns, hs = sig(g, short=True)
    nl, ns, hl = nl.loc[START:], ns.loc[START:], hl.loc[START:]
    yrs=len(nl)/365.0
    longs.append(float((1+nl).prod())**(1/yrs)-1)
    shorts.append(float((1+ns).prod())**(1/yrs)-1)
    raw=g["c"].pct_change().fillna(0.0).loc[START:]
    flatdays.append(float(raw[~hl.astype(bool)].sum()))
d["d1_no_shorting"] = dict(
  n=len(longs),
  median_long_leg_cagr=round(float(np.median(longs)),4),
  median_short_leg_cagr=round(float(np.median(shorts)),4),
  pct_short_leg_profitable=round(float(np.mean(np.array(shorts)>0)*100),1),
  median_sum_of_returns_while_flat=round(float(np.median(flatdays)),4),
  note="short leg = the identical rule mirrored (EMA down + breakdown of prior lows), "
       "costed. A spot-only mandate cannot take it.")

# d2. no leverage -- 2x daily-rebalanced BTC, costed on the rebalance
btc = p[p.symbol=="BTCUSDT"].set_index("date")["c"].pct_change().dropna().loc[START:]
for L in (1, 2, 3):
    r = btc*L
    eq = (1+r).cumprod(); yrs = len(r)/365.0
    d[f"d2_btc_{L}x_daily_rebal"] = dict(
      cagr=round(float(eq.iloc[-1]**(1/yrs)-1),4) if eq.iloc[-1] > 0 else None,
      max_dd=round(float((eq/eq.cummax()-1).min()),4),
      sharpe_arith=round(float(r.mean()/r.std()*np.sqrt(365)),3),
      worst_day=round(float(r.min()),4),
      ruined=bool((1+r).min() <= 0))

# d3. no perps -- what the funding record says a perp short financed by spot would have paid
try:
    f = pd.read_parquet(f"{HOME}/earn-panels/funding.parquet")
    fc = [c for c in f.columns if "rate" in c.lower() or "funding" in c.lower()]
    col = fc[0] if fc else f.columns[-1]
    ann = f[col].astype(float)*3*365
    d["d3_no_perps_funding"] = dict(rows=int(len(f)), col=col,
      median_annualised_funding=round(float(ann.median()),4),
      p25=round(float(ann.quantile(.25)),4), p75=round(float(ann.quantile(.75)),4),
      note="positive = longs pay shorts. A cash-and-carry (long spot / short perp) would "
           "have collected this, and needs a derivatives account the mandate forbids.")
except Exception as e:
    d["d3_no_perps_funding"] = dict(error=str(e))

# d4. no listing inside the first 180 days
newrows = []
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 200: continue
    c = g["c"].to_numpy()
    newrows.append(dict(symbol=sym, r180=c[min(180,len(c)-1)]/c[0]-1,
                        r365=c[min(365,len(c)-1)]/c[0]-1 if len(c) > 200 else np.nan,
                        peak180=c[:min(181,len(c))].max()/c[0]-1))
N = pd.DataFrame(newrows)
d["d4_no_first_180_days"] = dict(n=len(N),
  median_ret_first180=round(float(N.r180.median()),4),
  pct_positive_first180=round(float((N.r180>0).mean()*100),1),
  median_ret_first365=round(float(N.r365.median()),4),
  median_peak_within_180=round(float(N.peak180.median()),4),
  pct_that_double_within_180=round(float((N.peak180>1.0).mean()*100),1))

d["d5_no_nonUSDT_quote"] = "computed in m4"
res["d_structure"] = d
json.dump(res, open(f"{OUT}/m3.json","w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
