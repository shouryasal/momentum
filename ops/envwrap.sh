#!/usr/bin/env bash
# Per-job secret allowlisting: every scheduled job runs through this wrapper, so a job's
# process environment carries ONLY the secrets that job needs. A model-facing job gets a
# Claude credential and nothing else; an exchange credential never reaches one (runs/
# common.py:guard_env() hard-fails if one ever did).
#
# Usage:
#   ops/envwrap.sh <job> -- <command...>
#   ops/envwrap.sh --print-allowlist <job>      # names only, for tests and the UI
#   ops/envwrap.sh --print-env <job>            # NAMES only of what would be exported
#
# Claude auth mode (EARN_CLAUDE_AUTH_MODE in .env, kept in sync by the console when
# models.yaml auth.claude_mode is saved; anything unknown falls back to subscription):
#
#   subscription  CLAUDE_CODE_OAUTH_TOKEN only. A present ANTHROPIC_API_KEY is renamed
#                 to EARN_FALLBACK_ANTHROPIC_API_KEY, UNCONDITIONALLY — token or no
#                 token. It used to be dropped only when an OAuth token was also
#                 present, which is exactly backwards: on a host with
#                 `auth.subscription_source: login` there is legitimately no token in
#                 .env, so every cron model job got the plain key, the headless CLI
#                 accepted it, and the spend was metered with no cap and nothing
#                 journalled. The job inherits its environment verbatim in any stage
#                 runner that passes no `env=` overlay, so the plain name must simply
#                 never be exported outside api_key mode.
#   api_key       ANTHROPIC_API_KEY only; the OAuth token is dropped. The one mode in
#                 which the plain name is exported, because metered spend IS the intent.
#   auto          the OAuth token (if any) PLUS the API key renamed to
#                 EARN_FALLBACK_ANTHROPIC_API_KEY, a name the CLI can never pick up
#                 implicitly — only runs/llm/providers/claude_sdk.py reads it. The
#                 rename is unconditional: a host with no OAuth token still must not
#                 spend metered money except through an explicit api_key attempt.
#
# The console's signing secret and login token appear in NO allowlist below (asserted by
# a test that greps this file): an automated run must never be able to sign a mode file
# or log into the console.

set -euo pipefail

usage() {
  echo "usage: envwrap.sh <job> -- <cmd...> | --print-allowlist <job> | --print-env <job>" >&2
  exit 2
}

MODE="run"
case "${1:-}" in
  --print-allowlist) MODE="allowlist"; shift ;;
  --print-env)       MODE="printenv";  shift ;;
  -h|--help)         usage ;;
esac

JOB="${1:-}"
[ -n "$JOB" ] || usage
shift || true
[ "${1:-}" = "--" ] && shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${EARN_ENV_FILE:-$REPO_ROOT/.env}"

# ---------------------------------------------------------------- allowlists

allowlist() {
  case "$1" in
    # Model-facing jobs: a Claude credential only. Nothing else, ever.
    research|review|daily_review|maintenance|signals|scanner|ingest|discovery)
                    echo "CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY" ;;
    # Alerting + bot control. healthcheck also drains the alert outbox, so it is the one
    # job that always has the Telegram token.
    healthcheck|digest)
                    echo "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET HEALTHCHECKS_URL" ;;
    # The Telegram command bot additionally verifies /approve HMACs.
    telegram)
                    echo "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET HEALTHCHECKS_URL EARN_APPROVAL_KEY" ;;
    nav|nav_tick)   echo "FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET" ;;
    # Host-side only: ledger-vs-exchange reconciliation and the live preflight read the
    # exchange account. Never a model job.
    #
    # BINANCE_DEMO_KEY/SECRET are the DEMO venue's credentials (demo-api.binance.com) and
    # are deliberately separate names from the live BINANCE_KEY_A/B — a demo key is never
    # filed under a live name, so neither can be promoted to the other by a one-word .env
    # edit. Both venues' names appear here because these two jobs are the ones that probe
    # whichever venue the sleeve's mode binds them to; which one they may actually USE is
    # decided per sleeve by ops.lib.exchange_endpoints.resolve_binding, not by this list.
    reconcile|preflight)
                    echo "FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET BINANCE_KEY_A BINANCE_SECRET_A BINANCE_KEY_B BINANCE_SECRET_B BINANCE_DEMO_KEY BINANCE_DEMO_SECRET" ;;
    backup)         echo "BACKUP_RCLONE_REMOTE" ;;
    # The console is NOT run through envwrap (it needs its own secrets and refuses to
    # start under EARN_AUTOMATED_RUN=1); the entry exists so tooling can ask, and get
    # "nothing".
    console)        echo "" ;;
    # The database snapshot needs no credential of any kind: it copies two local sqlite
    # files under the ops lock and talks to nothing. See ops/lib/snapshot.py.
    tca|excel|snapshot) echo "" ;;
    *) echo "envwrap: unknown job '$1'" >&2; exit 2 ;;
  esac
}

ALLOWED="$(allowlist "$JOB")"

if [ "$MODE" = "allowlist" ]; then
  for k in $ALLOWED; do echo "$k"; done
  exit 0
fi

# ---------------------------------------------------------------- .env parsing
# Tolerant of CRLF line endings (the repo is edited from Windows), `export KEY=v`,
# surrounding single or double quotes, and inline padding. Nothing is exported into this
# shell: values are collected and handed to `env -i` explicitly.

declare -A WANT
for k in $ALLOWED; do WANT[$k]=1; done

declare -A VALUES
AUTH_MODE_RAW="${EARN_CLAUDE_AUTH_MODE:-}"

strip_quotes() {
  local v="$1"
  v="${v%$'\r'}"
  v="${v#"${v%%[![:space:]]*}"}"   # ltrim
  v="${v%"${v##*[![:space:]]}"}"   # rtrim
  if [[ ${#v} -ge 2 && "${v:0:1}" == '"' && "${v: -1}" == '"' ]]; then
    v="${v:1:${#v}-2}"
  elif [[ ${#v} -ge 2 && "${v:0:1}" == "'" && "${v: -1}" == "'" ]]; then
    v="${v:1:${#v}-2}"
  fi
  printf '%s' "$v"
}

if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" != *"="* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#export }"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    val="$(strip_quotes "$val")"
    if [ "$key" = "EARN_CLAUDE_AUTH_MODE" ] && [ -z "$AUTH_MODE_RAW" ]; then
      AUTH_MODE_RAW="$val"
    fi
    [[ -n "${WANT[$key]:-}" && -n "$val" ]] && VALUES[$key]="$val"
  done < "$ENV_FILE"
fi

# ---------------------------------------------------------------- auth mode

case "$AUTH_MODE_RAW" in
  subscription|api_key|auto) AUTH_MODE="$AUTH_MODE_RAW" ;;
  *)                         AUTH_MODE="subscription" ;;
esac

HAVE_KEY=0;   [ -n "${VALUES[ANTHROPIC_API_KEY]:-}" ]       && HAVE_KEY=1

# ALWAYS rename, token or no token, in every mode but api_key. The only thing that makes
# metered spend deliberate is that the CLI cannot see the key on its own: the child CLI
# inherits this environment verbatim whenever a caller passes no `env=` overlay (every
# research/review/daily stage does), so the plain name reaching a job IS the spend. The
# credential is not lost — ops.lib.claude_auth.api_key_from() reads the fallback name
# first, and env_for('claude:api_key') materialises it under the real name for an
# explicit, journalled, capped api_key attempt.
rename_key() {
  if [ "$HAVE_KEY" = "1" ]; then
    VALUES[EARN_FALLBACK_ANTHROPIC_API_KEY]="${VALUES[ANTHROPIC_API_KEY]}"
    unset 'VALUES[ANTHROPIC_API_KEY]'
  fi
}

case "$AUTH_MODE" in
  subscription) rename_key ;;
  auto)         rename_key ;;
  api_key)
    [ "$HAVE_KEY" = "1" ] && unset 'VALUES[CLAUDE_CODE_OAUTH_TOKEN]'
    ;;
esac

# ---------------------------------------------------------------- environment

ENV_ARGS=()
for k in "${!VALUES[@]}"; do ENV_ARGS+=("$k=${VALUES[$k]}"); done

# Roots a worktree session needs. Not secrets; passed through only when already set.
for passthru in EARN_STATE_ROOT EARN_WORKTREE EARN_LIVE_ROOT; do
  [ -n "${!passthru:-}" ] && ENV_ARGS+=("$passthru=${!passthru}")
done

if [ "$MODE" = "printenv" ]; then
  # Names only — this must stay safe to paste into a log or a bug report.
  for kv in "${ENV_ARGS[@]:-}"; do [ -n "$kv" ] && echo "${kv%%=*}"; done | sort
  echo "EARN_AUTOMATED_RUN"
  echo "EARN_JOB"
  echo "EARN_CLAUDE_AUTH_MODE=$AUTH_MODE"
  exit 0
fi

[ "$#" -gt 0 ] || usage

# The venv must lead PATH: skill scripts and `python` inside a job otherwise run on the
# system interpreter, which has none of Earn's dependencies.
exec env -i \
  PATH="$REPO_ROOT/.venv/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}" \
  VIRTUAL_ENV="$REPO_ROOT/.venv" \
  HOME="${HOME:-/root}" LANG="${LANG:-C.UTF-8}" TZ="${TZ:-Asia/Dubai}" \
  EARN_AUTOMATED_RUN=1 EARN_JOB="$JOB" EARN_CLAUDE_AUTH_MODE="$AUTH_MODE" \
  ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
  "$@"
