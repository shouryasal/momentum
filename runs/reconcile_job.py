"""Balance reconciliation job (*/15, and after every live bot start and go-live).

Rebuilds each sleeve's position book from the journalled fills of its active run and
compares it with what the venue says is there. In **live** the venue is Binance; in
**test** it is the bot's own simulated wallet, which is still worth checking because a
disagreement there means the journal and the bot have drifted apart — the same bug class,
caught before real money is involved.

Which of the two is decided by :mod:`ops.lib.mode_view`, never by ``mode_state.load()``.
This job holds ``BINANCE_KEY_A/B`` but *not* ``EARN_CONSOLE_SECRET``, so the signed mode
file never verified here: every live sleeve was silently reconciled against the bot's own
numbers instead of the exchange, and ``baseline_for()`` was skipped, so the one independent
check on real money never ran in the mode that has any. Anything short of a **provable**
TEST now goes to Binance with that sleeve's preflight baseline.

A mismatch above ``risk.reconcile.tolerance_pct`` sets the ``reconcile_mismatch``
block-entries flag (so the gate refuses new entries) and raises a critical alert. Every
comparison is journalled either way.

The exchange and bot clients are injected; :func:`main` is the only thing that builds real
ones, and nothing in the test suite calls it.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config
from ops.lib import mode_state as ms
from ops.lib import mode_view
from ops.lib import reconcile as rec

#: ``(sleeve, live) -> {asset: total}`` — Binance in live, the bot's wallet in test
BalanceSource = Callable[[str, bool], Mapping[str, float]]
#: ``(pair) -> price`` in the quote currency
PriceSource = Callable[[str], float | None]


@dataclass(frozen=True)
class SleeveReconciliation:
    sleeve: str
    run_id: str | None
    status: str
    detail: str
    flagged: bool = False

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def bot_balances(api: Any, *, quote: str = "USDT") -> dict[str, float]:
    """``{asset: total}`` from a freqtrade ``/balance`` payload."""
    out: dict[str, float] = {}
    for row in (api.balance() or {}).get("currencies", []) or []:
        name = str(row.get("currency", "")).upper()
        total = float(row.get("balance", row.get("free", 0.0)) or 0.0)
        if name and total:
            out[name] = total
    out.setdefault(quote.upper(), 0.0)
    return out


def candle_prices(
    kdb: sqlite3.Connection, cfg: EarnConfig, *, tf: str = "1h"
) -> dict[str, float]:
    """Latest closed price per universe asset, from the shared candle store."""
    prices: dict[str, float] = {}
    for asset in cfg.universe.assets:
        pair = f"{asset}/{cfg.universe.quote}"
        row = kdb.execute(
            "SELECT close FROM candles WHERE pair=? AND tf=? AND is_closed=1"
            " ORDER BY close_time DESC LIMIT 1",
            (pair, tf),
        ).fetchone()
        if row is not None and row["close"] is not None:
            prices[asset.upper()] = float(row["close"])
    return prices


def baseline_for(
    jdb: sqlite3.Connection, run_id: str | None, sleeve: str | None = None
) -> dict[str, float]:
    """The pre-existing exchange balances preflight recorded when **this sleeve** armed.

    The query used to have no ``sleeve`` predicate at all, so with both sleeves armed the
    newest completed transition won whichever sleeve it belonged to: sleeve a's baseline
    was subtracted from sleeve b's exchange snapshot and a genuine divergence on one sleeve
    was masked by the other's opening balances. ``ops.preflight`` captures the baseline
    from that sleeve's *own* keys (``binance_check.keys_for(sleeve)``), so the baselines are
    per-sleeve and routinely differ.

    Only ``LIVE_*`` transitions write ``preflight_json``, so a go-test row leaves it NULL
    and cannot poison the lookup; an all-zero baseline is still a real answer and is
    returned as one.
    """
    if not run_id:
        return {}
    import json

    sql = ("SELECT preflight_json FROM mode_transitions WHERE status='completed'"
           " AND preflight_json IS NOT NULL")
    params: tuple[str, ...] = ()
    if sleeve:
        sql += " AND sleeve=?"
        params = (str(sleeve).lower(),)
    rows = jdb.execute(sql + " ORDER BY id DESC LIMIT 20", params).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["preflight_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        baseline = payload.get("baseline") or {}
        if baseline:
            return {str(k).upper(): float(v) for k, v in baseline.items()}
    return {}


def reconcile_sleeve(
    cfg: EarnConfig,
    jdb: sqlite3.Connection,
    *,
    sleeve: str,
    run_id: str | None,
    seed_usdt: float,
    balances: Mapping[str, float],
    prices: Mapping[str, float],
    baseline: Mapping[str, float] | None = None,
    flags_path: Path | str | None = None,
    now: datetime | None = None,
    source: str = "exchange",
    flag_audit_conn: sqlite3.Connection | None = None,
) -> SleeveReconciliation:
    """Compare one sleeve, journal the result and set or clear its flag."""
    ledger = rec.ledger_snapshot(
        jdb, sleeve=sleeve, run_id=run_id, seed_usdt=seed_usdt, quote=cfg.universe.quote
    )
    exchange = rec.exchange_snapshot(balances, quote=cfg.universe.quote, source=source)
    result = rec.compare(
        ledger,
        exchange,
        prices=prices,
        tolerance_pct=cfg.risk.reconcile.tolerance_pct,
        dust_usdt=cfg.risk.reconcile.dust_usdt,
        baseline=baseline,
        quote=cfg.universe.quote,
    )
    rec.record(
        jdb, sleeve=sleeve, run_id=run_id, ledger=ledger, exchange=exchange, result=result,
        ts_utc=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    flagged = False
    if flags_path is not None:
        flagged = rec.apply_flag(
            cfg, result, sleeve=sleeve, flags_path=flags_path, now=now,
            audit_conn=flag_audit_conn,
        )
    return SleeveReconciliation(sleeve, run_id, result.status, result.detail, flagged)


def run(
    cfg: EarnConfig,
    jdb: sqlite3.Connection,
    kdb: sqlite3.Connection,
    *,
    balances: BalanceSource,
    state: ms.ModeState | None = None,
    flags_path: Path | str | None = None,
    now: datetime | None = None,
    alert: Callable[[str, str], None] | None = None,
    sleeves: Sequence[str] = ("a", "b"),
    root: Path | None = None,
) -> list[SleeveReconciliation]:
    st = state if state is not None else ms.load()
    view = mode_view.load(jdb=jdb, state=st, root=root)
    prices = candle_prices(kdb, cfg)
    out: list[SleeveReconciliation] = []
    for sleeve in sleeves:
        run_row = jdb.execute(
            "SELECT * FROM sleeve_runs WHERE sleeve=? AND status='active'"
            " ORDER BY started_utc DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
        if run_row is None:
            continue
        # ``assume_live`` and not ``is_live``: this job is the ONLY independent check on
        # real money, and it is the one job handed BINANCE_KEY_A/B (ops/envwrap.sh) — yet
        # it has no EARN_CONSOLE_SECRET, so ``mode_state.load()`` always said TEST and the
        # ledger was silently reconciled against the *bot's own numbers* with no baseline.
        # Anything short of a proven TEST therefore goes to the exchange.
        live = view.sleeve(sleeve).assume_live
        try:
            venue = balances(sleeve, live)
        except Exception as e:  # noqa: BLE001 - an unreachable venue is a warning row
            out.append(SleeveReconciliation(sleeve, run_row["run_id"], rec.STATUS_ERROR, str(e)))
            continue
        result = reconcile_sleeve(
            cfg, jdb, sleeve=sleeve, run_id=str(run_row["run_id"]),
            seed_usdt=float(run_row["seed_usdt"]), balances=venue, prices=prices,
            baseline=baseline_for(jdb, str(run_row["run_id"]), sleeve) if live else None,
            flags_path=flags_path, now=now, source="binance" if live else "freqtrade",
            flag_audit_conn=kdb,
        )
        out.append(result)
        if result.status == rec.STATUS_MISMATCH and alert is not None:
            alert(
                "critical",
                f"Earn: sleeve {sleeve} ledger/exchange mismatch — {result.detail}",
            )
    return out


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - cron entry point
    from ops.lib import binance_check, locks, tg
    from ops.lib.freqtrade_api import BotApi

    cfg = load_config()
    state = ms.load()
    apis = {s: BotApi.for_sleeve(cfg, s) for s in ("a", "b")}

    def balances(sleeve: str, live: bool) -> Mapping[str, float]:
        if not live:
            return bot_balances(apis[sleeve], quote=cfg.universe.quote)
        client = binance_check.BinanceClient(binance_check.keys_for(sleeve))
        return binance_check.total_balances(
            client.account(), [*cfg.universe.assets, cfg.universe.quote]
        )

    with locks.acquire("reconcile"):
        with db.opened(REPO_ROOT / cfg.paths.journal_db) as jdb, db.opened(
            REPO_ROOT / cfg.paths.knowledge_db
        ) as kdb:
            results = run(
                cfg, jdb, kdb, balances=balances, state=state,
                flags_path=REPO_ROOT / cfg.paths.flags_file,
                alert=lambda sev, text: tg.send(
                    text, sev, dedupe_key="reconcile", ttl_min=cfg.telegram.dedupe_ttl_min,
                    conn=kdb,
                ),
            )
    for r in results:
        print(f"reconcile {r.sleeve} {r.run_id}: {r.status} — {r.detail}")
    return 1 if any(r.status in (rec.STATUS_MISMATCH, rec.STATUS_ERROR) for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "BalanceSource",
    "PriceSource",
    "SleeveReconciliation",
    "baseline_for",
    "bot_balances",
    "candle_prices",
    "main",
    "reconcile_sleeve",
    "run",
]
