"""Loss-scenario register measurements, part 2: outage slippage, lock cost, funding."""
import glob
import numpy as np, pandas as pd
PANEL = '/home/shourya/earn-panels/panel_1d.parquet'
p = pd.read_parquet(PANEL).sort_values(['symbol', 'date'])
end = p['date'].max()

WL = [x.replace('/', '') for x in (
    "BTC/USDT,ETH/USDT,ZEC/USDT,NEAR/USDT,XRP/USDT,SOL/USDT,DOGE/USDT,TRX/USDT,"
    "UNI/USDT,TAO/USDT,PUMP/USDT,LINK/USDT,AAVE/USDT,LTC/USDT,XLM/USDT,PEPE/USDT,"
    "BCH/USDT,INJ/USDT,ONDO/USDT,ENA/USDT,WLD/USDT,ADA/USDT,SUI/USDT,HBAR/USDT,"
    "FIL/USDT,AVAX/USDT,FET/USDT,PENGU/USDT,TRUMP/USDT,DOT/USDT,XPL/USDT").split(',')]

# ---------------- M2 the bot is down while the stop is breached ----------------
print("=== M2 OUTAGE SLIPPAGE (hourly, BTC and ETH) ===")
files = sorted(glob.glob('/home/shourya/earn-run/data/binance/*-1h.feather'))
print("1h feather files:", len(files))
print("sample:", [f.split('/')[-1] for f in files[:6]])
STOP = 0.06
for sym in ('BTC_USDT', 'ETH_USDT', 'BTC-USDT', 'ETH-USDT'):
    cand = [f for f in files if sym in f]
    if not cand:
        continue
    d = pd.read_feather(cand[0])
    tcol = 'date' if 'date' in d.columns else d.columns[0]
    d = d.sort_values(tcol).reset_index(drop=True)
    print("\n", cand[0].split('/')[-1], "bars", len(d), "from", str(d[tcol].iloc[0])[:16],
          "to", str(d[tcol].iloc[-1])[:16], "cols", list(d.columns))
    lo, cl = d['low'].to_numpy(), d['close'].to_numpy()
    n = len(d)
    # every hour where the bar's low pierced 6% below the close 24 bars earlier
    # (a stand-in for "a stop set 6% under a recent entry was breached this hour")
    for H in (14, 63, 24, 48):
        losses = []
        for i in range(24, n - H):
            stop = cl[i - 24] * (1 - STOP)
            if lo[i] <= stop:
                # the bot is asleep for H hours; it sells at the close H bars later
                losses.append(cl[i + H] / stop - 1)
        if not losses:
            continue
        a = np.array(losses)
        print(f"  outage {H}h after a 6pct stop breach: n={len(a)} "
              f"median {100*np.median(a):+.2f}pct  mean {100*a.mean():+.2f}pct  "
              f"p5 {100*np.quantile(a,.05):+.2f}pct  p1 {100*np.quantile(a,.01):+.2f}pct  "
              f"worst {100*a.min():+.2f}pct  share worse than the stop {100*(a<0).mean():.1f}pct")
    # worst adverse move over the two measured outage windows, unconditionally
    for H in (14, 63):
        r = cl[H:] / cl[:-H] - 1
        print(f"  unconditional worst {H}h close-to-close move: {100*r.min():+.2f}pct "
              f"(p1 {100*np.quantile(r,.01):+.2f}pct)")

# ---------------- M2b daily-panel version for the whole whitelist ----------------
print("\n=== M2b DAILY: how much further does it fall after the stop line breaks ===")
w = p[p['symbol'].isin(WL)].copy()
w['prev_c'] = w.groupby('symbol')['c'].shift(1)
for k in (1, 3, 7):
    w['fwd' + str(k)] = w.groupby('symbol')['c'].shift(-k)
w = w.dropna(subset=['prev_c', 'fwd7'])
stopline = w['prev_c'] * (1 - STOP)
br = w[w['l'] <= stopline].copy()
br['stop'] = br['prev_c'] * (1 - STOP)
print("breach bars:", len(br))
for k in (1, 3, 7):
    x = br['fwd' + str(k)] / br['stop'] - 1
    print(f"  sell {k} day(s) late instead of at the stop: median {100*x.median():+.2f}pct "
          f"mean {100*x.mean():+.2f}pct p5 {100*x.quantile(.05):+.2f}pct "
          f"worst {100*x.min():+.2f}pct")

# ---------------- M6 the daily hold stop: what the lock costs ----------------
print("\n=== M6 DAILY -3pct STOP, response = hold (locks entries 24h) ===")
wide = w.pivot_table(index='date', columns='symbol', values='c').sort_index()
ret = wide.pct_change()
common = ret.dropna(axis=1, thresh=int(0.5 * len(ret)))
book = common.mean(axis=1) * 0.80
trig = book[book <= -0.03].index
print("trigger days:", len(trig), "of", int(book.notna().sum()),
      "=", round(100 * len(trig) / int(book.notna().sum()), 1), "pct of days")
fwd = {}
for k in (1, 2, 3, 7, 30):
    f = (1 + book.fillna(0)).cumprod().pct_change(k).shift(-k)
    fwd[k] = f
    on, off = f.loc[f.index.isin(trig)], f.loc[~f.index.isin(trig)]
    print(f"  forward {k}d book return AFTER a trigger: median {100*on.median():+.2f}pct "
          f"mean {100*on.mean():+.2f}pct   |  on all other days: median {100*off.median():+.2f}pct "
          f"mean {100*off.mean():+.2f}pct")
btcd = ret['BTCUSDT']
eqb = (1 + btcd.fillna(0)).cumprod()
for k in (1, 7):
    f = eqb.pct_change(k).shift(-k)
    on = f.loc[f.index.isin(trig)]
    print(f"  BTC forward {k}d after a trigger: median {100*on.median():+.2f}pct "
          f"mean {100*on.mean():+.2f}pct (all days mean {100*f.mean():+.2f}pct)")

# ---------------- M6b cap breach: 422 of 3326 days, what the excess did ----------------
print("\n=== M6b BTC CAP BREACH (cap 0.40) ===")
btcr = ret['BTCUSDT'].fillna(0)
cash = 1 - 0.40
units = 0.40
nav = pd.Series(index=btcr.index, dtype=float)
wts = pd.Series(index=btcr.index, dtype=float)
eq_btc = (1 + btcr).cumprod()
# a never-rebalanced 40pct BTC sleeve against 60pct flat cash: the weight that drifts
val = 0.40 * eq_btc / eq_btc.iloc[0]
navs = val + 0.60
wts = val / navs
br = wts[wts > 0.40 + 1e-9]
print("days above the 0.40 BTC cap with no order to refuse:", len(br), "of", len(wts),
      "=", round(100 * len(br) / len(wts), 1), "pct")
print("median excess weight on those days:", round(float((br - 0.40).median()), 4),
      "max excess:", round(float((br - 0.40).max()), 4))
fwd30 = eq_btc.pct_change(30).shift(-30)
print("BTC forward 30d on breach days: median",
      round(100 * float(fwd30.loc[fwd30.index.isin(br.index)].median()), 2), "pct",
      "| on all days:", round(100 * float(fwd30.median()), 2), "pct")
exc = (br - 0.40)
f30 = fwd30.loc[fwd30.index.isin(br.index)]
print("excess x forward 30d return, mean:", round(100 * float((exc * f30).mean()), 3), "pct of NAV")
print("worst single breach-day outcome (excess x fwd30):",
      round(100 * float((exc * f30).min()), 2), "pct of NAV")

# ---------------- M8 funding: what spot-only is NOT exposed to ----------------
print("\n=== M8 FUNDING (spot-only avoids this) ===")
try:
    f = pd.read_parquet('/home/shourya/earn-panels/funding.parquet')
    print("funding rows", len(f), "cols", list(f.columns))
    sc = [c for c in f.columns if 'sym' in c.lower()]
    rc = [c for c in f.columns if 'rate' in c.lower() or c in ('r', 'funding')]
    dc = [c for c in f.columns if 'time' in c.lower() or 'date' in c.lower()]
    print("guess symbol/rate/date cols:", sc, rc, dc)
    if sc and rc and dc:
        s, r, dcol = sc[0], rc[0], dc[0]
        b = f[f[s].astype(str).str.upper().str.startswith('BTC')].sort_values(dcol)
        print("BTC funding rows", len(b), "from", str(b[dcol].iloc[0])[:10],
              "to", str(b[dcol].iloc[-1])[:10])
        rr = b[r].astype(float)
        print("  mean per interval", round(float(rr.mean()), 6),
              "-> annualised", round(100 * float(rr.mean()) * 3 * 365, 2), "pct")
        cum = rr.rolling(90).sum()
        print("  worst 90-interval (30d) cumulative funding a perp long would pay:",
              round(100 * float(cum.max()), 2), "pct")
        print("  worst single interval:", round(100 * float(rr.max()), 4), "pct")
        print("  total funding paid by a always-long BTC perp over the sample:",
              round(100 * float(rr.sum()), 1), "pct of notional")
except Exception as e:
    print("funding read failed:", e)
