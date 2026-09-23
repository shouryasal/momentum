"""``python -m runs.watch {once|status|show}`` — the holdings watcher.

``once`` is the cron entry point: one cycle over every open position, one ``watch_events``
row each. It is idempotent and safe to rerun; a cycle that finds nothing open exits 0 and
says so. It is designed to be wrapped exactly like the scanner —
``flock -n ops/locks/cron-watch.lock timeout <deadline> ops/envwrap.sh signals -- …`` —
so a slow cycle can never overlap the next one.

``status`` prints the last few events without touching a model, which is what you want at
2am. ``show`` renders the prompt for one holding and prints its token count, so the budget
can be checked without spending anything.

Exit codes: 0 for a completed cycle (escalations are not failures — they are the product),
1 for a cycle that could not run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from ops import db as earn_db
from ops.config import load_config
from ops.lib import paths as earn_paths


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runs.watch", description="Earn holdings watcher")
    sub = p.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="one watch cycle over every open position")
    once.add_argument("--json", action="store_true", help="print the full cycle report")
    st = sub.add_parser("status", help="recent watch events, no model call")
    st.add_argument("--limit", type=int, default=10)
    show = sub.add_parser("show", help="render one holding's prompt and print its size")
    show.add_argument("--pair", default=None, help="default: the first open position")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    root = earn_paths.state_root()

    if args.command == "status":
        return _status(cfg, root, args.limit)
    if args.command == "show":
        return _show(cfg, root, args.pair)

    from runs.watch.runner import run_once

    report = run_once(cfg, root=root, now=datetime.now(UTC))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps({
            "cycle_id": report.cycle_id, "holdings": report.holdings,
            "checked": report.checked, "model_calls": report.model_calls,
            "escalations": report.escalations, "schema_invalid": report.schema_invalid,
            "schema_valid_rate": report.schema_valid_rate, "notes": report.notes,
        }, sort_keys=True))
        for r in report.results:
            print(f"  {r.sleeve}:{r.pair:<10} {r.kind:<18} {r.severity:<8} "
                  f"tok={r.prompt_tokens} lat={r.latency_ms}ms "
                  f"model={r.model_alias or '-'} :: {r.reason[:110]}")
    return 0


def _status(cfg, root, limit: int) -> int:
    from runs.watch.guard import HandRaiseOnly

    path = earn_db.journal_path(cfg, root)
    if not path.is_file():
        print("no journal database yet")
        return 1
    with earn_db.opened(path, readonly=True) as raw:
        conn = HandRaiseOnly(raw)
        try:
            rows = conn.execute(
                "SELECT ts_utc, sleeve, pair, kind, severity, state, confidence,"
                " escalated, prompt_tokens, latency_ms, model_alias, reason"
                " FROM watch_events ORDER BY ts_utc DESC LIMIT ?", (int(limit),)
            ).fetchall()
        except Exception as e:  # noqa: BLE001 — a missing table means it never ran
            print(f"no watch events yet ({e})")
            return 0
    for row in rows:
        print(json.dumps({k: row[k] for k in row.keys()}, sort_keys=True, default=str))
    return 0


def _show(cfg, root, pair: str | None) -> int:
    from runs.watch import headlines as news_mod
    from runs.watch import invalidation as inval_mod
    from runs.watch import positions as pos_mod
    from runs.watch import thesis as thesis_mod
    from runs.watch.guard import HandRaiseOnly
    from runs.watch.prompt import render

    now = datetime.now(UTC)
    jraw = earn_db.connect(earn_db.journal_path(cfg, root))
    kpath = earn_db.knowledge_path(cfg, root)
    kdb = earn_db.connect(kpath, readonly=True) if kpath.is_file() else None
    conn = HandRaiseOnly(jraw)
    try:
        holdings = pos_mod.open_holdings(cfg, kdb=kdb, jdb=conn.raw_for_reads, root=root,
                                         now=now)
        if pair:
            holdings = [h for h in holdings if h.pair == pair]
        if not holdings:
            print("no open positions" + (f" for {pair}" if pair else ""))
            return 1
        h = holdings[0]
        told = thesis_mod.thesis_for(conn.raw_for_reads, h.base, h.pair)
        verdict = inval_mod.evaluate(told.invalidation, h.facts())
        raw = news_mod.collect(kdb, h.base, window_min=cfg.watch.news.window_min,
                               limit=cfg.watch.news.max_headlines, now=now,
                               include_market_wide=cfg.watch.news.include_market_wide)
        news = news_mod.cluster(raw, base_url=None, model=cfg.watch.news.embed_model,
                                threshold=float(cfg.watch.news.cluster_threshold),
                                token_threshold=float(cfg.watch.news.token_threshold),
                                limit=int(cfg.watch.news.max_headlines))
        rendered = render(cfg, h, told, news, verdict, root=root)
        print(rendered.text)
        print(f"\n--- {h.pair}: {rendered.tokens} est. tokens, {rendered.chars} chars,"
              f" {len(news.items)} of {news.considered} headlines"
              f" ({news.method}) ---")
    finally:
        if kdb is not None:
            kdb.close()
        jraw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
