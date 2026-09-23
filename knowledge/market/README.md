# knowledge/market — the cached copy of every vendor-hosted series

Tier 0, and **committed on purpose**. A cached series is the only reason a vendor-hosted
feature can still be replayed a year from now: the vendors here either cap their history
(Binance's `futures/data/*` endpoints retain ~30 days) or could withdraw it, and a backtest
that silently loses its inputs is a backtest nobody can reproduce.

Nothing in here is edited by hand. Each file is written by the module that owns it, by an
idempotent merge, and only **closed** bars are stored — a row stamped `D` was knowable at
`D+1 00:00Z` and never earlier. That is what makes these files safe to replay over.

| Path | Owner | Contents | Refresh cadence |
|---|---|---|---|
| `dvol/<CUR>-1d.csv` | `runs/features/deribit.py` | Deribit DVOL daily OHLC, `date,open,high,low,close` | once per UTC day, after 00:10Z |

## dvol/

* Source: `https://www.deribit.com/api/v2/public/get_volatility_index_data` — free, keyless,
  no account. A browser `User-Agent` is required; Deribit resets the connection on
  urllib's default and the failure looks like a network outage.
* History: **2021-03-24 onward for both BTC and ETH** (2,009 daily bars each as of
  2026-09-23). The API returns the *latest* 1,000 points inside a requested span, so a
  single wide request looks as though ETH's history starts in late 2023; paging backwards
  on the response's `continuation` timestamp reaches the true start for both.
* Refresh: `python3 .claude/skills/vol-surface/scripts/compute_volsurface.py` updates it as
  a side effect, re-fetching from five days before the cached tail so a revised bar is
  corrected rather than duplicated. `--offline` skips the fetch and uses what is here.
* When Deribit is unreachable the cached series is used with `stale=true` and the true
  `as_of` of the last cached bar. Nothing invents a level.
