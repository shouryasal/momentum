"""MEASURE 3 / step 1 — what Binance lists, what Earn looks at, and who is missed.

Reads:  ~/pa3/binance_usdt_spot.json      (live /api/v3/exchangeInfo, status TRADING)
        ~/earn-run/knowledge/universe/2026-09-23.json  (the weekly resolver's snapshot)
        ~/earn-run/config/riskgate.json                (what the gate authorises)
        ~/earn-panels/panel_1d.parquet                 (survivorship-free daily panel)
Writes: ~/pa3/out/universe_stats.csv, ~/pa3/out/m1.json
No production code. Read-only on ~/earn-run.
"""
import json, os
import numpy as np, pandas as pd

OUT = os.path.expanduser("~/pa3/out"); os.makedirs(OUT, exist_ok=True)
HOME = os.path.expanduser("~")

live = json.load(open(f"{HOME}/pa3/binance_usdt_spot.json"))
live_syms = sorted(s["symbol"] for s in live)
snap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
rg   = json.load(open(f"{HOME}/earn-run/config/riskgate.json"))

watch = set(snap["pairs"])                       # 107 watchlist pairs
watch_sym = {p.replace("/", "") for p in watch}
trade = set(rg["universe"]["pairs"])             # 31 whitelist pairs
trade_sym = {p.replace("/", "") for p in trade}
exit_only = set(rg["universe"]["snapshot"]["exit_only"])
enterable_sym = {p.replace("/", "") for p in trade if p.split("/")[0] not in exit_only}
excl_reason = {k: v for k, v in snap["excluded"].items()}

# candles actually ingested
cand = os.listdir(f"{HOME}/earn-run/data/binance")
ingested = {f.split("-")[0].replace("_", "") for f in cand if f.endswith("-1d.feather")}

p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date", "c", "qv", "symbol"])
p = p.sort_values(["symbol", "date"])
END = p["date"].max()

rows = []
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 5:
        continue
    first, last = g["date"].iloc[0], g["date"].iloc[-1]
    age = (END - first).days
    r = g["c"].pct_change().dropna()
    vol60 = float(r.tail(60).std() * np.sqrt(365)) if len(r) >= 30 else np.nan
    adv90 = float(g["qv"].tail(90).median()) if len(g) >= 30 else np.nan
    rows.append(dict(symbol=sym, first=first, last=last, age_days=age,
                     n_days=len(g), vol60=vol60, adv90=adv90,
                     dead=(END - last).days > 7, days_since_last=(END - last).days))
st = pd.DataFrame(rows).set_index("symbol")

st["listed_today"]  = st.index.isin(live_syms)
st["in_watchlist"]  = st.index.isin(watch_sym)
st["in_whitelist"]  = st.index.isin(trade_sym)
st["enterable"]     = st.index.isin(enterable_sym)
st["ingested"]      = st.index.isin(ingested)
st["excl_reason"]   = [excl_reason.get(s, "") for s in st.index]
st.to_csv(f"{OUT}/universe_stats.csv")

live_only = st[st.listed_today]
missing_from_panel = sorted(set(live_syms) - set(st.index))

# ---- the exclusion filter from growth-audit.md §1.5 / earn.yaml satellite_eligibility
EL = (st.vol60 <= 1.00) & (st.age_days >= 1095) & (st.adv90 >= 10_000_000)
st["passes_eligibility"] = EL

not_looked = live_only[~live_only.in_watchlist]
passers = not_looked[EL.reindex(not_looked.index).fillna(False)]

res = dict(
  venue=dict(usdt_symbols_any_status=snap["counts"]["quote_symbols"],
             usdt_spot_TRADING_today=len(live_syms),
             usdt_spot_TRADING_in_snapshot=496),
  earn=dict(watchlist=len(watch), ingested_bases=len(ingested),
            whitelist=len(trade), exit_only=len(exit_only),
            enterable=len(enterable_sym),
            screened_cheap=len(watch), screened_rich_cap=20),
  funnel=snap["funnel"],
  panel=dict(symbols=int(st.shape[0]), dead=int(st.dead.sum()),
             alive=int((~st.dead).sum()),
             live_listed_covered=int(live_only.shape[0]),
             live_listed_missing_from_panel=len(missing_from_panel),
             missing_examples=missing_from_panel[:20]),
  a_not_looked_at=dict(
      n_not_looked=int(not_looked.shape[0]),
      n_pass_eligibility=int(passers.shape[0]),
      names=[dict(symbol=s, vol60=round(float(r.vol60),3),
                  age_days=int(r.age_days), adv90_musd=round(float(r.adv90)/1e6,1),
                  removed_by=r.excl_reason)
             for s, r in passers.sort_values("adv90", ascending=False).iterrows()]),
  included_passing=dict(
      watchlist_pass=int(live_only[live_only.in_watchlist].pipe(
          lambda d: EL.reindex(d.index).fillna(False)).sum()),
      whitelist_pass=int(live_only[live_only.in_whitelist].pipe(
          lambda d: EL.reindex(d.index).fillna(False)).sum())),
)
json.dump(res, open(f"{OUT}/m1.json","w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
