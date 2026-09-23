# event-blackout — the measurement behind the window

Every number on this page was computed in this repo from local data or a verified free
endpoint. Nothing is quoted from a paper.

## The study

**79 FOMC statement releases, 2017-09-20 → 2026-09-16**, anchored at 14:00 America/New_York
with DST handled by `zoneinfo`, against **79,657 Binance BTC/USDT hourly bars**
(2017-08-17 → 2026-09-23, the local feather archive). Statement dates come from
`federalreserve.gov` (`monetary(\d{8})a\.htm`): the current calendar page yields 47 and the
`fomchistorical<year>.htm` pages 2015–2020 take it to **100 distinct dates**, of which 79
fall inside the candle history.

Returns are simple hourly returns; the statistic is the **median** `|1h return|`, because a
handful of 2020 bars otherwise set the entire profile.

## Volatility — the whole result

| | measured |
|---|---|
| unconditional median `\|1h\|` | 0.2515% |
| median `\|T → T+4h\|` | **0.850%** vs **0.496%** unconditional → **ratio 1.716** |
| `P(\|4h return\| > 2%)` | **20.3%** vs **10.9%** |

## The hour-by-hour profile

`multiple` is that hour's median `|1h|` divided by the unconditional median. `smoothed` is
the 3-hour centred rolling median, which is what the width is read off.

| hour | median \|1h\| | multiple | smoothed |
|---|---|---|---|
| T−9h | 0.2884% | 1.147 | 1.147 |
| T−8h | 0.3107% | 1.236 | 1.147 |
| T−7h | 0.2482% | 0.987 | **1.236** |
| T−6h | 0.3641% | 1.448 | 1.349 |
| T−5h | 0.3392% | 1.349 | 1.417 |
| T−4h | 0.3564% | 1.417 | 1.349 |
| T−3h | 0.3226% | 1.283 | 1.283 |
| T−2h | 0.2777% | 1.104 | 1.283 |
| T−1h | 0.3643% | 1.449 | 1.449 |
| **T+0h** | **0.5119%** | **2.036** | **1.576** |
| T+1h | 0.3962% | 1.576 | 1.576 |
| T+2h | 0.3217% | 1.279 | 1.526 |
| T+3h | 0.3839% | 1.526 | 1.279 |
| T+4h | 0.2845% | 1.131 | 1.285 |
| T+5h | 0.3231% | 1.285 | 1.285 |
| T+6h | 0.3430% | 1.364 | 1.312 |
| T+7h | 0.3299% | 1.312 | **1.312** |
| T+8h | 0.2706% | 1.076 | 1.115 |
| T+9h | 0.2805% | 1.115 | 1.076 |
| T+10h | 0.2154% | 0.857 | 1.065 |

**Why smoothed.** At n=79 a single hour swings by about 0.3× — T−2h reads 1.104 between
neighbours of 1.283 and 1.449, and T−7h reads 0.987 between 1.236 and 1.448. Reading the
boundary off the raw series makes a shipped config value a function of one noisy bucket: the
raw contiguous run at 1.20× stops at T−1h / T+3h, the smoothed run at T−7h / T+7h. The
smoothed answer is the one that survives dropping any event.

**Leave-one-out: all 100 variants returned the same window, (420, 480).** That is the
strongest evidence on this page — the recommendation does not depend on any single release.

## The recommendation

```
suggested_window(threshold = 1.20x, smooth = 3)  ->  pre 420 min / post 480 min
```

Against the shipped `risk.blackout.window_minutes: 60`, symmetric. The release bar's own
hour belongs to `post`, because a window that ends *at* the release ends before the bar it
is protecting against has finished printing.

At a 1.10× threshold the same walk gives **pre 360 / post 480** — the width is not sensitive
to the threshold in any way that changes the decision. What *is* decisive is that 60 minutes
symmetric covers only the peak hour and leaves the entire elevated shoulder open on both
sides.

This page recommends. Changing the live value is a tier-2 config edit and goes through
`changes/*.json` with this study attached.

## Direction — reported so that its absence stays visible

| horizon | n | mean | sd | t |
|---|---|---|---|---|
| T → T+1h | 79 | −0.227% | 1.093% | **−1.85** |
| T → T+24h | 78 | −0.204% | 3.697% | **−0.49** |

Neither is significant. You are being offered roughly double the variance for zero expected
return, before fees and before the wider spread. **Standing aside is the positive-expectancy
action**, and it is the only action this skill has: no script here exposes a field that could
carry a direction, a surprise or an expected move.

## ETH, honestly

The same 79 events against 79,657 ETH/USDT hourly bars: unconditional median `|1h|` 0.3405%,
4h ratio **1.191**, `P(|4h| > 2%)` 24.1% vs 16.4%, peak at the release bar **2.024×** — but
the surrounding elevation is diluted, and the same walk returns a window of only 60/120. The
event is visible on ETH; the *shoulder* is not, because ETH's unconditional volatility is
35% higher so the event premium is relatively smaller. The blackout is portfolio-wide and
BTC anchors the width. Recorded here so nobody re-derives it and thinks the skill is broken.

## Decay watch

`rolling_vol_multiplier_20ev` re-argues the width from the most recent 20 releases every
time the study runs. Measured on the same panel:

| events | 4h ratio |
|---|---|
| last 20 | 1.407 |
| last 40 | 1.910 |
| first half (n≈39) | 1.271 |
| second half (n≈40) | 1.910 |
| full sample | 1.716 |

The effect is present in both halves and is *stronger* in the recent one. The last-20 number
being lower than the last-40 is noise at this sample size, not decay — which is exactly why
the skill reports the rolling figure rather than acting on it.

## The golden fixture

`tests/fixtures/btc_1h_2024-08_2026-09.json` is 18,805 contiguous real BTC/USDT hourly
closes with 18 FOMC releases inside it. Measured on it: unconditional 0.2058%, 4h ratio
**1.618**, peak **3.341× at the release bar**, T+24h t = **−0.56**. It is deliberately thin —
18 events — and pins the machinery, not the recommendation. The recommendation comes from
the 79-event study above.

## Sources

| | URL | Verified 2026-09-23 | Key | Cadence |
|---|---|---|---|---|
| FOMC released statements | `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` | HTTP 200, 165,460 B, **47** dates ending 2026-09-16 | none | weekly |
| FOMC scheduled meetings | same page, `fomc-meeting__month` / `__date` panels | **54** dates out to 2027-12-08 | none | weekly |
| FOMC history | `.../fomchistorical{year}.htm` | 2019 → 9 dates, 2020 → 12; 2015–2020 plus the current page gives 100 | none | once |
| CPI, post-hoc | `https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0` | HTTP 200, `{"year":"2026","period":"M08","latest":"true","value":"334.980"}` | **none** | daily |

### Three traps, recorded rather than buried

**1. `monetary<YYYYMMDD>a.htm` only exists after the meeting.** That link is the *published
statement*, so scraping it gives a complete record of the past and **nothing about the
future**. Verified on 2026-09-23: the pattern yields 47 dates ending 2026-09-16 while the
page's own panels already list October and December. An audit built on it can confirm that
a past blackout was real and can never tell you next month's row is wrong — which is the
only direction that matters for a calendar.

The fix is `fomc_scheduled_from_html()`, which reads the calendar's own meeting panels —
`<YYYY> FOMC Meetings` → `fomc-meeting__month` → `fomc-meeting__date` — and resolves a
two-day meeting to its **last** day (`27-28` in October 2026 → `20261028`), rolling over a
month boundary (`29-1` in April → 1 May). Verified: **54 scheduled dates out to 2027-12-08**,
against 47 released. Combined, the fetch returns 57 distinct dates.

With it, the audit does its job for the first time: both shipped FOMC rows
(`2026-10-28T18:00:00Z`, `2026-12-09T19:00:00Z`) matched the published schedule with
**0.0 minutes of drift**, and the two 2027 meetings inside the 180-day horizon were
correctly reported as `missing`.

**2. `federalreserve.gov/json/ne-fomccalendar.json` is a 404 that returns an HTML body.** It
would poison a parser silently, so it is not used, and there is a test that a page which
*fetches* but yields zero dates counts as a **failure**, not as "no events".

**3. Every `www.bls.gov` path returns 403 from this host.** `api.bls.gov` works keyless, but
it is *post-hoc release detection*: `latest:true` flips at release, so it says a print
happened, never when the next one is. `config/macro_calendar.yaml` therefore stays
human-maintained and tier 2, and the staleness alarm is mandatory rather than optional.
