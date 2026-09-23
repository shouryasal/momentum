"""The ONE place that knows where Earn's files live.

Two roots, deliberately separate:

``REPO_ROOT``
    the checkout this code was imported from. Source, committed config, SQL, prompts.
    A review/daily session runs inside a git *worktree*, so ``REPO_ROOT`` there is the
    worktree, not the live checkout.

``state_root()``
    the live data root: ``$EARN_STATE_ROOT`` when set, otherwise ``REPO_ROOT``. Databases,
    ``var/``, locks, logs, proposals and knowledge state live under it. Worktree sessions
    get ``EARN_STATE_ROOT`` pointing at the live root so they read and write the real data
    while editing tier-1 files in their own checkout.

Everything machine-local and mode-dependent lives under ``var/`` (0700, gitignored):

    var/state/mode.json            signed per-sleeve mode (ops.lib.mode_state)
    var/state/config.bless.json    signed digest of the protected config (ops.lib.config_guard)
    var/runtime/freqtrade-<s>.mode.json   freqtrade overlay, second --config
    var/runtime/runtime-<s>.json          what the strategy reads via $EARN_RUNTIME
    var/runtime/compose.override.yml      exchange credentials by env reference only

This module imports nothing from the rest of the repo, so every other module may import it.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

STATE_ROOT_ENV = "EARN_STATE_ROOT"
WORKTREE_ENV = "EARN_WORKTREE"
LIVE_ROOT_ENV = "EARN_LIVE_ROOT"
RUNTIME_ENV = "EARN_RUNTIME"
AUTOMATED_RUN_ENV = "EARN_AUTOMATED_RUN"

DIR_MODE = 0o700
FILE_MODE = 0o600

SLEEVES = ("a", "b")


def state_root(env: dict[str, str] | None = None) -> Path:
    """The live data root: ``$EARN_STATE_ROOT`` when set, else the checkout root."""
    raw = (env if env is not None else os.environ).get(STATE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return REPO_ROOT


def in_worktree(env: dict[str, str] | None = None) -> bool:
    """True when this process runs inside a review/daily git worktree."""
    e = env if env is not None else os.environ
    return bool(e.get(WORKTREE_ENV)) or state_root(e) != REPO_ROOT


def data_path(rel: str | Path, env: dict[str, str] | None = None) -> Path:
    """Resolve a repo-relative data path (``paths.*`` in earn.yaml) against the state root.

    Absolute paths are returned untouched so an operator can point a DB elsewhere.
    """
    p = Path(rel).expanduser()
    return p if p.is_absolute() else state_root(env) / p


def var_dir(env: dict[str, str] | None = None) -> Path:
    return state_root(env) / "var"


def state_dir(env: dict[str, str] | None = None) -> Path:
    return var_dir(env) / "state"


def runtime_dir(env: dict[str, str] | None = None) -> Path:
    return var_dir(env) / "runtime"


def logs_dir(env: dict[str, str] | None = None) -> Path:
    return state_root(env) / "logs"


def locks_dir(env: dict[str, str] | None = None) -> Path:
    return state_root(env) / "ops" / "locks"


def mode_state_path(env: dict[str, str] | None = None) -> Path:
    return state_dir(env) / "mode.json"


def bless_path(env: dict[str, str] | None = None) -> Path:
    return state_dir(env) / "config.bless.json"


def ops_lock_path(env: dict[str, str] | None = None) -> Path:
    return locks_dir(env) / "ops.lock"


def mode_overlay_path(sleeve: str, env: dict[str, str] | None = None) -> Path:
    """The freqtrade overlay config passed as the second ``--config``."""
    return runtime_dir(env) / f"freqtrade-{sleeve.lower()}.mode.json"


def sleeve_runtime_path(sleeve: str, env: dict[str, str] | None = None) -> Path:
    """What the strategy reads through ``$EARN_RUNTIME``."""
    return runtime_dir(env) / f"runtime-{sleeve.lower()}.json"


def compose_override_path(env: dict[str, str] | None = None) -> Path:
    return runtime_dir(env) / "compose.override.yml"


def ft_run_db(run_id: str, sleeve: str) -> str:
    """The in-container sqlite URL for one test/live run (freqtrade ``db_url``)."""
    return f"sqlite:////freqtrade/user_data/runs/{run_id}.sqlite"


def ensure_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create ``path`` (and parents) and tighten its mode. Idempotent."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except (OSError, NotImplementedError):  # pragma: no cover - non-POSIX filesystems
        pass
    return path


def ensure_var_layout(env: dict[str, str] | None = None) -> dict[str, Path]:
    """Create ``var/``, ``var/state`` and ``var/runtime`` at 0700. Idempotent."""
    out = {
        "var": ensure_dir(var_dir(env)),
        "state": ensure_dir(state_dir(env)),
        "runtime": ensure_dir(runtime_dir(env)),
    }
    return out


def write_private(path: Path, text: str, mode: int = FILE_MODE) -> Path:
    """Atomic write of a 0600 file inside a 0700 directory."""
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(mode)
    except (OSError, NotImplementedError):  # pragma: no cover - non-POSIX filesystems
        pass
    os.replace(tmp, path)
    return path


def is_automated_run(env: dict[str, str] | None = None) -> bool:
    """True inside an unattended Claude run (``EARN_AUTOMATED_RUN=1``)."""
    e = env if env is not None else os.environ
    return e.get(AUTOMATED_RUN_ENV, "") not in ("", "0", "false", "False")
