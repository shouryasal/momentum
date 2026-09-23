"""The single inbound channel: a long-polling Telegram command bot (one chat, one
user; no webhook is ever registered, no listening socket — outbound HTTPS only; a
test asserts this file never calls the webhook API). Freqtrade's own Telegram stays
disabled; runbook actions relay to the bots' localhost REST.

Command handlers are pure functions (cfg, deps) -> reply text, so tests exercise
them without Telegram. run_bot() is the thin python-telegram-bot wiring.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import flags as flagslib
from ops.lib import kill as killlib
from ops.lib.freqtrade_api import BotApi, FreqtradeApiError


def authorized(cfg: EarnConfig, chat_id: int, user_id: int) -> bool:
    return (cfg.telegram.chat_id != 0 and chat_id == cfg.telegram.chat_id
            and user_id == cfg.telegram.user_id)


# ------------------------------------------------------------------ handlers

def cmd_status(cfg: EarnConfig, jdb, apis: dict[str, BotApi], now: datetime) -> str:
    lines = [f"Earn status {now.strftime('%Y-%m-%d %H:%M')}Z — phase {cfg.phase}"]
    for sleeve, api in apis.items():
        lines.append(f"  bot {sleeve}: {'up' if api.ping() else 'DOWN'}")
    navs = jdb.execute("SELECT sleeve, nav_usdt FROM nav_daily WHERE date_utc ="
                       " (SELECT MAX(date_utc) FROM nav_daily)").fetchall()
    for r in navs:
        lines.append(f"  NAV {r['sleeve']}: {r['nav_usdt']:.0f}")
    try:
        active = flagslib.active_flags(REPO_ROOT / cfg.paths.flags_file, now)
        lines.append(f"  flags: {', '.join(sorted(active)) or 'none'}")
    except flagslib.FlagsError:
        lines.append("  flags: UNREADABLE")
    if killlib.is_engaged(cfg):
        lines.append("  KILL ENGAGED")
    return "\n".join(lines)


def cmd_flags(cfg: EarnConfig, now: datetime) -> str:
    try:
        active = flagslib.active_flags(REPO_ROOT / cfg.paths.flags_file, now)
    except flagslib.FlagsError as e:
        return f"flags unreadable: {e}"
    if not active:
        return "no active flags"
    return "\n".join(f"{n}: {f['severity']} ({f['reason']}, by {f['set_by']})"
                     for n, f in sorted(active.items()))


def cmd_tca(jdb) -> str:
    rows = jdb.execute("SELECT sleeve, window, n_fills, total_bps_med FROM tca_rolling"
                       " WHERE day = (SELECT MAX(day) FROM tca_rolling)"
                       " ORDER BY sleeve, window").fetchall()
    if not rows:
        return "no TCA rows yet"
    return "\n".join(f"{r['sleeve']} {r['window']}: {r['total_bps_med']:.1f} bps"
                     f" ({r['n_fills']} fills)" for r in rows
                     if r["total_bps_med"] is not None) or "no reconciled fills"


def cmd_forceexit(apis: dict[str, BotApi], sleeve: str, tradeid: str) -> str:
    api = apis.get(sleeve)
    if api is None:
        return f"unknown sleeve {sleeve!r} (a|b)"
    try:
        api.forceexit(tradeid)
        return f"forceexit {tradeid} sent to sleeve {sleeve}"
    except FreqtradeApiError as e:
        return f"forceexit failed: {e}"


def cmd_pause(apis: dict[str, BotApi], sleeve: str) -> str:
    api = apis.get(sleeve)
    if api is None:
        return f"unknown sleeve {sleeve!r} (a|b)"
    try:
        api.stopbuy()
        return f"sleeve {sleeve} paused (no new entries; exits still active)"
    except FreqtradeApiError as e:
        return f"pause failed: {e}"


def cmd_kill(cfg: EarnConfig, reason: str) -> str:
    p = killlib.engage(cfg, reason or "engaged via /kill")
    return f"KILL engaged ({p}). Healthcheck cancels open orders within 5 min."


def cmd_unfreeze(cfg: EarnConfig, now: datetime) -> str:
    ok = flagslib.clear_flag(REPO_ROOT / cfg.paths.flags_file, "tier1_freeze",
                             by="human", now=now)
    return "tier1_freeze cleared" if ok else "tier1_freeze was not active"


def cmd_mode(cfg: EarnConfig) -> str:
    """Per-sleeve mode, straight from the signed file (unverified reads as TEST)."""
    from ops.lib import mode_state as ms

    state = ms.load()
    lines = [f"mode: {ms.describe(state)}"]
    for sleeve in ("a", "b"):
        sl = state.sleeve(sleeve)
        seed = f"{sl.seed_usdt:g}" if sl.seed_usdt is not None else "?"
        lines.append(
            f"  {sleeve}: {sl.state}"
            + (f"·{sl.submode}" if sl.submode else "")
            + f" seed {seed} run {sl.run_id or '-'}"
        )
    return "\n".join(lines)


def cmd_pending(cfg: EarnConfig, jdb, now: datetime) -> str:
    """Proposals waiting on a human, with the time left on each."""
    from runs import approvals

    rows = approvals.pending(jdb, cfg, now=now, limit=10)
    waiting = [r for r in rows if r["status"] == approvals.STATUS_PENDING]
    if not waiting:
        return "no proposals awaiting approval"
    return "\n".join(
        f"{r['run_id']} — {r['seconds_left'] // 60} min left"
        f" ({'abstain' if r['abstain'] else 'targets'})"
        for r in waiting
    )


def cmd_proposal_decision(cfg: EarnConfig, jdb, run_id: str, decision: str,
                          now: datetime, note: str | None = None) -> str:
    """``/approve <run_id>`` and ``/reject <run_id>`` — the live propose-mode gate.

    Signs the approval with ``$EARN_APPROVAL_KEY`` and writes it where the in-container
    proposal loader can verify it; the console shows the same row.
    """
    from ops.lib import audit
    from runs import approvals

    try:
        decided = approvals.decide(
            jdb, cfg, run_id=run_id, decision=decision, actor=audit.actor_telegram(),
            channel="telegram", note=note, now=now,
        )
    except approvals.ApprovalError as e:
        return f"{decision} failed: {e}"
    if decision == "approve":
        return (
            f"proposal {run_id} APPROVED — valid until {decided.expires_utc}"
            f" ({decided.path.name if decided.path else 'no file'})"
        )
    return f"proposal {run_id} rejected"


def cmd_approve(cfg: EarnConfig, jdb, args: list[str], user_id: int,
                now: datetime) -> str:
    """``/approve <run_id>`` for a proposal, ``/approve change <id>`` for a change."""
    if not args:
        return "usage: /approve <proposal run_id> | /approve change <change_id>"
    if args[0] == "change":
        if len(args) < 2:
            return "usage: /approve change <change_id>"
        return cmd_approval(jdb, "change", args[1], "approve", user_id)
    return cmd_proposal_decision(cfg, jdb, args[0], "approve", now,
                                 " ".join(args[1:]) or None)


def cmd_reject(cfg: EarnConfig, jdb, args: list[str], user_id: int,
               now: datetime) -> str:
    if not args:
        return "usage: /reject <proposal run_id> | /reject change <change_id>"
    if args[0] == "change":
        if len(args) < 2:
            return "usage: /reject change <change_id>"
        return cmd_approval(jdb, "change", args[1], "reject", user_id)
    return cmd_proposal_decision(cfg, jdb, args[0], "reject", now,
                                 " ".join(args[1:]) or None)


def cmd_approval(jdb, kind: str, ref: str, decision: str, user_id: int) -> str:
    jdb.execute("INSERT INTO approvals(ts_utc, kind, ref, decision, by_user)"
                " VALUES (?,?,?,?,?)",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), kind, ref,
                 decision, user_id))
    jdb.commit()
    return f"{decision} recorded for {kind} {ref}"


# ------------------------------------------------------------------ PTB wiring

def run_bot() -> int:  # pragma: no cover — needs a live token; logic is tested above
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

    cfg = load_config()
    apis = {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")}
    jdb = db.connect(REPO_ROOT / cfg.paths.journal_db)

    def guard(handler):
        async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            msg = update.effective_message
            if (msg is None or update.effective_chat is None
                    or update.effective_user is None
                    or not authorized(cfg, update.effective_chat.id,
                                      update.effective_user.id)):
                return  # drop silently
            await msg.reply_text(handler(update, ctx))
        return wrapped

    now = lambda: datetime.now(UTC)  # noqa: E731
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("status", guard(lambda u, c: cmd_status(cfg, jdb, apis, now()))))
    app.add_handler(CommandHandler("flags", guard(lambda u, c: cmd_flags(cfg, now()))))
    app.add_handler(CommandHandler("tca", guard(lambda u, c: cmd_tca(jdb))))
    app.add_handler(CommandHandler("forceexit", guard(
        lambda u, c: cmd_forceexit(apis, *(c.args + ["all"])[:2]) if c.args
        else "usage: /forceexit <a|b> [tradeid|all]")))
    app.add_handler(CommandHandler("pause", guard(
        lambda u, c: cmd_pause(apis, c.args[0]) if c.args else "usage: /pause <a|b>")))
    app.add_handler(CommandHandler("kill", guard(
        lambda u, c: cmd_kill(cfg, " ".join(c.args)))))
    app.add_handler(CommandHandler("unfreeze", guard(lambda u, c: cmd_unfreeze(cfg, now()))))
    app.add_handler(CommandHandler("mode", guard(lambda u, c: cmd_mode(cfg))))
    app.add_handler(CommandHandler("pending", guard(
        lambda u, c: cmd_pending(cfg, jdb, now()))))
    app.add_handler(CommandHandler("approve", guard(
        lambda u, c: cmd_approve(cfg, jdb, list(c.args), u.effective_user.id, now()))))
    app.add_handler(CommandHandler("reject", guard(
        lambda u, c: cmd_reject(cfg, jdb, list(c.args), u.effective_user.id, now()))))
    app.add_handler(MessageHandler(filters.COMMAND, guard(lambda u, c: "unknown command")))
    # Long polling ONLY — never a webhook, never a listening socket.
    app.run_polling(allowed_updates=["message"])
    return 0


if __name__ == "__main__":
    sys.exit(run_bot())
