#!/usr/bin/env bash
# Per-job secret allowlisting: every cron job runs through this wrapper so a job's
# process environment carries ONLY the secrets it needs. In particular the research
# and review runs get the Anthropic key and NOTHING else (spec §4).
#
# Usage: ops/envwrap.sh <job> -- <command...>
#   e.g. ops/envwrap.sh research -- .venv/bin/python -m runs.research_run 0830

set -euo pipefail

JOB="${1:?usage: envwrap.sh <job> -- <cmd...>}"
shift
[ "${1:-}" = "--" ] && shift

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

allowlist() {
  case "$1" in
    research|review)     echo "ANTHROPIC_API_KEY" ;;
    healthcheck|digest|telegram)
                         echo "TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET HEALTHCHECKS_URL" ;;
    nav)                 echo "FT_API_PASSWORD_A FT_API_PASSWORD_B FT_JWT_SECRET" ;;
    classify)            echo "ANTHROPIC_API_KEY" ;;
    backup)              echo "BACKUP_RCLONE_REMOTE" ;;
    ingest|tca|excel)    echo "" ;;
    *) echo "envwrap: unknown job '$1'" >&2; exit 2 ;;
  esac
}

ALLOWED="$(allowlist "$JOB")"

# Read .env without exporting everything, then hand the command a scrubbed environment
# containing PATH/HOME/LANG plus only the allowlisted keys.
declare -A WANT
for k in $ALLOWED; do WANT[$k]=1; done

ENV_ARGS=()
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    if [[ -n "${WANT[$key]:-}" ]]; then
      ENV_ARGS+=("$key=$val")
    fi
  done < "$ENV_FILE"
fi

exec env -i \
  PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" TZ="${TZ:-Asia/Dubai}" \
  EARN_AUTOMATED_RUN=1 EARN_JOB="$JOB" \
  "${ENV_ARGS[@]}" \
  "$@"
