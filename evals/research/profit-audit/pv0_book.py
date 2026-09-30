"""pv0: independent 8-slot 5%-stake book, costed, 2019-01-01 -> panel end."""
import pandas as pd, numpy as np, re, json, sys
PANEL='/home/shourya/earn-panels/panel_1d.parquet'
START=pd.Timestamp('2019-01-01',tz='UTC')
COST=0.0015  # per side
p=pd.read_parquet(PANEL).sort_values(['symbol','date'])
inc={s.replace('/','') for s in json.load(open('/home/shourya/earn-run/config/freqtrade-a.json'))['exchange']['pair_whitelist']}
lev=set(s for s in p.symbol.unique() if re.search(r'(UP|DOWN|BULL|BEAR)USDT$',s))
stab={'BUSD','USDC','TUSD','DAI','FDUSD','EUR','USDP','PAX','SUSD','AEUR','USD1','USDE','PYUSD','GBP','AUD','BIDR','IDRT','NGN','RUB','TRY','BRL','ZAR','UAH','BVND'}
stab={s for s in p.symbol.unique() if s[:-4] in stab}
g=p.groupby('symbol')
p['age']=g.cumcount()
p['adv90']=g['qv'].transform(lambda s:s.rolling(90,min_periods=30).median())
p['ret']=g['c'].pct_change()
p['vol60']=g['ret'].transform(lambda s:s.rolling(60,min_periods=40).std()*np.sqrt(365))
p['ma125']=g['c'].transform(lambda s:s.rolling(125,min_periods=125).mean())
p['elig']=(p.age>=1095)&(p.adv90>=1e7)&(p.vol60<=1.0)&p.ma125.notna()
p['up']=p.c>p.ma125
def run(univ,label):
    d=p[p.symbol.isin(univ)&(p.date>=START-pd.Timedelta(days=1))]
    dates=sorted(d.date.unique()); dates=[x for x in dates if x>=START]
    byd={k:v for k,v in d.groupby('date')}
    held={}; nav=1.0; rows=[]
    prev=None
    for dt in dates:
        cur=byd[dt].set_index('symbol')
        # mark to market yesterday's holdings using today's return
        pnl=0.0; exp=0.0
        for s,wt in held.items():
            r=cur['ret'].get(s,np.nan)
            if not np.isfinite(r): r=0.0
            pnl+=wt*r; exp+=wt
        nav*= (1+pnl)
        # signals known at close of dt (one-bar lag -> act next day, approximated by trading at close)
        cand=cur[(cur['elig'])&(cur['up'])].sort_values('vol60')
        want=set(cand.index[:8])
        gone=[s for s in held if s not in want]
        turn=0.0
        for s in gone: turn+=held.pop(s)
        free=8-len(held)
        for s in list(want):
            if s in held: continue
            if free<=0: break
            held[s]=0.05; free-=1; turn+=0.05
        nav*= (1-COST*turn)
        rows.append((dt,nav,exp))
    r=pd.DataFrame(rows,columns=['date','nav','exp']).set_index('date')
    ret=r['nav'].pct_change().dropna()
    yrs=len(ret)/365
    eq=r['nav']
    print(f"{label:16s} n={len(ret)} sharpe={ret.mean()/ret.std(ddof=1)*np.sqrt(365):.3f} "
          f"cagr={(eq.iloc[-1]**(1/yrs)-1)*100:6.2f}% dd={(eq/eq.cummax()-1).min()*100:6.2f}% "
          f"meanexp={r['exp'].mean()*100:.1f}% nsym={len(univ)}")
allsym=set(p.symbol.unique())
exc=allsym-inc-lev-stab
run(sorted(inc),'included-31')
run(sorted(exc),f'excluded-{len(exc)}')
