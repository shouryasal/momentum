"""Signed proposal approvals — the human gate in front of a LIVE_PROPOSE sleeve.

In ``LIVE_PROPOSE`` a proposal is inert until a human approves it. The approval is an
HMAC-signed file written next to the proposals and mounted read-only into the container;
the in-container proposal loader verifies it with nothing but ``json``, ``hmac`` and
``hashlib`` (the same canonical form as every other signed Earn file), so approval cannot
be faked by anything that lacks ``$EARN_APPROVAL_KEY``.

An approval binds three things at once, and the loader checks all three:

* the **run id** it was granted for,
* the **sha256 of the proposal file**, so an approval cannot be replayed against a
  proposal that was rewritten afterwards,
* an **expiry** (``modes.live.approval_ttl_hours``), so a forgotten approval goes stale
  instead of standing open forever.

Console and Telegram both go through :func:`decide`, so the ``proposal_approvals`` row, the
``proposals.approval_status`` column and the file on disk can never disagree about what was
decided. ``modes.test.simulate_approval`` makes the same flow run in TEST, where the
approval is written and journalled exactly as in live but no real order follows it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops import db
from ops.config import REPO_ROOT, EarnConfig
from ops.lib import audit, signing

VERSION = 1
APPROVED_DIRNAME = "approved"
DECISIONS: tuple[str, ...] = ("approve", "reject")
CHANNELS: tuple[str, ...] = ("console", "telegram")

#: The decision vocabulary differs on purpose between the row and the file.
#: ``proposal_approvals.decision`` has a CHECK of ``approve``/``reject``; the in-container
#: loader (``strategies/proposal_loader.verify_approval``) matches ``approved`` and reads
#: ``expires_at``. The file therefore speaks the loader's language and carries
#: ``expires_utc`` as well, so both sides read the same signed bytes.
FILE_DECISION: dict[str, str] = {"approve": "approved", "reject": "rejected"}
ACCEPTED_APPROVALS: frozenset[str] = frozenset({"approve", "approved"})

STATUS_NA = "n/a"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

#: verification failure reasons — stable strings the loader and the UI both use
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_BAD_JSON = "bad_json"
REASON_BAD_SHAPE = "bad_shape"
REASON_BAD_VERSION = "bad_version"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_NO_SECRET = "no_secret"
REASON_EXPIRED = "expired"
REASON_REJECTED = "rejected"
REASON_WRONG_RUN = "wrong_run"
REASON_WRONG_PROPOSAL = "wrong_proposal"


class ApprovalError(Exception):
    pass


# --------------------------------------------------------------------------- paths


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def safe_name(run_id: str) -> str:
    """``2026-09-22T08:30+04:00`` -> ``2026-09-22T08-30-04-00`` (a portable filename)."""
    out = []
    for ch in run_id:
        out.append(ch if ch.isalnum() or ch in "-_." else "-")
    return "".join(out).strip("-")


def approvals_dir(cfg: EarnConfig, root: Path | None = None) -> Path:
    return (root or REPO_ROOT) / cfg.paths.proposals_dir / APPROVED_DIRNAME


def approval_path(cfg: EarnConfig, run_id: str, root: Path | None = None) -> Path:
    return approvals_dir(cfg, root) / f"{safe_name(run_id)}.json"


def proposal_file_for(cfg: EarnConfig, run_id: str, root: Path | None = None) -> Path | None:
    """The ``proposals/YYYY-MM-DD-HHMM.json`` a Gulf-time run id refers to, if it exists."""
    try:
        date_part, time_part = run_id.split("T", 1)
        hhmm = time_part[:5].replace(":", "")
    except ValueError:
        return None
    path = (root or REPO_ROOT) / cfg.paths.proposals_dir / f"{date_part}-{hhmm}.json"
    return path if path.exists() else None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _proposal_path(
    conn: sqlite3.Connection, cfg: EarnConfig, run_id: str, root: Path | None
) -> Path | None:
    """The proposal file this approval binds to: the journalled path, else the derived one."""
    row = conn.execute(
        "SELECT path FROM proposals WHERE run_id=? AND shadow=0", (run_id,)
    ).fetchone()
    if row is not None and row["path"]:
        candidate = Path(str(row["path"]))
        if not candidate.is_absolute():
            candidate = (root or REPO_ROOT) / candidate
        if candidate.exists():
            return candidate
    return proposal_file_for(cfg, run_id, root)


# --------------------------------------------------------------------------- payload


@dataclass(frozen=True)
class Approval:
    run_id: str
    decision: str
    decided_utc: str
    expires_utc: str
    actor: str
    channel: str
    proposal_sha256: str | None = None
    note: str | None = None
    nonce: str = ""
    version: int = VERSION

    def payload(self) -> dict[str, Any]:
        """The signable record. ``decision``/``expires_at`` use the loader's vocabulary."""
        return {
            "version": self.version,
            "run_id": self.run_id,
            "decision": FILE_DECISION.get(self.decision, self.decision),
            "decided_utc": self.decided_utc,
            "expires_at": self.expires_utc,
            "expires_utc": self.expires_utc,
            "actor": self.actor,
            "channel": self.channel,
            "proposal_sha256": self.proposal_sha256,
            "note": self.note,
            "nonce": self.nonce,
        }


def build(
    cfg: EarnConfig,
    *,
    run_id: str,
    decision: str,
    actor: str,
    channel: str,
    now: datetime,
    note: str | None = None,
    proposal_sha256: str | None = None,
    ttl_hours: int | None = None,
) -> Approval:
    if decision not in DECISIONS:
        raise ApprovalError(f"decision must be one of {DECISIONS}, got {decision!r}")
    if channel not in CHANNELS:
        raise ApprovalError(f"channel must be one of {CHANNELS}, got {channel!r}")
    ttl = cfg.modes.live.approval_ttl_hours if ttl_hours is None else ttl_hours
    return Approval(
        run_id=run_id,
        decision=decision,
        decided_utc=_iso(now),
        expires_utc=_iso(now + timedelta(hours=max(1, int(ttl)))),
        actor=actor,
        channel=channel,
        proposal_sha256=proposal_sha256,
        note=note,
        nonce=signing.new_nonce(8),
    )


def sign(approval: Approval, secret: str) -> dict[str, Any]:
    return signing.sign_payload(approval.payload(), secret)


def write_file(cfg: EarnConfig, signed: dict[str, Any], root: Path | None = None) -> Path:
    """Atomically write the signed approval where the container can read it."""
    from runs.common import atomic_write_json

    path = approval_path(cfg, str(signed["run_id"]), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, signed)
    return path


def remove_file(cfg: EarnConfig, run_id: str, root: Path | None = None) -> bool:
    path = approval_path(cfg, run_id, root)
    try:
        path.unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- verify


def verify_payload(
    payload: Any,
    *,
    secret: str | None,
    now: datetime | None = None,
    run_id: str | None = None,
    proposal_sha256: str | None = None,
) -> tuple[bool, str]:
    """The exact check the in-container loader performs. Never raises."""
    if not isinstance(payload, dict):
        return False, REASON_BAD_SHAPE
    if int(payload.get("version", 0)) != VERSION:
        return False, REASON_BAD_VERSION
    for key in ("run_id", "decision", "decided_utc", "expires_at", "actor", "channel"):
        if not payload.get(key):
            return False, REASON_BAD_SHAPE
    if not secret:
        return False, REASON_NO_SECRET
    if not signing.verify_payload(payload, secret):
        return False, REASON_BAD_SIGNATURE
    if str(payload["decision"]).lower() not in ACCEPTED_APPROVALS:
        return False, REASON_REJECTED
    if run_id is not None and payload["run_id"] != run_id:
        return False, REASON_WRONG_RUN
    if (
        proposal_sha256 is not None
        and payload.get("proposal_sha256")
        and payload["proposal_sha256"] != proposal_sha256
    ):
        return False, REASON_WRONG_PROPOSAL
    if _parse(payload["expires_at"]) <= (now or datetime.now(UTC)):
        return False, REASON_EXPIRED
    return True, REASON_OK


def verify_file(
    path: Path | str,
    *,
    secret: str | None,
    now: datetime | None = None,
    run_id: str | None = None,
    proposal_sha256: str | None = None,
) -> tuple[bool, str]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, REASON_MISSING
    except OSError:
        return False, REASON_MISSING
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, REASON_BAD_JSON
    return verify_payload(
        payload, secret=secret, now=now, run_id=run_id, proposal_sha256=proposal_sha256
    )


# --------------------------------------------------------------------------- decide


@dataclass(frozen=True)
class Decision:
    run_id: str
    decision: str
    actor: str
    channel: str
    decided_utc: str
    expires_utc: str
    path: Path | None = None
    note: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "decision": self.decision,
            "actor": self.actor,
            "channel": self.channel,
            "decided_utc": self.decided_utc,
            "expires_utc": self.expires_utc,
            "path": str(self.path) if self.path else None,
            "note": self.note,
            **self.detail,
        }


def decide(
    conn: sqlite3.Connection,
    cfg: EarnConfig,
    *,
    run_id: str,
    decision: str,
    actor: str,
    channel: str,
    note: str | None = None,
    now: datetime | None = None,
    secret: str | None = None,
    root: Path | None = None,
) -> Decision:
    """Record one approval decision: DB row, proposal status and the signed file.

    Approving writes the file; rejecting removes any file that was there, so a reject can
    undo an approval that has not been acted on yet. Called by the console router and by
    the Telegram bot with the same arguments.
    """
    ts = now or datetime.now(UTC)
    key = secret if secret is not None else signing.get_secret(signing.APPROVAL_SECRET_ENV)
    if not key:
        raise ApprovalError(f"{signing.APPROVAL_SECRET_ENV} is not set; refusing to sign")

    existing = conn.execute(
        "SELECT applied FROM proposal_approvals WHERE run_id=?", (run_id,)
    ).fetchone()
    if existing is not None and int(existing["applied"] or 0):
        raise ApprovalError(f"proposal {run_id} has already been acted on")

    proposal_file = _proposal_path(conn, cfg, run_id, root)
    sha = sha256_of(proposal_file) if proposal_file else None

    approval = build(
        cfg, run_id=run_id, decision=decision, actor=actor, channel=channel, now=ts,
        note=note, proposal_sha256=sha,
    )
    signed = sign(approval, key)

    path: Path | None = None
    if decision == "approve":
        path = write_file(cfg, signed, root)
    else:
        remove_file(cfg, run_id, root)

    db.write(
        conn,
        "INSERT INTO proposal_approvals(run_id, decision, decided_utc, actor, channel, note,"
        " sig, expires_utc, applied) VALUES (?,?,?,?,?,?,?,?,0)"
        " ON CONFLICT(run_id) DO UPDATE SET decision=excluded.decision,"
        " decided_utc=excluded.decided_utc, actor=excluded.actor, channel=excluded.channel,"
        " note=excluded.note, sig=excluded.sig, expires_utc=excluded.expires_utc, applied=0",
        (
            run_id, decision, approval.decided_utc, actor, channel, note,
            str(signed[signing.SIG_FIELD]), approval.expires_utc,
        ),
    )
    db.write(
        conn,
        "UPDATE proposals SET approval_status=? WHERE run_id=? AND shadow=0",
        (STATUS_APPROVED if decision == "approve" else STATUS_REJECTED, run_id),
    )
    audit.try_record(
        conn, actor=actor, action=f"proposal.{decision}", target=run_id,
        detail={"channel": channel, "note": note, "proposal_sha256": sha}, result="ok",
    )
    return Decision(
        run_id=run_id, decision=decision, actor=actor, channel=channel,
        decided_utc=approval.decided_utc, expires_utc=approval.expires_utc, path=path,
        note=note, detail={"proposal_sha256": sha},
    )


def mark_applied(conn: sqlite3.Connection, run_id: str) -> None:
    db.write(conn, "UPDATE proposal_approvals SET applied=1 WHERE run_id=?", (run_id,))


# --------------------------------------------------------------------------- queries


def mark_pending(conn: sqlite3.Connection, run_id: str) -> None:
    db.write(
        conn, "UPDATE proposals SET approval_status=? WHERE run_id=? AND shadow=0", (STATUS_PENDING, run_id)
    )


def expire_stale(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[str]:
    """Flip approvals past their TTL to ``expired``; returns the run ids touched."""
    ts = _iso(now or datetime.now(UTC))
    rows = conn.execute(
        "SELECT run_id FROM proposal_approvals WHERE applied=0 AND decision='approve'"
        " AND expires_utc <= ?",
        (ts,),
    ).fetchall()
    ids = [str(r["run_id"]) for r in rows]
    for run_id in ids:
        db.write(
            conn, "UPDATE proposals SET approval_status=? WHERE run_id=? AND shadow=0",
            (STATUS_EXPIRED, run_id),
        )
    return ids


def pending(
    conn: sqlite3.Connection, cfg: EarnConfig, *, now: datetime | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Proposals waiting on a human, newest first, with the seconds left on each."""
    ts = now or datetime.now(UTC)
    ttl = timedelta(hours=max(1, int(cfg.modes.live.approval_ttl_hours)))
    rows = conn.execute(
        "SELECT p.run_id, p.ts_utc, p.abstain, p.valid, p.approval_status,"
        " a.decision, a.decided_utc, a.expires_utc, a.actor, a.channel, a.note, a.applied"
        " FROM proposals p LEFT JOIN proposal_approvals a ON a.run_id = p.run_id"
        " WHERE p.shadow=0 AND (p.approval_status IS NULL OR p.approval_status IN (?, ?))"
        " ORDER BY p.ts_utc DESC LIMIT ?",
        (STATUS_PENDING, STATUS_APPROVED, int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        expires = _parse(r["expires_utc"]) if r["expires_utc"] else _parse(r["ts_utc"]) + ttl
        out.append(
            {
                "run_id": r["run_id"],
                "ts_utc": r["ts_utc"],
                "valid": bool(r["valid"]),
                "abstain": bool(r["abstain"]),
                "status": r["approval_status"] or STATUS_PENDING,
                "decision": r["decision"],
                "decided_utc": r["decided_utc"],
                "actor": r["actor"],
                "channel": r["channel"],
                "note": r["note"],
                "applied": bool(r["applied"]),
                "expires_utc": _iso(expires),
                "seconds_left": max(0, int((expires - ts).total_seconds())),
            }
        )
    return out


def history(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM proposal_approvals ORDER BY decided_utc DESC LIMIT ?", (int(limit),)
        )
    ]


def status_of(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute(
        "SELECT approval_status FROM proposals WHERE run_id=? AND shadow=0", (run_id,)
    ).fetchone()
    return str(row["approval_status"]) if row and row["approval_status"] else STATUS_PENDING


__all__ = [
    "APPROVED_DIRNAME",
    "CHANNELS",
    "DECISIONS",
    "REASON_BAD_SIGNATURE",
    "REASON_EXPIRED",
    "REASON_MISSING",
    "REASON_OK",
    "REASON_REJECTED",
    "REASON_WRONG_PROPOSAL",
    "REASON_WRONG_RUN",
    "STATUS_APPROVED",
    "STATUS_EXPIRED",
    "STATUS_NA",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "VERSION",
    "Approval",
    "ApprovalError",
    "Decision",
    "approval_path",
    "approvals_dir",
    "build",
    "decide",
    "expire_stale",
    "history",
    "mark_applied",
    "mark_pending",
    "pending",
    "proposal_file_for",
    "remove_file",
    "safe_name",
    "sha256_of",
    "sign",
    "status_of",
    "verify_file",
    "verify_payload",
    "write_file",
]
