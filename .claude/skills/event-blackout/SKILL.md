---
name: event-blackout
description: Owns the macro-event blackout window for Earn, re-arguing its width from a measured event study and raising an alarm when the hand-maintained calendar runs out, and refuses to predict any print, its surprise or its direction. Triggers on event blackout, macro calendar, FOMC, CPI, blackout window, is the calendar stale, how wide should the window be.
allowed-tools: Read, Grep, Glob, Write(knowledge/**), Write(reports/**), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)
---

# Event blackout

<!-- TIER 1: this body changes only through changes/*.json + runs/apply_changes.py.
     scripts/** is TIER 2. config/macro_calendar.yaml is TIER 2 and stays human-maintained:
     this skill AUDITS the calendar, it never writes it. -->

Macro prints are volatility events with no direction. Measured on **79 FOMC statement
releases 2017-09 → 2026-09** against **79,657 BTC/USDT hourly bars**:

| | measured |
|---|---|
| median `\|T → T+4h\|` | **0.850%** vs 0.496% unconditional — **1.72×** |
| `P(\|4h return\| > 2%)` | **20.3%** vs 10.9% |
| peak hour | the release bar itself, **2.04×** the unconditional median `\|1h\|` |
| T → T+1h direction | mean −0.227%, sd 1.093, **t = −1.85** |
| T → T+24h direction | mean −0.204%, sd 3.697, **t = −0.49** |

Double the variance for no expected return is a pure cost to enter into. Standing aside is
the positive-expectancy action, and it is the *only* action this skill has.

## When this runs

Bound to `research.flags` and `daily`. Run it by hand before a known print, when someone
asks how wide the window should be, or when a proposal is rejected for `blackout`.

## Inputs

| What | Where | Who produces it |
|---|---|---|
| Blackout state, next event, calendar staleness, FOMC audit | `knowledge/state/macro.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/macro_state.py` |
| The hour-by-hour event study and the suggested width | `knowledge/state/macro_event_study.json` | `python3 ${CLAUDE_SKILL_DIR}/scripts/fomc_study.py` |
| The event list itself | `config/macro_calendar.yaml` | **a human, quarterly** — tier 2 |
| The measurement behind the width | `references/window.md` | this study, dated |

Numbers come from the scripts. Never estimate a multiplier, a window or a date.

## Procedure

1. Current state:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/macro_state.py
   ```

   `--audit` also checks the calendar's FOMC rows against federalreserve.gov and the last
   CPI print against api.bls.gov. `--write-flags` moves the blackout flag.

2. Re-argue the width when asked, or on the daily run:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/fomc_study.py
   ```

3. Read the JSON and report:

   - **`calendar_stale_days` first.** Under 45 days of future coverage is an alarm: the
     calendar is hand-maintained and its real failure mode is running out silently. Say how
     many events remain and the date of the furthest one.
   - `in_blackout`, the event's name, and when the window ends.
   - `hours_to_next_macro_event`.
   - The audit block: `missing` (a scheduled FOMC meeting inside 180 days with no calendar
     row), `drifted` (a row whose time is more than 90 minutes from the 14:00 ET release),
     and whether the scrape succeeded at all. The audit reads the calendar's **scheduled
     meeting panels**, not the statement links — a statement link only exists after the
     meeting, so it can never verify a future row.
   - From the study: `suggested_window`, `peak_multiple`, and the direction blocks — quote
     the t-statistics, because the point of reporting direction is to show it is absent.

4. If `suggested_window` disagrees materially with the shipped
   `risk.blackout.window_minutes`, raise a **change proposal** in `changes/`. Never widen
   or narrow anything directly.

## Hard stops

- **Never predict a print, its surprise or its direction.** Not the rate, not the CPI
  number, not "the market expects". The measured T+24h t-statistic is −0.49; there is
  nothing there to forecast and the scripts expose no field that could carry one.
- **Never write `config/macro_calendar.yaml`.** It is tier 2 and human-maintained. This
  skill audits it and reports what is missing.
- **Never silently extend a window.** A width change goes through `changes/*.json` with the
  study attached, like any other change.
- **Never fail open.** If federalreserve.gov is unreachable the last known dates stand, the
  scrape is flagged, and the blackout still applies. An unreachable source is never
  evidence that there is no event.
- **Raise `calendar_stale` below 45 days of coverage**, and say it before anything else.
- The asymmetric window is not decoration: the measured elevation runs from about T−7h to
  T+8h, so a symmetric window either leaves the tail open or blacks out hours that are
  already back at baseline.

## Output

`knowledge/state/macro.json`, `knowledge/state/macro_event_study.json`, optionally the
`macro_blackout` flag through `ops.lib.flags`, and a change proposal when the width is
wrong. Nothing else.
