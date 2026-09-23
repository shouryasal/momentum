#!/usr/bin/env bash
# Optional privilege separation: run the Claude CLI as a dedicated unix user.
#
#   sudo ops/setup_agent_user.sh earn-agent
#
# Why: LIVE_EXECUTE means a model session runs on a host that also holds exchange
# credentials, the console secret and the ability to write ops/. A separate user makes
# "the model cannot read .env" a kernel guarantee rather than a convention. It is
# required for LIVE_EXECUTE when modes.live.require_agent_user_for_execute is true.
#
# What the agent user gets:
#   read+execute on the checkout, write on the tier-0/tier-1 data dirs it legitimately
#   owns (knowledge/, journal/, reports/, changes/, proposals/, lessons.md, prompts/),
#   and NOTHING on .env, var/, ops/ or .git/config.
#
# After running this, set in config/earn.yaml:
#   security: { agent_user: earn-agent, agent_cli_wrapper: ops/agent_cli.sh }

set -euo pipefail

AGENT_USER="${1:-earn-agent}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c '%U' "$REPO_ROOT")"
GROUP="earn-agents"

if [ "$(id -u)" != "0" ]; then
  echo "run me with sudo: sudo ops/setup_agent_user.sh $AGENT_USER" >&2
  exit 2
fi

echo "repo:  $REPO_ROOT (owner $OWNER)"
echo "agent: $AGENT_USER"

if ! getent group "$GROUP" >/dev/null; then
  groupadd "$GROUP"
  echo "created group $GROUP"
fi

if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin --gid "$GROUP" "$AGENT_USER"
  echo "created user $AGENT_USER"
else
  usermod -aG "$GROUP" "$AGENT_USER"
  echo "user $AGENT_USER already exists"
fi

# The owner joins the group so both can share the writable data directories.
usermod -aG "$GROUP" "$OWNER"

# Secrets and machine-local state: owner only, full stop.
for secret in "$REPO_ROOT/.env" "$REPO_ROOT/var"; do
  [ -e "$secret" ] || continue
  chown -R "$OWNER" "$secret"
  chmod -R go-rwx "$secret"
  echo "locked $secret to $OWNER"
done

# Shared, writable data: group-writable with the setgid bit so new files keep the group.
for shared in knowledge journal reports changes proposals prompts logs lessons.md; do
  path="$REPO_ROOT/$shared"
  [ -e "$path" ] || continue
  chgrp -R "$GROUP" "$path"
  chmod -R g+rwX "$path"
  [ -d "$path" ] && find "$path" -type d -exec chmod g+s {} +
  echo "shared $shared with $GROUP"
done

# Everything else: readable, not writable.
chgrp -R "$GROUP" "$REPO_ROOT/config" "$REPO_ROOT/ops" "$REPO_ROOT/runs" \
  "$REPO_ROOT/strategies" 2>/dev/null || true
chmod -R g-w "$REPO_ROOT/config" "$REPO_ROOT/ops" "$REPO_ROOT/runs" \
  "$REPO_ROOT/strategies" 2>/dev/null || true

cat <<EOF

Done. Next:
  1. config/earn.yaml:
       security:
         agent_user: $AGENT_USER
         agent_cli_wrapper: ops/agent_cli.sh
  2. verify the agent cannot read the secrets:
       sudo -u $AGENT_USER cat $REPO_ROOT/.env   # must be 'Permission denied'
  3. re-run the live preflight; the agent-user check should now pass.
EOF
