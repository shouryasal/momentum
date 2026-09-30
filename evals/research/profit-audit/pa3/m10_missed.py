"""MEASURE 3 / step (a), re-derived independently against the FRESH venue snapshot.

Of the USDT spot pairs Binance lists TODAY that Earn does NOT look at, how many would pass
the growth-audit exclusion filter (vol60 <= 1.00, age >= 1095d, median 90d quote volume
>= $10M)? Name every one and name the rule that removed it, so a human can judge whether
the removal was deliberate.

Also widens the net deliberately: every not-looked-at pair with >= $10M ADV whatever its
vol or age, because the owner's question is "is anything amiss", not "does the filter agree
with itself".

0 new selection trials -- a census, not a search. Read-only. Writes ~/pa3/out/m10*.
"""
import json, os
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pa3/out"

ei = json.load(open("/tmp/ei_fresh.json"))
usdt = [s for s in ei["symbols"] if s["quoteAsset"] == "USDT"]
listed = {s["symbol"] for s in usdt if s["status"] == "TRADING"}

snap = json.load(open(f"{HOME}/earn-run/knowledge/universe/2026-09-23.json"))
watch = {q.replace("/", "") for q in snap["pairs"]}
rg = json.load(open(f"{HOME}/earn-run/config/riskgate.json"))
trade = {q.replace("/", "") for q in rg["universe"]["pairs"]}
exit_only = set(rg["universe"]["snapshot"]["exit_only"])
enterable = {q.replace("/", "") for q in rg["universe"]["pairs"]
             if q.split("/")[0] not in exit_only}
why = snap["excluded"]

p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date", "c", "qv", "symbol"]).sort_values(["symbol", "date"])
END = p["date"].max()
rows = []
for sym, g in p.groupby("symbol", sort=False):
    g = g.dropna(subset=["c"])
    if len(g) < 5:
        continue
    r = g["c"].pct_change().dropna()
    rows.append(dict(
        symbol=sym,
        age_days=(END - g["date"].iloc[0]).days,
        vol60=float(r.tail(60).std() * np.sqrt(365)) if len(r) >= 30 else np.nan,
        adv90=float(g["qv"].tail(90).median()) if len(g) >= 30 else np.nan))
st = pd.DataFrame(rows).set_index("symbol")
st["listed_today"] = st.index.isin(listed)
st["looked_at"] = st.index.isin(watch)
st["authorised"] = st.index.isin(trade)
st["enterable"] = st.index.isin(enterable)
st["removed_by"] = [why.get(s, "") for s in st.index]
st["passes_filter"] = ((st.vol60 <= 1.00) & (st.age_days >= 1095)
                       & (st.adv90 >= 10_000_000)).fillna(False)
st.to_csv(f"{OUT}/m10_universe.csv")

L = st[st.listed_today]
missed = L[~L.looked_at]
passers = missed[missed.passes_filter]
big = missed[missed.adv90 >= 10_000_000]

res = dict(
    funnel_today=dict(
        binance_usdt_spot_TRADING=len(listed),
        earn_watchlist=len(watch),
        earn_screened_cheap_tier=len(watch),
        earn_screened_rich_tier=20,
        earn_gate_authorised=len(trade),
        earn_enterable_now=len(enterable),
        pct_of_venue_looked_at=round(100 * len(watch) / len(listed), 1),
        pct_of_venue_buyable=round(100 * len(enterable) / len(listed), 1)),
    not_looked_at=int(missed.shape[0]),
    pass_the_exclusion_filter=int(passers.shape[0]),
    passers=[dict(symbol=s, vol60=round(float(r.vol60), 3), age_days=int(r.age_days),
                  adv90_musd=round(float(r.adv90) / 1e6, 1), removed_by=r.removed_by)
             for s, r in passers.sort_values("adv90", ascending=False).iterrows()],
    wider_net_any_vol_any_age=dict(
        n_with_adv_ge_10musd=int(big.shape[0]),
        names=[dict(symbol=s, vol60=round(float(r.vol60), 3), age_days=int(r.age_days),
                    adv90_musd=round(float(r.adv90) / 1e6, 1), removed_by=r.removed_by)
               for s, r in big.sort_values("adv90", ascending=False).iterrows()]),
    included_that_pass_the_filter=dict(
        of_107_watchlist=int(L[L.looked_at].passes_filter.sum()),
        of_31_authorised=int(L[L.authorised].passes_filter.sum()),
        of_16_enterable=int(L[L.enterable].passes_filter.sum())),
    venue_symbols_with_no_panel_history=sorted(listed - set(st.index)))
json.dump(res, open(f"{OUT}/m10.json", "w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
