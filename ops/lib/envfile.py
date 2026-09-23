"""A comment-preserving, atomic editor for the repo ``.env``.

The console has to be able to *set* a credential without ever being able to *show* one.
This module is where that asymmetry is implemented:

* :func:`set_value` / :func:`delete` rewrite one line of ``.env`` in place, keeping every
  comment, blank line and ordering, writing through a temp file and ``os.replace`` at
  0600 inside a 0700 parent;
* :func:`info` / :func:`infos` return :class:`SecretInfo` — ``present`` and the **last four
  characters** only. There is no API here that hands a secret back to a caller that only
  had a name, and the console layer never calls the one function that reads a value;
* :func:`value_of` is the single host-side reader, used by the credential probes in
  ``console/services/credential_tests.py``. It is deliberately ugly to reach for, and its
  result must never enter a response, a log line or a journal row.

Writes refuse under ``EARN_AUTOMATED_RUN=1``: an unattended model run has no business
editing the secret store, the same rule ``ops.lib.mode_state.write`` applies to the mode
file. Values are stripped of a trailing ``\\r`` and of one layer of surrounding quotes on
read, matching what ``ops/envwrap.sh`` does, so a Windows-edited ``.env`` behaves.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ops.lib.paths import FILE_MODE, REPO_ROOT, is_automated_run

__all__ = [
    "ENV_PATH",
    "EnvFileError",
    "SecretInfo",
    "delete",
    "info",
    "infos",
    "last4",
    "names",
    "parse",
    "set_value",
    "value_of",
]

ENV_PATH = REPO_ROOT / ".env"

_ASSIGN = re.compile(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(Exception):
    """A refused or impossible ``.env`` edit."""


@dataclass(frozen=True)
class SecretInfo:
    """What the console may know about a secret. Never the value."""

    name: str
    present: bool = False
    last4: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "present": self.present,
            "last4": self.last4,
            "updated_at": self.updated_at,
        }


def last4(value: str | None) -> str | None:
    """The last four characters of a secret, or ``None`` when there is nothing to show."""
    if not value:
        return None
    return value[-4:] if len(value) >= 4 else "*" * len(value)


def _clean(raw: str) -> str:
    """One ``.env`` value: strip ``\\r``, whitespace and one layer of quotes."""
    v = raw.strip().rstrip("\r")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def _quote(value: str) -> str:
    """Quote only when the value would not survive a naive shell/env parse."""
    if value == "" or re.fullmatch(r"[A-Za-z0-9_@%+=:,./~-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse(text: str) -> dict[str, str]:
    """Every assignment in ``text``; later lines win, as a shell would resolve them."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            out[m.group(2)] = _clean(m.group(3))
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as e:  # pragma: no cover - unreadable .env is an ops problem
        raise EnvFileError(f"cannot read {path}: {e}") from e


def _path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else ENV_PATH


def names(*, path: Path | str | None = None) -> list[str]:
    """Every name assigned in the file, in file order."""
    p = _path(path)
    seen: list[str] = []
    for line in _read_text(p).splitlines():
        m = _ASSIGN.match(line)
        if m and m.group(2) not in seen:
            seen.append(m.group(2))
    return seen


def value_of(name: str, *, path: Path | str | None = None) -> str | None:
    """The raw value — host-side probes only. Never return this to a client."""
    return parse(_read_text(_path(path))).get(name) or None


def info(name: str, *, path: Path | str | None = None) -> SecretInfo:
    """``present`` + ``last4`` for one name."""
    p = _path(path)
    value = parse(_read_text(p)).get(name)
    if not value:
        return SecretInfo(name=name, present=False, last4=None, updated_at=None)
    return SecretInfo(name=name, present=True, last4=last4(value), updated_at=_mtime(p))


def infos(wanted: Iterable[str], *, path: Path | str | None = None) -> list[SecretInfo]:
    """``present`` + ``last4`` for many names, in the order asked for."""
    p = _path(path)
    values = parse(_read_text(p))
    stamp = _mtime(p)
    out: list[SecretInfo] = []
    for name in wanted:
        value = values.get(name)
        out.append(
            SecretInfo(name=name, present=bool(value), last4=last4(value),
                       updated_at=stamp if value else None)
        )
    return out


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def _guard(name: str, *, env: Mapping[str, str] | None) -> None:
    if is_automated_run(dict(env) if env is not None else None):
        raise EnvFileError("secret writes are refused under EARN_AUTOMATED_RUN=1")
    if not _NAME.match(name):
        raise EnvFileError(f"invalid secret name {name!r}")


def _write(path: Path, lines: list[str]) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(FILE_MODE)
    except (OSError, NotImplementedError):  # pragma: no cover - non-POSIX filesystems
        pass
    os.replace(tmp, path)
    try:
        path.chmod(FILE_MODE)
    except (OSError, NotImplementedError):  # pragma: no cover
        pass


def set_value(
    name: str,
    value: str,
    *,
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> SecretInfo:
    """Set one key, preserving comments and order. Returns present/last4 only."""
    _guard(name, env=env)
    value = value.strip()
    if not value:
        raise EnvFileError(f"{name}: refusing to store an empty value (use delete)")
    p = _path(path)
    rendered = f"{name}={_quote(value)}"
    lines: list[str] = []
    replaced = False
    for line in _read_text(p).splitlines():
        m = _ASSIGN.match(line)
        if m and m.group(2) == name:
            if replaced:
                continue          # a duplicate assignment of the same key: drop it
            lines.append(m.group(1) + rendered)
            replaced = True
            continue
        lines.append(line)
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(rendered)
    _write(p, lines)
    return SecretInfo(name=name, present=True, last4=last4(value), updated_at=_mtime(p))


def delete(
    name: str,
    *,
    path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> SecretInfo:
    """Remove every assignment of ``name``. Idempotent."""
    _guard(name, env=env)
    p = _path(path)
    lines = _read_text(p).splitlines()
    kept = [line for line in lines
            if not ((m := _ASSIGN.match(line)) and m.group(2) == name)]
    if kept != lines:
        _write(p, kept)
    return SecretInfo(name=name, present=False, last4=None, updated_at=None)
