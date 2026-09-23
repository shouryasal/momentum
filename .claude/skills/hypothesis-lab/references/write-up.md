# The write-up

One shape for every study, positive or negative. The order matters: the falsifier comes
before the result, so a reader sees what was risked before they see what was won.

```markdown
## <id> — <one-line statement>

**Hypothesis (pre-registered <opened_utc>, seal `<first 16 chars>`)**
<observable> predicts <target> at <horizon>, on <surface>.

**Falsifier** <the number that would kill it>.
**Triggered:** yes | no — measured value `<x>` against the threshold `<y>`.

**Ledger check** research-scout `verdict=open` (nearest entry: `<id>` — not this, because …)
**Trial count** N = <n> after this study; deflated hurdle = <baseline> + <expected_max_SR>
**Sample** rows <n_rows> → **effective_n <n_eff>** (edge-audit), purged 5-fold, embargo <bars>

**Costs** <bps> bps, measured (TCA-calibrated), applied to every leg.

### Result, out-of-sample

| | candidate | strategy_baseline | btc_buy_and_hold |
|---|---|---|---|
| Sharpe (OOS) | | | |
| CAGR | | | |
| MaxDD | | | |
| months < −10% | | | |

In-sample, for reference only: <…>

### By regime

| regime | n | candidate | baseline | delta |
|---|---|---|---|---|
| bull | | | | |
| bear | | | | |

### Both directions of the rule

- **Avoided:** <what the rule kept you out of, with the number>
- **Gave up:** <what it also kept you out of, with the number>

### Parameter surface

<the whole sweep, not the peak. Say where the plateau is and where the peak is, and which
one is being proposed.>

### Verdict

`supported` | `refuted` | `inconclusive` — and why, in one paragraph.

### What would change this

<the next measurement that would move the verdict, or "nothing available free", naming the
reason from research-scout's gate.>
```

## Writing the negative

A refuted hypothesis gets the same shape and the same care. Three things make a negative
useful rather than a shrug:

1. **Say what you expected and how big it would have been.** "No effect" is weak; "the
   effect would have needed to be 0.4 Sharpe to matter and the OOS estimate is 0.02 ± 0.31"
   is a result someone can build on.
2. **Say whether it is dead or merely unmeasurable here.** Those are different verdicts with
   different consequences: the first belongs in the rejection ledger, the second is
   `observe_only` and may be revisited when the data accrues.
3. **Say what it cost.** The trial counter moved; the hurdle for every future study is now
   fractionally higher. That is the honest accounting.

## What never appears in a write-up

- A number the author computed in their head, or restated from an earlier study without
  re-running it.
- A row count presented as a sample size.
- An in-sample headline.
- A single baseline.
- A peak without the plateau around it.
- A live-period result described as validation, without `years_to_detect` beside it.
