# The exit, the horizon, and what the bot can actually do for you

Status: measured 2026-09-29. Written for the owner, who asked four questions in plain words and
deserves plain answers. Nothing in this document is switched on by it. No production code was
written, no commit was made, no bot, cron, unit or console was touched, and nothing was written into
the live runtime. The measurement scripts live in `evals/research/exit-horizon/`.

Three independent measurement teams ran today. They read the code first, then measured on the
survivorship-free daily panel (747 USDT tickers that ever existed, 282 of them dead, 2017-08-17 →
2026-09-24, 3,326 days). Every number below is **net of a 0.30% round trip** (10 bps fee + 5 bps
slippage per side), always on. Risk-adjusted return is the repo's arithmetic Sharpe
(mean/std × √365), never CAGR ÷ vol.

---

## The four answers, in four sentences

1. **Will it choose coins that rally?** No — and nothing measured in nine years can pick them, which
   is why the system holds BTC and ETH and rides the market's own trend instead.
2. **Will it sell to USDT, or rotate, before a drop?** No. It rides the drop down, and it never
   rotates at all.
3. **Does the logic cover daily, short-term and mid-term, and all scenarios?** It covers mid-term
   only, and that is the right answer on horizons rather than a gap — but "all scenarios" is a no,
   and the holes are operational, not statistical.
4. **Are the coins researched and the stats checked well?** Yes, unusually well — better than the
   code they justify, which is how today's defect was found.

Then: one change to make (§5), eleven things not to do (§6), and what I cannot stand behind (§7).

---

## 1. Will it choose coins that rally?

**No, and nothing measurable can. There is no coin-picking in this system, by design, and the
evidence says that is correct.**

The bot holds BTC and ETH. It does not look for the next coin that will rally, and every attempt in
this repo's history to build that has been measured and killed:

| what was tried | the measured result |
|---|---|
| Ranking coins by trailing 30–90d return | Rank IC **−0.016 to −0.069** across three panels — the *wrong sign*, consistently. `growth-audit.md` |
| A costed rotation: top 8 by 90d momentum, weekly, 15 bps | **−13.5% CAGR with a −98.9% drawdown**, and it loses *before* costs. `growth-audit.md` rejection row |
| The same idea at 20 / 60 / 120-day lookbacks | Sharpe 1.04 / 0.85 / 0.55 — a swing that size across a routine choice is the definition of an artefact. Ledger `cross-sectional-momentum` |
| Buying dips in a basket of screened coins | **0 of 6,720** costed configurations beat holding BTC; 0 of 6,720 beat the MA125 filter already shipped. `dip-strategy.md` §3.4 |
| Machine learning on direction | Not forecastable at a size that survives 15 bps. A single feature (`−vol_60`) beat the best ML model at 4 of 6 targets. `ml-forecast.md` |

And the base rate is brutal: in 2023-24 only **3.4%** of coins beat BTC (`growth-audit.md`). Today's
own measurement adds an independent construction of the same fact from a new angle — the holding
period. Using the repo's own quality screen (annualised vol ≤ 1.00, listing age ≥ 3 years, median
90d volume ≥ $10M) *and* the trend gate on top, the **median** altcoin position is loss-making net
of costs at every holding period, and gets worse the longer it is held:

| holding period | 1d | 20d | 90d | 180d |
|---|---|---|---|---|
| median net return | −0.26% | −1.46% | −8.56% | **−17.63%** |
| share of positions profitable | 46.6% | 46.3% | 40.9% | **36.4%** |

The *average* is positive at every horizon. That average is a thin right tail — at six months barely
a third of positions make money — so it is not investable without knowing in advance which third,
and the rank IC above says nobody here can.

**What this means for you.** When the system works, it is not because it picked a winner. It is
because it was holding BTC and ETH while they went up, and because it was holding less of them when
the trend was weak. "Which coin" is not the lever. "How much, and when to have less" is the lever —
which is exactly what §2 is about, and exactly where the code is broken.

---

## 2. Will it sell to USDT, or convert to a better coin, before a drop?

**No. As the code stands today it rides the drop down, and it converts to nothing. This is the
defect this document exists to report.**

### What the code does

The system has a good instrument for this and does not use it. `runs/features/trend.py` computes a
fifteen-member trend signal on BTC and ETH — ten moving averages and five Donchian channels, each
voting on or off — and its output is a weight between 0 and 1. The design document that justified
building it (`docs/design/trend-ensemble.md`, from `dip-strategy.md` §0.3) measured a book in which
**your exposure equals that weight every day**: as votes switch off, the book sells down; as they
come back, it buys back. That book earned **43.1% a year with a −45.6% worst drawdown**, against
buy-and-hold BTC's 38.6% and −83.2%.

In the running code that weight is read **only when buying**. This is not an inference. The code says
so itself, at `strategies/earn_base.py:581-586`:

> "Applied in exactly two places, both on the BUY side: `custom_stake_amount` (a new entry) and
> `_gated_add` (every add). **It never touches an exit** …"

I read every line of the exit path to confirm it. `custom_exit` (`earn_base.py:985-990`) reads only
the monthly/daily flatten flag and a sleeve hook that is `return None` and is not overridden by
SleeveA. SleeveA's entire exit signal is one line, `SleeveA.py:92`:

```python
dataframe.loc[dataframe["regime_1d"].fillna(0) == 0, "exit_long"] = 1
```

`regime_1d` is a **single** 200-day moving average with 2% hysteresis. And nothing on the sizing side
can sell either: `SleeveA._desired_stake` returns `0.0` when there is no gap to fill
(`SleeveA.py:201-206`) and `SleeveA._sleeve_adjust` returns `None` (`SleeveA.py:257-259`). Neither
can return a negative number. **SleeveA has no way to make a position smaller.**

Because the weight is applied to the *headroom* (`weight × target × NAV − position`), a rising weight
buys more and a falling weight merely buys less. The book scales **in** and never **out**.

Every other exit is switched off in the shipped configuration: the take-profit ladder is empty
(`earn.yaml:264`), the ROI table is 10.0 which means off (`:263`), trailing stop `false` (`:258`),
ATR stop `false` (`:260`), and the daily-loss response is `hold` (`:189`). So the shipped sleeve has
exactly **three** sells, none of them the trend signal:

1. the 200-day MA flip — and it is *throttleable*, see below;
2. the 10% per-trade stop (`trading.stoploss.fixed_pct`, ceiling `risk.stoploss_per_trade: 0.15`);
3. the sleeve-level monthly −10% flatten in `riskgate.loop_tick` — a circuit breaker, not a strategy.

In nine years of simulation that produced **40 sells in total** (23 regime exits, 17 stop-outs, zero
trims) on 37 distinct days — **4.1 sells a year** — against 42 entries and 49 scheduled top-ups. The
bot traded on 117 of 3,326 days.

### What it costs

Three teams modelled the deployed book three different ways. They disagree on the return figure and
they agree completely on the verdict:

| construction | the deployed book | the book the docs justify | BTC buy-and-hold |
|---|---|---|---|
| Full sleeve mechanics (shipped 0.40/0.30 targets, vol targeting, caps, stop, DCA, band) | 21.4% CAGR / **Sharpe 0.83** / −45.8% | 14.7% / **1.16** / −14.7% | 38.6% / **0.83** / −83.2% |
| Signal level, exposure latched at its running maximum | 33.4% / **0.88** / −60.2% | 43.1% / **1.10** / −45.6% | 38.6% / **0.83** / −83.2% |
| Signal level, exposure latched at entry | 26.2% / **0.90** / −51.1% | 43.1% / **1.10** / −45.6% | 38.7% / **0.83** / −83.2% |

**Read the middle column of risk-adjusted returns. The deployed book scores 0.83–0.90. Buy-and-hold
BTC scores 0.83. The book that justified building the whole trend system scores 1.10 to 1.16.** The
entire measured edge is consumed between the signal and the book. On a formal test the deployed book
is statistically indistinguishable from simply holding Bitcoin (ΔSharpe +0.007, p = 0.48).

The single cleanest comparison needs no normalising. At the signal level, the scale-out book and the
deployed book hold **almost exactly the same average exposure** — 0.45 against 0.46 of NAV. Same
average amount of money at risk. The deployed one earns **9.7 points a year less** and suffers
**14.5 points more drawdown**. It is not a cautious version of what was tested; it is worse on both
counts, and it earns less than doing nothing at all (33.4% against BTC's 38.6%).

Fees do not explain any of it. The deployed book pays **0.19%/yr** in fees against the re-sized
book's 0.58%/yr. **It is cheaper and still worse.** Of the total damage, 94% is the missing trim; the
choice of MA200 over the ensemble as the exit trigger costs only 2.7 points of CAGR and is
drawdown-neutral.

### What it looks like in the falls you would actually live through

Across the five worst BTC drawdowns after warm-up, here is how long each book took to halve the
exposure it carried into the peak:

| peak → trough | BTC fall | signal halved in | deployed halved in | days late |
|---|---|---|---|---|
| 2021-11 → 2022-11 | −76.6% | 32d | 27d | **+8** |
| **2021-04 → 2021-07** | −53.1% | 37d | **74d** | **+44** |
| 2025-10 → 2026-06 | −53.0% | 11d | 29d | **+24** |
| 2025-01 → 2025-04 | −28.1% | 16d | 48d | **+12** |
| **2024-03 → 2024-09** | −26.2% | 49d | **114d** | **+65** |

Median **24 extra days**, up to 65. Two are stark. In April 2021 the deployed book carried **0.89 of
NAV** into a −53% fall, took 74 days to halve it, and lost **25.1%** where the same targets re-sized
daily lost **1.4%**. In March 2024 it took 114 days against the signal's 49 and absorbed **97% of
the entire fall** on the exposure it started with. The one case where the gap is small is the slow
2021-22 bear, where a 200-day average and the ensemble happen to agree. The asymmetry bites in *fast*
falls — the ones that make the drawdown.

On the 535 days the signal's requested exposure fell, the request dropped 6.8 points a day on
average and the deployed book's actual exposure dropped 1.5. **Its exposure falls because the asset
falls, not because it sold.**

### It also quietly breaks three of its own limits

The risk gate is the safety layer, and it validates **orders**. A position that grows past its cap
generates no order, so there is nothing for the gate to refuse
(`riskgate.py` — `weight_cap`, `gross_cap` and `usdt_floor` are all of the form
`(position + stake) / nav ≤ cap`). Over nine years the never-trimming book:

| limit | shipped value | what the deployed book did |
|---|---|---|
| max gross exposure | 0.80 | reached **0.921**, over on **136 days** |
| BTC weight cap | 0.40 | peaked **0.509**, over on **422 days (12.7%)** |
| ETH weight cap | 0.30 | peaked **0.607**, over on **265 days (8.0%)** |
| USDT floor | 0.20 | pierced on **136 days** |
| sum of caps | 0.70 | over on 278 days, longest unbroken run **158 days** |

Its single worst day for exposure in nine years is **2021-05-12 at 0.921 of NAV invested — the day
before the May-2021 crash.** The ten highest-exposure days are all 4–15 May 2021.

The repo even ships a check for precisely this — `trend.check_exposure_tracks_signal`, whose
docstring promises realised exposure within ±0.10 of the signal's weight *every day*, and whose
stated purpose is to catch "a stuck signal, a fill that never happened and **an exit the ensemble did
not ask for**". Pointed at the deployed rules for the first time today, it **fails on 1,103 of 3,069
days (35.9%), worst deviation 0.803 of NAV against a 0.10 tolerance.** It had only ever been aimed at
plumbing, never at the strategy.

### And the one sell it has can be refused

`confirm_trade_exit` (`earn_base.py:876-901`) runs every exit through
`riskgate.check_discretionary_exit`, which waves through anything `mechanics.is_risk_exit` recognises
and otherwise applies three churn checks: orders per day, turnover per day, and the monthly fee
budget. `exit_signal` — SleeveA's only discretionary sell — is **not** in `RISK_EXIT_REASONS` and
matches no prefix. So the regime-flip exit is subject to a fee budget. This is `crisis-policy.md`
**G6**, already on the books; what is new is that it applies to the shipped 4h sleeve, not just to
the fast profile it was filed against.

**The honest summary for you.** It does not sell before a drop. It sells after, on one slow signal,
about four times a year, and only if a fee budget lets it. It never converts one coin into another.
The measured cost of that is the whole difference between a system with an edge and a system that
matches buy-and-hold while breaching its own exposure limits on a third of all days.

---

## 3. Does the logic cover daily, short-term and mid-term — and all scenarios?

**On horizons: it covers mid-term only, and that is the correct answer rather than a hole to fill —
the daily and intraday layers cannot pay at this fee tier, and one of them is losing money right
now. On "all scenarios": no, and the gaps are operational rather than statistical.**

### The three horizons, as the code actually implements them

| horizon | what implements it | what sells | intended hold | evidence |
|---|---|---|---|---|
| **Daily signal (1d)** | the 15-member trend ensemble | *nothing — never read on the sell side* | continuous | **Strongest in the repo.** Reproduced to the decimal today |
| **Mid-term (4h sleeves)** | SleeveA / SleeveB, `timeframe: 4h` | one MA200 flip, the 10% stop | months | The book measured is not the book shipped (§2) |
| **Short-term (1h `fast-test`)** | `SleeveFast`, runtime profile only | its own EMA cross, ladder, ROI, trailing, 6% stop | median 4h | **Yes, and it is negative: −1.29%/30d, −26.45% over 2024-01…2026-09** |
| Signal pipeline | `runs/signals/*` | has no order path | grades itself on 24h | measurement only |
| Holdings watcher | `runs/watch/` | structurally cannot act (`WRITABLE_TABLES = {"watch_events"}`) | — | G9: escalation has no consumer |

There is **no minimum holding period anywhere in the repo** — a grep for one returns nothing. And
`risk.min_edge` exists only in the static form that `analogue-timing.md` §4.5 measured as **vacuous**
(it refuses 0 of 463 entries); the minimum-*holding* form that the same document says is the form
that works is not built. On the shipped 4h config it cannot bind at all, because a book that takes no
profit has no smallest booked target — `earn.yaml:237` admits this in a comment.

### Why the daily horizon cannot pay

A round trip costs **0.300%** of the position. Your own gate demands **0.900%** before it will let an
entry through (`min_edge`: 0.30% × 3). Measured against that floor:

| what | measured mean gross edge | fraction of the 0.300% floor |
|---|---|---|
| Shipped 1h rule, held 1 hour | **−0.0095%** | negative |
| Shipped 1h rule, held 4 hours (its actual median hold) | **+0.022%** | **7%** |
| Same rule across 108 pairs at 4 hours | **+0.0001%** | 0% |
| Same rule, held 10 days | +1.822% | 607% |

**The fast profile needs 13.7× more edge than it produces.** Its own live evidence agrees from a
completely different direction: −1.29% net over 30 days with 1.35% paid in fees implies a gross edge
of +0.0136% per trade against the 0.300% needed — a **22× shortfall**. Two methods, nine years apart
in sample, same verdict. **The fees were not *a* cause of that loss; the gross edge was
indistinguishable from zero and the fees were the entire loss.**

Break-even, under the shipped entry rule: **~2 days** on the mean and ~3.5 days on the median for
BTC/ETH; ~1.9 days on the mean for the screened altcoin universe but **never on the median, out to
180 days**. Ten days is where the gate's own 0.90% requirement is first met. (This corrects a figure
we have been quoting loosely: `analogue-timing.md`'s "−0.92% at one-day holds, break-even about ten
days" is specific to the *analogue* entry on the alt book. Under the shipped trend entry the one-day
gross is +0.208% on BTC/ETH. Ten days remains right for the number the gate asks for; two days is
right for break-even alone.)

And the shape of it is unambiguous. Across 78 combinations of timeframe (1h, 4h, 1d), rule, and
holding period, risk-adjusted return rises monotonically with holding period on **every** column, and
the whole gradient is fees: 633% of NAV a year at a one-hour hold, 26.6% at one day, 0.42% at 180
days. **Every one of those 78 books is at or below buy-and-hold BTC's 0.83.** The best is 0.875. The
only book in this entire study that meaningfully beats the baseline is the one that scales
continuously out. It is the exit rule — not the entry rule, not the timeframe — where the edge lives.

For a daily sleeve to pay, one of four things would have to be true: costs fall ~14× (Binance spot's
best tier plus the irreducible slippage half makes this unreachable — and the ledger entry
`execution-timing` measured that Earn is **67× short** of even VIP1); a rule exists with 14× the
measured drift (`ml-forecast.md` says direction is not forecastable at a size that survives 15 bps);
maker-only execution captures the spread instead of paying it (unmeasured here, and the shipped
config deliberately pays the spread to get fills); or **you stop trading at that horizon**. Only the
last is available. The same signal held 10–20 days instead of 4 hours moves the measured edge from
+0.022% to +1.82%.

### The scenarios that are not covered

This is the part of the answer I would push you hardest on, because it is where the real exposure is
and none of it is about returns. Cross-checked against the code and the G1–G17 ledger in
`crisis-policy.md` §5.3, which I read directly:

| scenario | covered? | why not |
|---|---|---|
| **USDT stops being worth a dollar** | **No** | **G4.** `venue-guard` is bound to nothing and invoked by nothing — its own skill body claims a binding that does not exist. **Nothing in any scheduled job ever checks the peg.** Every price, every limit and every stop distance is quoted in USDT, so a 2% discount silently rescales all of them |
| **A coin is halted, or the venue is** | **No** | Same G4. Only `exchangeInfo` could block it, and nothing calls it |
| **A delisting** | **7-day latency** | **G12.** `universe.exit_only` is written only by the Sunday 18:00 refresh, baked in at config-generation time, and needs a container restart. **G11**: the flag schema is hard-wired to BTC/ETH, so a notice about any of the other 29 pairs can only block *everything* or nothing |
| **A gap straight through a stop** | **No, when live** | **F4.** The market-only-stop invariant does not hold on Binance spot; in TEST the stop is a candle-close check, so one candle past it is unprotected |
| **A 30% single candle** | **No** | 4h candles, stop checked on close, and the exit signal queues behind a fee budget (G6) |
| **Exchange outage mid-position** | **Partial** | Reconcile runs every 15 min and blocks on mismatch, but KILL does not flatten (**G10**) and `near_stop` — the only fast path a no-news crash has — **can never be true** because it reads an empty table (**G2**) |
| **A crash** | **Covered, correctly, by doing nothing** | `crisis-policy.md` §0: hold **+30.53%** CAGR, halve **+13.05%**, flatten **−5.23%**. Selling into a numeric trigger is the single worst policy measured in this system. `daily_loss_response: hold` is right. **G3**: the only selling tier with measured support (halve) has no mechanism at all |

Four of those — the peg check, the halt check, the delisting latency, and the throttleable exit — are
worth more of your attention than any return idea in this report. They are cheap to fix and they are
the ones that can cost you the book rather than a few points of it.

---

## 4. Are the coins researched, and the statistics checked, well?

**Yes — unusually well, and better than the code they justify. That is precisely how today's defect
was found. What the statistics say, though, is uncomfortable, and it is worth hearing plainly.**

On method, this repo does things most retail systems never do:

- The universe is **survivorship-free**: 747 tickers that ever existed, including the 282 that died.
  Testing on today's survivors is explicitly on the rejection ledger (`survivorship-universe`).
- **Costs are always on** — 0.30% round trip, on every number, in every study.
- Hypotheses are **pre-registered with numeric falsifiers before any number is computed**. Today six
  were sealed; the script adjudicated them, and it downgraded two of my colleagues' "supported"
  verdicts to "inconclusive" against their own preference. That is the machinery working.
- Walk-forward is **purged and embargoed** — the parameter for each out-of-sample year is chosen only
  on bars ending 280 days before it starts.
- There is a **deflated hurdle** that accounts for how many ideas have been tried. Cumulatively about
  **7,930 selection trials** before today; the three teams added **204** (9 + 31 + 164), taking it to
  about **8,134**. The hurdle sits at roughly **2.32** in risk-adjusted terms.
- Both baselines — buy-and-hold BTC *and* the deployed rules — are reported beside every result.
- Negative results get published. The rejection ledger has 20-odd entries, several of them ideas
  someone here was enthusiastic about.

**Nothing in today's work clears the hurdle, and I am not claiming it does.** The best figure measured
anywhere across all three teams is 1.32; the hurdle is 2.32. Everything in §2 is a *drawdown and
limit-compliance* finding — the deployed code does not implement its own documented design, and
breaches its own limits doing it — which needs no hurdle because it is not a claim about beating the
market. If anyone later quotes §2 as an alpha discovery, they are misquoting it.

Two honest blemishes in our own record, both found today:

- We have been quoting the ensemble's edge as **1.86×** buy-and-hold. That used a geometric ratio
  that flatters a low-volatility book. On the standard estimator it is **1.33×** (1.10 vs 0.83). The
  drawdown result (−45.6% vs −83.2%) is unchanged and remains the real finding.
- The repo has **three different hurdle numbers** in circulation — 2.07, 2.32 and 2.43 — because they
  disagree about how many *effective* years the sample is worth. Somebody should reconcile it: it is
  the difference between a 2.07 and a 2.43 bar for every future change. Related: the live trial
  counter still reads **3**, because the ~7,443 trials from the dip study were never entered (a
  human-only write, outstanding since `trend-ensemble.md` limit 7). The counter understates the bar
  by more than a full point.

On the coins themselves: the screen is sound (vol ≤ 1.00 annualised, listing age ≥ 3 years, median
90d volume ≥ $10M) and it was validated on the survivorship-free panel. The uncomfortable part is
the answer it gives — §1's table. Screening well does not make a bad universe good; it tells you the
universe is bad, and the honest response is the one already taken: hold BTC and ETH.

---

## 5. The one change the measurements support

**Let the signal that already sizes a new position also shrink an existing one. Nothing else.**

Concretely: in SleeveA, when the position is above `ensemble weight × target weight × NAV` by more
than the 5% rebalance band, sell the difference — walking the position down the same way it is
currently walked up. This is not new machinery. **SleeveB already does exactly this shape** at
`strategies/SleeveB.py:363-390`: it computes `gap = target × NAV − committed`, and when the gap is
negative it clears the trim with `check_discretionary_exit(pair, trim, ps, "rebalance")`, clamps to
the exchange filters, converts to the trade's cost basis via `_cost_basis_exit`, and returns an
`AdjustPlan` with a **negative** stake, journalled as `partial_exit`. Every part of that path is
built, shipped and tested. SleeveA's version would differ in one respect: its target is the
ensemble-scaled one, which is the target the buy side already uses.

### Measured effect, over 3,326 days at 15 bps a side

| | deployed today | with the trim |
|---|---|---|
| Risk-adjusted return (arithmetic Sharpe) | **0.83** | **1.12** |
| Worst drawdown | **−45.8%** | **−20.6%** |
| Peak exposure reached | 0.921 of NAV | 0.642 |
| Days over the 0.80 gross ceiling | **136** | **0** |
| Days over the BTC cap | **422 (12.7%)** | **0** |
| Days over the ETH cap | **265 (8.0%)** | **0** |
| Days below the USDT floor | **136** | **0** |
| Exposure-tracking check failures | 1,103 of 3,069 days | 44 (1.4%) |
| Fees | 0.19%/yr | **0.29%/yr** |
| Turnover | 1.29× NAV/yr | 1.95× NAV/yr |

In the current regime — the 632 days inside the ongoing −53% BTC drawdown — the deployed book made
**+0.75%** a year at 0.13 risk-adjusted, where the trim-enabled book made **+4.62%** at 0.48. This is
costing money now, not only in 2021.

**It keeps the crash behaviour that works.** The trim is added *on top of* the MA200 flip, not instead
of it. In a sustained bear the 200-day average still takes the book flat and keeps it flat — the
behaviour that made the deployed exit the single best rule in 2022 (−6.65% against −20.60% for a pure
scale-out). You get the scale-out on the way down and the binary flat when it becomes a bear. That
combination is the point.

### What it costs, honestly

- **+0.10 percentage points of fees a year** and half again the turnover. That is the entire
  financial cost.
- **It is a tier-2 change** (`strategies/**`), so it is yours to make in a normal session with the
  test suite, not something Claude can propose through the change queue. Note the irony: the change
  that looks cheapest to ship (§6.1) is the one not to make, and the right one needs you.
- **The trim must be classified deliberately.** A `"rebalance"` trim is a *discretionary* exit, so it
  passes through the same three churn checks that can already refuse the regime exit — and
  `crisis-policy.md` **G7** warns about exactly this: *"a partial de-risk would be throttled by its
  own order caps unless its reason carries a `risk_stop` prefix."* At the measured turnover
  (1.95×/yr, 0.29%/yr against a 1%/month budget) the caps are unlikely to bind, but I did **not**
  model them, and a de-risk that the gate can refuse on a bad day is not a de-risk. Decide that
  question at the same time as the trim.
- **It adds no parameter.** The weight it trims to is the weight the buy side already computes. There
  is no new number to pick, and therefore no new number to be wrong about.
- **It is not an edge claim.** 1.12 against a 2.32 hurdle. The case for it is that the code should do
  what its own design document says it does, should not breach its own limits on a third of all days,
  and should not take 114 days to halve exposure in a −26% fall. That case does not need a hurdle.

Two things worth doing alongside, both already known: point
`trend.check_exposure_tracks_signal` at the strategy and not just the plumbing (it would have caught
this years of backtest ago), and settle the peg/halt gap in §3 (**G4**).

---

## 6. What NOT to do, and why

Every entry here has been measured and killed. The point of writing them down is that re-testing a
dead idea does not merely waste an afternoon — **it raises the bar every honest idea after it has to
clear**. Each line cites where it died so it is not re-proposed.

1. **Do not change the exit moving average from 200 days to 40–50 days**, even though it is the
   best-scoring thing measured today (50.3% CAGR / 1.25 / −37.5% against the deployed 33.4% / 0.88 /
   −60.2%, and out-of-sample the walk-forward never once picked a value above 50 in seven blocks).
   Three reasons. It is a *return* claim, and at 1.31 against a 2.32 hurdle it does not clear. It
   costs **14.6 points in the one crash year** (2022: −21.3% against the deployed −6.7%) because a
   fast average whipsaws in a bear. And 40–50 is **outside its own bounds**: `earn.yaml` sets
   `sleeve_a.trend.ma_days` to min 100, max 300, max step 25, so reaching it needs you to widen a
   tier-2 bound first. The in-bounds best is MA100 (49.5% / 1.15 / −47.1%) and it is still a return
   claim on the exact parameter-fragility axis the ensemble exists to remove. **Stop choosing a
   lookback; the trim removes the need to choose one.**
2. **Do not replace the binary exit with a pure scale-out.** 2022: the scale-out lost 20.6% where the
   deployed MA200 lost 6.7%, because it kept residual exposure all the way down while MA200 went flat
   in January and stayed flat. Add the trim; keep the flip.
3. **Do not add a trailing stop, or a time stop, as the exit rule.** Trailing stops were **refuted**
   against their own pre-registered falsifier: at 15% / 25% / 35% the drawdowns were −61.5% / −66.3%
   / −67.6%, all *deeper* than the deployed −60.2%, at up to 9× NAV/yr of turnover. Made "sticky" it
   cuts drawdown to −25.6% and collapses return to 8.4% — dodging the crash by dodging the rally, 30
   points below simply holding, which is `crisis-policy.md` §0's flatten result again. Time stops are
   dominated on all three axes.
4. **Do not sell into a numeric trigger.** `crisis-policy.md` §0: at a −3% daily trigger, hold
   **+30.53%** CAGR, halve **+13.05%**, flatten **−5.23%**. The flatten shape is the worst policy
   measured in this system. `daily_loss_response: hold` is already correct — leave it alone.
5. **Do not build a daily or intraday sleeve, and switch `fast-test` off.** Measured edge +0.022% per
   trade against 0.300% needed (13.7× short); its own live 30 days were −1.29% net with 1.35% paid in
   fees; −26.45% over 2024-01…2026-09. Its own profile header says it must not be left running.
6. **Do not rotate between coins, or widen the universe, in search of return.** Ledger
   `cross-sectional-momentum`: Sharpe 1.04 / 0.85 / 0.55 across three routine lookbacks with −77% to
   −89% drawdowns. `growth-audit.md`: costed top-8 momentum rotation **−13.5% CAGR, −98.9%
   drawdown**, losing *before* costs, rank IC the wrong sign. Ledger `survivorship-universe`: any
   widening that picks today's liquid listings is selecting on coins that survived.
7. **Do not add satellites or dip-buying.** `dip-strategy.md` §3.4: **0 of 6,720** costed
   configurations beat holding BTC; 0 of 6,720 beat the MA125 filter already shipped; 30-day exits
   were the worst of any exit horizon.
8. **Do not reach for machine learning on the exit.** Ledger `meta-labelling`: run honestly it made
   things *worse* (Sharpe 1.16 → 0.80). Ledger `hmm-regime`: a regime model fitted with deliberate
   full-sample look-ahead still scored 0.78, below the un-fitted MA200 × vol-target at 0.87. Ledger
   `deep-learning-ohlcv`: ~3,020 effective independent observations in nine years. The binding
   constraint is sample size, not algorithm.
9. **Do not try to settle any of this by running it live for a quarter.** Ledger
   `live-testing-proves-edge`: distinguishing 1.14 from 0.83 at 80% power takes about **245 years**.
   A live run proves the plumbing works and nothing whatever about edge.
10. **Do not spend effort on execution, slicing or slippage.** Ledger `execution-timing`: measured
    slippage against mid is 0.001–0.019 bps at every size Earn will ever trade, against a 10 bps fee;
    maker equals taker at this venue's entry tier and Earn is 67× short of the next one. A false
    priority, not a false signal.
11. **Do not read the deployed book's −45.8% drawdown as "the same as the −45.6% backtest".** It is
    the same number at 0.26 average exposure instead of 0.45. At matched targets it is −14.7% against
    −45.8% — **three times the drawdown for under twice the exposure.** That coincidence must never be
    quoted as reassurance.

One thing in the *open* set is worth noting: the rejection ledger's own closing section already lists
**"what the shipped strategy's missing exit rule costs"** as an open question. Today's work answered
it. The other open questions it lists — when to take profit, how wide a stop should be given the
forecast drawdown, how many satellites are worth holding — are still open, and they are exit and
sizing questions. Not another entry signal.

---

## 7. Honest limits

**Every modelling shortcut in §2 flatters the deployed book, so the measured gap is a floor, not a
ceiling.** Entries are modelled as always filling, when the shipped order type is a limit with a
20-minute timeout and no repricing, so the real book enters less often and later. The stop fires on
the day's own low and fills *at* the stop, which no real market stop does. Every omitted limit
(blackout windows, 4 trades a day, 16 orders a day, 50% daily turnover, the drawdown protection)
constrains *buying* only. And the live sleeve runs 4h candles, so a real exit can fire up to 20 hours
earlier than mine — but the days-late figures are 8 to 65 days, so sub-day timing cannot explain any
of them.

Beyond that:

1. **Three constructions, three different return figures for "the deployed book"** — 21.4%, 26.2% and
   33.4% CAGR — because they model the sleeve's mechanics at different depths. They agree on the
   risk-adjusted verdict (0.83–0.90 against 1.10–1.16) and on the direction. **Do not quote any single
   CAGR as *the* deployed number.** The Sharpe verdict and the drawdown mechanics are what survive
   triangulation.
2. **None of it is a Freqtrade backtest.** These are signal- and sleeve-level reconstructions whose
   indicators were verified byte-identical to the shipped modules (ensemble weight, regime series,
   realised vol and target weights all matched with maximum difference 0.0). Replaying SleeveA
   through Freqtrade over the same window is the confirming test, and **nobody ran it today.** It
   should be run before the change ships.
3. **Book D's trim was measured without the exit-side churn caps** that would actually apply to it
   (16 orders/day, 50% turnover/day, 1% fee budget/month) — see G7 in §5. Unlikely to bind at 1.95×
   turnover; not modelled.
4. **The trim was not measured on 2022 in isolation.** Over the 2019-22 block it shows −20.6%
   drawdown against the deployed −45.7%, and it retains the MA200 flip, so I expect the 2022
   behaviour to be preserved. I did not measure that year on its own, and I am not asserting it.
5. **Statistical significance is at the edge, not past it.** The risk-adjusted gap is +0.329 with
   p = 0.057, and the trim's own gap +0.287 with p = 0.080, on a single nine-year path of two assets
   on one venue. Large, consistent in sign across all three regime blocks, not conclusive at 5%. The
   *deterministic* findings — 35.9% of days outside the shipped tolerance, 0.921 peak exposure, 422
   days over the BTC cap, 40 sells in nine years, 114 days to halve in 2024 — carry no p-value and
   would be unchanged by any resampling.
6. **Nothing clears the hurdle.** Best figure anywhere: 1.32. Hurdle: ~2.32 at ~8,134 trials. The
   204 trials added today moved the hurdle by 0.001 — at this N the trial count is no longer the
   binding constraint; the effect sizes are.
7. **The cost model is about to change.** `ED-2`: on **2026-10-01** the monthly calibration
   overwrites the cost model with dry-run medians, dropping slippage from 5.0 to ~2.3 bps and making
   every future backtest optimistic. That is two days away. It does not rescue the fast sleeve (the
   floor falls to ~0.246% against a measured 0.022% edge — still 11× short) but it will quietly flatter
   everything else.
8. **Scope.** This is SleeveA's shipped 4h configuration. **SleeveB** (Claude's proposals, different
   exit path, and the one sleeve that already has a trim) and **SleeveFast** were not measured for the
   exit question. Per G17 the profile actually running is `fast-test` at 1h, so these numbers describe
   what is *configured*, not necessarily what is executing this minute.
9. **One claim in the measurement set is wrong and I am correcting it here:** "there is no trim in the
   codebase". There is no trim in *SleeveA*; SleeveB has a complete one (§5). That makes the
   recommended change cheaper than it was reported to be, not harder.
10. **The 1h and 4h work carries survivorship bias** that the daily panel work does not — the feather
    store covers 108 pairs, not the 747-ticker panel, and ends 2026-09-23. It is also an
    out-of-sample test of parameters that were themselves tuned on 30 days, which is the honest
    direction but means it judges *that rule*, not intraday trend-following in general.
11. **Means on the wide universe are not investable** and are not used as such here: an apparent
    +22.8% mean one-day return on the unfiltered universe is new-listing tail noise. Every altcoin
    conclusion in §1 rests on medians and hit rates.
12. **The literature gap, not papered over.** No peer-reviewed result establishing "enter slow, exit
    fast" as a general finding could be located, and the full text of the standard stop-loss paper
    (Kaminski & Lo, 2014) was unobtainable from this host. It is cited from its abstract, with no
    figure taken from it. "Enter slow, exit fast" is practitioner folklore here; what today measured
    is that it holds for a fast *level* rule and fails for fast *ensemble members*.
13. **Nothing was written to the live runtime**, no venue was called, no bot, cron, unit or console was
    touched, no commit was made. Scripts: `evals/research/exit-horizon/{asym_books, asym_measure,
    asym_detail, asym_drift, exit_rules, ma_exit_plateau, exit_shape, horizon_1d, horizon_books,
    fast_edge}.py`, all lint-clean, nothing imports them. Pre-registrations are archived under
    `evals/research/exit-horizon/preregistrations/`.

---

## Sources

Measured today: the three measurement sets behind this document, scripted in
`evals/research/exit-horizon/`. Cited, not re-derived: `docs/design/trend-ensemble.md` (the build and
its limit 3, which already declared this asymmetry without pricing it), `dip-strategy.md` §0.3, §3.4,
§6.2, `growth-audit.md` §1.5 and its rejection rows, `crisis-policy.md` §0 and the G1–G17 ledger,
`analogue-timing.md` §4.4-4.5, `ml-forecast.md`, `audit-and-research-2026-09-29.md` (the Sharpe
estimator correction and ED-2), `wide-universe.md`,
`paper-trading-review-2026-09-29.md`, and `.claude/skills/research-scout/references/rejected-ledger.md`.
Code read and quoted: `strategies/earn_base.py`, `SleeveA.py`, `SleeveB.py`, `sleeve_common.py`,
`mechanics.py`, `riskgate.py`, `runs/features/trend.py`, `config/earn.yaml`,
`config/params-sleeve-a.json`.
