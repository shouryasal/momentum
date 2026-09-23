"""Read model for the Self-Improvement page, and the autonomy matrix editor.

No FastAPI here. The router is thin; this is where a change becomes something a human can
judge: the git diff, the checks the gate ran, and — the point of v2 — the model's **claimed**
evidence side by side with what ``evals/verify_change.py`` **recomputed**, with every
mismatch over 10% called out.

Executing anything (approve, reject, revert, attach) belongs to
:mod:`runs.apply_changes`, which runs in the live checkout under the ops lock. This module
never moves the live branch.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ops.config import EarnConfig
from ops.lib import paths
from runs import apply_changes

STATUSES = ("proposed", "verifying", "held", "auto_merged", "approved", "rejected",
            "reverted", "superseded")
OPEN_STATUSES = ("proposed", "verifying", "held")
MISMATCH_PCT = 0.10

_MAX_DIFF_BYTES = 200_000


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else paths.REPO_ROOT


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# --------------------------------------------------------------------------- listing


def list_changes(jdb, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Newest first, optionally filtered by status (``open`` means proposed/verifying/held)."""
    sql = ("SELECT change_id, proposed_at, kind, op, target, status, author_model,"
           " author_run_id, decided_at, decided_by, reason, merge_commit, branch,"
           " revert_of, reverted_by FROM change_log")
    params: list[Any] = []
    if status == "open":
        sql += " WHERE status IN ({})".format(",".join("?" * len(OPEN_STATUSES)))
        params.extend(OPEN_STATUSES)
    elif status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY proposed_at DESC, change_id DESC LIMIT ?"
    params.append(int(limit))
    return [_row_to_dict(r) for r in jdb.execute(sql, params).fetchall()]


def counts_by_status(jdb) -> dict[str, int]:
    rows = jdb.execute("SELECT status, COUNT(*) AS n FROM change_log GROUP BY status")
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    return out


def events(jdb, change_id: str) -> list[dict[str, Any]]:
    rows = jdb.execute(
        "SELECT id, ts_utc, event, actor, commit_sha, note FROM change_events"
        " WHERE change_id=? ORDER BY ts_utc, id", (change_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def diff_for(commit: str | None, *, root: Path | None = None) -> str:
    """The commit's patch, truncated — a diff is context, not a download."""
    if not commit:
        return ""
    r = _git(_root(root), "show", "--patch", "--stat", "--no-color", commit)
    if r.returncode != 0:
        return ""
    text = r.stdout or ""
    if len(text) > _MAX_DIFF_BYTES:
        return text[:_MAX_DIFF_BYTES] + "\n… (diff truncated)\n"
    return text


def evidence_pairs(claimed: Mapping[str, Any] | None,
                   verified: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Flattened claimed-vs-verified rows, each flagged when they disagree by >10%."""
    from evals.verify_change import find_mismatches, flatten

    c = flatten(dict(claimed or {}))
    v = flatten(dict(verified or {}))
    bad = {m["field"]: m for m in find_mismatches(dict(claimed or {}), dict(verified or {}),
                                                  pct=MISMATCH_PCT)}
    rows: list[dict[str, Any]] = []
    for field in sorted(set(c) | set(v)):
        if field.endswith("verified_at"):
            continue
        rows.append({
            "field": field,
            "claimed": c.get(field),
            "verified": v.get(field),
            "mismatch": field in bad,
            "delta_pct": bad.get(field, {}).get("delta_pct"),
        })
    return rows


def get_change(jdb, change_id: str, *, root: Path | None = None) -> dict[str, Any] | None:
    row = jdb.execute("SELECT * FROM change_log WHERE change_id=?", (change_id,)).fetchone()
    if row is None:
        return None
    data = _row_to_dict(row)
    claimed = _loads(data.get("claimed_evidence_json")) or {}
    verified = _loads(data.get("verified_evidence_json")) or {}
    checks = _loads(data.get("checks_json")) or []
    file_path = _root(root) / "changes" / f"{change_id}.json"
    payload = _loads(file_path.read_text(encoding="utf-8")) if file_path.exists() else None
    return {
        **{k: v for k, v in data.items()
           if k not in ("claimed_evidence_json", "verified_evidence_json", "checks_json")},
        "claimed_evidence": claimed,
        "verified_evidence": verified,
        "evidence": evidence_pairs(claimed, verified),
        "mismatch_count": sum(1 for e in evidence_pairs(claimed, verified) if e["mismatch"]),
        "checks": checks,
        "events": events(jdb, change_id),
        "diff": diff_for(data.get("source_commit") or data.get("merge_commit"), root=root),
        "change_file": payload,
        "can_revert": bool(data.get("merge_commit")) and data.get("status") != "reverted",
        "can_decide": data.get("status") in OPEN_STATUSES,
    }


def merge_timeline(jdb, *, limit: int = 200) -> list[dict[str, Any]]:
    """Merges and reverts as points a NAV chart can overlay."""
    rows = jdb.execute(
        "SELECT change_id, ts_utc, event, commit_sha FROM change_events"
        " WHERE event IN ('merged','reverted') ORDER BY ts_utc DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def recurring_causes(jdb, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = jdb.execute(
        "SELECT recurrence_key, COUNT(DISTINCT review_week) AS weeks, COUNT(*) AS events,"
        " MAX(escalated) AS escalated FROM root_cause_events"
        " GROUP BY recurrence_key ORDER BY weeks DESC, events DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- autonomy


def autonomy_matrix(cfg: EarnConfig, *, root: Path | None = None) -> dict[str, Any]:
    """The matrix as the UI shows it, plus the effective mode and this week's cap usage."""
    kinds = {}
    for key in apply_changes.AUTONOMY_KEYS:
        entry = (cfg.autonomy.kinds or {}).get(key)
        kinds[key] = {
            "test": getattr(entry, "test", "approve") if entry else "approve",
            "live": getattr(entry, "live", "approve") if entry else "approve",
        }
    mode = apply_changes.effective_mode(_root(root))
    effective = {key: apply_changes.autonomy_setting(cfg, key, mode) for key in kinds}
    return {
        "tier1_auto_merge": bool(cfg.autonomy.tier1_auto_merge),
        "live_forces_human": bool(cfg.autonomy.live_forces_human),
        "max_auto_merges_per_week": int(cfg.autonomy.max_auto_merges_per_week),
        "auto_revert": {
            "enabled": bool(cfg.autonomy.auto_revert.enabled),
            "window_days": int(cfg.autonomy.auto_revert.window_days),
            "validity_drop_pct": int(cfg.autonomy.auto_revert.validity_drop_pct),
            "breach_increase": int(cfg.autonomy.auto_revert.breach_increase),
        },
        "kinds": kinds,
        "mode": mode,
        "effective": effective,
        "invariants": [
            "a skill_new touching scripts/** is always held for a human",
            "a model promotion is always held for a human",
            "the weekly auto-merge cap is a ceiling, not a target",
        ],
    }


def autonomy_patch(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn the matrix editor's payload into RFC-6902-shaped ops for ``config/earn.yaml``."""
    ops: list[dict[str, Any]] = []
    for key in ("tier1_auto_merge", "live_forces_human", "max_auto_merges_per_week"):
        if key in body:
            ops.append({"op": "replace", "path": f"/autonomy/{key}", "value": body[key]})
    for key, value in (body.get("kinds") or {}).items():
        if key not in apply_changes.AUTONOMY_KEYS:
            raise ValueError(f"unknown change kind: {key}")
        for column in ("test", "live"):
            if column in value:
                if value[column] not in ("auto", "approve", "off"):
                    raise ValueError(f"autonomy[{key}][{column}] must be auto|approve|off")
                ops.append({"op": "replace",
                            "path": f"/autonomy/kinds/{key}/{column}",
                            "value": value[column]})
    for key, value in (body.get("auto_revert") or {}).items():
        if key not in ("enabled", "window_days", "validity_drop_pct", "breach_increase"):
            raise ValueError(f"unknown auto_revert key: {key}")
        ops.append({"op": "replace", "path": f"/autonomy/auto_revert/{key}", "value": value})
    if not ops:
        raise ValueError("nothing to change")
    return ops


def pending_for_approvals(jdb, *, limit: int = 20) -> list[dict[str, Any]]:
    """Held changes, for the header's pending-approvals counter and the approvals queue."""
    rows = jdb.execute(
        "SELECT change_id, kind, op, target, status, reason, proposed_at, author_model"
        " FROM change_log WHERE status='held' ORDER BY proposed_at DESC LIMIT ?",
        (int(limit),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_index(jdb, *, limit: int = 200) -> list[dict[str, str]]:
    """Rows for the Ctrl-K index."""
    out = []
    for r in list_changes(jdb, limit=limit):
        out.append({"kind": "change", "id": r["change_id"],
                    "title": f"{r['kind']}/{r['op']} {r['target']}",
                    "subtitle": f"{r['status']} — {r.get('reason') or ''}"})
    return out


def as_sequence(value: Sequence[str] | None) -> list[str]:
    return list(value or [])
