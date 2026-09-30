"""MEASURE 3 / step (d) completed, and the (c) delisting nature check.

Three things m3/m4 left open or got wrong:
  1. m3's funding measurement crashed (it picked the 'symbol' column; the rate column is
     'fr'). m4_funding.json has numbers but no script, so they are unreproducible. Redone
     here, costed, with the cash-and-carry stated as gross AND net of a spot round trip.
  2. "no pairs quoted in anything but USDT" was never priced. The question that matters is
     not how many non-USDT pairs exist, it is how many BASE ASSETS are reachable only
     through a non-USDT quote. Measured from the live venue.
  3. (c) said exactly one dead name (MATICUSDT) was ENTERABLE when its tape stopped. A
     ticker migration is not a loss. Each dead satellite-tier name is checked for a
     successor ticker still TRADING today.

0 new selection trials: every number is a descriptive measurement, not a search.
Read-only. Writes ~/pa3/out/m8*.
"""
import json, os
import numpy as np, pandas as pd

HOME = os.path.expanduser("~")
OUT = f"{HOME}/pa3/out"
START = pd.Timestamp("2019-01-01", tz="UTC")
COST_SIDE = 0.0015
res = {}

ei = json.load(open("/tmp/ei_fresh.json"))
SYMS = ei["symbols"]
trading = [s for s in SYMS if s["status"] == "TRADING"]
usdt_trading = {s["baseAsset"] for s in trading if s["quoteAsset"] == "USDT"}

# ---------------------------------------------------------------- 1. funding / no perps
f = pd.read_parquet(f"{HOME}/earn-panels/funding.parquet")
f["fr"] = f["fr"].astype(float)
ann = f["fr"] * 3 * 365
per_sym = f.groupby("symbol")["fr"].median() * 3 * 365
rg = json.load(open(f"{HOME}/earn-run/config/riskgate.json"))
trade_sym = {q.replace("/", "") for q in rg["universe"]["pairs"]}
have = sorted(set(per_sym.index) & trade_sym)

p = pd.read_parquet(f"{HOME}/earn-panels/panel_1d.parquet",
                    columns=["date", "c", "symbol"]).sort_values(["symbol", "date"])
btc_px = p[p.symbol == "BTCUSDT"].set_index("date")["c"]
# cash-and-carry: long spot BTC, short BTC perp. P&L = funding only (basis netted at expiry
# does not exist on a perp). One spot round trip + one perp round trip at entry and exit.
fb = f[f.symbol == "BTCUSDT"].set_index("t")["fr"].sort_index()
daily = fb.resample("1D").sum().reindex(
    pd.date_range(fb.index.min().normalize(), fb.index.max().normalize(), tz="UTC")
).fillna(0.0)
daily = daily.loc[START:]
yrs = len(daily) / 365.0
eq_gross = (1 + daily).cumprod()
# net: 4 legs of 15 bps once, at the start, on 1x notional each side = 0.60% one-off
eq_net = eq_gross * (1 - 4 * COST_SIDE)
res["d3_no_perps_cash_and_carry_BTC"] = dict(
    funding_rows=int(len(f)), perp_symbols=int(f["symbol"].nunique()),
    median_annualised_funding_all_symbols=round(float(ann.median()), 4),
    pct_of_8h_intervals_positive=round(float((f["fr"] > 0).mean() * 100), 1),
    n_of_31_whitelist_with_a_perp=len(have),
    median_of_per_symbol_annualised_medians_31=round(float(per_sym.reindex(have).median()), 4),
    btc_median_annualised=round(float(per_sym.get("BTCUSDT", np.nan)), 4),
    eth_median_annualised=round(float(per_sym.get("ETHUSDT", np.nan)), 4),
    carry_cagr_gross=round(float(eq_gross.iloc[-1] ** (1 / yrs) - 1), 4),
    carry_cagr_net_of_one_round_trip=round(float(eq_net.iloc[-1] ** (1 / yrs) - 1), 4),
    carry_max_dd=round(float((eq_gross / eq_gross.cummax() - 1).min()), 4),
    carry_sharpe_arith=round(float(daily.mean() / daily.std() * np.sqrt(365)), 3),
    worst_8h_funding_paid=round(float(fb.min()), 6),
    note=("long spot / short perp collects funding and is market-neutral. It needs a "
          "derivatives account, which a spot-only mandate forbids. Sharpe is high because "
          "the payoff is a near-constant drip; it is a financing rate, not alpha, and it "
          "carries perp liquidation and exchange-credit risk this panel cannot price."))

# ---------------------------------------------------------------- 2. non-USDT quotes
bases_by_quote = {}
for s in trading:
    bases_by_quote.setdefault(s["quoteAsset"], set()).add(s["baseAsset"])
all_bases = set().union(*bases_by_quote.values())
only_non_usdt = sorted(all_bases - usdt_trading)
# how liquid are those, per the panel? they have no USDT tape, so the panel cannot see them
panel_bases = {s[:-4] for s in p["symbol"].unique() if str(s).endswith("USDT")}
res["d5_no_nonUSDT_quote"] = dict(
    trading_pairs_all_quotes=len(trading),
    distinct_quote_assets=len(bases_by_quote),
    top_quotes_by_pair_count=dict(sorted(
        ((k, len(v)) for k, v in bases_by_quote.items()), key=lambda x: -x[1])[:10]),
    distinct_base_assets_anywhere=len(all_bases),
    base_assets_with_a_USDT_pair=len(usdt_trading),
    base_assets_reachable_ONLY_without_USDT=len(only_non_usdt),
    names=only_non_usdt,
    in_panel=sorted(b for b in only_non_usdt if b in panel_bases),
    note=("a base asset with no TRADING USDT pair is genuinely out of reach for a "
          "USDT-quoted book. Everything else is a routing choice, not lost access."))

# ---------------------------------------------------------------- 3. delisting nature
S = pd.read_csv(f"{OUT}/m3_survivorship.csv").set_index("symbol")
dead_sat = S[(~S.alive) & (S.satellite)].copy()
KNOWN_MIGRATION = {  # old ticker -> successor still trading, verified against the venue
    "MATICUSDT": "POL", "RNDRUSDT": "RENDER", "AGIXUSDT": "FET",
    "BCHABCUSDT": "BCH", "BCCUSDT": "BCH", "TONUSDT": "TON",
    "FTMUSDT": "S", "OCEANUSDT": "FET", "ERDUSDT": "EGLD",
}
rows = []
for sym, r in dead_sat.iterrows():
    succ = KNOWN_MIGRATION.get(sym)
    rows.append(dict(symbol=sym, last=str(r["last"])[:10],
                     adv90_musd=round(float(r["adv90"]) / 1e6, 1),
                     enterable=bool(r["enterable"]),
                     ret_last30=round(float(r["ret_last30"]), 4),
                     ret_last90=round(float(r["ret_last90"]), 4),
                     claimed_successor=succ,
                     successor_trading_today=bool(succ and succ in usdt_trading),
                     kind=("rename/merge" if succ and succ in usdt_trading else "delisting")))
DS = pd.DataFrame(rows).sort_values("last")
DS.to_csv(f"{OUT}/m8_dead_satellites.csv", index=False)
real = DS[DS.kind == "delisting"]
res["c_delisting_nature"] = dict(
    dead_satellite_members=len(DS),
    of_which_rename_or_merge=int((DS.kind == "rename/merge").sum()),
    of_which_a_real_delisting=len(real),
    enterable_dead=DS[DS.enterable].symbol.tolist(),
    enterable_dead_that_were_real_delistings=real[real.enterable].symbol.tolist(),
    real_delisting_median_last30=round(float(real.ret_last30.median()), 4),
    real_delisting_worst_last30=round(float(real.ret_last30.min()), 4),
    nav_cost_per_event_at_5pct_stake=round(float(real.ret_last30.median()) * 0.05, 5),
    nav_cost_worst_event_at_5pct_stake=round(float(real.ret_last30.min()) * 0.05, 5),
    events_per_year_over_8y=round(len(real) / 8.0, 2),
    table=DS.to_dict("records"))

json.dump(res, open(f"{OUT}/m8.json", "w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
