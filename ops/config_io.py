"""Round-trip config editing: JSON-pointer patches that keep comments and key order.

The console edits ``config/*.yaml`` in place. A YAML file here is documentation as much as
configuration — every limit carries the comment that explains it — so an edit that reflows
the file or drops a comment destroys the thing the operator reads at 3am.

Two strategies, picked per operation:

* **Surgical** (the common case): replacing the value at an existing pointer. ruamel's
  round-trip parser gives the ``(line, column)`` of that value; this module computes the
  value's exact span in the source text and splices the new rendering in. Every byte
  outside that span — comments, blank lines, flow style, alignment — is untouched.
* **Structural** (adding or removing a key, or replacing a block collection): the document
  is re-emitted by ruamel. Comments survive; flow style and alignment may normalise. A
  :class:`PatchResult` says which strategy ran, so callers can warn.

JSON files (``config/params-sleeve-*.json``) have no comments and are always re-emitted,
sorted and 2-space indented, which is the shape the generator seeds them in.

Nothing here validates: :mod:`ops.config_store` owns validation, bless and audit.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from ops.lib.signing import sha256_text

Fmt = Literal["yaml", "json"]

__all__ = [
    "Document",
    "PatchError",
    "PatchResult",
    "apply_patch",
    "atomic_write",
    "changed_paths",
    "diff_text",
    "dotted",
    "dump",
    "flatten",
    "get_at",
    "load",
    "parse",
    "pointer",
    "pointer_parts",
    "read_document",
    "sha_of",
]


class PatchError(Exception):
    """A patch that cannot be applied: bad pointer, missing parent, unknown op."""


# --------------------------------------------------------------------------- pointers


def pointer_parts(ptr: str) -> list[str]:
    """RFC 6901 pointer → path segments. ``""`` is the document root."""
    if ptr in ("", "/"):
        return [] if ptr == "" else [""]
    if not ptr.startswith("/"):
        raise PatchError(f"json pointer must start with '/': {ptr!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]


def pointer(parts: Sequence[str | int]) -> str:
    """Path segments → RFC 6901 pointer."""
    if not parts:
        return ""
    return "".join(
        "/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts
    )


def dotted(ptr: str) -> str:
    """``/risk/max_weight/BTC`` → ``risk.max_weight.BTC`` — the form the schema indexes by."""
    return ".".join(pointer_parts(ptr))


def pointer_from_dotted(path: str) -> str:
    return pointer(path.split(".")) if path else ""


# --------------------------------------------------------------------------- parse / dump


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def parse(text: str, *, fmt: Fmt = "yaml") -> Any:
    """Text → data. YAML keeps its round-trip node types; JSON is plain."""
    if fmt == "json":
        return json.loads(text) if text.strip() else {}
    return _yaml().load(text) if text.strip() else {}


def dump(data: Any, *, fmt: Fmt = "yaml") -> str:
    """Data → text, in the file's house style."""
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=True) + "\n"
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def sha_of(text: str) -> str:
    return sha256_text(text)


def fmt_for(path: Path | str) -> Fmt:
    return "json" if str(path).endswith(".json") else "yaml"


# --------------------------------------------------------------------------- documents


@dataclass(frozen=True)
class Document:
    """A config file as it is on disk, plus its parsed form."""

    path: Path
    rel: str
    text: str
    fmt: Fmt
    data: Any

    @property
    def sha(self) -> str:
        return sha_of(self.text)

    @property
    def values(self) -> Any:
        """Plain Python (no ruamel node types) — what an API response carries."""
        return plain(self.data)


def read_document(path: Path | str, *, rel: str | None = None) -> Document:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    f = fmt_for(p)
    return Document(path=p, rel=rel or p.name, text=text, fmt=f, data=parse(text, fmt=f))


def load(path: Path | str) -> Any:
    return read_document(path).data


def plain(value: Any) -> Any:
    """Strip ruamel's CommentedMap/CommentedSeq wrappers so ``json.dumps`` works."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- traversal


def _child(node: Any, key: str) -> Any:
    if isinstance(node, Mapping):
        if key in node:
            return node[key]
        raise PatchError(f"no such key: {key!r}")
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        try:
            return node[int(key)]
        except (ValueError, IndexError) as e:
            raise PatchError(f"no such index: {key!r}") from e
    raise PatchError(f"cannot descend into {type(node).__name__} at {key!r}")


def get_at(data: Any, ptr: str) -> Any:
    node = data
    for part in pointer_parts(ptr):
        node = _child(node, part)
    return node


def _parent_of(data: Any, parts: Sequence[str]) -> Any:
    node = data
    for part in parts[:-1]:
        node = _child(node, part)
    return node


def _exists(data: Any, parts: Sequence[str]) -> bool:
    try:
        node = _parent_of(data, parts)
    except PatchError:
        return False
    key = parts[-1]
    if isinstance(node, Mapping):
        return key in node
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        try:
            return 0 <= int(key) < len(node)
        except ValueError:
            return False
    return False


# --------------------------------------------------------------------------- flatten / diff


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted path → scalar value, for blame, search and change detection.

    Empty containers are emitted as themselves so that emptying a list is a visible change.
    """
    out: dict[str, Any] = {}
    if isinstance(data, Mapping):
        if not data:
            out[prefix] = {}
        for k, v in data.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, (list, tuple)) and not isinstance(data, (str, bytes)):
        if not data:
            out[prefix] = []
        for i, v in enumerate(data):
            out.update(flatten(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        out[prefix] = plain(data)
    return out


def changed_paths(before: Any, after: Any) -> list[str]:
    """Dotted paths whose value differs (added, removed or changed), sorted."""
    a, b = flatten(before), flatten(after)
    keys = set(a) | set(b)
    sentinel = object()
    return sorted(k for k in keys if a.get(k, sentinel) != b.get(k, sentinel))


def diff_text(before: str, after: str, *, rel: str) -> str:
    """Unified diff, exactly what lands in ``config_audit.diff``."""
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=3,
        )
    )


# --------------------------------------------------------------------------- rendering


_PLAIN_UNSAFE = set(":#{}[],&*!|>'\"%@`")


def _render_scalar(value: Any, *, quote: str | None) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if quote:
        body = text.replace("\\", "\\\\").replace(quote, "\\" + quote) if quote == '"' else text
        if quote == "'":
            body = text.replace("'", "''")
        return f"{quote}{body}{quote}"
    if (
        text == ""
        or text.strip() != text
        or any(c in _PLAIN_UNSAFE for c in text)
        or text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}
        or _looks_numeric(text)
    ):
        return json.dumps(text)
    return text


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _render_flow(value: Any, *, padded: bool = False) -> str:
    """A one-line flow rendering that quotes strings the way the source files do.

    ruamel's emitter would drop the quotes around values like ``"08:30"``; those quotes are
    load-bearing documentation in this repo, so flow collections are rendered here instead.
    """
    pad = " " if padded else ""
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        items = ", ".join(f"{k}: {_render_flow(v)}" for k, v in value.items())
        return f"{{{pad}{items}{pad}}}"
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        if not value:
            return "[]"
        items = ", ".join(_render_flow(v) for v in value)
        return f"[{pad}{items}{pad}]"
    return _render_scalar(value, quote=None)


def _is_collection(value: Any) -> bool:
    return isinstance(value, Mapping) or (
        isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes))
    )


# --------------------------------------------------------------------------- surgical edit


def _line_offsets(text: str) -> list[int]:
    offsets, pos = [0], 0
    for line in text.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    return offsets


def _value_position(parent: Any, key: str) -> tuple[int, int] | None:
    """``(line, col)`` of the value ruamel parsed for ``key`` inside ``parent``."""
    lc = getattr(parent, "lc", None)
    if lc is None:
        return None
    try:
        if isinstance(parent, Mapping):
            pos = lc.value(key)
        else:
            pos = lc.item(int(key))
    except (KeyError, ValueError, IndexError, TypeError):
        return None
    if not pos:
        return None
    return int(pos[0]), int(pos[1])


def _scan_span(text: str, start: int) -> int | None:
    """End offset of the value that begins at ``start``.

    Handles a quoted or plain scalar and a single-line flow collection; returns ``None``
    when the value is a block collection (which has no contiguous one-line span).
    """
    n = len(text)
    if start >= n:
        return None
    ch = text[start]
    if ch in "[{":
        return _scan_flow(text, start)
    if ch in "\"'":
        return _scan_quoted(text, start)
    if ch in "\r\n":
        return None  # block collection or a folded scalar: not surgically editable
    i = start
    while i < n and text[i] not in "\r\n":
        if text[i] == "#" and i > start and text[i - 1] in " \t":
            break
        if text[i] in ",}]" and _inside_flow(text, start, i):
            break
        i += 1
    while i > start and text[i - 1] in " \t":
        i -= 1
    return i


def _inside_flow(text: str, start: int, index: int) -> bool:
    """Is the value at ``start`` part of a flow collection opened earlier on its line?"""
    line_start = text.rfind("\n", 0, start) + 1
    depth = 0
    in_quote = ""
    for c in text[line_start:index]:
        if in_quote:
            if c == in_quote:
                in_quote = ""
            continue
        if c in "\"'":
            in_quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
    return depth > 0


def _scan_quoted(text: str, start: int) -> int:
    quote = text[start]
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == "\\" and quote == '"':
            i += 2
            continue
        if c == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def _scan_flow(text: str, start: int) -> int | None:
    depth = 0
    in_quote = ""
    i = start
    while i < len(text):
        c = text[i]
        if in_quote:
            if c == "\\" and in_quote == '"':
                i += 2
                continue
            if c == in_quote:
                in_quote = ""
            i += 1
            continue
        if c in "\"'":
            in_quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _surgical_replace(text: str, data: Any, parts: Sequence[str], value: Any) -> str | None:
    """Replace one value in ``text`` without touching another byte. ``None`` = not possible."""
    try:
        parent = _parent_of(data, parts)
    except PatchError:
        return None
    pos = _value_position(parent, parts[-1])
    if pos is None:
        return None
    line, col = pos
    offsets = _line_offsets(text)
    if line >= len(offsets):
        return None
    start = offsets[line] + col
    end = _scan_span(text, start)
    if end is None:
        return None
    old = text[start:end]
    if _is_collection(value):
        if not (old.startswith("[") or old.startswith("{")):
            return None
        padded = len(old) > 2 and old[1] == " " and old[-2] == " "
        new = _render_flow(value, padded=padded)
    else:
        if old.startswith("[") or old.startswith("{"):
            return None
        quote = old[0] if old[:1] in ("'", '"') and isinstance(value, str) else None
        new = _render_scalar(value, quote=quote)
    return text[:start] + new + text[end:]


# --------------------------------------------------------------------------- patch


@dataclass(frozen=True)
class PatchResult:
    text: str
    data: Any
    surgical: bool
    changed: list[str] = field(default_factory=list)
    reflowed: bool = False

    @property
    def sha(self) -> str:
        return sha_of(self.text)


def _normalise_ops(patch: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in patch:
        if not isinstance(raw, Mapping):
            raise PatchError(f"patch op must be an object, got {type(raw).__name__}")
        op = str(raw.get("op") or "replace")
        if op not in ("replace", "add", "remove", "set"):
            raise PatchError(f"unsupported patch op: {op!r}")
        ptr = raw.get("path")
        if not isinstance(ptr, str):
            raise PatchError("patch op needs a string 'path'")
        if ptr and not ptr.startswith("/"):
            ptr = pointer_from_dotted(ptr)  # dotted paths are accepted too
        out.append({"op": "replace" if op == "set" else op, "path": ptr, "value": raw.get("value")})
    return out


def _mutate(data: Any, ops: Sequence[Mapping[str, Any]]) -> None:
    for op in ops:
        parts = pointer_parts(op["path"])
        if not parts:
            raise PatchError("patching the document root is not supported; use raw mode")
        parent = _parent_of(data, parts)
        key = parts[-1]
        if op["op"] == "remove":
            if isinstance(parent, Mapping):
                if key not in parent:
                    raise PatchError(f"cannot remove missing key: {op['path']}")
                del parent[key]
            else:
                del parent[int(key)]
            continue
        value = op["value"]
        if isinstance(parent, Mapping):
            if op["op"] == "replace" and key not in parent:
                raise PatchError(f"cannot replace missing key: {op['path']}")
            parent[key] = value
        elif isinstance(parent, Sequence) and not isinstance(parent, (str, bytes)):
            idx = len(parent) if key == "-" else int(key)
            if op["op"] == "add":
                parent.insert(idx, value)
            else:
                parent[idx] = value
        else:
            raise PatchError(f"cannot set {op['path']} on {type(parent).__name__}")


def apply_patch(
    doc: Document, patch: Iterable[Mapping[str, Any]]
) -> PatchResult:
    """Apply JSON-pointer ops to ``doc``, preferring the surgical path.

    Every op that only replaces an existing scalar (or a one-line flow collection) is
    spliced into the source text. As soon as one op is structural, the whole document is
    re-emitted by ruamel instead and ``reflowed`` is set.
    """
    ops = _normalise_ops(patch)
    if not ops:
        return PatchResult(text=doc.text, data=doc.data, surgical=True)

    before = parse(doc.text, fmt=doc.fmt)
    after = parse(doc.text, fmt=doc.fmt)
    _mutate(after, ops)
    changed = changed_paths(before, after)

    if doc.fmt == "json":
        return PatchResult(text=dump(after, fmt="json"), data=after, surgical=False,
                           changed=changed)

    text = doc.text
    surgical = True
    for op in ops:
        parts = pointer_parts(op["path"])
        if op["op"] != "replace" or not _exists(before, parts):
            surgical = False
            break
        spliced = _surgical_replace(text, parse(text, fmt="yaml"), parts, op["value"])
        if spliced is None:
            surgical = False
            break
        text = spliced

    if not surgical:
        text = dump(after, fmt="yaml")
        return PatchResult(text=text, data=after, surgical=False, changed=changed, reflowed=True)

    return PatchResult(text=text, data=parse(text, fmt="yaml"), surgical=True, changed=changed)


def apply_raw(doc: Document, raw: str) -> PatchResult:
    """Whole-file replacement (the Raw YAML tab). Parsed so callers can validate it."""
    text = raw if raw.endswith("\n") else raw + "\n"
    data = parse(text, fmt=doc.fmt)
    return PatchResult(
        text=text,
        data=data,
        surgical=False,
        changed=changed_paths(doc.data, data),
    )


# --------------------------------------------------------------------------- writing


def atomic_write(path: Path | str, text: str, *, mode: int = 0o600) -> Path:
    """Temp file in the same directory, fsync, ``os.replace`` — never a half-written config."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    keep = p.stat().st_mode & 0o777 if p.exists() else mode
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, keep)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return p


def iter_leaves(data: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Dotted path/value pairs for every scalar leaf, in document order."""
    yield from flatten(data, prefix).items()
