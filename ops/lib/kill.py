"""Kill-switch semantics: ops/killdir/KILL — presence is the switch, content is the reason.

Created only by a human (shell ``touch`` or the Telegram /kill confirm). Checked,
uncached, by: the gate (refuse entries), research/review/apply_changes (exit early),
and healthcheck (cancel open orders + stopbuy + alert). Removal is human-only.
"""

from __future__ import annotations

from pathlib import Path

from ops.config import REPO_ROOT, EarnConfig


def kill_path(cfg: EarnConfig, root: Path | None = None) -> Path:
    return (root or REPO_ROOT) / cfg.risk.kill_file


def is_engaged(cfg: EarnConfig, root: Path | None = None) -> bool:
    return kill_path(cfg, root).exists()


def engage(cfg: EarnConfig, reason: str, root: Path | None = None) -> Path:
    p = kill_path(cfg, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(reason.strip() + "\n")
    return p


def reason(cfg: EarnConfig, root: Path | None = None) -> str | None:
    p = kill_path(cfg, root)
    try:
        return p.read_text().strip()
    except OSError:
        return None
