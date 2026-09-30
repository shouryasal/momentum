"""MEASURE 3 / step 2 (b) -- the SHIPPED entry rule on the set Earn trades vs the set it
refuses. 2019-01-01 -> 2026-09-24, costed 15 bps/side, survivorship-free.

PRE-REGISTERED (written before any number was computed)
  H : the currently-listed USDT pairs Earn does NOT look at are, as a group, a better home
      for the shipped rule than the 31 pairs the gate authorises.
  Falsifier: refuted if the excluded set per-trade net edge and its implementable book
      Sharpe are both at or below the included set AND at or below BTC hold.
  Baselines always reported: BTC buy-and-hold, and buy-and-hold of the same set.
  Sharpe = ARITHMETIC mean(daily)/std(daily)*sqrt(365).
  Trials added: 2 parameterisations x 5 universes = 10.

Two constructions, on purpose:
  A. PER-SYMBOL (artifact-free): the rule net CAGR on each coin over that coin own life
     vs buy-and-hold over the identical window. Reported as a cross-sectional distribution.
     Immune to the equal-weight daily-rebalance bonus that makes a basket of dead microcaps
     print an impossible number.
  B. IMPLEMENTABLE BOOK: at most 8 concurrent positions (freqtrade max_open_trades = 8),
     5% of NAV each (target_pct_nav), ranked deterministically by trailing 90d median quote
     volume, daily-rebalanced within the held names, every entry and exit costed.
"""
import json, os
import numpy as np, pandas as pd

HOME = os.path.expanduser("~"); OUT = f"{HOME}/pa3/out"; os.makedirs(OUT, exist_ok=True)
COST_SIDE, START, MAXPOS, STAKE = 0.0015, pd.Timestamp("2019-01-01", tz="UTC"), 8, 0.05

snap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
rg   = json.load(open(f"{HOME}/earn-run/config/riskgate.json"))
live = {s["symbol"] for s in json.load(open(f"{HOME}/pa3/binance_usdt_spot.json"))}
watch_sym = {p.replace("/", "") for p in snap["pairs"]}
trade_sym = {p.replace("/", "") for p in rg["universe"]["pairs"]}
eo = set(rg["universe"]["snapshot"]["exit_only"])
enter_sym = {p.replace("/", "") for p in rg["universe"]["pairs"] if p.split("/")[0] not in eo}

p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date","o","h","l","c","qv","symbol"]).sort_values(["symbol","date"])

def atr(h,l,c,n=14):
    pc=c.shift(1); tr=pd.concat([h-l,(h-pc).abs(),(l-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()

def per_symbol(g, fast, slow, lb, min_atr, max_atr):
    c,h,l = g["c"],g["h"],g["l"]
    ef=c.ewm(span=fast,adjust=False,min_periods=fast).mean()
    es=c.ewm(span=slow,adjust=False,min_periods=slow).mean()
    ap=atr(h,l,c)/c.where(c>0); bh=h.rolling(lb,min_periods=lb).max().shift(1)
    up=(ef>es); entry=up&(c>bh)&(ap>=min_atr)&(ap<=max_atr); ex=~up
    pos=np.zeros(len(g),bool); on=False
    E,X=entry.to_numpy(),ex.to_numpy()
    for i in range(len(g)):
        if on and X[i]: on=False
        elif (not on) and E[i]: on=True
        pos[i]=on
    held=pd.Series(pos,index=g.index).shift(1).fillna(False)
    r=c.pct_change().fillna(0.0)
    turn=held.astype(int).diff().fillna(0).abs()
    net=r.where(held,0.0)-turn*COST_SIDE
    return pd.DataFrame({"net":net,"held":held.astype(float),"turn":turn,"raw":r,
                         "qv90":g["qv"].rolling(90,min_periods=20).median()})

PARAMS={"shipped_6_18_3":dict(fast=6,slow=18,lb=3,min_atr=0.0015,max_atr=0.070),
        "alt_9_21_6":     dict(fast=9,slow=21,lb=6,min_atr=0.0,   max_atr=1.0)}
allsyms=p["symbol"].unique().tolist()
UNIV={"included_31_whitelist":[s for s in allsyms if s in trade_sym],
      "included_16_enterable":[s for s in allsyms if s in enter_sym],
      "included_107_watchlist":[s for s in allsyms if s in watch_sym],
      "excluded_383_listed_today":[s for s in allsyms if s in live and s not in watch_sym],
      "excluded_ever_incl_dead":[s for s in allsyms if s not in watch_sym]}

btc_r=p[p.symbol=="BTCUSDT"].set_index("date")["c"].pct_change().loc[START:]
def stats(d,label,**kw):
    d=pd.Series(d).dropna()
    if len(d)<60: return dict(label=label,n_days=len(d),**kw)
    eq=(1+d).cumprod(); yrs=len(d)/365.0
    return dict(label=label,n_days=len(d),
                sharpe_arith=round(float(d.mean()/d.std()*np.sqrt(365)),3),
                cagr=round(float(eq.iloc[-1]**(1/yrs)-1),4),
                max_dd=round(float((eq/eq.cummax()-1).min()),4),
                total_x=round(float(eq.iloc[-1]),3),**kw)

per_rows=[]; book_rows=[]
for pname,pr in PARAMS.items():
    per={}
    for sym,g in p.groupby("symbol",sort=False):
        g=g.dropna(subset=["c"]).set_index("date")
        if len(g)<220: continue
        d=per_symbol(g,**pr).loc[START:]
        if len(d)<180: continue
        per[sym]=d
    for uname,syms in UNIV.items():
        rec=[]
        for s in syms:
            if s not in per: continue
            d=per[s]; n=len(d); yrs=n/365.0
            eq_r=float((1+d["net"]).prod()); eq_h=float((1+d["raw"]).prod())
            tr=float(d["turn"].sum()/2)
            pt=(eq_r-1)/tr*100 if tr>=1 else np.nan
            b=btc_r.reindex(d.index).fillna(0.0)
            rec.append(dict(sym=s,yrs=yrs,rule_cagr=eq_r**(1/yrs)-1,hold_cagr=eq_h**(1/yrs)-1,
                            btc_cagr=float((1+b).prod())**(1/yrs)-1,trades=tr,per_trade_pct=pt,
                            exposure=float(d["held"].mean())))
        r=pd.DataFrame(rec)
        if r.empty: continue
        per_rows.append(dict(params=pname,universe=uname,n=len(r),
            med_rule_cagr=round(float(r.rule_cagr.median()),4),
            med_hold_cagr=round(float(r.hold_cagr.median()),4),
            mean_rule_cagr=round(float(r.rule_cagr.mean()),4),
            pct_rule_beats_hold=round(float((r.rule_cagr>r.hold_cagr).mean()*100),1),
            pct_rule_beats_btc=round(float((r.rule_cagr>r.btc_cagr).mean()*100),1),
            pct_rule_positive=round(float((r.rule_cagr>0).mean()*100),1),
            med_per_trade_pct=round(float(r.per_trade_pct.median()),4),
            med_trades_per_yr=round(float((r.trades/r.yrs).median()),2),
            med_exposure=round(float(r.exposure.median()),3)))
        r.to_csv(f"{OUT}/persym_{pname}_{uname}.csv",index=False)
    for uname,syms in UNIV.items():
        have=[s for s in syms if s in per]
        if len(have)<2: continue
        idx=pd.DatetimeIndex(sorted(set().union(*[set(per[s].index) for s in have])))
        H=pd.DataFrame({s:per[s]["held"] for s in have}).reindex(idx)
        R=pd.DataFrame({s:per[s]["raw"] for s in have}).reindex(idx)
        Q=pd.DataFrame({s:per[s]["qv90"] for s in have}).reindex(idx)
        rank=Q.rank(axis=1,ascending=False,na_option="bottom")
        sel=(H.fillna(0)>0)&(rank<=MAXPOS)
        prev=sel.shift(1).fillna(False)
        trades=(sel.astype(int)-prev.astype(int)).clip(lower=0).sum(axis=1)
        exits =(prev.astype(int)-sel.astype(int)).clip(lower=0).sum(axis=1)
        gross=(R.fillna(0)*prev.astype(float)).sum(axis=1)*STAKE
        cost=(trades+exits)*STAKE*COST_SIDE
        book=(gross-cost).loc[START:]
        book_rows.append(stats(book,f"{pname}|{uname}|BOOK8",n_symbols=len(have),
            round_trips=int(trades.sum()),
            mean_names_held=round(float(prev.sum(axis=1).mean()),2),
            mean_gross_exposure=round(float(prev.sum(axis=1).mean()*STAKE),3)))
book_rows.append(stats(btc_r,"BASELINE|BTC_HOLD_100pct"))
book_rows.append(stats(btc_r*0.40,"BASELINE|BTC_HOLD_at_40pct_cap"))

A=pd.DataFrame(per_rows); B=pd.DataFrame(book_rows)
A.to_csv(f"{OUT}/m2_persymbol.csv",index=False); B.to_csv(f"{OUT}/m2_book8.csv",index=False)
with open(f"{OUT}/m2.txt","w") as f:
    f.write("=== A. PER-SYMBOL, rule vs hold on each coin own life ===\n"+A.to_string()+
            "\n\n=== B. IMPLEMENTABLE 8-SLOT BOOK, 5% stakes, costed ===\n"+B.to_string()+"\n")
print(open(f"{OUT}/m2.txt").read())
