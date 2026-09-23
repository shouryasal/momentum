"""Scanner loop, persistence and the planner handoff (spec §2.1 steps 3-7, 11-12).

``scan()`` is one cycle: features → detectors → dedupe → rows → fast path → screener →
score → detached validations. ``plan()`` is the handoff: a validated signal asks
``TriggerEngine.guards()`` — **the** guard implementation, not a copy — and, if nothing
blocks, spawns ``research_run --signal-id <id>``, which is the only way a signal reaches a
proposal. ``signals.planner.enabled: false`` keeps everything up to the verdict and stops
there, which is the observe-only rollout.

Nothing in this module talks to an exchange, and nothing here decides a trade: the
deterministic gate in ``strategies/riskgate.py`` still stands between a proposal and an
order.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from runs.common import gulf_now, run_id_for, utc_iso
from runs.signals import detectors as detectorslib
from runs.signals import screener as screenerlib
from runs.signals.features import Features
from runs.signals.features import build as build_features

__all__ = [
    "PlanResult",
    "ScanReport",
    "dedupe_key",
    "funnel",
    "mark_acted",
    "on_ingest",
    "plan",
    "record_manual",
    "scan",
    "signal_id_for",
]

FAST_PATH_DETECTORS = ("near_stop",)


# --------------------------------------------------------------------------- reports


@dataclass
class ScanReport:
    scan_id: str
    candidates: int = 0
    new: list[str] = field(default_factory=list)
    duplicates: int = 0
    fast_path: list[str] = field(default_factory=list)
    screened: list[str] = field(default_factory=list)
    screened_out: list[str] = field(default_factory=list)
    hallucinations: int = 0
    screen_ok: bool = False
    screen_provider: str | None = None
    screen_model: str | None = None
    spawned: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class PlanResult:
    signal_id: str
    fired: bool
    status: str
    reasons: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    run_id: str | None = None


# --------------------------------------------------------------------------- keys


def dedupe_key(candidate: detectorslib.Candidate, *, now: datetime,
               dedupe_minutes: int) -> str:
    """``detector:pair:direction:bucket`` — the whole dedupe rule, in one place."""
    minutes = int(now.timestamp() // 60)
    bucket = minutes // max(int(dedupe_minutes), 1)
    return f"{candidate.dedupe_base}:{bucket}"


def signal_id_for(candidate: detectorslib.Candidate, *, now: datetime,
                  taken: set[str]) -> str:
    base = (candidate.pair.split("/")[0].lower() if candidate.pair else "mkt")
    stem = f"sig-{now.strftime('%Y%m%dT%H%MZ')}-{base}-{candidate.detector}"
    if stem not in taken:
        return stem
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def _scan_id(now: datetime) -> str:
    return f"scan-{now.strftime('%Y%m%dT%H%M%SZ')}"


# --------------------------------------------------------------------------- spawn


def _detached(cmd: list[str], root: Path) -> None:
    subprocess.Popen(cmd, cwd=root, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _spawn_validation(cfg: EarnConfig, signal_id: str, root: Path, spawn) -> None:
    spawn([
        "flock", "-n", str(root / "ops" / "locks" / "validate.lock"),
        "timeout", str(int(cfg.signals.validator.deadline_s)),
        "bash", str(root / "ops" / "envwrap.sh"), "signals", "--",
        sys.executable, "-m", "runs.signals", "validate", "--signal-id", signal_id,
    ])


# --------------------------------------------------------------------------- scan


def _is_fast_path(cfg: EarnConfig, candidate: detectorslib.Candidate) -> bool:
    det = cfg.signals.scanner.detectors
    if candidate.detector == "near_stop":
        return bool(det.near_stop.fast_path)
    if candidate.detector == "news_event":
        return candidate.detail.get("event_class") in set(det.news_event.fast_path_events)
    return False


def _insert(jdb: sqlite3.Connection, *, signal_id: str, scan_id: str,
            candidate: detectorslib.Candidate, features: Features, now: datetime,
            key: str, fast: bool) -> None:
    payload = {
        "detector": candidate.detector,
        "detail": candidate.detail,
        "cited": {k: features.flat().get(k) for k in candidate.feature_keys},
        "pair": candidate.pair,
    }
    jdb.execute(
        "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
        " direction, detector_score, screen_score, strength, features_json,"
        " news_refs_json, dedupe_key, fast_path, status, updated_utc)"
        " VALUES (?,?,?,'detector',?,?,?,?,NULL,?,?,?,?,?,?,?)",
        (signal_id, utc_iso(now), scan_id, candidate.detector, candidate.pair,
         candidate.direction, candidate.strength, candidate.strength,
         json.dumps(payload, sort_keys=True, default=str),
         json.dumps(list(candidate.news_hashes)) if candidate.news_hashes else None,
         key, int(fast), "valid" if fast else "candidate", utc_iso(now)))


def scan(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection, *,
         root: Path | None = None, now: datetime | None = None,
         models_cfg: Any | None = None, spawn=None, runner=None,
         screen_fn=None, plan_fast_path: bool = True) -> ScanReport:
    """One scanner cycle. Never raises: every failure is a field on the report."""
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)
    spawn = spawn or (lambda cmd: _detached(cmd, root))
    scfg = cfg.signals.scanner
    report = ScanReport(scan_id=_scan_id(now))

    features = build_features(kdb, cfg, now=now, jdb=jdb, root=root)
    ctx = detectorslib.Ctx(cfg=cfg, features=features, kdb=kdb, jdb=jdb, root=root, now=now)
    candidates = detectorslib.run_all(ctx)
    report.candidates = len(candidates)
    report.errors.update(ctx.errors)

    taken = {r["signal_id"] for r in jdb.execute(
        "SELECT signal_id FROM signals WHERE ts_utc >= ?",
        (utc_iso(now - timedelta(days=2)),))}
    rows: dict[str, detectorslib.Candidate] = {}
    for candidate in candidates:
        key = dedupe_key(candidate, now=now, dedupe_minutes=scfg.dedupe_minutes)
        seen = jdb.execute("SELECT 1 FROM signals WHERE dedupe_key=? LIMIT 1",
                           (key,)).fetchone()
        if seen is not None:
            report.duplicates += 1
            continue
        sid = signal_id_for(candidate, now=now, taken=taken)
        taken.add(sid)
        fast = _is_fast_path(cfg, candidate)
        _insert(jdb, signal_id=sid, scan_id=report.scan_id, candidate=candidate,
                features=features, now=now, key=key, fast=fast)
        report.new.append(sid)
        rows[sid] = candidate
        if fast:
            report.fast_path.append(sid)
    jdb.commit()

    to_screen = [sid for sid in report.new if sid not in report.fast_path]
    to_screen = to_screen[:scfg.max_candidates_per_cycle]
    if to_screen:
        payload = [_candidate_payload(sid, rows[sid]) for sid in to_screen]
        screen_call = screen_fn or screenerlib.screen
        kwargs: dict[str, Any] = {"models_cfg": models_cfg, "root": root}
        if runner is not None:
            kwargs["runner"] = runner
        outcome = screen_call(cfg, payload, features, **kwargs)
        report.screen_ok = outcome.ok
        report.hallucinations = outcome.hallucinations
        report.screen_provider = outcome.provider
        report.screen_model = outcome.model
        for sid in to_screen:
            status, reason = _apply_score(cfg, jdb, sid, rows[sid], outcome, now)
            (report.screened if status == "screened" else report.screened_out).append(sid)
            if status == "screened":
                _spawn_validation(cfg, sid, root, spawn)
                report.spawned.append(sid)
            elif reason:
                report.errors.setdefault(sid, reason)
        jdb.commit()

    if plan_fast_path:
        for sid in report.fast_path:
            result = plan(cfg, jdb, kdb, sid, root=root, now=now, spawn=spawn)
            if result.fired:
                report.planned.append(sid)

    from runs.signals import validator as validatorlib

    report.expired = validatorlib.expire_stale(cfg, jdb, now=now)
    return report


def _candidate_payload(signal_id: str, candidate: detectorslib.Candidate) -> dict[str, Any]:
    return {
        "signal_id": signal_id, "detector": candidate.detector, "pair": candidate.pair,
        "direction": candidate.direction, "detector_score": round(candidate.strength, 4),
        "reason": candidate.reason, "feature_keys": list(candidate.feature_keys),
        "news_hashes": list(candidate.news_hashes), "detail": candidate.detail,
    }


def _apply_score(cfg: EarnConfig, jdb: sqlite3.Connection, signal_id: str,
                 candidate: detectorslib.Candidate, outcome, now: datetime
                 ) -> tuple[str, str | None]:
    scfg = cfg.signals.scanner
    detector_score = float(candidate.strength)
    item = outcome.items.get(signal_id) if outcome.ok else None
    reason: str | None = None
    if item is None:
        # the screener is down, dropped this item, or never answered for it: the
        # deterministic score stands alone, and the row says so.
        score = detector_score
        screen_score = None
        reason = (outcome.dropped.get(signal_id) if outcome.ok else
                  f"screen_down:{outcome.failure}") or "screen_missing"
        reason = f"screen_unavailable({reason})"
    elif not item.keep:
        score = detector_score * scfg.scoring.detector_weight + \
            item.score * scfg.scoring.screen_weight
        screen_score = item.score
        reason = "screener:keep=false"
    else:
        screen_score = item.score
        score = (detector_score * scfg.scoring.detector_weight
                 + item.score * scfg.scoring.screen_weight)
    status = "screened" if (score >= scfg.screen.min_score and
                            (item is None or item.keep)) else "screened_out"
    jdb.execute(
        "UPDATE signals SET screen_score=?, strength=?, screen_provider=?,"
        " screen_model=?, screen_rationale=?, status=?, status_reason=?, updated_utc=?"
        " WHERE signal_id=?",
        (screen_score, round(score, 6), outcome.provider, outcome.model,
         item.rationale if item else None, status, reason, utc_iso(now), signal_id))
    return status, reason


# --------------------------------------------------------------------------- planner


def _planner_limits(cfg: EarnConfig) -> tuple[int, int]:
    """(cooldown_hours, max_per_day) from ``signals.planner``, legacy keys as fallback."""
    planner = cfg.signals.planner
    cooldown = planner.cooldown_hours if planner.cooldown_hours is not None \
        else (cfg.triggers.cooldown_hours or 0)
    cap = planner.max_per_day if planner.max_per_day is not None \
        else (cfg.triggers.max_per_day or 0)
    return int(cooldown), int(cap)


def _verdict_ok(cfg: EarnConfig, verdict: str | None) -> bool:
    allowed = {"valid"} if cfg.signals.planner.min_verdict == "valid" \
        else {"valid", "uncertain"}
    return verdict in allowed


def plan(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection,
         signal_id: str, *, root: Path | None = None, now: datetime | None = None,
         spawn=None) -> PlanResult:
    """Hand a validated signal to the planner, through the SAME guards as a trigger."""
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)
    row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
    if row is None:
        return PlanResult(signal_id=signal_id, fired=False, status="error",
                          blocked=["unknown_signal"])
    reasons = [_reason_of(row)]

    if not cfg.signals.planner.enabled:
        _mark(jdb, signal_id, "valid", "observe_only", now)
        return PlanResult(signal_id=signal_id, fired=False, status="valid",
                          reasons=reasons, blocked=["planner_disabled"])
    if row["status"] not in ("valid", "uncertain"):
        return PlanResult(signal_id=signal_id, fired=False, status=row["status"],
                          reasons=reasons, blocked=["status"])
    if not _verdict_ok(cfg, row["status"]):
        return PlanResult(signal_id=signal_id, fired=False, status=row["status"],
                          reasons=reasons, blocked=["min_verdict"])

    from runs.triggers import TriggerEngine

    engine = TriggerEngine(cfg, jdb, kdb, root=root, now=now,
                           spawn=spawn or (lambda cmd: _detached(cmd, root)))
    blocked = engine.guards()
    if blocked:
        jdb.execute(
            "UPDATE signals SET status='blocked', status_reason=?, blocked_json=?,"
            " updated_utc=? WHERE signal_id=?",
            (",".join(blocked), json.dumps(blocked), utc_iso(now), signal_id))
        jdb.commit()
        engine.journal(False, reasons, blocked, None, signal_ids=[signal_id])
        return PlanResult(signal_id=signal_id, fired=False, status="blocked",
                          reasons=reasons, blocked=blocked)

    run_id = engine.fire(reasons, signal_id=signal_id)
    jdb.execute(
        "UPDATE signals SET status='planned', status_reason=NULL, run_id=?,"
        " updated_utc=? WHERE signal_id=?", (run_id, utc_iso(now), signal_id))
    jdb.commit()
    engine.journal(True, reasons, [], run_id, signal_ids=[signal_id])
    return PlanResult(signal_id=signal_id, fired=True, status="planned",
                      reasons=reasons, run_id=run_id)


def _reason_of(row: sqlite3.Row) -> str:
    try:
        payload = json.loads(row["features_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    detail = payload.get("detail") or {}
    if row["detector"] == "news_event" and detail.get("event_class"):
        return f"news:{detail['event_class']}"
    if row["detector"] == "near_stop":
        return "near_stop"
    pair = (row["pair"] or "").split("/")[0] or "MKT"
    return f"{row['detector']}:{pair}:{row['direction']}"


def _mark(jdb: sqlite3.Connection, signal_id: str, status: str, reason: str | None,
          now: datetime) -> None:
    jdb.execute("UPDATE signals SET status=?, status_reason=?, updated_utc=?"
                " WHERE signal_id=?", (status, reason, utc_iso(now), signal_id))
    jdb.commit()


def mark_acted(jdb: sqlite3.Connection, signal_id: str, proposal_run_id: str, *,
               now: datetime | None = None) -> None:
    """Called by ``research_run`` once a proposal carrying this signal is on disk."""
    jdb.execute(
        "UPDATE signals SET status='acted', proposal_run_id=?, updated_utc=?"
        " WHERE signal_id=?", (proposal_run_id, utc_iso(now or datetime.now(UTC)),
                               signal_id))
    jdb.commit()


# --------------------------------------------------------------------------- entry points


def on_ingest(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection, *,
              root: Path | None = None, now: datetime | None = None,
              spawn=None, models_cfg: Any | None = None) -> ScanReport | None:
    """Ingest's hook. Returns ``None`` when the pipeline is off or not wired in."""
    if not cfg.signals.enabled or cfg.signals.integration != "pipeline":
        return None
    if not cfg.signals.scanner.run_after_ingest:
        return None
    return scan(cfg, jdb, kdb, root=root, now=now, spawn=spawn, models_cfg=models_cfg)


def record_manual(cfg: EarnConfig, jdb: sqlite3.Connection, *, pair: str | None,
                  direction: str, note: str, actor: str,
                  now: datetime | None = None) -> str:
    """A human-injected signal (console ``POST /api/signals/manual``).

    It enters as ``screened`` so it takes the SAME validator and guard path as a detected
    one — a human may start the process, never skip it.
    """
    now = now or datetime.now(UTC)
    base = (pair.split("/")[0].lower() if pair else "mkt")
    sid = f"sig-{now.strftime('%Y%m%dT%H%M%SZ')}-{base}-manual"
    payload = {"detector": "manual", "detail": {"note": note, "actor": actor},
               "cited": {}, "pair": pair}
    jdb.execute(
        "INSERT INTO signals(signal_id, ts_utc, scan_id, source, detector, pair,"
        " direction, detector_score, screen_score, strength, features_json,"
        " news_refs_json, dedupe_key, fast_path, status, status_reason, updated_utc)"
        " VALUES (?,?,?,'manual','manual',?,?,?,NULL,?,?,NULL,?,0,'screened',?,?)",
        (sid, utc_iso(now), f"manual-{utc_iso(now)}", pair, direction, 1.0, 1.0,
         json.dumps(payload, sort_keys=True), f"manual:{pair or '-'}:{direction}:{sid}",
         f"manual:{actor}", utc_iso(now)))
    jdb.commit()
    return sid


def funnel(jdb: sqlite3.Connection, *, hours: int = 24,
           now: datetime | None = None) -> dict[str, int]:
    """detected → screened → validated → valid → planned → acted, for the UI."""
    now = now or datetime.now(UTC)
    since = utc_iso(now - timedelta(hours=hours))
    counts = {r["status"]: int(r["n"]) for r in jdb.execute(
        "SELECT status, COUNT(*) AS n FROM signals WHERE ts_utc >= ? GROUP BY status",
        (since,))}
    detected = sum(counts.values())
    terminal = ("screened", "validating", "valid", "invalid", "uncertain", "blocked",
                "planned", "acted", "expired")
    screened = sum(counts.get(s, 0) for s in terminal)
    validated = sum(counts.get(s, 0) for s in
                    ("valid", "invalid", "uncertain", "blocked", "planned", "acted"))
    valid = sum(counts.get(s, 0) for s in ("valid", "blocked", "planned", "acted"))
    return {
        "detected": detected,
        "screened": screened,
        "validated": validated,
        "valid": valid,
        "planned": counts.get("planned", 0) + counts.get("acted", 0),
        "acted": counts.get("acted", 0),
        "screened_out": counts.get("screened_out", 0),
        "expired": counts.get("expired", 0),
        "errors": counts.get("error", 0),
    }


def next_run_id(now: datetime | None = None) -> str:
    """The run id a signal-fired research run would take (Gulf slot of ``now``)."""
    g = gulf_now(now)
    return run_id_for(g.strftime("%H%M"), now)
