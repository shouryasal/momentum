#!/usr/bin/env bash
# Run a model session as the dedicated agent user (security.agent_user).
#
#   ops/agent_cli.sh <job> -- <command...>
#
# It is envwrap.sh plus a uid drop: the command still gets exactly the job's allowlisted
# secrets, and now also runs as a user that cannot read .env, var/ or ops/ at all. The
# console uses this wrapper for LIVE_EXECUTE sessions when
# modes.live.require_agent_user_for_execute is true.
#
# Chosen deliberately: `sudo -n -u <user>` and NOT `setpriv`/`su`, because the sudoers
# rule can be narrowed to this one command, is auditable in /var/log, and fails loudly
# (-n = never prompt) instead of hanging a cron job on a password prompt.
#
# Required sudoers line (visudo, replace <owner> and <agent>):
#   <owner> ALL=(<agent>) NOPASSWD: /usr/bin/env

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

JOB="${1:?usage: agent_cli.sh <job> -- <cmd...>}"
shift
[ "${1:-}" = "--" ] && shift
[ "$#" -gt 0 ] || { echo "usage: agent_cli.sh <job> -- <cmd...>" >&2; exit 2; }

AGENT_USER="$(
  "$REPO_ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || true
from ops.config import load_config
print(load_config().security.agent_user or "")
PY
)"

if [ -z "$AGENT_USER" ]; then
  echo "agent_cli: security.agent_user is not set — run ops/setup_agent_user.sh first" >&2
  exit 3
fi

if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
  echo "agent_cli: user '$AGENT_USER' does not exist on this host" >&2
  exit 3
fi

if [ "$(id -un)" = "$AGENT_USER" ]; then
  exec bash "$REPO_ROOT/ops/envwrap.sh" "$JOB" -- "$@"
fi

exec sudo -n -u "$AGENT_USER" /usr/bin/env \
  HOME="$(getent passwd "$AGENT_USER" | cut -d: -f6)" \
  bash "$REPO_ROOT/ops/envwrap.sh" "$JOB" -- "$@"
