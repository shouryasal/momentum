"""MEASURE 3 / step (c) final -- what a delisting actually cost Earn's membership rules.

m8's successor map missed two rebrands (MKR->SKY, EOS->A) and one redenomination
(BTT->BTTC). Every claimed successor here is CHECKED against the live venue: a successor
counts only if that base asset has a TRADING USDT pair today. A rename or merge is not a
loss -- the holder keeps the position under a new ticker -- so it is reported separately
from a real delisting, where the holder is forced out.

Also prices the delisting path Earn actually walks: exit_only_weeks = 4, i.e. a name that
leaves the snapshot stays whitelisted and sellable for 4 more weeks. So the realistic hit
is the return over the LAST 30 DAYS of tape, not the terminal price.

0 new selection trials. Read-only. Writes ~/pa3/out/m9.json.
"""
import json, os
import pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pa3/out"

ei = json.load(open("/tmp/ei_fresh.json"))
trading_bases = {s["baseAsset"] for s in ei["symbols"]
                 if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}

# Documented ticker successors. Each is only honoured if the successor is TRADING today.
SUCC = {
    "MATICUSDT": "POL",      # Polygon MATIC -> POL migration
    "RNDRUSDT": "RENDER",    # Render ticker change
    "AGIXUSDT": "FET",       # ASI merger
    "OCEANUSDT": "FET",      # ASI merger
    "FTMUSDT": "S",          # Fantom -> Sonic
    "MKRUSDT": "SKY",        # Maker -> Sky rebrand
    "EOSUSDT": "A",          # EOS -> Vaulta (A)
    "BTTUSDT": "BTTC",       # BitTorrent redenomination
    "ERDUSDT": "EGLD",       # Elrond -> EGLD
    "BCCUSDT": "BCH",        # Bitcoin Cash ticker consolidation
    "BCHABCUSDT": "BCH",     # Bitcoin Cash ABC -> BCH
}

S = pd.read_csv(f"{OUT}/m3_survivorship.csv").set_index("symbol")
dead_sat = S[(~S.alive) & (S.satellite)].copy()
dead_watch = S[(~S.alive) & (S.passes_funnel)].copy()

rows = []
for sym, r in dead_sat.iterrows():
    succ = SUCC.get(sym)
    ok = bool(succ and succ in trading_bases)
    rows.append(dict(symbol=sym, last=str(r["last"])[:10],
                     adv90_musd=round(float(r["adv90"]) / 1e6, 1),
                     was_enterable=bool(r["enterable"]),
                     ret_last30=round(float(r["ret_last30"]), 4),
                     ret_last90=round(float(r["ret_last90"]), 4),
                     successor=succ if ok else None,
                     kind="rename/merge" if ok else "forced exit"))
DS = pd.DataFrame(rows).sort_values("last")
DS.to_csv(f"{OUT}/m9_dead_satellites.csv", index=False)
forced = DS[DS.kind == "forced exit"]
span_years = 8.1   # 2018-11 -> 2026-09, the span of the dead-satellite tape

res = dict(
    universe_census=dict(
        panel_tickers_ever=747, dead_not_trading_today=254, alive=493,
        note="254 = 253 exchangeInfo status BREAK + 1 removed from exchangeInfo entirely"),
    membership_of_the_dead=dict(
        dead_that_passed_the_WATCHLIST_funnel_at_the_end=int(dead_watch.shape[0]),
        dead_that_were_SATELLITE_members_at_the_end=int(DS.shape[0]),
        dead_that_were_ENTERABLE_at_the_end=int(DS.was_enterable.sum()),
        enterable_dead_names=DS[DS.was_enterable].symbol.tolist()),
    split=dict(
        rename_or_merge=int((DS.kind == "rename/merge").sum()),
        forced_exit=int(len(forced)),
        rename_names=DS[DS.kind == "rename/merge"].symbol.tolist(),
        forced_names=forced.symbol.tolist(),
        enterable_forced_exits=forced[forced.was_enterable].symbol.tolist()),
    cost_of_a_forced_exit=dict(
        events=len(forced), events_per_year=round(len(forced) / span_years, 2),
        median_ret_last30=round(float(forced.ret_last30.median()), 4),
        mean_ret_last30=round(float(forced.ret_last30.mean()), 4),
        worst_ret_last30=round(float(forced.ret_last30.min()), 4),
        worst_name=forced.loc[forced.ret_last30.idxmin(), "symbol"],
        pct_of_forced_exits_that_rose_in_final_30d=round(
            float((forced.ret_last30 > 0).mean() * 100), 1),
        nav_hit_median_at_5pct_stake=round(float(forced.ret_last30.median()) * 0.05, 5),
        nav_hit_worst_at_5pct_stake=round(float(forced.ret_last30.min()) * 0.05, 5),
        annual_nav_drag_median=round(
            float(forced.ret_last30.median()) * 0.05 * len(forced) / span_years, 5)),
    table=DS.to_dict("records"))
json.dump(res, open(f"{OUT}/m9.json", "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in res.items() if k != "table"}, indent=1, default=str))
print()
print(DS.to_string(index=False))
