# What makes a source usable

Four gates. All four must pass, and `scout.py gate` is the only place the answer is
recorded. Each gate exists because of a specific verified failure, named below — none of
them is a preference.

```
python3 scripts/scout.py gate --name "<source>" \
  --free yes --keyless yes --history-days <N> --lag-min <M> --backtestable yes
```

## 1. Free

An automated run has one Claude credential and nothing else. A paywalled series cannot be
re-pulled by the scheduled job, cannot be reproduced in a decision replay, and cannot be
re-checked by the change gate — so a result built on one is unverifiable by construction,
not merely expensive.

Verified failures: farside.co.uk **403**; `api.llama.fi/emissions` **402**; bridge-volume
endpoints **402**; CoinGlass and SoSoValue key-gated; Glassnode and CryptoQuant
subscription-only.

## 2. Keyless

Account-scoped is out for the same reason plus one more: Binance keys are allowlisted only
for `reconcile` and `preflight`, and `runs/common.py: guard_env()` hard-fails if one ever
reaches a model job. A feature that needs an exchange key therefore **cannot be computed in
any research run at all**, whatever its merits.

`needs_key=True` in the feature registry is a rejection, not a caveat.

Verified failures: `fapi/v1/forceOrders` **401** (account-scoped); beaconcha.in staking
queue **401**.

## 3. Deep enough — the floor is 730 days

This gate exists because of one specific, extremely easy mistake:

> **Every `fapi/…/futures/data/*` endpoint retains about 30 days.** `openInterestHist` with
> `limit=500` and no `startTime` returns exactly **31** daily rows; with a `startTime` one
> year back it returns HTTP 400 `parameter 'startTime' is invalid`.

A backtest built on that endpoint silently has a **one-month sample** and will look
magnificent. Nothing in the response says so. Deep history for those series exists only in
the free `data.binance.vision` bulk archive (metrics from **2020-09-01**, bookDepth from
**2023-01-01**).

730 days is a floor, not a target, and it is generous: the sample that matters is the
*effective* one. 19,879 labelled 4h rows carry **3,020** independent observations — 6.6 rows
per real one. `edge-audit` computes the effective N; this gate only stops the obviously
hopeless.

The other verified short-history traps: Blockscout balance history returns **10 days**;
`eth_feeHistory` reaches back about **1,024 blocks**; the monthly bulk funding archive does
not reach 2019 (2019-09 and 2019-10 are **404**) even though the REST endpoint does.

## 4. Low lag — the ceiling is one day

A value older than the decision cadence is a memory, not an input. Real-time SOPR and NUPL
are withheld for **7 days** behind a subscription; a 7-day-old profitability reading cannot
inform a 1d decision and is barred from the decide feature set rather than allowed to look
current.

Set `--max-lag-min` tighter than the default when the decision is faster than daily. The
registry's per-key `max_lag_min` is the number a consumer checks at runtime; this gate is
the design-time version of the same question.

## 5. Backtestable — point-in-time or nothing

The gate that catches the cleverest mistakes. A source can be free, keyless, and years deep
and still fail this one:

- **Forward-only.** The `!forceOrder@arr` liquidation websocket is free and keyless and has
  no history at all. It may accrue an observe-only record; it may not feed a decision for at
  least twelve months.
- **Vendor-restated.** A series whose past values change when the vendor recomputes them
  cannot be replayed. Exchange balance history is both restated and 10 days deep.
- **Silently forward-looking.** Any series that only exists for instruments that still
  exist. A universe backtest on today's listed pairs cannot lose money on the coins that got
  delisted, because they are not in the API.

`observe_only` is the honest label for all three, and it is a real, respectable answer. A
"backtest result" on 10 days is noise with a number attached.

## The gate that is not on the list

**"Is it interesting?"** is not a gate, and neither is "is the data verified?". A verified
*source* is not a verified *signal* — the session-hours ETF-flow proxy is free,
backtestable, cleanly labelled by the researcher who proposed it, and still worthless,
because there is no measured relationship between the proxy and the thing it proxies. The
only thing that turns a source into a signal is a pre-registered hypothesis measured against
both baselines with costs on, which is `hypothesis-lab`'s job, not this one's.
