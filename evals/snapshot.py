"""Decision input snapshots — what makes week-5 replay possible at all.

research_run calls write_snapshot() immediately before every decision-stage model
call. Raw inputs are stored (so a CANDIDATE prompt can be re-assembled over identical
inputs) plus the rendered prompt (so the BASELINE replay can be byte-compared).
Write-once per run_id; sha256 manifest detects tampering.

Layout: journal/snapshots/<slug>/   slug = 20260922-0830 (colon-free run_id)
  manifest.json  state.json  brief.md  positions.json  graded_recent.txt
  lessons.md  flags.json  limits.yaml  fewshot.txt  rendered_prompt.md
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ops.config import REPO_ROOT

INPUT_FILES = {
    "state": "state.json", "brief": "brief.md", "positions": "positions.json",
    "graded": "graded_recent.txt", "lessons": "lessons.md", "flags": "flags.json",
}


class SnapshotError(Exception):
    pass


@dataclass(frozen=True)
class SnapshotMeta:
    run_id: str
    created_at: str
    prompt_version: str
    model: str
    git_commit: str
    token_budget: int
    escalation_reasons: list[str] = field(default_factory=list)


def slug_for(run_id: str) -> str:
    # 2026-09-22T08:30+04:00 -> 20260922-0830
    date, rest = run_id.split("T")
    return date.replace("-", "") + "-" + rest[:5].replace(":", "")


def snapshots_dir(root: Path | None = None) -> Path:
    return (root or REPO_ROOT) / "journal" / "snapshots"


def git_commit(root: Path | None = None) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root or REPO_ROOT,
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_snapshot(run_id: str, *, inputs: dict[str, str], limits: str, fewshot: str,
                   rendered_prompt: str, meta: SnapshotMeta,
                   jdb: sqlite3.Connection | None = None,
                   root: Path | None = None) -> Path:
    d = snapshots_dir(root) / slug_for(run_id)
    if d.exists():
        raise SnapshotError(f"snapshot for {run_id} already exists (write-once)")
    d.mkdir(parents=True)
    files: dict[str, str] = {}
    for key, fname in INPUT_FILES.items():
        content = inputs.get(key, "")
        (d / fname).write_text(content)
        files[fname] = _sha(content)
    for fname, content in (("limits.yaml", limits), ("fewshot.txt", fewshot),
                           ("rendered_prompt.md", rendered_prompt)):
        (d / fname).write_text(content)
        files[fname] = _sha(content)
    manifest = {"meta": asdict(meta), "files": files}
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (d / "manifest.json").write_text(manifest_text)
    if jdb is not None:
        jdb.execute(
            "INSERT OR REPLACE INTO snapshot_index(run_id, path, sha256, created_at,"
            " prompt_version, model, git_commit) VALUES (?,?,?,?,?,?,?)",
            (run_id, str(d), _sha(manifest_text), meta.created_at,
             meta.prompt_version, meta.model, meta.git_commit))
        jdb.commit()
    return d


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    path: Path
    meta: SnapshotMeta
    inputs: dict[str, str]
    limits: str
    fewshot: str
    rendered_prompt: str


def read_snapshot(run_id_or_path: str | Path, root: Path | None = None) -> Snapshot:
    d = Path(run_id_or_path)
    if not d.exists():
        d = snapshots_dir(root) / slug_for(str(run_id_or_path))
    try:
        manifest = json.loads((d / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SnapshotError(f"snapshot unreadable at {d}: {e}") from e
    for fname, want in manifest["files"].items():
        got = _sha((d / fname).read_text())
        if got != want:
            raise SnapshotError(f"snapshot tampered: {fname} sha mismatch in {d}")
    meta = SnapshotMeta(**manifest["meta"])
    inputs = {key: (d / fname).read_text() for key, fname in INPUT_FILES.items()}
    return Snapshot(run_id=meta.run_id, path=d, meta=meta, inputs=inputs,
                    limits=(d / "limits.yaml").read_text(),
                    fewshot=(d / "fewshot.txt").read_text(),
                    rendered_prompt=(d / "rendered_prompt.md").read_text())


def list_snapshots(days: int, now: datetime | None = None,
                   root: Path | None = None) -> list[Path]:
    now = now or datetime.now(UTC)
    cutoff = now.strftime("%Y%m%d")
    since = (now.timestamp() - days * 86400)
    since_slug = datetime.fromtimestamp(since, tz=UTC).strftime("%Y%m%d")
    d = snapshots_dir(root)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_dir() and since_slug <= p.name[:8] <= cutoff)
