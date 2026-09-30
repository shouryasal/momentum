"""Loss-scenario register measurements, part 1. Read-only on the panel."""
import numpy as np, pandas as pd
PANEL = '/home/shourya/earn-panels/panel_1d.parquet'
p = pd.read_parquet(PANEL).sort_values(['symbol', 'date'])
end = p['date'].max()
print("PANEL_END", end, "rows", len(p), "symbols", p['symbol'].nunique())

WL = [x.replace('/', '') for x in (
    "BTC/USDT,ETH/USDT,ZEC/USDT,NEAR/USDT,XRP/USDT,SOL/USDT,DOGE/USDT,TRX/USDT,"
    "UNI/USDT,TAO/USDT,PUMP/USDT,LINK/USDT,AAVE/USDT,LTC/USDT,XLM/USDT,PEPE/USDT,"
    "BCH/USDT,INJ/USDT,ONDO/USDT,ENA/USDT,WLD/USDT,ADA/USDT,SUI/USDT,HBAR/USDT,"
    "FIL/USDT,AVAX/USDT,FET/USDT,PENGU/USDT,TRUMP/USDT,DOT/USDT,XPL/USDT").split(',')]

# ---------------- M1 delisting / death while held ----------------
last = p.groupby('symbol')['date'].max()
dead = last[last < end - pd.Timedelta(days=30)].index
print("\n=== M1 DEAD SYMBOLS ===")
print("dead (last bar >30d before panel end):", len(dead), "of", p['symbol'].nunique())
rows = []
for s in dead:
    d = p[p['symbol'] == s]
    if len(d) < 40:
        continue
    c = d['c'].to_numpy()
    r7 = c[-1] / c[-8] - 1 if len(c) > 8 else np.nan
    r30 = c[-1] / c[-31] - 1 if len(c) > 31 else np.nan
    r90 = c[-1] / c[-91] - 1 if len(c) > 91 else np.nan
    seg = c[-31:]
    w7 = min(seg[i + 7] / seg[i] - 1 for i in range(len(seg) - 7)) if len(seg) > 8 else np.nan
    rows.append((s, r7, r30, r90, w7, float(d['qv'].tail(30).median())))
dd = pd.DataFrame(rows, columns=['symbol', 'r7', 'r30', 'r90', 'worst7', 'qv_med30'])
print("usable dead names:", len(dd))
print(dd[['r7', 'r30', 'r90', 'worst7']].describe(
    percentiles=[.05, .1, .25, .5, .75, .9]).to_string())
print("share of dead names losing >20pct in final 30d:", round(float((dd.r30 < -0.20).mean()), 3))
print("share losing >50pct in final 30d:", round(float((dd.r30 < -0.50).mean()), 3))
print("share losing >20pct in final 7d:", round(float((dd.r7 < -0.20).mean()), 3))
print("median final-7d return:", round(float(dd.r7.median()), 4), "mean:", round(float(dd.r7.mean()), 4))
print("median worst-7d inside final 30d:", round(float(dd.worst7.median()), 4))
print("p10 worst-7d:", round(float(dd.worst7.quantile(0.10)), 4))
liq = dd[dd.qv_med30 >= 10e6]
print("liquid dead (median qv >= 10m):", len(liq))
if len(liq):
    print("  median r7", round(float(liq.r7.median()), 4),
          "median r30", round(float(liq.r30.median()), 4),
          "median worst7", round(float(liq.worst7.median()), 4))

# ---------------- M3 wicks / flash crash ----------------
print("\n=== M3 WICKS ===")
w = p[p['symbol'].isin(WL)].copy()
print("whitelist symbols in panel:", w['symbol'].nunique(), "of 31")
print("missing:", sorted(set(WL) - set(w['symbol'].unique())))
w['prev_c'] = w.groupby('symbol')['c'].shift(1)
w = w.dropna(subset=['prev_c'])
w['low_vs_prevc'] = w['l'] / w['prev_c'] - 1
w['close_vs_prevc'] = w['c'] / w['prev_c'] - 1
w['wick'] = w['l'] / w[['o', 'c']].min(axis=1) - 1
STOP = 0.06
hit = w[w['low_vs_prevc'] <= -STOP]
print("bars whose low was 6pct or more below the prior close:", len(hit), "of", len(w),
      "=", round(100 * len(hit) / len(w), 2), "pct")
rec = hit[hit['close_vs_prevc'] > -STOP]
print("  of those, closed back above the -6pct line (whipsaw):", len(rec),
      "=", round(100 * len(rec) / max(len(hit), 1), 1), "pct")
if len(rec):
    print("  median same-day recovery above the -6pct line:",
          round(100 * float((rec['close_vs_prevc'] + STOP).median()), 2), "pct")
btc = w[w['symbol'] == 'BTCUSDT']
print("BTC bars:", len(btc), "6pct-touch days:", int((btc['low_vs_prevc'] <= -0.06).sum()),
      "of which closed above the line:",
      int(((btc['low_vs_prevc'] <= -0.06) & (btc['close_vs_prevc'] > -0.06)).sum()))
print("BTC wick depth pct (p50, p5, p1, min):",
      [round(100 * float(btc['wick'].quantile(q)), 2) for q in (.5, .05, .01)],
      round(100 * float(btc['wick'].min()), 2))
print("whitelist wick depth pct (p50, p5, p1, min):",
      [round(100 * float(w['wick'].quantile(q)), 2) for q in (.5, .05, .01)],
      round(100 * float(w['wick'].min()), 2))

# ---------------- M4 correlated drawdown ----------------
print("\n=== M4 CORRELATION / BOOK DRAWDOWN ===")
wide = w.pivot_table(index='date', columns='symbol', values='c').sort_index()
ret = wide.pct_change()
common = ret.dropna(axis=1, thresh=int(0.5 * len(ret)))
print("pairs with at least 50pct history:", common.shape[1])
cm = common.corr()
allc = cm.values[np.triu_indices_from(cm, 1)]
allc = allc[~np.isnan(allc)]
print("average pairwise correlation, whole sample:", round(float(np.nanmean(allc)), 3),
      "(gate cap max_avg_pairwise_corr = 0.70)")
btcr = ret['BTCUSDT']
for label, idx in (("worst 5pct BTC days", btcr[btcr < btcr.quantile(0.05)].index),
                   ("best 5pct BTC days", btcr[btcr > btcr.quantile(0.95)].index)):
    c2 = common.loc[common.index.isin(idx)].corr()
    v = c2.values[np.triu_indices_from(c2, 1)]
    v = v[~np.isnan(v)]
    print("average pairwise correlation on the", label + ":", round(float(np.nanmean(v)), 3))
ew = common.mean(axis=1)
bookret = ew * 0.80
print("equal-weight 31-pair book at 80pct gross: worst 1-day",
      round(100 * float(bookret.min()), 2), "pct on", str(bookret.idxmin())[:10])
eq = (1 + bookret.fillna(0)).cumprod()
print("  max drawdown", round(100 * float((eq / eq.cummax() - 1).min()), 2), "pct")
for k in (2, 3, 7, 14, 30):
    print("  worst", k, "day:", round(100 * float(eq.pct_change(k).min()), 2), "pct")
print("  days the book lost 3pct or more (the daily stop trigger):",
      int((bookret <= -0.03).sum()), "of", int(bookret.notna().sum()))

# ---------------- M5 single-name blowup ----------------
print("\n=== M5 SINGLE-NAME BLOWUP ===")
allr = p.copy()
allr['pr'] = allr.groupby('symbol')['c'].shift(1)
allr = allr.dropna(subset=['pr'])
allr['r'] = allr['c'] / allr['pr'] - 1
al = allr[allr.groupby('symbol')['qv'].transform('median') >= 10e6]
print("liquid-name daily returns:", len(al), "names", al['symbol'].nunique())
for q in (0.001, 0.005, 0.01, 0.05):
    print("  p" + str(q * 100) + ":", round(100 * float(al['r'].quantile(q)), 2), "pct")
i = al['r'].idxmin()
print("  worst single day:", round(100 * float(al['r'].min()), 2), "pct",
      al.loc[i, 'symbol'], str(al.loc[i, 'date'])[:10])
print("  name-days a liquid name fell more than 30pct:", int((al['r'] < -0.30).sum()),
      "=", round(100 * float((al['r'] < -0.30).mean()), 3), "pct")
al['c7'] = al.groupby('symbol')['c'].shift(7)
a7 = al.dropna(subset=['c7']).copy()
a7['r7'] = a7['c'] / a7['c7'] - 1
print("  worst 7-day for a liquid name:", round(100 * float(a7['r7'].min()), 2), "pct")
print("  p1 of 7-day:", round(100 * float(a7['r7'].quantile(0.01)), 2), "pct")

# ---------------- M9 liquidity ----------------
print("\n=== M9 LIQUIDITY ===")
recent = w[w['date'] >= end - pd.Timedelta(days=90)]
lq = recent.groupby('symbol')['qv'].median().sort_values()
ORDER = 0.20 * 10000
print("median daily quote volume, last 90d, thinnest 6 (usd m):")
print((lq.head(6) / 1e6).round(2).to_string())
print("a 2000 usdt order as a share of the thinnest name daily volume:",
      round(100 * ORDER / float(lq.iloc[0]), 4), "pct")
print("BTC median daily volume (usd m):", round(float(lq.get('BTCUSDT', np.nan)) / 1e6, 1))
print("all present pairs above 10m?", bool((lq >= 10e6).all()),
      "names under 10m:", list(lq[lq < 10e6].index))

# ---------------- M7 stablecoin peg ----------------
print("\n=== M7 STABLECOIN PEG ===")
for s in ('USDCUSDT', 'TUSDUSDT', 'FDUSDUSDT', 'BUSDUSDT', 'DAIUSDT', 'USDPUSDT'):
    d = p[p['symbol'] == s]
    if len(d) == 0:
        print(" ", s, "not in panel")
        continue
    j = d['c'].idxmin()
    print(" ", s, "bars", len(d), "min low", round(float(d['l'].min()), 4),
          "max high", round(float(d['h'].max()), 4),
          "worst daily close", round(float(d['c'].min()), 4),
          "on", str(d.loc[j, 'date'])[:10])
