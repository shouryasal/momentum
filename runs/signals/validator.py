"""The strong validator — steps 8-10 of the pipeline (spec §2.1).

One screened signal at a time, in a detached process under ``ops/locks/validate.lock``:

1. Python builds a deterministic **evidence pack** and writes it to
   ``journal/snapshots/signals/<signal_id>/pack.json`` — that file is the record of what
   the model was actually shown.
2. ``llm.run_task('validate')`` runs the chain (sonnet → opus) with read-only tools and the
   ``market-state`` / ``asset-dossier`` skills. ``min_tier`` is 3 and the floor lives in
   ``runs.llm.types.MIN_TIER_FLOOR``: no config edit and no tier-1 overlay can route a
   validation to a local model.
3. The answer is validated host-side, every cited feature key is checked against the pack,
   and a ``signal_validations`` row is written whatever the verdict.

The signal becomes actionable only when ``verdict == 'valid'`` **and**
``confidence >= signals.validator.min_confidence`` **and**
``suggested.direction != 'hold'``. Acting is still the planner's decision, and the planner
still asks ``TriggerEngine.guards()``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.config import REPO_ROOT, EarnConfig
from runs.common import atomic_write_json, utc_iso
from runs.signals import run_ctx_for, run_task, stage_prompt_text
from runs.signals.features import Features
from runs.signals.features import build as build_features
from schemas.signals import SignalInvalid, Validation, validate_validation, validation_schema

__all__ = [
    "ValidationOutcome",
    "evidence_pack",
    "expire_stale",
    "pack_path",
    "render_prompt",
    "validate_signal",
]


@dataclass
class ValidationOutcome:
    """The result of one validation attempt, including the reasons it did not happen."""

    ok: bool
    signal_id: str
    status: str                     # the signals.status the row ended in
    reason: str | None = None       # why it was skipped, when it was
    verdict: str | None = None
    confidence: float | None = None
    actionable: bool = False
    validation_id: int | None = None
    provider: str | None = None
    model: str | None = None
    escalated: bool = False
    pack_path: str | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None


# --------------------------------------------------------------------------- pack


def pack_path(signal_id: str, root: Path | None = None) -> Path:
    return ((root or REPO_ROOT) / "journal" / "snapshots" / "signals" / signal_id
            / "pack.json")


def evidence_pack(cfg: EarnConfig, row: sqlite3.Row, *, features: Features,
                  jdb: sqlite3.Connection | None = None,
                  kdb: sqlite3.Connection | None = None,
                  root: Path | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """Everything the validator may see, computed by Python. Deterministic and complete."""
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)
    pair = row["pair"]
    stored = _loads(row["features_json"], {})
    news_refs = _loads(row["news_refs_json"], [])
    cited = {n for n in news_refs if isinstance(n, str)}
    state = _read_json(root / cfg.paths.state_latest)
    flags = _read_json(root / cfg.paths.flags_file)
    return {
        "signal": {
            "signal_id": row["signal_id"], "ts_utc": row["ts_utc"],
            "detector": row["detector"], "pair": pair, "direction": row["direction"],
            "detector_score": row["detector_score"], "screen_score": row["screen_score"],
            "strength": row["strength"], "fast_path": bool(row["fast_path"]),
            "screen_rationale": row["screen_rationale"],
            "detector_detail": stored.get("detail", {}),
        },
        "built_at": utc_iso(now),
        "features": features.flat(),
        "pair_features": features.pairs.get(pair, {}) if pair else {},
        "news": [n.as_dict() for n in features.news
                 if not cited or n.url_hash in cited or not news_refs],
        "market_state": state,
        "active_flags": {k: v for k, v in (flags.get("flags") or {}).items()
                         if isinstance(v, dict) and v.get("active")},
        "positions": _positions(jdb),
        "limits": {
            "max_weight": dict(cfg.risk.max_weight),
            "max_gross_exposure": cfg.risk.max_gross_exposure,
            "usdt_floor": cfg.risk.usdt_floor,
            "stoploss_per_trade": cfg.risk.stoploss_per_trade,
            "min_notional_usdt": cfg.risk.min_notional_usdt,
        },
        "universe": {"assets": list(cfg.universe.assets), "pairs": list(cfg.universe.pairs)},
    }


def _loads(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, json.JSONDecodeError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _positions(jdb: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if jdb is None:
        return []
    try:
        rows = jdb.execute(
            "SELECT date_utc, sleeve, nav_usdt, positions_json FROM nav_daily"
            " WHERE date_utc = (SELECT MAX(date_utc) FROM nav_daily)").fetchall()
    except sqlite3.Error:
        return []
    return [{"date_utc": r["date_utc"], "sleeve": r["sleeve"], "nav_usdt": r["nav_usdt"],
             "positions": _loads(r["positions_json"], None)} for r in rows]


# --------------------------------------------------------------------------- prompt


def render_prompt(cfg: EarnConfig, pack: dict[str, Any], *,
                  root: Path | None = None) -> str:
    root = root or REPO_ROOT
    template = stage_prompt_text(cfg, "validate", root)
    return (template
            .replace("{{SIGNAL}}", json.dumps(pack["signal"], indent=2, sort_keys=True))
            .replace("{{PACK}}", json.dumps(pack, indent=2, sort_keys=True))
            .replace("{{MIN_CONFIDENCE}}", f"{cfg.signals.validator.min_confidence:.2f}"))


# --------------------------------------------------------------------------- caps


def _day_prefix(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _validations_today(jdb: sqlite3.Connection, now: datetime) -> int:
    row = jdb.execute("SELECT COUNT(*) AS n FROM signal_validations WHERE ts_utc LIKE ?",
                      (_day_prefix(now) + "%",)).fetchone()
    return int(row["n"]) if row else 0


def _asset_cooldown_blocked(jdb: sqlite3.Connection, cfg: EarnConfig, pair: str | None,
                            now: datetime) -> bool:
    minutes = cfg.signals.validator.cooldown_min_per_asset
    if not minutes or pair is None:
        return False
    since = utc_iso(now - timedelta(minutes=minutes))
    row = jdb.execute(
        "SELECT 1 FROM signal_validations v JOIN signals s ON s.signal_id = v.signal_id"
        " WHERE s.pair = ? AND v.ts_utc >= ? LIMIT 1", (pair, since)).fetchone()
    return row is not None


def expire_stale(cfg: EarnConfig, jdb: sqlite3.Connection, *,
                 now: datetime | None = None) -> list[str]:
    """Screened signals nobody validated in time become ``expired``. Returns their ids."""
    now = now or datetime.now(UTC)
    cutoff = utc_iso(now - timedelta(minutes=cfg.signals.validator.expire_after_min))
    rows = jdb.execute(
        "SELECT signal_id FROM signals WHERE status IN ('screened','validating')"
        " AND ts_utc < ?", (cutoff,)).fetchall()
    ids = [r["signal_id"] for r in rows]
    for sid in ids:
        jdb.execute(
            "UPDATE signals SET status='expired', status_reason='expire_after_min',"
            " updated_utc=? WHERE signal_id=?", (utc_iso(now), sid))
    if ids:
        jdb.commit()
    return ids


# --------------------------------------------------------------------------- validate


def _set_status(jdb: sqlite3.Connection, signal_id: str, status: str,
                reason: str | None, now: datetime) -> None:
    jdb.execute("UPDATE signals SET status=?, status_reason=?, updated_utc=?"
                " WHERE signal_id=?", (status, reason, utc_iso(now), signal_id))
    jdb.commit()


def _skills_for(cfg: EarnConfig) -> list[str]:
    return list(cfg.skills.bindings.get("validate") or [])


def validate_signal(cfg: EarnConfig, jdb: sqlite3.Connection, kdb: sqlite3.Connection,
                    signal_id: str, *, root: Path | None = None,
                    now: datetime | None = None, models_cfg: Any | None = None,
                    features: Features | None = None, runner=run_task,
                    force: bool = False) -> ValidationOutcome:
    """Validate one signal. Never raises: every refusal is a reason on the outcome."""
    root = root or REPO_ROOT
    now = now or datetime.now(UTC)
    vcfg = cfg.signals.validator

    row = jdb.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
    if row is None:
        return ValidationOutcome(ok=False, signal_id=signal_id, status="error",
                                 reason="unknown signal_id")
    if row["status"] not in ("screened", "validating") and not force:
        return ValidationOutcome(ok=False, signal_id=signal_id, status=row["status"],
                                 reason=f"status {row['status']} is not validatable")
    age_min = (now - _parse_ts(row["ts_utc"], now)).total_seconds() / 60.0
    if age_min > vcfg.expire_after_min and not force:
        _set_status(jdb, signal_id, "expired", "expire_after_min", now)
        return ValidationOutcome(ok=False, signal_id=signal_id, status="expired",
                                 reason="expire_after_min")
    if not force and _validations_today(jdb, now) >= vcfg.max_per_day:
        return ValidationOutcome(ok=False, signal_id=signal_id, status=row["status"],
                                 reason="cap:max_per_day")
    if not force and _asset_cooldown_blocked(jdb, cfg, row["pair"], now):
        return ValidationOutcome(ok=False, signal_id=signal_id, status=row["status"],
                                 reason="cooldown:asset")

    _set_status(jdb, signal_id, "validating", None, now)
    if features is None:
        features = build_features(kdb, cfg, now=now, jdb=jdb, root=root)
    pack = evidence_pack(cfg, row, features=features, jdb=jdb, kdb=kdb, root=root, now=now)
    path = pack_path(signal_id, root)
    atomic_write_json(path, pack)
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

    prompt = render_prompt(cfg, pack, root=root)
    # The validation job IS the enclosing run: one signal, one detached process under
    # ops/locks/validate.lock with the same wall-clock budget its `timeout` uses.
    ctx = run_ctx_for(vcfg.task, run_id=signal_id, signal_id=signal_id, root=root,
                      deadline_s=float(vcfg.deadline_s), now=now)
    outcome = runner(vcfg.task, prompt, run_ctx=ctx, models_cfg=models_cfg,
                     output_schema=validation_schema(), tools_profile="read_only",
                     skills=_skills_for(cfg), root=root, cfg=cfg, jdb=jdb, kdb=kdb)

    parsed: Validation | None = None
    error = outcome.error or outcome.failure
    if outcome.ok and outcome.text:
        try:
            parsed = validate_validation(outcome.text)
        except SignalInvalid as e:
            error = f"schema: {e}"
    if parsed is not None:
        unknown = [k for k in parsed.cited_feature_keys if k not in features.keys()]
        if unknown:
            parsed = None
            error = f"unknown feature_key: {sorted(unknown)[:3]}"

    if parsed is None:
        vid = _insert_validation(jdb, signal_id, None, outcome, rel, error, now)
        _set_status(jdb, signal_id, "error", error, now)
        return ValidationOutcome(ok=False, signal_id=signal_id, status="error",
                                 reason=error, validation_id=vid, pack_path=rel,
                                 provider=outcome.provider, model=outcome.model,
                                 cost_usd=outcome.cost_usd)

    vid = _insert_validation(jdb, signal_id, parsed, outcome, rel, None, now)
    actionable = bool(parsed.actionable and parsed.confidence >= vcfg.min_confidence)
    reason = None if actionable else (
        "confidence below min_confidence" if parsed.actionable else parsed.verdict)
    _set_status(jdb, signal_id, parsed.verdict, reason, now)
    return ValidationOutcome(
        ok=True, signal_id=signal_id, status=parsed.verdict, reason=reason,
        verdict=parsed.verdict, confidence=parsed.confidence, actionable=actionable,
        validation_id=vid, provider=outcome.provider, model=outcome.model,
        escalated=outcome.escalated, pack_path=rel, cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms)


def _parse_ts(text: str, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return fallback


def _insert_validation(jdb: sqlite3.Connection, signal_id: str,
                       parsed: Validation | None, outcome, pack_rel: str,
                       error: str | None, now: datetime) -> int:
    cur = jdb.execute(
        "INSERT INTO signal_validations(signal_id, ts_utc, provider, model, verdict,"
        " confidence, suggested_json, horizon_hours, thesis, reasons_json,"
        " counter_evidence_json, invalidation, escalated, cost_usd, latency_ms,"
        " pack_path, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, utc_iso(now), outcome.provider or "none", outcome.model or "none",
         parsed.verdict if parsed else "uncertain",
         parsed.confidence if parsed else 0.0,
         json.dumps(parsed.suggested.model_dump()) if parsed else None,
         parsed.horizon_hours if parsed else None,
         parsed.thesis if parsed else None,
         json.dumps(parsed.reasons) if parsed else None,
         json.dumps(parsed.counter_evidence) if parsed else None,
         parsed.invalidation if parsed else None,
         int(bool(outcome.escalated)), outcome.cost_usd, outcome.latency_ms,
         pack_rel, error))
    jdb.commit()
    return int(cur.lastrowid)
