# The measurement API — how a run measures something itself

One command does all the measuring. It is allowlisted, so it is the only way a run starts
a backtest, and it is the only thing a run needs to know about docker, freqtrade or the
feather store — which is nothing.

```
python3 ${CLAUDE_SKILL_DIR}/scripts/research_api.py <subcommand> [--json]
```

The Python behind it is `evals/backtest_api.py`, `evals/hypothesis.py` and
`runs/features/series.py`. Read those docstrings when a subcommand is not enough.

## The order, and it is not optional

1. `hypothesis record --file h.json` — the prediction, written down **before** any number
   exists. A recorded hypothesis cannot be overwritten and cannot be edited: the file
   carries a digest and grading re-checks it.
2. `compare` or `walk-forward` — the measurement.
3. `hypothesis grade <id> --baseline base.json --measured cand.json` — the verdict, which
   is **computed** from the prediction and the numbers. There is no field to fill in that
   makes a result a success, a predicted metric may not be dropped from the measurement,
   and a hypothesis may be graded once.

A falsified hypothesis stays on disk. That record is the only thing stopping the loop
proposing the same idea again next quarter.

## What a patch may touch

`namespaces` prints the current answer; it is the source, not this page.

| namespace | lands in | typical use |
|---|---|---|
| `params.<key>` | `config/params-sleeve-<s>.json` | `params.vol.target_annual`, `params.trend.ma_days` |
| `trading.<key>` | `riskgate.json :: trading.sleeves.<s>` | `trading.take_profit.roi_table`, `trading.take_profit.ladder`, `trading.stoploss.trailing.enabled`, `trading.stoploss.atr.*`, `trading.dca.*`, `trading.pyramid.*` |
| `execution.<key>` | `riskgate.json :: execution` | `execution.rebalance_band` |

Everything else is **refused by name with the reason** — risk limits, the universe,
capital, mode, credentials, the bounds table, the timeframe, the strategy. Not ignored:
refused, with exit code 3. A patch that is silently dropped produces a result labelled with
a change that never happened, which is worse than no result.

Two more refusals worth knowing:

- A `params.*` value outside `riskgate.json: bounds` is refused, because the container
  would clamp it and keep going and the run would then report a number for a parameter it
  did not test. Moving a bound is a human change.
- `--pairs` **narrows** the configured whitelist to measure a subset. It can never add a
  pair; that would be a universe change.

Dotted vs nested: `{"trading.take_profit.roi_table": {...}}` replaces the table wholesale;
`{"trading": {"take_profit": {"roi_table": {...}}}}` merges into it. Prefer dotted.

## What comes back

`Metrics` carries net return, CAGR, max drawdown, Calmar, Sharpe (freqtrade's and an
equity-curve `sharpe_daily`), Sortino, profit factor, win rate, trade count, annual
turnover, **fees actually paid** summed from the filled orders, time in market, average
concurrent positions, the exit-reason breakdown, per-year and per-BTC-regime splits, and
the equity curve. Costs are always applied from `config/backtest.yaml`; there is no way to
ask for a free backtest.

`compare` prints `better` **and** `worse`, every time, plus the per-year deltas, the
exit-count deltas and the excess over costed buy-and-hold BTC. A change that moves nothing
past the noise floor is printed as *"this change does nothing"* — drop it yourself rather
than making the gate do it.

`walk-forward` states each fold's in-sample and out-of-sample span, and states in the
result that Sleeve A fits nothing, so the folds test **stability across regimes**, not an
estimator's generalisation. Do not quote them as the latter.

## Measuring something that is not a config change

`study` conditions a target on a feature over the real candle store:

```
research_api.py study --pairs BTC/USDT,SOL/USDT,... \
  --feature run_up:30 --target fwd_return:30 --buckets 5 --cut 2.0 --side below
```

Features: `run_up:N`, `dd_from_ath`, `days_since_ath`, `rs_vs_btc:N`, `vol:N`,
`volume_trend:N`, `listing_age`. Targets: `fwd_return:N`, `fwd_drawdown:N`.

`--cut` scores a proposed rule and always prints the **excluded** population next to the
kept one. A filter that dodges the dumps by also dodging the rallies looks excellent in one
headline number and is obvious here. The verdict is mechanical: `REJECT` when the excluded
rows did as well as the kept ones on both the mean and the tail — that rule removes sample,
not risk.

Every panel prints its own survivorship warning. If the pair list came from today's
whitelist, every coin in it survived, and no test in this toolkit can remove that.

## Costs and cache

Costs come from `config/backtest.yaml` (TCA-measured), applied per side, with slippage
folded into freqtrade's `--fee` because freqtrade has no slippage knob.

An identical run — same patch, timerange, pairs, costs, config digest, candle digest — is
served from disk and says `cached: true`. Changing the patch changes the digest and
re-runs. Do not try to get a different answer out of the same inputs; you will get the same
answer, which is the point.
