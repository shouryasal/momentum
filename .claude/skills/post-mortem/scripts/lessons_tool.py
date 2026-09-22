"""lessons.md tooling: parse, lint, append (append-only enforced), 180-day archive.
Model edits go through `append`; `reconfirm`/`archive` are code-side operations the
review preflight runs. Stdlib only."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

HEADER_RE = re.compile(r"^## (L-\d{4}-W\d{2}-\d{2}) — (.+)$")
REQUIRED = ("date", "status", "source", "cause", "evidence-type", "lesson",
            "falsified-if", "reconfirmed")
CAUSES = ("data", "execution", "ops", "reasoning", "strategy")  # noise is NEVER a lesson
EVIDENCE = ("process", "outcome-stats")


@dataclass
class Lesson:
    lesson_id: str
    title: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""


def parse(text: str) -> tuple[list[Lesson], list[str]]:
    lessons, errors = [], []
    current: Lesson | None = None
    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            if current:
                lessons.append(current)
            current = Lesson(m.group(1), m.group(2))
            current.raw = line + "\n"
            continue
        if current is None:
            continue
        current.raw += line + "\n"
        # multiple `key: value` pairs may share a line, separated by 2+ spaces or ·
        for segment in re.split(r"\s{2,}|·", line):
            fm = re.match(r"^\s*([a-z-]+):\s*(.*)$", segment)
            if fm:
                current.fields[fm.group(1)] = fm.group(2).strip()
    if current:
        lessons.append(current)
    return lessons, errors


def lint(text: str, outcome_min: int = 30) -> list[str]:
    lessons, errors = parse(text)
    seen: set[str] = set()
    for les in lessons:
        lid = les.lesson_id
        if lid in seen:
            errors.append(f"{lid}: duplicate id")
        seen.add(lid)
        for f in REQUIRED:
            if f not in les.fields or not les.fields[f]:
                errors.append(f"{lid}: missing field {f!r}")
        ev = les.fields.get("evidence-type", "")
        if ev not in EVIDENCE:
            errors.append(f"{lid}: evidence-type must be one of {EVIDENCE}")
        if ev == "outcome-stats":
            n = les.fields.get("stats-n")
            if not n or not n.isdigit() or int(n) < outcome_min:
                errors.append(f"{lid}: outcome-stats requires stats-n >= {outcome_min}")
        cause = les.fields.get("cause", "")
        if cause and cause not in CAUSES:
            errors.append(f"{lid}: cause {cause!r} invalid (market noise is never a lesson)")
        fi = les.fields.get("falsified-if", "")
        if fi and len(fi) < 10:
            errors.append(f"{lid}: falsified-if too vague")
    return errors


def append_only_ok(old: str, new: str) -> bool:
    return new.startswith(old.rstrip("\n")) if old.strip() else True


def next_id(text: str, week: str) -> str:
    ns = [int(m.group(1)) for m in
          re.finditer(rf"^## L-{re.escape(week)}-(\d{{2}})", text, re.M)]
    return f"L-{week}-{max(ns, default=0) + 1:02d}"


def append(path: Path, *, week: str, title: str, cause: str, decisions: str,
           evidence_type: str, lesson: str, falsified_if: str,
           stats_n: int | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    text = path.read_text() if path.exists() else ""
    lid = next_id(text, week)
    entry = (f"\n## {lid} — {title}\n"
             f"date: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}        status: active\n"
             f"source: review {week} · cause: {cause} · decisions: [{decisions}]\n"
             f"evidence-type: {evidence_type}\n")
    if evidence_type == "outcome-stats":
        entry += f"stats-n: {stats_n or 0}\n"
    entry += (f"lesson: {lesson}\n"
              f"falsified-if: {falsified_if}\n"
              f"reconfirmed: {now.strftime('%Y-%m-%d')}\n")
    new_text = text.rstrip("\n") + "\n" + entry if text.strip() else text + entry
    problems = lint(new_text)
    if problems:
        raise ValueError(f"lesson would not lint: {problems}")
    if not append_only_ok(text, new_text):
        raise ValueError("append-only violation")
    path.write_text(new_text)
    return lid


def archive_stale(lessons_path: Path, archive_path: Path, days: int = 180,
                  now: datetime | None = None) -> list[str]:
    """Move entries whose `reconfirmed` is older than `days` to the archive (code-only)."""
    now = now or datetime.now(UTC)
    text = lessons_path.read_text() if lessons_path.exists() else ""
    lessons, _ = parse(text)
    cutoff = now - timedelta(days=days)
    keep_ids, moved = [], []
    for les in lessons:
        try:
            rec = datetime.strptime(les.fields.get("reconfirmed", ""), "%Y-%m-%d"
                                    ).replace(tzinfo=UTC)
        except ValueError:
            rec = now
        (keep_ids if rec >= cutoff else moved).append(les)
    if not moved:
        return []
    header_end = text.find("\n## ")
    preamble = text[:header_end + 1] if header_end >= 0 else text
    lessons_path.write_text(preamble + "".join(les.raw for les in keep_ids))
    with archive_path.open("a") as fh:
        fh.write(f"\n<!-- archived {now.strftime('%Y-%m-%d')} -->\n")
        for les in moved:
            fh.write(les.raw)
    return [les.lesson_id for les in moved]


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_lint = sub.add_parser("lint")
    p_lint.add_argument("path")
    p_app = sub.add_parser("append")
    for f in ("path", "week", "title", "cause", "decisions", "evidence-type",
              "lesson", "falsified-if"):
        p_app.add_argument(f"--{f}" if f != "path" else f, required=(f != "path") or None)
    p_app.add_argument("--stats-n", type=int, default=None)
    args = ap.parse_args()
    if args.cmd == "lint":
        errs = lint(Path(args.path).read_text())
        for e in errs:
            print(e, file=sys.stderr)
        return 1 if errs else 0
    lid = append(Path(args.path), week=args.week, title=args.title, cause=args.cause,
                 decisions=args.decisions, evidence_type=args.evidence_type,
                 lesson=args.lesson, falsified_if=args.falsified_if,
                 stats_n=args.stats_n)
    print(lid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
