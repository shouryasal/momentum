"""MEASURE 3 / verification step -- the canonical census of the survivorship-free panel
against the live venue. Reconciles the three different ticker counts in circulation
(747 / 744 / 731) and fixes one definition of "dead". Read-only. 0 new selection trials.
Reads : ~/earn-panels/panel_1d.parquet, /tmp/ei_fresh.json (live /api/v3/exchangeInfo)
Writes: ~/pa3/out/panel_census.csv, ~/pa3/out/m5.json
"""
import json, os
import pandas as pd
HOME=os.path.expanduser("~"); OUT=f"{HOME}/pa3/out"
p=pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet", columns=["date","c","symbol"])
END=p["date"].max()
ei=json.load(open('/tmp/ei_fresh.json'))
usdt={s['symbol']:s['status'] for s in ei['symbols'] if s['quoteAsset']=='USDT'}
trading={k for k,v in usdt.items() if v=='TRADING'}
g=p.dropna(subset=["c"]).groupby("symbol")["date"]
df=pd.DataFrame({"first":g.min(),"last":g.max(),"n_candles":g.count()})
df["stale_days"]=(END-df["last"]).dt.days
df["in_exchangeinfo"]=df.index.isin(set(usdt))
df["trading_today"]=df.index.isin(trading)
df.to_csv(f"{OUT}/panel_census.csv")
res=dict(
 panel=dict(first_date=str(p["date"].min().date()), last_date=str(END.date()),
   distinct_symbols_no_filter=int(df.shape[0]),
   symbols_with_ge30_candles=int((df.n_candles>=30).sum()),
   symbols_with_ge220_candles=int((df.n_candles>=220).sum())),
 venue=dict(usdt_any_status=len(usdt),
   usdt_TRADING=len(trading), usdt_BREAK=len(usdt)-len(trading)),
 dead=dict(
   not_trading_today=int((~df.trading_today).sum()),
   in_exchangeinfo_but_BREAK=int((df.in_exchangeinfo & ~df.trading_today).sum()),
   absent_from_exchangeinfo_entirely=int((~df.in_exchangeinfo).sum()),
   tape_stale_gt7d=int((df.stale_days>7).sum())),
 coverage=dict(
   trading_today_with_no_panel_row=sorted(trading-set(df.index)),
   panel_rows_not_usdt_quoted=int(sum(1 for s in df.index if not str(s).endswith("USDT"))))
)
json.dump(res, open(f"{OUT}/m5.json","w"), indent=1)
print(json.dumps(res, indent=1))
