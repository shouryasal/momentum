# Two decisions and a patch plan — KILL and satellite sizing

Status: written 2026-09-30. **Nothing here is switched on by this document.** No file under
`strategies/`, `config/`, `ops/` or `tests/` was edited to produce it; no bot, cron job,
systemd unit or console was touched; no commit was made; no venue was called; nothing was
written into `~/earn-run`; the config digest was not re-blessed. Every claim below is cited to
a file and a line in the Windows working copy, which is the authority. Where I could not
verify something I say "not verified" rather than inferring it from a document.

Two questions were delegated: may SleeveA sell while the kill switch is engaged, and should
the satellite sleeve go to one seat. The answers are **no — only mechanical reductions may
sell under KILL**, and **do not change the seat count yet**. The second answer is a refusal to
spend a risk-limit change on a number that was measured with two of the gate's 27 checks
switched off. Both are argued below, then turned into an ordered patch plan.

---

## 0. What must NOT be done with this document

This is a risk-limit document, and the hazard is not that the analysis is wrong. The hazard is
that the analysis is convincing and someone deploys it. Every item below is a prohibition, not
a caution.

* **Do not deploy anything to `~/earn-run`.** The runtime tree is live and it is not the same
  tree as this one. `config/earn.yaml:610-612` records that the runtime deployment selects the
  `fast-test` profile where the committed tree selects none (`config/earn.yaml:613`
  `active: null`), so the runtime is running a different strategy from the one analysed here.
* **Do not re-bless `var/state/config.bless.json`.** `config/earn.yaml` and
  `config/riskgate.json` are both protected files; a re-bless is what makes a changed risk
  limit acceptable to preflight, and it is the single step that converts a reviewed patch into
  an armed one. Nothing in this plan is ready for it.
* **Do not arm live or demo trading, and do not change mode.** Both sleeves stay TEST. Mode is
  not config — it lives in the HMAC-signed `var/state/mode.json`, and an unverifiable file
  means every sleeve is TEST. Leave it that way.
* **Do not touch a bot, a cron line, a systemd unit or the console.** Not to restart, not to
  reload, not to "pick up the new config".
* **Do not regenerate `config/riskgate.json` until the config change it renders is actually
  being applied.** `python -m ops.gen_freqtrade_config` is the correct command at the right
  moment and a source of silent drift at any other.
* **Do not apply the patches in a different order than section 4 gives.** Two of them are
  ordered for safety rather than convenience: the fee-budget exemption (step 3) is load-bearing
  for the KILL decision (step 4), and the load-time validator's second layer (step 9) refuses
  the currently-committed config and must not land before the config it refuses has changed.
* **Do not apply the seat change at all in this pass.** Section 2 is a decision not to, with a
  named condition for revisiting. If the condition is met later, the sequencing precondition in
  §2.5 is not optional — applying `2 -> 1` while two satellites are held blocks every entry in
  the sleeve, BTC and ETH included.

---

## 1. Decision one — under KILL, only a mechanical reduction may sell

**Decision: `allow_only_mechanical`.**

The rule, stated so it fits in one sentence and can be held at 3am:

> While the kill switch is engaged, a sleeve may still reduce a position when both the trigger
> and the size come from what the bot observes itself — price, NAV, its own candles — or from a
> direct human instruction. It may not originate an order whose size comes from a file another
> process wrote.

### 1.1 What is true in the code today

KILL's reach is exactly co-extensive with one method. `self._kill` appears in `strategies/` at
three lines and no more: the provider at `strategies/riskgate.py:918`, the check at
`strategies/riskgate.py:970` (`checks["kill"] = not self._kill()`, inside `check_entry`), and
`strategies/earn_base.py:1081` (`check_entry_timeout`, cancelling resting *entry* orders).
Nothing else in the strategy layer asks.

So "may I sell under KILL?" is answered not by a policy but by which of the gate's three entry
points a path happens to call:

| path | gate method | KILL consulted? |
|---|---|---|
| new entry, DCA/pyramid add | `check_entry` (`riskgate.py:964`) | yes, `riskgate.py:970` |
| stop, flatten, `target_zero` | `check_exit` (`riskgate.py:1090-1092`) | n/a — returns allowed unconditionally |
| TP rung, SleeveA trim, SleeveB trim | `check_discretionary_exit` (`riskgate.py:1094-1117`) | **no** |

`check_discretionary_exit` runs exactly `EXIT_CHECK_ORDER = ("orders_per_day", "turnover_day",
"fee_budget")` (`riskgate.py:91`) and nothing else, and it returns before even those on the
first line for a risk exit (`riskgate.py:1101-1102`). `_mechanics_plan`'s only guard is
`if self.gate.flatten_pending(now): return None` at `strategies/earn_base.py:1126-1127`, and
`flatten_pending` never reads the kill file — it matches `risk_stop_monthly` and
`risk_stop_daily` only. The asymmetry at `earn_base.py:1081`, which writes
`self.gate._kill() or self.gate.flatten_pending(...)`, proves the author knew these are two
different conditions.

The proof that this is routing rather than policy sits inside a single function.
`_mechanics_plan` (`earn_base.py:1122-1145`) offers three candidates on the same candle:
`take_profit` and `rebalance` reach `check_discretionary_exit` and are invisible to KILL, while
`add` reaches `check_entry` and is blocked. Same function, same candle, opposite answers, for
no stated reason.

### 1.2 Why the permissive reading has no authority left

The only comment in `strategies/` that authorises a sell under KILL is
`riskgate.py:1091`: *"Exits reduce risk: always allowed (including under KILL — flattening must
work)"*. `docs/design/trend-trim-2026-09-30.md:411-414` cites exactly that method as its
authority for item 9, *"Under KILL the trim still trims."* But `check_exit` is not on any
production path — the trim calls `check_discretionary_exit` (`SleeveA.py:321`), as does the
ladder and as does SleeveB (`SleeveB.py:371`). A rule whose only written statement is attached
to a method the shipped code does not call is not the system's rule.

The same document's companion claim — *"This matches the ladder and SleeveB"* — is half false.
The ladder is `[]` in `config/earn.yaml:264` and in both committed renders
(`config/riskgate.json:213`, `:297`), so it sells nothing; the only configuration with a
non-empty ladder also replaces both sleeves with `SleeveFast`. SleeveB is a real precedent, but
an undocumented and untested one. What the three paths share is a silence, not a decision.

Meanwhile every prose statement of KILL's semantics in the repo is entry-side only:
`.claude/skills/ops-runbook/SKILL.md:52` (*"entries refuse immediately in the gate"*), and its
runaway-trading remedy at `:55` is *"engage, **then** force-exit both sleeves"* — i.e. KILL
alone is expected to sell nothing. `ops/lib/kill.py:80-82` says *"Exits are deliberately left
alone"* inside `cancel_open_entry_orders`, which means "do not cancel a resting exit", not "new
sells are authorised". The Invariants page carries one KILL row, `kill_never_locks`
(`console/services/invariants_service.py:348-354`), and it is silent on what KILL stops.

Three documents say entry-side. The code says otherwise. A test pins the code's answer:
`test_the_trim_still_fires_under_kill` (`tests/strategies/test_trend_trim_limits.py:49-54`),
whose file header at `:9-10` states the asymmetry deliberately. That test is why this gap was
promoted from an omission to a contract, and it is the thing to remove.

### 1.3 Why `allow_only_mechanical` and not the other two

All four lenses landed in the same place, so counting them proves nothing. The argument that
dominates is the consistency lens's, for one reason the other three do not supply: **it is the
only formulation that survives the strongest objection raised against each of the others.**

*The operations objection* — a two-class rule is an open-set classification over reason
strings, and that mechanism is exactly what produced the `exit_signal` defect in §3, where a
sell landed in the refusable class because its name happened to match no prefix
(`mechanics.py:47-61`). This objection is fatal to any fix that classifies `exit_reason`, and it
is *not* an objection to a provenance rule, because provenance is not a property of a string.
It is a property of the call site, and there are exactly three call sites in the repo that size
an order from a file another process wrote:

1. `EarnBaseStrategy._trend_weight_for_trim` (`earn_base.py:614-629`) → `_trend_weight`
   (`:602-603`) → `ts.weight_for(self._trend_state(), ...)`, reading `knowledge/state/trend.json`.
   It has exactly one caller, `SleeveA.py:289`.
2. `SleeveB._sleeve_adjust` (`SleeveB.py:324-389`), whose `gap = target_w * ps.nav -
   ps.committed(pair)` is computed from targets adopted from the signed proposal file.
3. `SleeveB._custom_exit_extra` (`SleeveB.py:432-437`), returning `target_zero` off the same
   proposal — the most permissive sell in the system today, because `target_zero` is in
   `RISK_EXIT_REASONS` (`mechanics.py:48`) so `check_discretionary_exit` waves it through with
   zero checks (`riskgate.py:1101-1102`).

A guard placed at those three enumerated seats cannot be defeated by someone adding a new exit
reason, because it never looks at reasons. That is auditable by enumeration, which is what the
operations lens said it needed.

*The safety objection* — suppressing the trim leaves a book above its gross ceiling with no
automated path back inside, because `reduce_pending` has no caller in `strategies/` and
`daily_loss_response: hold` (`config/earn.yaml:189`) sells nothing. This is the real cost, and
the provenance rule shrinks it further than the safety lens allowed for, because the MA200 flip
keeps running. The flip is `populate_exit_trend` setting `exit_long = 1` where `regime_1d == 0`
(`SleeveA.py:95`), and `regime_1d` is computed in-container from the bot's own dataframe
(`SleeveA.py:53-55`, `earn_base.py:530`) — self-observed, so it stays. So does the 15% per-trade
stop (`earn.yaml:204`), the trailing/ATR stop, `risk_stop_daily`/`risk_stop_monthly` via
`flatten_pending` (`riskgate.py:1242-1262`), and the operator's `forceexit`
(`ops/lib/kill.py:122-126`). The residual hole is therefore narrower and can be stated exactly:

> A position drifts above its weight or gross cap while its own 200-day regime is still up,
> during a KILL window.

That is the `TRIM_BREACH` branch (`mechanics.py:82`) and it is the only genuine loss. It is
handed to the human, which is what the runbook already tells them to do
(`SKILL.md:55`: engage, then force-exit) — provided the suppression is loud, which is why step 5
of the patch plan is not optional.

*The money objection* — the expected value is near zero and the fix spends a tier-2 change in
the order path. True, and it is the reason this decision is scoped to four small guards and a
public accessor rather than to the cap-clamped rewrite discussed in §1.5.

`block_sell_under_kill` is rejected because the blunt guard at `earn_base.py:1126` reaches
`_mechanics_plan`'s three candidates but not the stop, not `custom_exit`'s flattens, not
`exit_signal` and not `forceexit` — so it implements "no adjust-position sells under KILL",
which is a fourth rule, not a simpler one. It would also block the TP ladder, which is
price-sized and is the leg with the 10-for-10 live record. `allow_sell_under_kill` is rejected
because its only affirmative authority turned out to be dead code and an empty ladder.

### 1.4 The implementation refinement that decides whether this is done correctly

**The guard does not go in `_mechanics_plan`.** `docs/design/trend-trim-2026-09-30.md`'s own
follow-up shape and the obvious one-line fix both put it at `earn_base.py:1126`, beside the
flatten guard. That is wrong here: `_mechanics_plan` offers `take_profit` at
`earn_base.py:1133-1134`, and the TP ladder is sized from `current_profit` and
`position_value` — price, not a file. Guarding the dispatcher would suspend it along with the
trim, which is precisely backwards against the live record.

**And the line is not "trend versus not-trend."** Anyone implementing "block trend-driven sells
under KILL" naively will block the MA200 flip, because the flip *is* a trend signal. The line
is provenance: self-computed from the bot's own candles (flip — stays) versus delivered in a
file by another process (trim — goes). `strategies/trend_state.py` says of that very file that
it *"is an INPUT to sizing, never an authorisation"*, and letting the trim fire under KILL asks
it to authorise an order at the moment a human withdrew authorisation.

`_trend_weight_for_trim` is the right seat for SleeveA for four reasons: it exists to answer
"may a sell size against this weight?"; its docstring at `earn_base.py:615-623` already fails
closed on four plumbing reasons for exactly this hazard class; it has one caller; and it returns
*before* `mx.trim_reason` at `SleeveA.py:313`, so it closes the `TRIM_BREACH` branch too. A guard
in `check_discretionary_exit` would not — `riskgate.py:1101-1102` returns early on the
`risk_stop` prefix, so a `risk_stop_exposure` trim would slip straight past it.

### 1.5 What is deliberately not being built yet

The strictly more coherent design is to let `TRIM_BREACH` fire under KILL but sized to the
**cap** rather than to the signal — clamp the excess to `weight_cap * nav` instead of
`tw.weight * target_w * ps.nav` (`SleeveA.py:296`). That needs no `trend.json` at all, so it
satisfies the provenance rule while keeping the de-risk. It is not in this plan because it is
new arithmetic nothing has measured, and because no test anywhere covers the trim's re-fire
behaviour at all (§5.1). It is the right follow-up, and it should be measured before it ships.

---

## 2. Decision two — do not change the seat count in this pass

**Decision: leave `risk.max_satellite_positions` at 2 and `risk.max_satellite_gross` at 0.05.
Change nothing in `config/earn.yaml` now.**

This is not a rejection of the arithmetic. The defect is real and reproduces: two seats at 5%
gross ask 2.5% per seat, the volatility scalar takes that to a median 1.63%, and the 2.00%
floor deletes the position on 292 of 430 asset-days
(`evals/research/improve/missed_rally/out/diagnostics.json`, `min_position_squeeze`:
`raw_target_before_vol_scaling 0.025`, `vol_scaled_target_median 0.0163`,
`asset_days_zeroed_by_min_position 292`, `pct_zeroed 67.9`). The refusal is a refusal to spend a
tier-2 risk-limit change on the *number* that argues for it.

### 2.1 The number was measured with two of the 27 gate checks switched off

`corr` and `beta` appear nowhere in
`evals/research/improve/missed_rally/missed_rally.py` — a grep over the file returns zero hits
for either, while the file's own legend at `:1120` reads *"C13 The GATE refused it, and WHICH of
the 27 checks."* So the study labelled gate refusals but did not model the correlation or beta
caps. Every counterfactual in `out/sizing.json` inherits that:

| variant | return | Sharpe | max drawdown | asset-days held | asset-days zeroed |
|---|---|---|---|---|---|
| `B0_shipped_2seats_5pct` | +12.47% | 0.515 | −11.88% | 138 | 292 |
| `B7_ONE_seat_5pct` | +18.26% | 0.682 | −12.26% | 245 | 13 |
| `B8_2seats_10pct` | +17.37% | 0.660 | −11.61% | 394 | 36 |
| `B9_2seats_5pct_floor_removed` | +16.23% | 0.618 | −11.88% | 430 | 0 |

Two corrections to how this has been summarised. B7's drawdown is **0.38 points worse** than
shipped (−12.26% against −11.88%), not the same; the only variant that improves drawdown is B8.
And all four lose to holding BTC over the window — the +32.98% at 0.542 figure carried into this
decision is from the parent brief and I did not verify it against a file here, but the
direction is not in doubt and the conclusion does not depend on the digit: **this is a
self-inflicted gap, not an edge.**

### 2.2 Why that matters more than usual — the correlation cap is wired, and it is measured over a book that includes BTC-ETH

This is the finding that changes the answer, and it is entirely code-side.

`returns_provider` is **not** stubbed in production. `strategies/earn_base.py:235` constructs
the live gate as `RiskGate(cfg, store, returns_provider=self._daily_returns)`, and the backtest
branch at `:223-229` passes it too. So `riskgate.py:922`'s `lambda pair: None` fallback — the
thing that makes `corr_cap` and `beta_cap` vacuous — applies only where a caller omits the
provider. In the shipped strategy it is supplied.

`corr_cap` has no tier filter and no core carve-out. `check_entry` builds
`series = self._series_for(book)` at `riskgate.py:1031` over the book the entry *would create*
(`_book_after` = held positions plus this pair, `riskgate.py:931-934`); `_series_for`
(`riskgate.py:936-947`) iterates every pair in that book with no exclusion; and
`avg_pairwise_corr` (`riskgate.py:507-520`) is an **unweighted** mean over every unique pair.
So a book holding BTC and ETH measures the BTC-ETH correlation directly, and
`checks["corr_cap"]` at `riskgate.py:1035` tests it against `max_avg_pairwise_corr`, which is
0.70 (`config/earn.yaml:226`).

The consequence, stated as arithmetic rather than as a prediction: if the 60-day correlation of
BTC and ETH daily returns exceeds 0.70, then once BTC is held, an ETH entry creates a two-name
book whose only pair is BTC-ETH, and `corr_cap` refuses it. The sleeve would hold whichever core
name it bought first and could never add the second. The measurement carried into this decision
puts that correlation at a median of 0.865 over two years and above 0.70 on 93.0% of days; I
could not reproduce it here (no Python interpreter on this host) and I mark it unverified. But
the *code* claim needs no measurement, and the config's own justification quietly concedes the
population mismatch: `earn.yaml:226` defends 0.70 with *"calm p90 0.591"*, which is an alt-alt
number applied to a check that averages in the core pair.

No test could have caught this. `benign_gate` (`tests/strategies/conftest.py:104-111`) does not
set `returns_provider`, so `corr_cap` and `beta_cap` pass vacuously in every gate test except
the six at `tests/strategies/test_gate_wide_universe.py:259-315`, and those use synthetic
series: `_series(scale=...)` at `:241-244` returns `scale * _bench()`, a pure multiple, so every
such series is perfectly correlated regardless of scale, and `_uncorrelated()` at `:255-256`
alternates ±0.01. The nearest test to the real case,
`test_the_incremental_entry_is_what_is_refused` (`:302-310`), asserts a two-name book *passes* —
but the pair it uses is an uncorrelated one. No test anywhere puts two realistically correlated
names in one book.

So the position is: an upstream check that plausibly refuses the second *core* entry is
unresolved, untested against realistic data, and absent from the study whose output is the case
for the seat change. Tightening a different, book-wide limit underneath that is the wrong order
of work.

### 2.3 And one seat at 5% makes the correlation cap worse, not better

`avg_pairwise_corr` is unweighted over unique pairs. Going from `{BTC, ETH, a1, a2}` (six pairs)
to `{BTC, ETH, a1}` (three pairs) deletes the `a1-a2` pair — the lowest-correlation pair in the
book — while BTC-ETH stays. Concentrating to one seat therefore raises the average the cap is
tested against. The direction is structural and needs no measurement; the magnitude carried into
this decision is about 9 percentage points more refusals, which I did not reproduce.

### 2.4 Two more reasons one seat at 5% is not the clean one-liner it looks like

**Zero headroom on two limits at once.** `risk.tier_caps.satellite` is 0.05
(`config/earn.yaml:176`) and `max_satellite_gross` is 0.05 (`:224`). One seat asking the full 5%
sits exactly on both. `weight_cap` and `satellite_gross` pass at equality only on `_EPS = 1e-9`
(`riskgate.py:1023`, `:1025`, `:61`), and `cap_stake`'s satellite headroom
(`riskgate.py:1082-1084`) is then exactly zero — so every DCA add or pyramid into that satellite
is trimmed to 0, falls under `notional_floor` (`riskgate.py:1086`) and journals as `size_zero`.
At two seats that is true in aggregate; at one seat it arrives on day one.

**5% does not reliably clear the floor.** The breakpoint is `0.05 * scale >= 0.02`, i.e.
`scale >= 0.400`, i.e. book volatility at or below 0.750 annualised. `scale = min(target/
book_vol, 1.0)` (`strategies/sleeve_common.py:320`) has no lower clamp, and the study's own
counterfactual still deletes 13 asset-days at one seat (`sizing.json B7_ONE_seat_5pct:
zeroed_asset_days 13` against `asset_days_held 245`). **What to do about it: nothing, and say
so.** Do not chase it to zero by removing `min_position_pct_nav` (variant B9). That floor is
load-bearing and the code explains why at `riskgate.py:1008-1013`: below it a position can be
opened and closed but never *managed*, because at the $1,000 live seed a 2% position is $20 and a
half-trim of it is $10, under the $25 `min_notional` (`config/earn.yaml:203`). The residual
deletions cluster on the highest-book-volatility days, which is when a satellite is most likely
to be a mistake. The correct response is to make the deletion **visible** — step 8 of the patch
plan — and review the count quarterly, not to remove the floor that makes positions manageable.

### 2.5 The condition for revisiting, and the precondition for applying

Revisit when, and only when, `corr_cap` has been given an intent and the sizing study has been
re-run with `corr_cap` and `beta_cap` **on**. If one seat at 5% still pays after that, it is the
right variant — it keeps the satellite risk budget at 5% of NAV where B8 doubles it to 10%,
which would reverse the deliberate decision recorded at `config/earn.yaml:216-222` (*"Satellites
TO THE FLOOR… Was 4 / 0.10"*) on the strength of a sleeve that loses to holding BTC. For a
sleeve whose stated purpose is to keep the machinery *observable* (`earn.yaml:220-221`), the
right fix is the one that opens positions at the smallest risk budget, not the one that
maximises return.

When it is applied, the sequencing precondition is mandatory. `checks["satellite_count"]` at
`riskgate.py:1018-1021` counts satellites in `_book_after` — the whole book plus the incoming
pair — not the incremental order. With the limit at 1 and two non-dust satellites held, **every
entry in the sleeve is refused, BTC and ETH included**, with reason `satellite_count`. It does
not clear itself: `_trim_plan` returns `None` when the target is zero (`SleeveA.py:284-288`),
SleeveA does not override `_custom_exit_extra`, and `SleeveA.py:95` exits only on the name's own
200-day regime flip. A demoted satellite is simply held. So: apply only when at most one
non-dust satellite is held, or flatten satellites first.

---

## 3. The companion the KILL decision depends on — a fee budget can veto the MA200 flip

This is not a third topic. §1.3 rests the safety case for `allow_only_mechanical` on the MA200
flip continuing to work under KILL. It currently can be refused, so the KILL guard must not
ship without this.

`confirm_trade_exit` (`earn_base.py:922-947`) routes **every** exit through the churn gate:
`d = self.gate.check_discretionary_exit(pair, amount * rate, ps, exit_reason)` at `:929`, and
`return d.allowed` at `:947`. A `False` makes freqtrade abandon the exit. `is_risk_exit`
(`mechanics.py:56-61`) matches `RISK_EXIT_REASONS` (`:47-51`) or the prefixes
`("risk_stop", "stop_loss", "trailing_stop", "emergency")` (`:53`). `exit_signal` is in neither,
so SleeveA's MA200 flip — its one signal-driven sell, a 100% close — is refusable by
`orders_per_day`, `turnover_day` or `fee_budget` (`riskgate.py:1105-1111`).

Three things make the incentive backwards at once. `abs(stake)` sits in the turnover numerator
(`riskgate.py:1107-1108`), so the bigger the de-risk the likelier the refusal. `ACTION_PRIORITY`
(`mechanics.py:33-40`) ranks `add` above `rebalance`, so risk-*increasing* orders consume the
budget the de-risk later needs. And `mechanics.trim_reason` classifies off the book state before
the trim and says so outright at `mechanics.py:99` — *"A trim is never classified by its
size"* — so a full liquidation inside its cap is `TRIM_DRIFT` and refusable.

The refusal is also silent. `earn_base.py:941` hardcodes
`severity="allow" if d.allowed else "reject"`; it never consults `_BREACH_CHECKS`, and
`turnover_day`, `fee_budget` and `orders_per_day` are absent from that set anyway
(`earn_base.py:63-64`). `ops/healthcheck.py:1060` and `ops/preflight.py:840` both filter
`severity='breach'`. So a vetoed full-book de-risk leaves one unread row and raises nothing.

`fee_budget` is keyed to the Gulf month, so once tripped it refuses the close on every candle
until the month turns. The remaining protection is the 15% per-trade stop — the book rides to
−15% per position instead of exiting on signal, in exactly the month that was expensive enough
to exhaust the budget.

**The one check to run before acting**, because I could not: `exit_signal` is corroborated
internally (`console/web/src/pages/portfolio/why.ts`, `config/profiles/fast-test.yaml`, the test
literals) but the freqtrade constant itself is unread — there is no Python interpreter on this
host. Run `python -c "from freqtrade.enums import ExitType; print(ExitType.EXIT_SIGNAL.value)"`
in `~/earn-dev/.venv`. If it emits something matching a `RISK_EXIT_PREFIXES` entry the flip is
already unrefusable and step 3 shrinks to the trim only — still real, since the trim at ensemble
weight zero is a full-position sell in the refusable branch, but smaller.

---

## 4. The ordered patch plan

All steps are tier 2 (`strategies/**`, `config/**`, `ops/**`, `tests/**`) and therefore
human-only. Apply in this order. Steps 1, 2, 5, 6, 7 and 8 are safe independently; steps 3 and 4
are ordered with respect to each other; steps 9 and 11 are held.

| # | file | change | why here |
|---|---|---|---|
| 1 | `strategies/riskgate.py` | fail-closed fallbacks | free, no dependencies |
| 2 | `strategies/SleeveA.py` | idempotent refusal row | free, removes a write flood |
| 3 | `strategies/riskgate.py`, `config/earn.yaml`, `ops/config.py` | de-risk exemption by size | prerequisite for step 4 |
| 4 | `strategies/earn_base.py`, `strategies/SleeveB.py`, `strategies/riskgate.py` | the KILL guard | the decision |
| 5 | `strategies/earn_base.py` | grade a refused reduction `breach` | makes 3 and 4 observable |
| 6 | `tests/strategies/*` | the KILL tests, including the inversion | pins step 4 |
| 7 | docs, runbook, Invariants | say what KILL does | the disagreement is the defect |
| 8 | `strategies/sleeve_common.py`, `ops/autonomy.py` | the silent-zero detector | the sizing blind spot |
| 9 | `ops/config.py` | load-time sizing validator, layer 1 | catches the next contradiction |
| 10 | `tests/contract/test_freqtrade_contract.py` | pin the duplicate-order answer | unblocks 11 |
| 11 | — | held: trim cadence, resting-sell netting, layer 2, seat change | needs 10 or §2.5 |

### Step 1 — make the satellite fallbacks fail closed

`strategies/riskgate.py:243-244` (dataclass defaults) and `:378-379`
(`risk.get("max_satellite_positions", 4)` / `risk.get("max_satellite_gross", 0.10)`) still carry
the pre-2026-09-29 wide-universe numbers. A `riskgate.json` missing those keys silently restores
**4 seats at 10% gross** — a fallback that widens a risk limit. Change both to the tightest
value, not the historical one: `0` seats and `0.0` gross, so a missing key disables satellites
rather than quadrupling them. Note the file is stdlib-only and in-container
(`riskgate.py:56-58`).

*Tests:* `tests/strategies/test_gate_wide_universe.py` — add
`test_a_missing_satellite_key_disables_satellites_rather_than_widening_them`, constructing a
`GateConfig` from a `risk` block with both keys absent and asserting a satellite entry is
refused on `satellite_count`. Check first whether any existing test constructs `GateConfig()`
directly and relies on the old defaults; if so, those tests must pass the keys explicitly.

### Step 2 — make the trim's refusal row idempotent

`SleeveA._trim_plan` calls `self._journal_adjust(..., action="reject", ...)` at
`strategies/SleeveA.py:327-328` as a direct statement — not through `plan.then`, so it is gated
neither by winning `actions.choose()` (`earn_base.py:1140`) nor by `plan.commit()` (`:1144`). A
standing refused trim therefore writes one `gate_decisions` row **every loop**. At
`process_throttle_secs: 5` (`config/freqtrade-a.json:73`) that is 17,280 rows per pair per day,
graded `severity="breach"` whenever the failing check is in `_BREACH_CHECKS`
(`earn_base.py:765`). SleeveB structurally cannot do this: its cadence check returns at
`SleeveB.py:354-358`, before its reject path at `:371-375`.

Add `self._trim_reject_last: dict[str, str] = {}` beside `_trend_gate_last`
(`earn_base.py:248`) and apply the dedup shape already proven in `_journal_trend_gate`
(`earn_base.py:671-674`): key it `f"{decision.reason}:{reason}"` per pair and write the row only
on a change of that key. Clear the entry on every non-refusal return from `_trim_plan` (the
early returns at `SleeveA.py:284`, `:290`, `:294`, `:297` and the success path at `:340`), so a
refusal that recurs after the condition cleared is journalled again. Behaviour-neutral on
trading; it only stops the flood.

*Tests:* `tests/strategies/test_trend_trim.py` —
`test_a_standing_refused_trim_journals_one_row_not_one_per_candle` (call
`adjust_trade_position` three times with the fee budget exhausted; assert one
`record_gate_decision` call) and
`test_a_refusal_that_recurs_after_the_condition_cleared_is_journalled_again`.

### Step 3 — exempt a de-risk by size and direction, not by reason-name

In `check_discretionary_exit` (`strategies/riskgate.py:1101`), widen the exemption:

```python
if is_risk_exit(exit_reason) or abs(stake) >= cfg.derisk_exempt_pct * max(ps.nav, _EPS):
    return GateDecision(True, "ok", {"risk_exit": True})
```

Every call site is a reduction — `earn_base.py:929`, the ladder at `earn_base.py:1214`,
`SleeveA.py:321`, `SleeveB.py:371` — and all four pass a positive magnitude, so `abs(stake)` is
correct and no plumbing is needed. Add `risk.derisk_exempt_pct` to `config/earn.yaml` with the
`ops.config.F(...)` helper carrying `description`, `x-tier` and `x-group`, or
`tests/test_foundation/test_config_schema.py` fails; read it in `GateConfig` beside
`max_turnover_pct_per_day` (`riskgate.py:237`, `:383`).

**0.10 of NAV** separates the populations cleanly: a full core close is 0.30-0.40 of NAV at the
core tier cap (`earn.yaml:174`) and always clears, while a band trim is bounded by
`rebalance_band: 0.05` (`config/riskgate.json:39`) and a TP rung is smaller still, so both stay
fully budgeted. Set it below ~0.05 and ordinary trims start bypassing the budget, which makes the
budget meaningless. This subsumes `docs/design/crisis-policy.md` item 7 without special-casing a
string, so the next new sell reason inherits the protection instead of silently missing it —
which is the bug that produced this.

*Tests:* `tests/test_riskgate/test_min_edge.py:99` and
`tests/test_riskgate/test_crisis_block_entries.py:82-83` currently assert
`check_exit(..., "exit_signal").allowed`, which is vacuous — `check_exit`
(`riskgate.py:1090-1092`) returns allowed unconditionally and has **zero production callers**.
Repoint both at `check_discretionary_exit` with a store already at the turnover and fee limits.
Add `test_a_full_core_close_is_never_refused_by_the_fee_budget` and
`test_an_ordinary_band_trim_is_still_budgeted`. Then delete `check_exit` and correct the two
documents that cite it as the guarantee (`ops/config.py:986`,
`evals/research/profit-audit/vs-traders.md:62`) — I did not read those two lines myself and they
are from the source reads.

### Step 4 — the KILL guard, at three seats plus a public accessor

**4a.** Add `RiskGate.kill_engaged()` as a public wrapper for `self._kill`
(`strategies/riskgate.py:918`). `earn_base.py:1081` already reaches for the private name; a
second private reach is worse than one accessor.

**4b.** `EarnBaseStrategy._trend_weight_for_trim` (`strategies/earn_base.py:614-629`): consult
it *before* the `_SELLABLE_TREND_REASONS` test at `:625`, journal through the existing
`_journal_trend_gate` with a `kill_engaged` reason, and `return None`. Add `kill_engaged` to the
comment block at `:605-612` as the fifth member of the fail-closed set — an engaged KILL is the
same hazard class as a dead writer, with a human attached. Because this returns before
`mx.trim_reason` (`SleeveA.py:313`), it closes the `TRIM_BREACH` branch as well. The dedup key
is `side:pair` / `reason:weight` (`earn_base.py:671`), so `kill_engaged` dedups cleanly and will
not write a row per candle.

**4c.** `SleeveB._sleeve_adjust` (`strategies/SleeveB.py:324`): consult before the
`last_rebalance_{pair}` read at `:347` and return `None`. The add branch at `:364-369` is already
refused downstream by `_gated_add` → `check_entry` → `checks["kill"]`, so an early return is
behaviour-neutral for buys and correct for sells.

**4d.** `SleeveB._custom_exit_extra` (`strategies/SleeveB.py:432-437`): return `None` instead of
`"target_zero"` under KILL. This one sits in `custom_exit` (`earn_base.py:1072-1077`), not
`_mechanics_plan`, so it needs its own consult — and it is the most permissive sell in the
system, because `target_zero` is waved through with zero checks.

**Do not** add the guard at `earn_base.py:1126`. That would suspend the TP ladder, which is
price-sized (§1.4).

### Step 5 — grade a refused reduction as a breach

`strategies/earn_base.py:941` hardcodes `severity="allow" if d.allowed else "reject"`.
`confirm_trade_exit` is always a sell, so change the else branch to `"breach"`:
`ops/healthcheck.py:1060` already turns `breach` into a critical alert plus an incident with no
further work. For the trim path, grade off `is_entry` at `earn_base.py:765` rather than adding
`turnover_day`/`fee_budget`/`orders_per_day` to `_BREACH_CHECKS` wholesale
(`earn_base.py:63-64`), or a refused *add* starts paging too, which is not wanted.

This step alone is **not** a fix for step 3 — it only makes the loss visible. Ship both.

*Tests:* `tests/strategies/` — `test_a_refused_exit_is_graded_breach_and_a_refused_add_is_not`.

### Step 6 — the tests that do not exist

* `tests/strategies/test_trend_trim_limits.py:49-54` — replace
  `test_the_trim_still_fires_under_kill` with `test_the_trim_is_suspended_under_kill`, and
  rewrite the file header bullet at `:9-10` which currently documents the old behaviour as
  deliberate. Keep `test_an_entry_is_still_refused_under_kill` (`:56-61`) and rewrite its
  docstring, which currently reads *"buys stop, this sell does not"*.
* Add `test_the_breach_branch_is_also_suspended_under_kill` — reuse the fixture from
  `test_one_more_point_of_drift_and_the_breach_branch_does_fire` (`:111-116`, position
  `0.46 * NAV`, which does fire today) and assert `None` under KILL. This is the case a guard
  placed in `check_discretionary_exit` would have missed.
* Add `test_the_ma200_flip_still_exits_under_kill` and
  `test_the_stop_still_fires_under_kill` — the point of the rule is what keeps working.
* Add `test_the_tp_ladder_still_fires_under_kill` (needs a non-empty ladder fixture; the shipped
  renders are `[]` at `config/riskgate.json:213`, `:297`).
* `tests/strategies/test_sleeve_b_mandate.py` — add
  `test_a_proposal_trim_is_suspended_under_kill` and
  `test_target_zero_is_suspended_under_kill`.
* `tests/strategies/test_gate_guards.py:121` — rename
  `test_kill_blocks_entries_allows_exits` to `test_kill_blocks_entries_allows_risk_exits`. It
  asserts `check_exit(..., "risk_stop_daily")` (`:125`) through a method with no production
  callers, so its name claims a guarantee it does not test. Add
  `test_kill_and_a_discretionary_exit` covering `check_discretionary_exit` with
  `rebalance_trim` and with `risk_stop_exposure` under `kill_provider=lambda: True`.
  `tests/strategies/test_trend_trim.py` has zero occurrences of "kill" across its 77 tests.

### Step 7 — make the documents agree with the code

* `docs/design/trend-trim-2026-09-30.md:411-414` — item 9 is now false. Correct it and note
  that `check_exit`, its cited authority, has no production callers.
* `.claude/skills/ops-runbook/SKILL.md:52` — state what KILL stops on the sell side. While
  there, `kill` is check 2 of **27**, not 17: `CHECK_ORDER` (`riskgate.py:63-69`) has 27
  entries, and `missed_rally.py:1120` agrees. The same "17 checks" figure is in `CLAUDE.md`.
* `console/services/invariants_service.py` — add a row beside `kill_never_locks` (`:348-354`):
  *"KILL stops every new order and every file-sized reduction; a price- or NAV-sized reduction
  and the operator's force-exit still sell."* Its `enforced_by` should name the four seats from
  step 4. Be honest in review about the check's strength: `_check_kill_present` (`:257-262`) uses
  `_source_contains`, a token grep, so the row proves the consult is present, not that it is
  correct.
* `config/earn.yaml:306` — the comment reads *"tier-2 bounds for tier-1 params; apply_changes
  enforces"*. It does not: `runs/apply_changes.py` contains zero occurrences of `bounds` or
  `clamp`, and the only enforcement is `mx.clamp_params` at `strategies/earn_base.py:464`, which
  clamps at read time and journals a `params_out_of_bounds` breach (`:465-476`). Fix the comment
  to name the real seat.
* `strategies/riskgate.py:1062-1064` — `cap_stake`'s docstring still says *"what the 10% sleeve
  can still hold"*; the sleeve has been 5% since 2026-09-29 (`config/earn.yaml:224`).

### Step 8 — the silent-zero detector

A satellite target zeroed by the floor never reaches the gate, so it leaves no
`gate_decisions` row, so `check_trading_blocked` reports health. That is why the sizing defect
had to be found by replaying a panel.

Record a journal row when a target is non-zero before the cap/floor clamp and zero after it —
at `core_satellite_targets`' return (`strategies/sleeve_common.py:321`) or at the SleeveA caller
(`strategies/SleeveA.py:175-184`). Journal writers never raise into the bot loop.

**Correction to the obvious design:** this does **not** get picked up by the existing wedge
alarm for free. `ops/autonomy.py:1174` filters `intent='entry'` and `:1182` filters
`intent='exit'`, so a row written with `intent='adjust'` reaches neither counter. Either write
the row with an intent the liveness view reads, or extend those two queries. Pick one
deliberately — this is the same blind spot that hides the trim's own adjust rows.

*Tests:* `tests/strategies/test_satellite_selection.py` —
`test_a_target_deleted_by_the_min_position_floor_leaves_a_journal_row`; plus a
`tests/test_ops/` case asserting the liveness counter sees it.

### Step 9 — the load-time sizing validator, layer 1 only

Add `_validate_satellite_sizing(cfg)` to `ops/config.py`, called from `_cross_validate` after
`_validate_seeds` (`ops/config.py:3097`). House style is `_validate_min_edge`
(`ops/config.py:2881-2899`): raise `ConfigError` naming the key path, both numbers and the
remedy. Refuse only the *unconditional* impossibility, at scalar 1.0:

```python
per_seat = r.max_satellite_gross / r.max_satellite_positions
if per_seat + 1e-12 < r.min_position_pct_nav:
    raise ConfigError(...)
```

The `<= 0` early returns are required, not defensive: `max_satellite_positions` is declared
`ge=0` (`ops/config.py:1090`) and `max_satellite_gross` `ge=0` (`:1096`), so zero is a legal
config and a bare divide is a `ZeroDivisionError` at load. `_validate_min_edge` does the same at
`:2890-2891`.

**Be clear what this does not do.** `0.05 / 2 = 0.025 >= 0.02`, so layer 1 **passes the shipped
config** and would not have caught this defect. It catches the next one — three seats at 5%
gross gives 0.0167 — and that is all it is for. Layer 2, the version that validates against a
declared reference scalar and therefore actually refuses today's config, is **held to step 11**:
it would block every ops job and the console config page until the config changes, so it must
land in the same commit as the sizing change, not before.

The blast radius is bounded and the edit path is already safe: `load_config`
(`ops/config.py:3185-3208`) has no cache and is re-read per call, and
`ops/config_store.py:600-617` writes the candidate to a temp file and runs `load_config` on it
before the write, so a bad `earn.yaml` is rejected at the form. `strategies/riskgate.py` never
imports `ops.config` (its imports at `:44-58` are stdlib plus a local `mechanics` shim), so a
`ConfigError` cannot stop a running bot — it reads the generated `config/riskgate.json`.

*Tests:* `tests/test_foundation/test_risk_and_ladder.py`, inside the existing
`class TestSatelliteConcentration` (`:156`), house style `_raw()`/`_write(tmp_path, raw)` plus
`pytest.raises(ConfigError, match=...)`:

* `test_a_seat_that_can_never_open_is_refused_at_load_time` — 3 seats at 0.05 gross
* `test_the_shipped_seats_clear_the_floor_unscaled` — pins `0.025 >= 0.02`, so the test states
  what it does not cover
* `test_satellites_switched_off_is_not_a_division_by_zero` — 0 seats, 0 gross, loads

Also fix `test_the_limits_moved_to_the_floor` (`:157-163`). Its line `:161`,
`assert 2 * cfg.risk.min_position_pct_nav <= cfg.risk.max_satellite_gross`, is a *ceiling* test
— floor-sized seats fit inside the cap — one inequality away from the floor test that was
needed, and it is why the defect lived under a green assertion that looks like it covers this.
Replace it with `max_satellite_positions * min_position_pct_nav <= max_satellite_gross`, or keep
both and comment which is which.

### Step 10 — settle the freqtrade question

On a host where freqtrade is installed (`~/earn-dev/.venv`), read
`inspect.getsource(FreqtradeBot.check_and_call_adjust_trade_position)` and establish whether
`handle_similar_open_order` declines a duplicate adjustment order while the first rests. Pin the
answer in `tests/contract/test_freqtrade_contract.py`, which is where this repo already pins its
freqtrade assumptions. freqtrade is not installed on this host — `find / -name freqtradebot.py`
returns nothing and `pyproject.toml` is the only manifest — so this could not be settled here.

Why it matters: the trim has no latch. `_trim_plan` reads and writes nothing that remembers what
it last acted on (`grep` for `store.get`/`store.set` in `strategies/SleeveA.py` returns exactly
three hits, at `:124`, `:132` and `:228`, none inside `_trim_plan`), `trade.amount` falls only on
fill (`SleeveA.py:293`), and the exit order rests for 20 minutes
(`config/freqtrade-a.json:86-91`) at a 5-second loop — 240 consecutive evaluations of an
identical excess. Whether that becomes 240 *orders* depends entirely on a third-party behaviour
this repo neither pins nor states a requirement for. Note also that
`config/riskgate.json:186` carries `"min_interval_hours": 4` for sleeve `a` and **nothing reads
it**: `min_interval_hours` appears in `strategies/` only at `SleeveB.py:356` and
`SleeveFast.py:333`.

### Step 11 — held, and what unblocks each

* **Trim cadence on the drift branch only.** Mirror `SleeveB.py:347-358` with a distinct
  `last_trim_<pair>` key stamped inside the existing `plan.then(...)` at `SleeveA.py:341-343`,
  applied only when `reason == mx.TRIM_DRIFT` so `TRIM_BREACH` stays uncadenced. *Unblocked by
  step 10:* if freqtrade declines duplicates, this buys nothing for execution and costs real
  risk control — a position drifting over band ten minutes after a filled trim would sit over
  band for four hours.
* **Netting the resting sell out of `position_value`** (`SleeveA.py:293`). Needs an exit-side
  counterpart to `_reserved_for` (`earn_base.py:394-414`), which `continue`s on every order
  whose side is not the entry side — a new helper, not an edit to that one, whose NAV arithmetic
  must not change. *Unblocked by step 10, and do not ship it on reasoning:* if the netting errs
  high it under-reports the position and under-trims a genuine breach, which is the exact
  failure the trim exists to remove.
* **Validator layer 2** (declared reference scalar) and **layer 2b** (the params seat). Layer 2b
  cannot live in `ops/config_store.py:_bounds_issues` (`:573`) alone: `runs/apply_changes.py`
  never calls `ops/config_store.py:validate()` — its only validation is
  `jsonschema.validate(change, CHANGE_SCHEMA)` at `:368` plus a monthly param budget at
  `:394-397` — so the console path would be covered and the `changes/*.json` path would not. A
  tier-1 `sleeve_a.vol.target_annual: 0.10` is legal inside its bounds
  (`config/earn.yaml:308`, `{min: 0.10, max: 0.50, max_step: 0.05}`), cuts the scalar threefold
  and would walk straight past. Step 8's runtime detector covers this case regardless of why the
  scalar fell, which is the argument for preferring it.
* **The seat change itself.** Condition in §2.5.
* **The cap-clamped breach trim under KILL.** §1.5.

---

## 5. Corrections to the source reads this plan was built on

Recorded because they were load-bearing and because a plan that silently fixes its inputs
cannot be checked.

1. **`returns_provider` is wired in production.** The source read treated the
   `lambda pair: None` fallback (`riskgate.py:922`) as the reason `corr_cap` had never
   surfaced. It is supplied at `earn_base.py:235` for the live gate and `:228` for the backtest
   gate. The vacuity is a *test* artefact (`tests/strategies/conftest.py:104-111`), not a
   production one — which makes `corr_cap` a live check and turns the sizing question on its
   head (§2.2).
2. **`kill_provider=lambda: False` at `earn_base.py:227` is not a defect.** It is inside the
   `if self._is_backtest:` branch (`:221-229`). The live branch at `:235` takes the default,
   which is file existence (`riskgate.py:918`).
3. **The satellite divisor is `len(satellites)`, not the config cap.** `per = sat_gross /
   len(satellites)` at `strategies/sleeve_common.py:312`, over a list already filtered to names
   whose own regime is up (`SleeveA.py:172-174`). So "0.05 / 2 = 2.5%" is the two-seat case
   only; on one-seat days the request is already 5%. `diagnostics.json`'s own
   `vol_scaled_target_max` of 0.0474 is impossible otherwise.
4. **`_daily_returns` is not always daily.** `earn_base.py:302-318` reads
   `df["close_1d"] if "close_1d" in df else df["close"]` from
   `get_analyzed_dataframe(pair, self.timeframe)` and takes `tail(61)` of the de-duplicated
   series. SleeveA and SleeveB build the 1d frame (`SleeveA.py:53-55`, `SleeveB.py:86-88`), so
   for them it is ~60 daily observations. `strategies/SleeveFast.py` has no
   `populate_indicators_1d` — grep returns nothing — so under the `fast-test` profile the same
   function returns 60 observations at the **base** timeframe. `RISK_WINDOW_DAYS = 60`
   (`:300`) is documented as the 60-day window every correlation number was measured on; for a
   strategy without a 1d frame it silently is not. That is a second, independent defect in the
   same check.
5. **Step 8's row does not reach the existing alarm for free.** `ops/autonomy.py:1174` and
   `:1182` filter `intent='entry'` and `intent='exit'`; `intent='adjust'` reaches neither.
6. **`apply_changes` does not enforce tier-1 bounds.** `config/earn.yaml:306` says it does.
   `mx.clamp_params` at `earn_base.py:464` is the only caller of the clamp, and
   `runs/apply_changes.py` has zero occurrences of `bounds` or `clamp`.
7. **B7's drawdown is worse than shipped**, −12.26% against −11.88% (`sizing.json`). It has been
   summarised as "same drawdown".
8. **`CHECK_ORDER` has 27 entries** (`riskgate.py:63-69`), not the 17 stated in `CLAUDE.md` and
   `.claude/skills/ops-runbook/SKILL.md:52`.

---

## 6. Honest limits

**What I could not run.** There is no Python interpreter on this host and freqtrade is not
installed, so three things are unverified and two of them gate a step: freqtrade's
`handle_similar_open_order` behaviour (step 10 exists to settle it), the literal value of
`ExitType.EXIT_SIGNAL` (§3 — one command in `~/earn-dev/.venv` decides how urgent step 3 is),
and the BTC-ETH correlation itself. I reproduced no number in this document; every measured
figure is read out of a committed artefact (`diagnostics.json`, `sizing.json`) or carried in from
the source reads with its provenance named.

**The correlation claim is code-certain and measurement-borrowed.** That `corr_cap` averages the
BTC-ETH pair into the number it tests is verified at `riskgate.py:936-947`, `:1031-1035` and
`:507-520`. That the resulting correlation exceeds 0.70 is not verified here — median 0.865 over
two years and above 0.70 on 93.0% of days is borrowed. If that measurement is wrong, §2's
refusal weakens to "the study did not model two live checks", which is still a reason to
re-measure before changing a limit, but a much smaller one.

**I did not read the journal.** So there is no live frequency for any of this: not how often the
fee budget actually refuses a de-risk, not how often KILL is engaged, not how long a KILL window
lasts unattended, not whether a KILL window has ever overlapped a position above its cap. Every
code defect here stands on its own; none of the *frequencies* do. Two of the four lenses named
the same journal query as the thing that would most change their mind — a count of
`kill.engage` rows with each window's duration — and it remains the cheapest high-value check
available to the owner.

**Deployment state is settled only for the committed tree.** `config/earn.yaml:613` is
`active: null` and `config/freqtrade-a.json:83` is `SleeveA`, so the tree analysed here runs
SleeveA with the trim. `config/earn.yaml:610-612` states in prose that the runtime deployment
selects `fast-test`, under which both sleeves are `SleeveFast` and the trim is not running at
all. I did not read `var/runtime/**`. If that prose is current, every finding here is
pre-deployment — correctness unchanged, urgency lower, and the right time to fix it is before
the trim ships. One command settles it: `grep -n profile ~/earn-run/var/runtime/runtime-a.json`.

**The one thing this plan does not fix.** No test anywhere covers the trim's re-fire behaviour
in the submitted-but-unfilled state — `tests/strategies/test_trend_trim.py`'s nearest case
builds a fresh sleeve already at target, which assumes the fill already happened. Step 2 makes
the *journal* idempotent, which is the part that needs no freqtrade knowledge. Whether the
*order* re-fires is unknown, and until step 10 answers it, neither the defect nor any fix for it
is pinned. The breach branch is the sharp end: `check_discretionary_exit` returns before every
throttle for `risk_stop_exposure` (`riskgate.py:1101-1102`) and `record_order_fill` never charges
it to `orders_day` (`riskgate.py:1384`, `if not risk_exit:`, while turnover and fees are added
unconditionally at `:1386-1390`), so it has no repo-side count ceiling at all. Safety-critical
behaviour resting on an unverified library detail is itself the defect, whatever freqtrade turns
out to do.
