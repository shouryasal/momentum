"""Kill-switch semantics: ops/killdir/KILL — presence is the switch, content is the reason.

Created only by a human (shell ``touch``, the console's ``POST /api/kill``, or the
Telegram /kill confirm) and by ``ops/healthcheck.py`` when a bot contradicts the signed
mode file. Checked, uncached, by: the gate (refuse entries), research/review/apply_changes
(exit early), and healthcheck. Removal is human-only.

**The switch never waits for the ops lock.** Writing the file is the first thing
:func:`engage_and_enforce` does, so the switch is on even when every bot is unreachable
and even while a stuck transition holds ``ops/locks/ops.lock``; telling the bots is
best-effort afterwards and is reported per sleeve.

The root defaults to :func:`ops.lib.paths.state_root`, which is the checkout unless
``$EARN_STATE_ROOT`` says otherwise — so a worktree session or a test can never write a
KILL file into (or read one out of) the live checkout by accident.

``root`` is therefore the **state** root, never the checkout. Every caller that hands one
in passes ``paths.state_root()`` (the console) or its own ``state_root`` attribute, which
is derived from it (``TriggerEngine``, ``Healthcheck``, ``ResearchRun``, ``ReviewRun``,
``DailyReview``, ``Maintenance``). Those six used to pass ``ops.config.REPO_ROOT``, so on
any host with ``$EARN_STATE_ROOT`` set the human could engage the switch from the console
and every scheduled job would keep trading: they were reading a different file.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ops.config import EarnConfig
from ops.lib import paths


def kill_path(cfg: EarnConfig, root: Path | None = None) -> Path:
    return (root or paths.state_root()) / cfg.risk.kill_file


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


# --------------------------------------------------------------------------- enforcement


def stop_entries(bot: Any) -> str:
    """Stop new entries on one bot. ``stopentry`` where it exists, else ``stopbuy``.

    ``ops.lib.freqtrade_api.BotApi`` has ``stopentry`` (with its own ``stopbuy``
    fallback), so this only still probes for the older name because tests and the
    Telegram bot inject their own minimal clients.
    """
    for name in ("stopentry", "stopbuy"):
        method = getattr(bot, name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return str(result.get("status", result))
            return str(result)
    raise RuntimeError("bot client exposes neither stopentry nor stopbuy")


def cancel_open_entry_orders(bot: Any) -> list[str]:
    """Cancel the open entry order of every open trade; one line per attempt.

    An entry order that is still resting is capital about to be committed, so it goes
    before anything else. Exits are deliberately left alone.
    """
    out: list[str] = []
    status = getattr(bot, "status", None)
    cancel = getattr(bot, "cancel_open_order", None)
    if not callable(status) or not callable(cancel):
        return out
    for trade in status() or []:
        if not isinstance(trade, Mapping):
            continue
        trade_id = trade.get("trade_id")
        if trade_id is None or not trade.get("open_order_id"):
            continue
        try:
            cancel(int(trade_id))
            out.append(f"cancelled entry order on trade {trade_id}")
        except Exception as e:  # noqa: BLE001 - one unreachable bot never stops the rest
            out.append(f"trade {trade_id}: {e}")
    return out


def enforce(
    cfg: EarnConfig,
    *,
    bot_factory: Callable[[EarnConfig, str], Any],
    flatten: bool = False,
    sleeves: tuple[str, ...] = paths.SLEEVES,
) -> list[dict[str, Any]]:
    """Tell every bot the switch is on. Returns ``[{sleeve, ok, detail}, ...]``.

    Never raises: a bot that is down must not undo a KILL that is already written, and
    the operator needs to see *which* bot could not be reached rather than a 500.
    """
    results: list[dict[str, Any]] = []
    for sleeve in sleeves:
        lines: list[str] = []
        ok = True
        try:
            bot = bot_factory(cfg, sleeve)
            lines.append(f"stopentry: {stop_entries(bot)}")
            lines.extend(cancel_open_entry_orders(bot))
            if flatten:
                forceexit = getattr(bot, "forceexit", None)
                if callable(forceexit):
                    lines.append(f"forceexit: {forceexit('all')}")
                else:  # pragma: no cover - BotApi always has forceexit
                    ok = False
                    lines.append("forceexit: unsupported by this bot client")
        except Exception as e:  # noqa: BLE001
            ok = False
            lines.append(f"{type(e).__name__}: {e}")
        results.append({"sleeve": sleeve, "ok": ok, "detail": "; ".join(lines) or "no action"})
    return results


def engage_and_enforce(
    cfg: EarnConfig,
    reason_text: str,
    *,
    root: Path | None = None,
    bot_factory: Callable[[EarnConfig, str], Any],
    flatten: bool = False,
    sleeves: tuple[str, ...] = paths.SLEEVES,
) -> tuple[Path, list[dict[str, Any]]]:
    """Write the KILL file, then stop the bots. File first, always.

    The ordering is the guarantee: if the process dies between the two, the switch is
    still on and the gate refuses every entry on the next candle.
    """
    path = engage(cfg, reason_text, root)
    return path, enforce(cfg, bot_factory=bot_factory, flatten=flatten, sleeves=sleeves)


__all__ = [
    "cancel_open_entry_orders",
    "enforce",
    "engage",
    "engage_and_enforce",
    "is_engaged",
    "kill_path",
    "reason",
    "stop_entries",
]
