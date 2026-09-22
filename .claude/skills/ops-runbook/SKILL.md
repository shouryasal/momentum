---
name: ops-runbook
description: Step-by-step Earn operations runbook — restart a bot, recover from an exchange outage, rotate keys, pause a sleeve, resume after a monthly stop, restore a backup, use the kill switch. For manual invocation during incidents only.
disable-model-invocation: true
allowed-tools: Read, Bash(docker compose *), Bash(bash ops/*), Bash(python3 -m ops.*), Bash(touch ops/killdir/*), Bash(rm ops/killdir/*), Bash(sqlite3 *)
---

# Ops runbook (tier 2 — human-only, manual `/ops-runbook` invocation only)

Every incident ends with a note in `knowledge/incidents/<date>-<slug>.md`:
what happened, when, root cause, fix, follow-up.

## Restart a bot
1. `docker compose -f ops/docker-compose.yml restart freqtrade-a` (or `-b`)
2. `docker compose -f ops/docker-compose.yml logs --tail 50 freqtrade-a` — dry-run banner, no tracebacks
3. If it flaps 3x: `docker compose down`, check `config/freqtrade-*.json` drift
   (`python3 -m ops.gen_freqtrade_config --check`), then `up -d`.

## Exchange outage
1. The bot pauses itself on ccxt errors; healthcheck alerts. Do NOT touch keys.
2. When Binance recovers, confirm order status reconciles: bot REST `/status` vs
   the exchange's order history. 3. Resume = nothing (the bot resumes); note it.

## Kill switch
- Engage: `touch ops/killdir/KILL` (or `/kill` on Telegram). Healthcheck cancels
  open orders + stopbuy within 5 min; entries refuse immediately.
- Runaway trading: `/kill`, then `/forceexit a all` and `/forceexit b all`,
  inspect journal gate_decisions before clearing.
- Clear (human-only): `rm ops/killdir/KILL`; healthcheck announces it.

## Suspected key compromise
1. DELETE the key in the Binance console FIRST. 2. `touch ops/killdir/KILL`.
3. Rotate: new key (Reading + Spot trading only, withdrawals OFF, IP-restricted),
   update `.env`, `docker compose up -d --force-recreate`. 4. Incident note.
   Keys rotate every 90 days regardless.

## Pause one sleeve
`/pause a` (Telegram) or REST stopbuy — entries stop, exits continue. Resume:
`docker compose restart freqtrade-a` (initial_state running).

## Resume after MONTHLY stop (-10%)
Only after the review run has root-caused the loss:
`sqlite3 journal/journal.db "DELETE FROM risk_state WHERE sleeve='b' AND key='monthly_locked';"`
(the gate never clears this itself). Then restart the sleeve's container.

## Restore from backup
`bash ops/restore.sh YYYY-MM-DD --yes` — engages KILL, stops bots, restores DBs
(integrity-checked) + files, restarts. Remove KILL manually afterwards.
Rehearse this twice for G1.

## Model outage (Anthropic API down)
Nothing to do: sleeve B keeps last valid targets and follows sleeve A after 48h
automatically. Watch for the healthcheck proposal-failure alert clearing.
