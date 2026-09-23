<!-- prompts/stages/flags.v1.md — TIER 1. Rendered by runs/research_run.py:stage_flags.
Was a code constant (FLAGS_PROMPT); moved to a file so the console can edit it and the
change gate can replay it. -->

Run the reg-watch procedure (the reg-watch skill): scan the last 7 days of regulator and
exchange notices in the news archive for delistings, stablecoin depegs, licence changes
and trading halts affecting BTC, ETH or Binance, and return the flag proposals as the JSON
object the schema requires. Propose only what the evidence supports; an empty flags list is
a normal result. Do NOT include scheduled macro events (CPI/FOMC) — a deterministic
calendar handles those.
