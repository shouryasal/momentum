"""One command, the whole decision story: `python -m runs.trace <run_id>`.

Reconstructs a decision end-to-end from the journal, snapshots and lessons —
inputs (sha-verified snapshot) -> prompt/model/effort/auth -> proposal +
validation -> Sleeve B consumption -> gate decisions -> orders/fills -> TCA ->
outcome vs rules/BTC -> process grade + rubric -> root causes -> lessons citing
it -> what-if context. Writes reports/trace/<slug>.md.

None-tolerant by design: every section renders "(not recorded)" for missing
pieces, so it works on any historical run_id. NO model calls — cheap enough for
the daily review to bulk-generate one per wrong decision.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from evals import snapshot as snapshotlib
from ops import db
from ops.config import REPO_ROOT, EarnConfig, load_config

NR = "(not recorded)"


def _fmt(value, suffix: str = "") -> str:
    if value is None or value == "":
        return NR
    return f"{value}{suffix}"


def _row(jdb: sqlite3.Connection, sql: str, args=()) -> sqlite3.Row | None:
    try:
        return jdb.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def _rows(jdb: sqlite3.Connection, sql: str, args=()) -> list[sqlite3.Row]:
    try:
        return jdb.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _json_block(text: str | None) -> list[str]:
    if not text:
        return [NR]
    try:
        return ["```json", json.dumps(json.loads(text), indent=2), "```"]
    except (json.JSONDecodeError, TypeError):
        return [str(text)]


def _signal_section(jdb: sqlite3.Connection, run_id: str, run) -> list[str]:
    """The tiered-signal story behind a triggered run: detector, screener, validator.

    A scheduled run has no signal and the section says so in one line, so the trace shape
    stays the same whichever way a decision was reached.
    """
    L = ["## 2b · Signal that fired this run", ""]
    signal_id = None
    try:
        signal_id = run["signal_id"] if run is not None else None
    except (IndexError, KeyError):
        signal_id = None
    sig = None
    if signal_id:
        sig = _row(jdb, "SELECT * FROM signals WHERE signal_id=?", (signal_id,))
    if sig is None:
        sig = _row(jdb, "SELECT * FROM signals WHERE run_id=? OR proposal_run_id=?"
                        " ORDER BY ts_utc DESC LIMIT 1", (run_id, run_id))
    if sig is None:
        return [*L, "Scheduled run — no signal.", ""]
    L += [f"- signal: `{sig['signal_id']}` ({sig['detector']}"
          f" · {_fmt(sig['pair'])} · {_fmt(sig['direction'])})",
          f"- detected: {_fmt(sig['ts_utc'])} · scan `{_fmt(sig['scan_id'])}`"
          f" · fast path: {bool(sig['fast_path'])}",
          f"- detector score: {_fmt(sig['detector_score'])}"
          f" · screen score: {_fmt(sig['screen_score'])}"
          f" · combined: {_fmt(sig['strength'])}",
          f"- screener: {_fmt(sig['screen_provider'])} / {_fmt(sig['screen_model'])}"
          f" — {_fmt(sig['screen_rationale'])}",
          f"- status: {sig['status']} ({_fmt(sig['status_reason'])})"]
    if sig["blocked_json"]:
        L.append(f"- guards blocked: {sig['blocked_json']}")
    val = _row(jdb, "SELECT * FROM signal_validations WHERE signal_id=?"
                    " ORDER BY id DESC LIMIT 1", (sig["signal_id"],))
    if val is None:
        L += ["- validator: (not run)", ""]
        return L
    L += [f"- validator: {_fmt(val['provider'])} / {_fmt(val['model'])}"
          f" — **{val['verdict']}** at confidence {_fmt(val['confidence'])}"
          f" (escalated: {bool(val['escalated'])})",
          f"- thesis: {_fmt(val['thesis'])}",
          "- reasons:", *_json_block(val["reasons_json"]),
          "- counter-evidence:", *_json_block(val["counter_evidence_json"]),
          f"- invalidation: {_fmt(val['invalidation'])}",
          f"- horizon: {_fmt(val['horizon_hours'], 'h')}"
          f" · outcome: {_fmt(val['outcome_ret'], '%')}"
          f" · hit: {_fmt(val['outcome_hit'])}",
          f"- evidence pack: `{_fmt(val['pack_path'])}`", ""]
    return L


def build_trace(cfg: EarnConfig, jdb: sqlite3.Connection, run_id: str,
                root: Path | None = None) -> str:
    root = root or REPO_ROOT
    L: list[str] = [f"# Decision trace — {run_id}", ""]

    # ---- 1 inputs
    L += ["## 1 · Inputs (decision snapshot)", ""]
    try:
        snap = snapshotlib.read_snapshot(run_id, root=root)
        manifest = json.loads((snap.path / "manifest.json").read_text())
        L += ["sha256-verified snapshot at `" + str(snap.path) + "`:", ""]
        for fname, sha in sorted(manifest["files"].items()):
            L.append(f"- `{fname}` — `{sha[:16]}…`")
        L += ["", f"- git commit at decision time: `{snap.meta.git_commit}`",
              f"- token budget: {snap.meta.token_budget}"]
    except snapshotlib.SnapshotError as e:
        L.append(f"{NR} — {e}")
    L.append("")

    # ---- 2 model call
    L += ["## 2 · Model call", ""]
    run = _row(jdb, "SELECT * FROM runs WHERE run_id=? AND stage='decide'", (run_id,))
    if run:
        L += [f"- prompt version: {_fmt(run['prompt_version'])}",
              f"- requested model: {_fmt(run['requested_model'])}"
              f" · served: {_fmt(run['served_model'])}",
              f"- effort: {_fmt(run['effort'])} · auth: {_fmt(run['auth_source'])}"
              " ('none' = subscription)",
              f"- escalated: {bool(run['escalated'])}"
              f" ({_fmt(run['escalation_reasons'])})",
              f"- triggered by: {_fmt(run['trigger_reason'])}",
              f"- status: {run['status']} ({_fmt(run['error'])})",
              f"- cost estimate: ${run['cost_usd'] or 0:.2f}"
              f" · turns: {_fmt(run['num_turns'])}"]
    else:
        L.append(NR)
    shadow_run = _row(jdb, "SELECT * FROM runs WHERE run_id=? AND stage='decide_shadow'",
                      (run_id,))
    if shadow_run:
        L.append(f"- shadow call: {_fmt(shadow_run['served_model'])}"
                 f" -> {shadow_run['status']}")
    outputs = snapshotlib.snapshots_dir(root) / snapshotlib.slug_for(run_id) / "outputs"
    if outputs.exists():
        L.append("- raw responses: " + ", ".join(
            f"`{p.name}`" for p in sorted(outputs.iterdir())))
    L.append("")

    # ---- 2b the signal that fired this run, if any
    L += _signal_section(jdb, run_id, run)

    # ---- 3 proposal
    L += ["## 3 · Proposal & validation", ""]
    prop = _row(jdb, "SELECT * FROM proposals WHERE run_id=? AND shadow=0", (run_id,))
    if prop:
        L += [f"- module: {_fmt(prop['module'])} · abstain: {_fmt(prop['abstain'])}"
              f" · confidence: {_fmt(prop['confidence'])}"
              f" · horizon: {_fmt(prop['horizon_days'], 'd')}",
              f"- exposure scale: {_fmt(prop['exposure_scale'])}",
              f"- valid: {bool(prop['valid'])} ({_fmt(prop['invalid_reason'])})",
              f"- invalidation condition: {_fmt(prop['invalidation'])}",
              f"- hard-case flags: {_fmt(prop['hard_case_flags_json'])}",
              "- targets:", *_json_block(prop["targets_json"]),
              "- rationale:", *_json_block(prop["rationale_json"])]
    else:
        L.append(NR)
    L.append("")

    # ---- 4 consumption
    L += ["## 4 · Sleeve B consumption", ""]
    if prop:
        L.append(f"- {_fmt(prop['consumed_status'])}"
                 f" at {_fmt(prop['consumed_at'])}"
                 f" ({_fmt(prop['consumed_reason'])})")
    else:
        L.append(NR)
    L.append("")

    # ---- 5+6 gate, orders, fills, TCA
    L += ["## 5 · Gate, orders, fills, TCA", ""]
    orders = _rows(jdb, "SELECT * FROM orders WHERE proposal_run_id=? ORDER BY ts_utc",
                   (run_id,))
    if not orders:
        L.append("no orders reference this proposal (dead-band hold, abstain,"
                 " rejection — or not recorded)")
    for o in orders:
        gate = _row(jdb, "SELECT * FROM gate_decisions WHERE id=?",
                    (o["gate_decision_id"],)) if o["gate_decision_id"] else None
        gate_s = (f"gate #{gate['id']} {('ALLOWED' if gate['allowed'] else 'REJECTED')}"
                  f" [{gate['reason']}]" if gate else f"gate {NR}")
        L.append(f"- order #{o['id']} {o['ts_utc']} {o['side']} {o['pair']}"
                 f" {_fmt(o['amount'])} @ {_fmt(o['price'])} ({o['status']}) — {gate_s}")
        for f in _rows(jdb, "SELECT * FROM fills WHERE order_id=?", (o["id"],)):
            tca = _row(jdb, "SELECT * FROM tca_fill_costs WHERE fill_id=?", (f["id"],))
            tca_s = (f"{tca['total_bps']:.1f} bps total"
                     if tca and tca["total_bps"] is not None else f"TCA {NR}")
            L.append(f"  - fill #{f['id']} {f['fill_amount']} @ {f['fill_price']}"
                     f" — {tca_s}")
    L.append("")

    # ---- 7 outcome + 8 grade
    L += ["## 6 · Outcome & grade", ""]
    g = _row(jdb, "SELECT * FROM decision_grades WHERE run_id=?", (run_id,))
    if g:
        L += [f"- vs rules sleeve: {_fmt(g['outcome_vs_rules_bps'], ' bps')}"
              f" · vs BTC: {_fmt(g['outcome_vs_btc_bps'], ' bps')}"
              f" · outcome: {_fmt(g['outcome_grade'])}"
              f" (resolved {_fmt(g['outcome_resolved_at'])})",
              f"- process grade: {g['process_grade']}/100"
              f" (graded {g['graded_at']}, week {g['review_week']},"
              f" by {g['grader_model']})",
              "- rubric:", *_json_block(g["process_rubric_json"])]
    else:
        L.append(f"not graded yet {NR}")
    L.append("")

    # ---- 9 root causes
    L += ["## 7 · Root causes", ""]
    causes = _rows(jdb, "SELECT * FROM root_cause_events WHERE ref=?"
                        " OR evidence_json LIKE ?", (run_id, f"%{run_id}%"))
    if causes:
        for c in causes:
            L.append(f"- [{c['review_week']}] {c['kind']} -> cause={c['cause']}"
                     f" (recurrence key `{c['recurrence_key']}`,"
                     f" fix: {c['fix_path']})")
    else:
        L.append("none recorded")
    L.append("")

    # ---- 10 lessons
    L += ["## 8 · Lessons citing this run", ""]
    lessons_path = root / "lessons.md"
    cited = []
    if lessons_path.exists():
        block: list[str] = []
        for line in lessons_path.read_text().splitlines():
            if line.startswith("## "):
                if any(run_id in b for b in block):
                    cited.append(block[0])
                block = [line]
            else:
                block.append(line)
        if block and any(run_id in b for b in block):
            cited.append(block[0])
    L += [f"- {c}" for c in cited] or ["none"]
    L.append("")

    # ---- 11 what-if
    L += ["## 9 · What-if context", ""]
    wi = _row(jdb, "SELECT * FROM whatif_nav WHERE last_proposal_run_id=?"
                   " ORDER BY date_utc LIMIT 1", (run_id,))
    if wi:
        L.append(f"- first what-if NAV under this proposal: {wi['nav_usdt']:.2f}"
                 f" USDT on {wi['date_utc']} (turnover {_fmt(wi['turnover'])},"
                 f" cost {_fmt(wi['cost_usdt'])} USDT)")
    else:
        L.append(f"what-if simulation {NR}")
    L.append("")
    return "\n".join(L)


def write_trace(cfg: EarnConfig, jdb: sqlite3.Connection, run_id: str,
                root: Path | None = None) -> Path:
    root = root or REPO_ROOT
    out = root / "reports" / "trace" / f"{snapshotlib.slug_for(run_id)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_trace(cfg, jdb, run_id, root=root))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m runs.trace <run_id>", file=sys.stderr)
        return 2
    cfg = load_config()
    with db.connect(REPO_ROOT / cfg.paths.journal_db) as jdb:
        p = write_trace(cfg, jdb, argv[0])
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
