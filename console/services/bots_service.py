"""What the two Freqtrade containers are doing, and the four things an operator may tell them.

Reads compose one picture per sleeve out of ``/ping``, ``/show_config``, ``/status``,
``/balance`` and ``/locks``, and they are all best effort: a bot that is down produces
``{"up": false, "error": …}`` rather than a 500, because the Bots panel exists precisely to
show you that a bot is down.

Writes are the opposite — each one is a single explicit call whose result is returned
verbatim, so an operator never has to guess whether ``forceexit`` reached the bot. The one
action that touches the host rather than the bot (``restart``) goes through the ops lock and
``ops.lib.compose``, because recreating a container beside a running mode transition is
exactly the interleaving that lock exists to prevent.

Every endpoint of ``BotApi`` that F0a has not added yet is reached through its request
helpers behind :func:`_call`, so this module keeps working whichever version of the client
is installed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ops.config import EarnConfig
from ops.lib import compose as composelib
from ops.lib import mode_state as ms
from ops.lib import oplock, paths

LOCK_TIMEOUT_S = 30.0


class BotActionError(RuntimeError):
    pass


BotFactory = Callable[[EarnConfig, str], Any]


def _get(bot: Any, path: str) -> Any:
    helper = getattr(bot, "_get", None)
    if helper is None:
        raise BotActionError("bot client has no GET helper")
    return helper(path)


def _post(bot: Any, path: str, payload: Mapping[str, Any] | None = None) -> Any:
    helper = getattr(bot, "_post", None)
    if helper is None:
        raise BotActionError("bot client has no POST helper")
    return helper(path, dict(payload) if payload else None)


def _delete(bot: Any, path: str) -> Any:
    helper = getattr(bot, "_delete", None)
    if helper is None:
        raise BotActionError("bot client has no DELETE helper")
    return helper(path)


# --------------------------------------------------------------------------- reads


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a panel that shows "down" beats a 500
        return default


def status(
    cfg: EarnConfig,
    factory: BotFactory,
    sleeve: str,
    *,
    conn: sqlite3.Connection | None = None,
    state: ms.ModeState | None = None,
) -> dict[str, Any]:
    """One sleeve's whole picture. Never raises; ``up=False`` carries the reason."""
    sleeve = sleeve.lower()
    st = state if state is not None else ms.load()
    sl = st.sleeve(sleeve)
    out: dict[str, Any] = {
        "sleeve": sleeve,
        "service": cfg.ops.bots[sleeve].service,  # type: ignore[index]
        "mode_state": sl.state,
        "submode": sl.submode,
        "run_id": sl.run_id,
        "expected_strategy": getattr(cfg.sleeves, sleeve).strategy,
        "up": False,
    }
    try:
        bot = factory(cfg, sleeve)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    out["up"] = bool(_safe(bot.ping, False))
    if not out["up"]:
        out["error"] = "no response to /ping"
        return out

    show = _safe(lambda: _get(bot, "show_config"), {}) or {}
    trades = _safe(lambda: bot.status(), []) or []
    balance = _safe(lambda: bot.balance(), {}) or {}
    locks = _safe(lambda: _get(bot, "locks"), {}) or {}
    health = _safe(lambda: bot.health(), None)

    out.update(
        {
            "dry_run": show.get("dry_run"),
            "strategy": show.get("strategy"),
            "bot_name": show.get("bot_name"),
            "state": show.get("state"),
            "version": show.get("version"),
            "timeframe": show.get("timeframe"),
            "stoploss_on_exchange": (show.get("order_types") or {}).get("stoploss_on_exchange"),
            "open_trades": len(trades),
            "trades": [
                {
                    "trade_id": t.get("trade_id"),
                    "pair": t.get("pair"),
                    "amount": t.get("amount"),
                    "stake_amount": t.get("stake_amount"),
                    "open_rate": t.get("open_rate"),
                    "current_rate": t.get("current_rate"),
                    "profit_abs": t.get("profit_abs"),
                    "profit_ratio": t.get("profit_ratio"),
                    "has_open_orders": bool(t.get("open_order_id") or t.get("has_open_orders")),
                }
                for t in trades
                if isinstance(t, Mapping)
            ],
            "balance": {
                "total": balance.get("total"),
                "currencies": [
                    {"currency": c.get("currency"), "free": c.get("free"),
                     "balance": c.get("balance")}
                    for c in (balance.get("currencies") or [])
                    if isinstance(c, Mapping)
                ],
            },
            "locks": locks.get("locks", locks if isinstance(locks, list) else []),
            "last_process_ts": (health or {}).get("last_process_ts") if health else None,
        }
    )
    if out.get("strategy") and out["strategy"] != out["expected_strategy"]:
        out["warning"] = f"running {out['strategy']}, config says {out['expected_strategy']}"
    if sl.is_live and out.get("dry_run") is True:
        out["warning"] = "mode says LIVE but the bot is in dry-run"
    return out


def all_status(
    cfg: EarnConfig,
    factory: BotFactory,
    *,
    conn: sqlite3.Connection | None = None,
    sleeves: Sequence[str] = paths.SLEEVES,
) -> list[dict[str, Any]]:
    state = ms.load()
    return [status(cfg, factory, s, conn=conn, state=state) for s in sleeves]


# --------------------------------------------------------------------------- writes


def stop_entries(cfg: EarnConfig, factory: BotFactory, sleeve: str) -> dict[str, Any]:
    bot = factory(cfg, sleeve.lower())
    for name in ("stopentry", "stopbuy"):
        method = getattr(bot, name, None)
        if callable(method):
            return {"sleeve": sleeve, "result": method()}
    return {"sleeve": sleeve, "result": _post(bot, "stopentry")}


def start(cfg: EarnConfig, factory: BotFactory, sleeve: str) -> dict[str, Any]:
    return {"sleeve": sleeve, "result": _post(factory(cfg, sleeve.lower()), "start")}


def force_exit(
    cfg: EarnConfig, factory: BotFactory, sleeve: str, trade_id: str = "all"
) -> dict[str, Any]:
    return {
        "sleeve": sleeve,
        "result": factory(cfg, sleeve.lower()).forceexit(str(trade_id)),
    }


def cancel_order(
    cfg: EarnConfig, factory: BotFactory, sleeve: str, trade_id: int
) -> dict[str, Any]:
    return {
        "sleeve": sleeve,
        "result": factory(cfg, sleeve.lower()).cancel_open_order(int(trade_id)),
    }


def delete_lock(
    cfg: EarnConfig, factory: BotFactory, sleeve: str, lock_id: int
) -> dict[str, Any]:
    """Freqtrade's own pair lock, removed through ``DELETE /locks/<id>``."""
    return {"sleeve": sleeve, "result": _delete(factory(cfg, sleeve.lower()), f"locks/{lock_id}")}


def restart(
    cfg: EarnConfig,
    sleeve: str,
    *,
    root: Any,
    runner: composelib.Runner | None = None,
    runtime_dir: Any = None,
    lock_path: Any = None,
    timeout_s: float = LOCK_TIMEOUT_S,
) -> dict[str, Any]:
    """Recreate one container under the ops lock, with the live overlay re-rendered."""
    sleeve = sleeve.lower()
    service = cfg.ops.bots[sleeve].service  # type: ignore[index]
    with oplock.acquire(f"bot.restart:{sleeve}", timeout_s=timeout_s, path=lock_path):
        composelib.write_live_file(cfg, root=root, runtime_dir=runtime_dir)
        compose = composelib.Compose(cfg, root=root, runtime_dir=runtime_dir, runner=runner)
        result = compose.up([service])
    return {"sleeve": sleeve, "service": service, "ok": result.ok, "detail": result.output[:2000]}


__all__ = [
    "BotActionError",
    "BotFactory",
    "all_status",
    "cancel_order",
    "delete_lock",
    "force_exit",
    "restart",
    "start",
    "status",
    "stop_entries",
]
