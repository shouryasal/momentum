#!/usr/bin/env bash
# One-shot, idempotent host setup for Earn on WSL2 Ubuntu 24.04.
#
#   ops/setup.sh              # check + create everything, never overwrite a secret
#   ops/setup.sh --check      # report only, change nothing
#   ops/setup.sh --force-ext4 # proceed even if the checkout is not on ext4 (NOT for live)
#
# What it guarantees, in this order:
#   1. the checkout is on ext4 — a /mnt/c (drvfs/9p) copy corrupts SQLite WAL and git
#      worktrees, which is exactly how this project is edited from Windows;
#   1b. the distro timezone is the configured one (Asia/Dubai). Every schedule in this
#      repo is Gulf time and ops/healthcheck.py forces Gulf before croniter, so a stock
#      UTC host makes the watchdog expect every daily job four hours early: it reruns
#      them detached and alerts, then the real cron fire runs them again. The generated
#      crontab now carries CRON_TZ so the SCHEDULE is right regardless, but logs, the
#      console and every `date` on this box still read the distro clock, so this is
#      blocking;
#   2. every runtime directory exists and is owned by the invoking user — a missing
#      logs/ or ops/locks/ used to make EVERY cron line fail before its job started,
#      silently, because MAILTO was empty. They are created under $EARN_STATE_ROOT (the
#      live data root) when it is set, because that is where ops.lib.paths and ops.db
#      resolve var/, logs/, ops/locks, ops/killdir and every cfg.paths.* entry — this
#      script used to create them beside the checkout unconditionally, so on a split-root
#      host it reported "ok" for directories nothing ever reads;
#   3. .env exists (0600), ops/.env symlinks to it for docker compose interpolation, and
#      EARN_CONSOLE_SECRET / EARN_CONSOLE_TOKEN / EARN_APPROVAL_KEY are generated once
#      and never regenerated or printed in full;
#   4. the venv exists and carries the project;
#   5. both databases are initialised at the current schema version;
#   6. git identity is set, so an automated commit cannot fail at 02:00.
#
# Running it twice is a no-op. It NEVER writes a secret that already has a value and
# NEVER echoes one.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The live data root, exactly as ops.lib.paths.state_root() computes it: $EARN_STATE_ROOT
# when set, else the checkout. Everything machine-local (var/, logs/, ops/locks,
# ops/killdir, both databases, proposals, changes, reports) lives under THIS, not under
# the checkout — a review/daily session and the console both honour it.
STATE_ROOT="${EARN_STATE_ROOT:-$REPO_ROOT}"
STATE_ROOT="${STATE_ROOT/#\~/$HOME}"
if [ -d "$STATE_ROOT" ]; then
  STATE_ROOT="$(cd "$STATE_ROOT" && pwd)"
fi

CHECK_ONLY=0
FORCE_EXT4=0
for arg in "$@"; do
  case "$arg" in
    --check)      CHECK_ONLY=1 ;;
    --force-ext4) FORCE_EXT4=1 ;;
    -h|--help)    sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PROBLEMS=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  warn  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
step() { printf '\n%s\n' "$*"; }

do_it() { [ "$CHECK_ONLY" = "0" ]; }

# ---------------------------------------------------------------- 1. filesystem

step "1. filesystem"
FSTYPE="$(findmnt -T "$REPO_ROOT" -o FSTYPE -n 2>/dev/null || echo unknown)"
case "$FSTYPE" in
  ext4|btrfs|xfs) ok "checkout is on $FSTYPE" ;;
  9p|drvfs|cifs|v9fs)
    if [ "$FORCE_EXT4" = "1" ]; then
      warn "checkout is on $FSTYPE — SQLite WAL and git worktrees corrupt here (forced)"
    else
      fail "checkout is on $FSTYPE. Clone to the WSL filesystem (e.g. ~/earn) and run"
      fail "  setup there. SQLite WAL and git worktrees are not safe on $FSTYPE."
    fi
    ;;
  *) warn "could not determine the filesystem of $REPO_ROOT (findmnt said '$FSTYPE')" ;;
esac

# ---------------------------------------------------------------- 1b. timezone
#
# ops/crontab requires the Gulf clock and, until now, NOTHING in the documented setup
# either set it or checked it: the requirement lived in a comment in the generated file
# and in one prose aside in CLAUDE.md, while `Environment=TZ=Asia/Dubai` in the systemd
# units sets a process environment and pins no schedule at all — a guard that only looks
# like one. cron and any systemd timer evaluate their expressions in the distro
# timezone, and ops/healthcheck.py forces Gulf before croniter, so a UTC host makes the
# watchdog expect nav_job/backup/maintenance/review/daily_review/research four hours
# early, rerun each one detached with an alert, and then run it again when cron really
# fires. Blocking, because it is silent and it duplicates LLM spend every single day.

step "1b. timezone"

# config/earn.yaml meta.display_timezone is the single source; the literal is only the
# answer for a checkout with no venv yet (ops/gen_ops_files.py reads the same key).
configured_tz() {
  local py="$REPO_ROOT/.venv/bin/python"
  if [ -x "$py" ] && "$py" -c 'from ops.config import load_config
print(load_config().meta.display_timezone)' 2>/dev/null; then
    return 0
  fi
  echo "Asia/Dubai"
}

host_tz() {
  local tz=""
  tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  if [ -z "$tz" ] && [ -L /etc/localtime ]; then
    tz="$(readlink -f /etc/localtime | sed 's#.*/zoneinfo/##')"
  fi
  printf '%s' "$tz"
}

WANT_TZ="$(configured_tz)"
WANT_TZ="${WANT_TZ:-Asia/Dubai}"
HOST_TZ="$(host_tz)"
if [ -z "$HOST_TZ" ]; then
  warn "could not read the distro timezone (no timedatectl, no /etc/localtime symlink)"
  warn "  it must be $WANT_TZ — every schedule in this repo is Gulf time"
elif [ "$HOST_TZ" = "$WANT_TZ" ]; then
  ok "distro timezone is $HOST_TZ"
else
  fail "distro timezone is '$HOST_TZ', config/earn.yaml meta.display_timezone is '$WANT_TZ'."
  fail "  Fix it with:  sudo timedatectl set-timezone $WANT_TZ"
  fail "  (then: .venv/bin/python -m ops.gen_ops_files --install --yes)"
fi

# ---------------------------------------------------------------- 2. directories

step "2. runtime directories"
if [ "$STATE_ROOT" != "$REPO_ROOT" ]; then
  ok "state root: $STATE_ROOT (\$EARN_STATE_ROOT)"
  warn "ops/docker-compose.yml mounts ../data, ../journal, ../knowledge, ../proposals"
  warn "  and ./killdir from the CHECKOUT — symlink them at the state root, or the"
  warn "  containers read a different tree from the jobs."
else
  ok "state root: the checkout (\$EARN_STATE_ROOT unset)"
fi

# Resolved against the STATE root: this is where ops.lib.paths and ops.db look.
STATE_DIRS=(
  logs
  ops/locks
  ops/killdir
  data
  knowledge/state
  knowledge/briefs
  journal/snapshots
  proposals/pending
  proposals/approved
  proposals/shadow
  changes
  reports/daily
  var
  var/state
  var/runtime
)
# Resolved against the CHECKOUT: ops/docker-compose.yml mounts ../ft_userdata/<s> by a
# path relative to the compose file, so these two must exist beside the compose file.
REPO_DIRS=(
  ft_userdata/a/runs
  ft_userdata/b/runs
)

check_dir() {
  local path="$1" label="$2"
  if [ -d "$path" ]; then
    ok "$label"
  elif do_it; then
    mkdir -p "$path" && ok "$label (created)"
  else
    warn "$label missing"
  fi
}

for d in "${STATE_DIRS[@]}"; do
  if [ "$STATE_ROOT" = "$REPO_ROOT" ]; then
    check_dir "$STATE_ROOT/$d" "$d"
  else
    check_dir "$STATE_ROOT/$d" "$STATE_ROOT/$d"
  fi
done
for d in "${REPO_DIRS[@]}"; do
  check_dir "$REPO_ROOT/$d" "$d"
done
if do_it; then
  chmod 700 "$STATE_ROOT/var" "$STATE_ROOT/var/state" "$STATE_ROOT/var/runtime" \
    2>/dev/null || true
  ok "var/ tightened to 0700"
fi

# ---------------------------------------------------------------- 3. .env + secrets

step "3. .env and generated secrets"
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  if do_it; then
    cp "$REPO_ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok ".env created from .env.example (0600)"
  else
    warn ".env missing"
  fi
else
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  ok ".env present (0600)"
fi

# docker compose interpolates ops/.env relative to the compose file.
if [ -L "$REPO_ROOT/ops/.env" ]; then
  ok "ops/.env symlink"
elif [ -e "$REPO_ROOT/ops/.env" ]; then
  fail "ops/.env exists and is not a symlink — remove it, it must point at ../.env"
elif do_it; then
  ln -s ../.env "$REPO_ROOT/ops/.env" && ok "ops/.env -> ../.env (created)"
else
  warn "ops/.env symlink missing"
fi

# A generated secret is written exactly once, and never printed.
gen_secret() {
  local key="$1"
  local current
  current="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
  current="${current%$'\r'}"
  if [ -n "$current" ]; then
    ok "$key present (unchanged)"
    return
  fi
  if ! do_it; then
    warn "$key empty"
    return
  fi
  local value
  value="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = open(path, encoding="utf-8").read().splitlines()
out = [f"{key}={value}" if ln.split("=", 1)[0].strip() == key else ln for ln in lines]
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
  ok "$key generated (32 bytes, not shown)"
}

for key in EARN_CONSOLE_SECRET EARN_CONSOLE_TOKEN EARN_APPROVAL_KEY; do
  gen_secret "$key"
done

# ---------------------------------------------------------------- 4. venv

step "4. python environment"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  ok ".venv present ($("$REPO_ROOT/.venv/bin/python" -V 2>&1))"
  if "$REPO_ROOT/.venv/bin/python" -c 'import ops.config' 2>/dev/null; then
    ok "the project imports inside the venv"
  elif do_it; then
    "$REPO_ROOT/.venv/bin/pip" install -q -e '.[dev]' && ok "project installed (editable)"
  else
    warn "the project is not installed in the venv (pip install -e '.[dev]')"
  fi
elif do_it; then
  python3 -m venv "$REPO_ROOT/.venv"
  "$REPO_ROOT/.venv/bin/pip" install -q --upgrade pip
  "$REPO_ROOT/.venv/bin/pip" install -q -e '.[dev]'
  ok ".venv created and populated"
else
  warn ".venv missing"
fi

# ---------------------------------------------------------------- 5. databases

step "5. databases"
if [ -x "$REPO_ROOT/.venv/bin/python" ] && do_it; then
  "$REPO_ROOT/.venv/bin/python" -m ops.init_dbs && ok "journal.db and earn.db at schema head"
else
  warn "skipped (no venv or --check)"
fi

# ---------------------------------------------------------------- 6. git identity

step "6. git"
if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(git -C "$REPO_ROOT" config user.email || true)" ]; then
    ok "git identity: $(git -C "$REPO_ROOT" config user.name) <$(git -C "$REPO_ROOT" config user.email)>"
  else
    fail "git user.email is unset — an automated commit would fail. Set it with:"
    fail "  git config user.name 'Your Name' && git config user.email you@example.com"
  fi
else
  warn "not a git checkout"
fi

# ---------------------------------------------------------------- 7. ops files

step "7. generated ops files"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  if "$REPO_ROOT/.venv/bin/python" -m ops.gen_ops_files --check >/dev/null 2>&1; then
    ok "ops/crontab and the systemd units match the config"
  else
    warn "ops files drift — run: .venv/bin/python -m ops.gen_ops_files --write"
  fi
  echo "  next: .venv/bin/python -m ops.gen_ops_files --install --yes"
else
  warn "skipped (no venv)"
fi

step "summary"
if [ "$PROBLEMS" -gt 0 ]; then
  echo "  $PROBLEMS blocking problem(s) — fix them before going anywhere near live."
  exit 1
fi
echo "  host is set up."
