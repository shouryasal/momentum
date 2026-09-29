"""The config store: read, validate, preview, save, revert — the only writer of tier-2 config.

`config/earn.yaml` is the system's constitution. Everything that touches it goes through
this module, so every change is validated by the same loader the jobs use, blessed with the
same digest preflight checks, audited into `config_audit`, and optionally committed. A
hand-edit is still possible — and the bless will catch it.

What lives here:

* the **registry** of editable files (schema-backed, plain, tier-1 params, read-only overlays);
* the **schema index**: the pydantic JSON Schema flattened to dotted paths carrying the
  ``x-*`` annotations, which is what drives the Settings form, the effects engine, the
  protected-path check and Ctrl-K search — a new pydantic field reaches all five with no
  other code change;
* **preview**: validate a candidate, diff it, name the changed paths, the protected ones,
  the effects and whether the save needs step-up or a typed phrase;
* **save**: optimistic concurrency on ``base_sha``, atomic write, snapshot, bless, audit,
  optional per-file git commit;
* **history / revert / blame**, all from ``config_audit`` plus the snapshot store.

Effects are *named* here and *applied* by :mod:`console.services.effects`; this module never
restarts a container.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from ops import config_io
from ops.config import CONFIG_VERSION, ConfigError, ConfigWarning, config_schema, load_config
from ops.config_io import Fmt
from ops.lib import audit as audit_lib
from ops.lib import config_guard, paths, signing
from ops.models_config import OVERLAY_PATH, load_models_cfg, models_schema

__all__ = [
    "ConfigFile",
    "ConfigStoreError",
    "ConflictError",
    "FieldMeta",
    "Issue",
    "LiveLockedError",
    "Preview",
    "REGISTRY",
    "SaveResult",
    "StepUpRequired",
    "ValidationFailed",
    "blame",
    "drift",
    "effects_for",
    "file_by_id",
    "history",
    "preview",
    "protected_for",
    "read",
    "registry",
    "revert",
    "save",
    "schema_index",
]


# --------------------------------------------------------------------------- errors


class ConfigStoreError(Exception):
    code = "config_error"
    status = 400


class ConflictError(ConfigStoreError):
    """``base_sha`` no longer matches the file: somebody else saved first."""

    code = "sha_conflict"
    status = 409

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"config changed on disk (expected {expected[:12]}, found {actual[:12]})")
        self.expected, self.actual = expected, actual


class ValidationFailed(ConfigStoreError):
    code = "invalid_config"
    status = 422

    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__(issues[0].msg if issues else "config is invalid")
        self.issues = list(issues)


class StepUpRequired(ConfigStoreError):
    """A protected path moved without step-up or without the typed phrase."""

    code = "step_up_required"
    status = 403

    def __init__(self, message: str, *, paths: Sequence[str], confirm_phrase: str) -> None:
        super().__init__(message)
        self.paths = list(paths)
        self.confirm_phrase = confirm_phrase


class LiveLockedError(ConfigStoreError):
    """Universe or live-mode keys cannot move while a sleeve is live."""

    code = "live_locked"
    status = 403

    def __init__(self, blocked: Sequence[str], sleeves: Sequence[str]) -> None:
        super().__init__(
            "refused while sleeve(s) "
            + ", ".join(sleeves)
            + " are live: "
            + ", ".join(blocked)
        )
        self.blocked = list(blocked)
        self.sleeves = list(sleeves)


class ReadOnlyError(ConfigStoreError):
    code = "read_only"
    status = 403


# --------------------------------------------------------------------------- registry


Kind = Literal["schema", "plain", "params", "overlay"]


@dataclass(frozen=True)
class ConfigFile:
    id: str
    rel: str
    fmt: Fmt
    title: str
    description: str
    kind: Kind
    editable: bool = True
    read_only_reason: str | None = None
    optional: bool = False

    @property
    def blessed(self) -> bool:
        return self.rel in config_guard.BLESSED_FILES

    def path(self, root: Path | None = None) -> Path:
        return (root or paths.REPO_ROOT) / self.rel


REGISTRY: tuple[ConfigFile, ...] = (
    ConfigFile(
        id="earn",
        rel="config/earn.yaml",
        fmt="yaml",
        title="Earn",
        description="Limits, universe, schedules — every job loads this file.",
        kind="schema",
    ),
    ConfigFile(
        id="models",
        rel="config/models.yaml",
        fmt="yaml",
        title="Models & routing",
        description="Providers, model aliases, per-task chains, switching and budgets.",
        kind="schema",
    ),
    ConfigFile(
        id="backtest",
        rel="config/backtest.yaml",
        fmt="yaml",
        title="Backtest costs",
        description="Fee and slippage assumptions; the costs block is recalibrated monthly.",
        kind="plain",
    ),
    ConfigFile(
        id="macro_calendar",
        rel="config/macro_calendar.yaml",
        fmt="yaml",
        title="Macro calendar",
        description="Scheduled CPI/FOMC events that become blackout flags.",
        kind="plain",
    ),
    ConfigFile(
        id="params-a",
        rel="config/params-sleeve-a.json",
        fmt="json",
        title="Sleeve A params",
        description="Tier-1 parameters. Changed through the change gate, inside bounds.",
        kind="params",
        editable=False,
        read_only_reason="tier-1: changed through changes/*.json and runs/apply_changes.py",
    ),
    ConfigFile(
        id="params-b",
        rel="config/params-sleeve-b.json",
        fmt="json",
        title="Sleeve B params",
        description="Tier-1 parameters. Changed through the change gate, inside bounds.",
        kind="params",
        editable=False,
        read_only_reason="tier-1: changed through changes/*.json and runs/apply_changes.py",
    ),
    ConfigFile(
        id="models-auto",
        rel="config/models-auto.yaml",
        fmt="yaml",
        title="Model overlay (auto)",
        description="Tier-1 overlay: chain[0] and the shadow block only.",
        kind="overlay",
        editable=False,
        read_only_reason="tier-1 overlay written by the change gate",
        optional=True,
    ),
    ConfigFile(
        id="skills-registry",
        rel="config/skills-registry.auto.yaml",
        fmt="yaml",
        title="Skill bindings (auto)",
        description="Tier-1 overlay over skills.bindings.",
        kind="overlay",
        editable=False,
        read_only_reason="tier-1 overlay written by the change gate",
        optional=True,
    ),
    ConfigFile(
        id="prompts-auto",
        rel="config/prompts-auto.yaml",
        fmt="yaml",
        title="Prompt overlay (auto)",
        description="Tier-1 overlay pinning active prompt versions.",
        kind="overlay",
        editable=False,
        read_only_reason="tier-1 overlay written by the change gate",
        optional=True,
    ),
)

_BY_ID = {f.id: f for f in REGISTRY}


def file_by_id(file_id: str) -> ConfigFile:
    try:
        return _BY_ID[file_id]
    except KeyError as e:
        raise ConfigStoreError(f"unknown config file: {file_id!r}") from e


#: Tier-2 prefixes that always need step-up plus a typed phrase, whatever the schema says.
#: Spec §3: "Human-only (tier 2, x-protected)".
PROTECTED_PREFIXES: tuple[str, ...] = (
    "risk",
    "bounds",
    "universe",
    "modes.live",
    "trading",
    "autonomy",
    "security",
    "git",
    "console",
    "runtime",
    "exchange",
    "paths",
    # Selecting a profile overlay re-renders both bots' mechanics from a different file.
    "profiles",
)
PROTECTED_EXACT: tuple[str, ...] = ("sleeves.a.strategy", "sleeves.b.strategy")

#: Paths that may not move while any sleeve is live — changing what Earn may trade, or the
#: live ceilings, under an open position is exactly the accident the console exists to stop.
#: ``profiles`` is here for the same reason: switching the profile switches the strategy
#: class and the timeframe out from under an open book.
LIVE_LOCKED_PREFIXES: tuple[str, ...] = ("universe", "modes.live", "sleeves.a.strategy",
                                         "sleeves.b.strategy", "profiles")


def confirm_phrase_for(file_id: str) -> str:
    """The phrase the operator types to save a protected change."""
    return f"SAVE {file_id.upper()}"


# --------------------------------------------------------------------------- schema index


@dataclass(frozen=True)
class FieldMeta:
    """One schema leaf, flattened. ``*`` stands for a dict key or a list index."""

    path: str
    title: str
    description: str
    type: str
    tier: str = "human"
    group: str = ""
    unit: str | None = None
    widget: str | None = None
    effects: tuple[str, ...] = ()
    protected: bool = False
    help_md: str | None = None
    enum: tuple[Any, ...] | None = None
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    deprecated: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["effects"] = list(self.effects)
        d["enum"] = list(self.enum) if self.enum is not None else None
        return d


def _deref(node: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` / ``anyOf`` into something with ``properties`` or a ``type``."""
    seen = 0
    cur: dict[str, Any] = dict(node)
    while seen < 10:
        seen += 1
        ref = cur.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1])
            if not isinstance(target, Mapping):
                return cur
            merged = {k: v for k, v in cur.items() if k != "$ref"}
            cur = {**dict(target), **merged}
            continue
        options = cur.get("anyOf") or cur.get("oneOf")
        if isinstance(options, list):
            picked = next(
                (o for o in options if isinstance(o, Mapping) and o.get("type") != "null"),
                None,
            )
            if isinstance(picked, Mapping):
                merged = {k: v for k, v in cur.items() if k not in ("anyOf", "oneOf")}
                cur = {**dict(picked), **merged}
                continue
        return cur
    return cur


def _meta_from(node: Mapping[str, Any], path: str, inherited: Mapping[str, Any]) -> FieldMeta:
    effects = node.get("x-effects") or inherited.get("effects") or ()
    return FieldMeta(
        path=path,
        title=str(node.get("title") or path.rsplit(".", 1)[-1].replace("_", " ").title()),
        description=str(node.get("description") or ""),
        type=str(node.get("type") or ("object" if "properties" in node else "any")),
        tier=str(node.get("x-tier") or inherited.get("tier") or "human"),
        group=str(node.get("x-group") or inherited.get("group") or ""),
        unit=node.get("x-unit"),
        widget=node.get("x-widget"),
        effects=tuple(effects),
        protected=bool(node.get("x-protected") or inherited.get("protected") or False),
        help_md=node.get("x-help-md"),
        enum=tuple(node["enum"]) if isinstance(node.get("enum"), list) else None,
        default=node.get("default"),
        minimum=node.get("minimum", node.get("exclusiveMinimum")),
        maximum=node.get("maximum", node.get("exclusiveMaximum")),
        deprecated=bool(node.get("deprecated") or node.get("x-deprecated") or False),
    )


def schema_index(schema: Mapping[str, Any]) -> dict[str, FieldMeta]:
    """Flatten a pydantic JSON Schema to ``dotted path -> FieldMeta``.

    Dict values and list items become a ``*`` segment, so ``risk.max_weight.BTC`` resolves
    through ``risk.max_weight.*``. Annotations inherit downwards: marking a section
    ``x-protected`` protects everything under it.
    """
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), Mapping) else {}
    out: dict[str, FieldMeta] = {}

    def walk(node: Mapping[str, Any], path: str, inherited: dict[str, Any], depth: int) -> None:
        if depth > 12:
            return
        node = _deref(node, defs)
        meta = _meta_from(node, path, inherited) if path else None
        if meta is not None:
            out[path] = meta
        down = {
            "tier": meta.tier if meta else inherited.get("tier"),
            "group": meta.group if meta else inherited.get("group"),
            "protected": meta.protected if meta else inherited.get("protected"),
            "effects": meta.effects if meta and meta.effects else inherited.get("effects"),
        }
        props = node.get("properties")
        if isinstance(props, Mapping):
            for key, child in props.items():
                if isinstance(child, Mapping):
                    walk(child, f"{path}.{key}" if path else str(key), down, depth + 1)
        extra = node.get("additionalProperties")
        if isinstance(extra, Mapping):
            walk(extra, f"{path}.*" if path else "*", down, depth + 1)
        items = node.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{path}.*" if path else "*", down, depth + 1)

    walk(schema, "", {}, 0)
    return out


def _wildcard_candidates(path: str) -> list[str]:
    """Every wildcard form of a concrete path, most specific first."""
    parts = path.split(".")
    seen: list[str] = [path]
    for i in range(len(parts) - 1, -1, -1):
        wild = parts[:]
        wild[i] = "*"
        cand = ".".join(wild)
        if cand not in seen:
            seen.append(cand)
        # also allow several wildcards (dict of dicts)
        for j in range(i - 1, -1, -1):
            w2 = wild[:]
            w2[j] = "*"
            c2 = ".".join(w2)
            if c2 not in seen:
                seen.append(c2)
    return seen


def resolve_meta(index: Mapping[str, FieldMeta], path: str) -> FieldMeta | None:
    """The schema entry for a concrete dotted path, honouring ``*`` segments and ancestors."""
    for cand in _wildcard_candidates(path):
        hit = index.get(cand)
        if hit is not None:
            return hit
    parts = path.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:cut])
        for cand in _wildcard_candidates(prefix):
            hit = index.get(cand)
            if hit is not None:
                return hit
    return None


def index_for(file_id: str) -> dict[str, FieldMeta]:
    """The schema index of a file, or ``{}`` for files with no pydantic model."""
    if file_id == "earn":
        return schema_index(config_schema())
    if file_id == "models":
        return schema_index(models_schema())
    return {}


def schema_for(file_id: str) -> dict[str, Any] | None:
    if file_id == "earn":
        return config_schema()
    if file_id == "models":
        return models_schema()
    return None


# --------------------------------------------------------------------------- annotations


def effects_for(file_id: str, changed: Iterable[str]) -> list[str]:
    """Which ``x-effects`` a set of changed paths implies, deduplicated and sorted.

    The schema is the only source. ``ops.config`` hangs ``x-effects`` on the section
    fields (``risk``, ``trading``, ``universe``, ``exchange``, ``sleeves``, …) and
    :func:`schema_index` inherits them downward, so a leaf with no annotation of its own
    still answers correctly. This module used to keep a second, private ``SECTION_EFFECTS``
    table for exactly that job — which the console's own ``console.schema_meta`` could not
    see, so the two disagreed about what a save implied. One owner now.
    """
    index = index_for(file_id)
    out: set[str] = set()
    for path in changed:
        meta = resolve_meta(index, path)
        if meta is not None and meta.effects:
            out.update(meta.effects)
    if file_id == "models" and changed:
        out.add("regen")
    return sorted(out)


def _matches_prefix(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == p or path.startswith(p + ".") for p in prefixes)


def protected_for(file_id: str, changed: Iterable[str]) -> list[str]:
    """Changed paths that need step-up plus a typed confirmation."""
    index = index_for(file_id)
    out: list[str] = []
    for path in changed:
        if file_id == "earn" and (
            _matches_prefix(path, PROTECTED_PREFIXES) or path in PROTECTED_EXACT
        ):
            out.append(path)
            continue
        meta = resolve_meta(index, path)
        if meta is not None and meta.protected:
            out.append(path)
    return sorted(set(out))


def live_locked(file_id: str, changed: Iterable[str], *, live_sleeves: Sequence[str]) -> list[str]:
    if file_id != "earn" or not live_sleeves:
        return []
    return sorted({p for p in changed if _matches_prefix(p, LIVE_LOCKED_PREFIXES)})


def _live_sleeves() -> list[str]:
    try:
        from ops.lib import mode_state

        return list(mode_state.load().live_sleeves())
    except Exception:  # noqa: BLE001 - fail closed is the mode module's job, not ours
        return []


# --------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class Issue:
    loc: str
    msg: str
    kind: str = "value_error"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strip_legacy(raw: Any) -> Any:
    try:
        from ops.config import _strip_legacy as strip  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the loader always ships it
        return raw
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConfigWarning)
        return strip(dict(raw or {}), "<preview>")


def _pydantic_issues(raw: Any) -> list[Issue]:
    from pydantic import ValidationError

    from ops.config import EarnConfig

    try:
        EarnConfig.model_validate(_strip_legacy(raw))
    except ValidationError as e:
        return [
            Issue(loc=".".join(str(p) for p in err.get("loc", ())), msg=str(err.get("msg")),
                  kind=str(err.get("type", "value_error")))
            for err in e.errors()[:25]
        ]
    except Exception:  # noqa: BLE001 - a non-validation failure is reported by the caller
        return []
    return []


def _tmp_file(text: str, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="earn-config-", suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return Path(name)


def _bounds_issues(values: Mapping[str, Any], *, sleeve: str, root: Path | None) -> list[Issue]:
    """Tier-1 params must sit inside ``bounds:`` from earn.yaml."""
    try:
        cfg = load_config(root=root)
    except ConfigError:
        return []
    flat = config_io.flatten(values.get("params") or {})
    issues: list[Issue] = []
    for key, bound in cfg.bounds.items():
        if key.startswith(f"sleeve_{sleeve}."):
            leaf = key.split(".", 1)[1]
        elif key.startswith("execution."):
            leaf = key.split(".", 1)[1]
        else:
            continue
        if leaf not in flat:
            continue
        value = flat[leaf]
        if not isinstance(value, (int, float)):
            continue
        if bound.min is not None and value < bound.min:
            issues.append(Issue(loc=f"params.{leaf}", msg=f"below bound {bound.min}"))
        if bound.max is not None and value > bound.max:
            issues.append(Issue(loc=f"params.{leaf}", msg=f"above bound {bound.max}"))
    return issues


def validate(file_id: str, text: str, *, root: Path | None = None) -> list[Issue]:
    """Run a candidate file through the same loader the jobs use."""
    spec = file_by_id(file_id)
    try:
        data = config_io.parse(text, fmt=spec.fmt)
    except Exception as e:  # noqa: BLE001 - any parse error is a user-visible issue
        return [Issue(loc="", msg=f"{spec.fmt} parse error: {e}", kind="parse_error")]
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        return [Issue(loc="", msg="the document must be a mapping", kind="shape_error")]

    if file_id == "earn":
        tmp = _tmp_file(text, ".yaml")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ConfigWarning)
                load_config(tmp, root=root)
        except ConfigError as e:
            return _pydantic_issues(data) or [Issue(loc="", msg=str(e))]
        finally:
            tmp.unlink(missing_ok=True)
        return []

    if file_id == "models":
        tmp = _tmp_file(text, ".yaml")
        try:
            load_models_cfg(tmp, overlay=OVERLAY_PATH)
        except ConfigError as e:
            return [Issue(loc="", msg=str(e))]
        finally:
            tmp.unlink(missing_ok=True)
        return []

    if file_id in ("params-a", "params-b"):
        return _bounds_issues(data, sleeve=file_id[-1], root=root)

    if file_id == "backtest":
        costs = data.get("costs")
        if not isinstance(costs, Mapping):
            return [Issue(loc="costs", msg="backtest.yaml needs a costs mapping")]
        for key in ("fee_bps", "slippage_bps"):
            value = costs.get(key)
            if not isinstance(value, (int, float)) or value < 0:
                return [Issue(loc=f"costs.{key}", msg="must be a non-negative number")]
        return []

    if file_id == "macro_calendar":
        events = data.get("events")
        if not isinstance(events, list):
            return [Issue(loc="events", msg="macro_calendar.yaml needs an events list")]
        for i, ev in enumerate(events):
            if not isinstance(ev, Mapping) or "name" not in ev or "at" not in ev:
                return [Issue(loc=f"events.{i}", msg="each event needs name and at")]
        return []

    return []


# --------------------------------------------------------------------------- read


def _read_doc(spec: ConfigFile, root: Path | None) -> config_io.Document:
    path = spec.path(root)
    if not path.exists():
        if spec.optional:
            return config_io.Document(path=path, rel=spec.rel, text="", fmt=spec.fmt, data={})
        raise ConfigStoreError(f"missing config file: {spec.rel}")
    return config_io.read_document(path, rel=spec.rel)


def registry(root: Path | None = None) -> list[dict[str, Any]]:
    """What ``GET /api/config`` returns: one row per editable or readable file."""
    bless = config_guard.verify(root=root)
    out: list[dict[str, Any]] = []
    for spec in REGISTRY:
        path = spec.path(root)
        exists = path.exists()
        out.append(
            {
                "id": spec.id,
                "rel": spec.rel,
                "title": spec.title,
                "description": spec.description,
                "kind": spec.kind,
                "format": spec.fmt,
                "editable": spec.editable,
                "read_only_reason": spec.read_only_reason,
                "blessed_file": spec.blessed,
                "exists": exists,
                "sha": signing.sha256_file(path) if exists else None,
                "has_schema": spec.kind == "schema",
                "bless_ok": bless.ok if spec.blessed else None,
            }
        )
    return out


def read(
    file_id: str,
    *,
    root: Path | None = None,
    conn: Any = None,
    include_schema: bool = True,
) -> dict[str, Any]:
    """Everything the Settings page needs for one file."""
    spec = file_by_id(file_id)
    doc = _read_doc(spec, root)
    index = index_for(file_id) if include_schema else {}
    bless = config_guard.verify(root=root)
    return {
        "id": spec.id,
        "rel": spec.rel,
        "title": spec.title,
        "description": spec.description,
        "kind": spec.kind,
        "format": spec.fmt,
        "editable": spec.editable,
        "read_only_reason": spec.read_only_reason,
        "schema": schema_for(file_id) if include_schema else None,
        "ui": [m.as_dict() for m in index.values()] if include_schema else [],
        "groups": sorted({m.group for m in index.values() if m.group}),
        "values": doc.values,
        "raw": doc.text,
        "sha": doc.sha,
        "blame": blame(conn, spec.rel) if conn is not None else {},
        "bless": {
            "ok": bless.ok,
            "reason": bless.reason,
            "blessed_at": bless.blessed_at,
            "blessed_by": bless.blessed_by,
            "changed": bless.changed,
            "file_is_blessed": spec.blessed,
        },
        "confirm_phrase": confirm_phrase_for(spec.id),
        "live_sleeves": _live_sleeves(),
        "config_version": CONFIG_VERSION if file_id == "earn" else None,
    }


# --------------------------------------------------------------------------- preview


@dataclass(frozen=True)
class Preview:
    file_id: str
    valid: bool
    errors: list[Issue] = field(default_factory=list)
    diff: str = ""
    changed_paths: list[str] = field(default_factory=list)
    protected_changed: list[str] = field(default_factory=list)
    locked_paths: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    restarts: list[str] = field(default_factory=list)
    requires_stepup: bool = False
    requires_confirm: bool = False
    confirm_phrase: str = ""
    reflowed: bool = False
    base_sha: str = ""
    new_sha: str = ""
    new_text: str = ""

    def as_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        d = {
            "file_id": self.file_id,
            "valid": self.valid,
            "errors": [e.as_dict() for e in self.errors],
            "diff": self.diff,
            "changed_paths": self.changed_paths,
            "protected_changed": self.protected_changed,
            "locked_paths": self.locked_paths,
            "effects": self.effects,
            "restarts": self.restarts,
            "requires_stepup": self.requires_stepup,
            "requires_confirm": self.requires_confirm,
            "confirm_phrase": self.confirm_phrase,
            "reflowed": self.reflowed,
            "base_sha": self.base_sha,
            "new_sha": self.new_sha,
        }
        if include_text:
            d["new_text"] = self.new_text
        return d


def _restarts_of(effects: Sequence[str]) -> list[str]:
    return sorted(e.split(":", 1)[1] for e in effects if e.startswith("restart:"))


def preview(
    file_id: str,
    *,
    patch: Sequence[Mapping[str, Any]] | None = None,
    raw: str | None = None,
    base_sha: str | None = None,
    root: Path | None = None,
    check_sha: bool = True,
) -> Preview:
    """Validate a candidate save and describe everything it implies. No writes."""
    spec = file_by_id(file_id)
    doc = _read_doc(spec, root)
    if check_sha and base_sha is not None and base_sha != doc.sha:
        raise ConflictError(base_sha, doc.sha)
    try:
        result = (
            config_io.apply_raw(doc, raw)
            if raw is not None
            else config_io.apply_patch(doc, patch or [])
        )
    except config_io.PatchError as e:
        raise ValidationFailed([Issue(loc="", msg=str(e), kind="patch_error")]) from e
    except Exception as e:  # noqa: BLE001 - an unparseable paste is a validation error
        raise ValidationFailed(
            [Issue(loc="", msg=f"{spec.fmt} parse error: {e}", kind="parse_error")]
        ) from e

    errors = validate(file_id, result.text, root=root)
    changed = result.changed
    protected = protected_for(file_id, changed)
    locked = live_locked(file_id, changed, live_sleeves=_live_sleeves())
    effects = effects_for(file_id, changed)
    return Preview(
        file_id=file_id,
        valid=not errors,
        errors=errors,
        diff=config_io.diff_text(doc.text, result.text, rel=spec.rel),
        changed_paths=changed,
        protected_changed=protected,
        locked_paths=locked,
        effects=effects,
        restarts=_restarts_of(effects),
        requires_stepup=bool(protected),
        requires_confirm=bool(protected),
        confirm_phrase=confirm_phrase_for(file_id),
        reflowed=result.reflowed,
        base_sha=doc.sha,
        new_sha=result.sha,
        new_text=result.text,
    )


# --------------------------------------------------------------------------- snapshots


def snapshot_dir(file_id: str, root: Path | None = None) -> Path:
    base = paths.var_dir() / "config_history" / file_id
    return base


def _snapshot(file_id: str, text: str, *, fmt: Fmt) -> str:
    """Keep every version we have ever written, keyed by its sha. Revert reads these back."""
    sha = config_io.sha_of(text)
    target = snapshot_dir(file_id) / f"{sha}.{fmt}"
    if not target.exists():
        paths.ensure_dir(target.parent)
        config_io.atomic_write(target, text)
    return sha


def snapshot_text(file_id: str, sha: str, *, fmt: Fmt = "yaml") -> str | None:
    target = snapshot_dir(file_id) / f"{sha}.{fmt}"
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


# --------------------------------------------------------------------------- git


def git_commit(rel: str, message: str, *, root: Path | None = None) -> str | None:
    """Commit exactly one file. Returns the sha, or ``None`` when git is unavailable."""
    base = root or paths.REPO_ROOT
    if not (base / ".git").exists():
        return None
    try:
        subprocess.run(
            ["git", "add", "--", rel], cwd=base, check=True, capture_output=True, timeout=30
        )
        proc = subprocess.run(
            ["git", "commit", "-m", message, "--", rel],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=base, capture_output=True, text=True, timeout=30
        )
        return head.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- save


@dataclass(frozen=True)
class SaveResult:
    file_id: str
    rel: str
    sha: str
    before_sha: str
    audit_id: int | None
    changed_paths: list[str]
    protected_changed: list[str]
    effects: list[str]
    restarts: list[str]
    diff: str
    blessed: bool
    git_commit: str | None
    reflowed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def save(
    file_id: str,
    *,
    patch: Sequence[Mapping[str, Any]] | None = None,
    raw: str | None = None,
    base_sha: str,
    reason: str,
    actor: str,
    conn: Any = None,
    root: Path | None = None,
    commit: bool = False,
    step_up: bool = False,
    confirm_phrase: str | None = None,
    secret: str | None = None,
    allow_read_only: bool = False,
    applied: bool = False,
) -> SaveResult:
    """Write a validated change: atomic, snapshotted, blessed, audited, maybe committed."""
    spec = file_by_id(file_id)
    if not spec.editable and not allow_read_only:
        raise ReadOnlyError(f"{spec.rel} is read-only: {spec.read_only_reason}")

    pv = preview(file_id, patch=patch, raw=raw, base_sha=base_sha, root=root)
    if not pv.valid:
        raise ValidationFailed(pv.errors)
    if not pv.changed_paths and pv.new_sha == pv.base_sha:
        raise ConfigStoreError("nothing to save: the file is unchanged")
    if pv.locked_paths:
        raise LiveLockedError(pv.locked_paths, _live_sleeves())
    if pv.protected_changed:
        want = confirm_phrase_for(file_id)
        if not step_up:
            raise StepUpRequired(
                "protected keys need step-up: " + ", ".join(pv.protected_changed),
                paths=pv.protected_changed,
                confirm_phrase=want,
            )
        if (confirm_phrase or "").strip() != want:
            raise StepUpRequired(
                f"type {want!r} to confirm a protected change",
                paths=pv.protected_changed,
                confirm_phrase=want,
            )

    doc_path = spec.path(root)
    before_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    if before_text:
        _snapshot(file_id, before_text, fmt=spec.fmt)
    config_io.atomic_write(doc_path, pv.new_text)
    _snapshot(file_id, pv.new_text, fmt=spec.fmt)

    blessed = False
    bless_sig: str | None = None
    if spec.blessed and signing.get_secret() is not None:
        try:
            payload = config_guard.bless(actor, reason=reason, root=root, secret=secret)
            blessed = True
            bless_sig = str(payload.get("sig"))
        except Exception:  # noqa: BLE001 - a failed bless must not lose the saved file
            blessed = False

    sha_after = config_io.sha_of(pv.new_text)
    commit_sha = (
        git_commit(spec.rel, f"config({file_id}): {reason}"[:160], root=root) if commit else None
    )

    audit_id: int | None = None
    if conn is not None:
        audit_id = audit_lib.record_config(
            conn,
            actor=actor,
            file=spec.rel,
            before_sha=pv.base_sha,
            after_sha=sha_after,
            changed_paths=pv.changed_paths,
            diff=pv.diff,
            reason=reason,
            protected_changed=bool(pv.protected_changed),
            effects=pv.effects,
            applied=applied,
            git_commit=commit_sha,
            bless_sig=bless_sig,
        )
        audit_lib.try_record(
            conn,
            actor=actor,
            action="config.save",
            target=spec.rel,
            detail={
                "changed_paths": pv.changed_paths,
                "effects": pv.effects,
                "protected": bool(pv.protected_changed),
                "config_audit_id": audit_id,
                "git_commit": commit_sha,
            },
            result="ok",
        )

    return SaveResult(
        file_id=file_id,
        rel=spec.rel,
        sha=sha_after,
        before_sha=pv.base_sha,
        audit_id=audit_id,
        changed_paths=pv.changed_paths,
        protected_changed=pv.protected_changed,
        effects=pv.effects,
        restarts=pv.restarts,
        diff=pv.diff,
        blessed=blessed,
        git_commit=commit_sha,
        reflowed=pv.reflowed,
    )


# --------------------------------------------------------------------------- history


def history(conn: Any, file_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first, with a flag saying whether the pre-change text can still be restored."""
    spec = file_by_id(file_id)
    if conn is None:
        return []
    rows = audit_lib.config_history(conn, spec.rel, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        before = row.get("before_sha")
        out.append(
            {
                "id": row.get("id"),
                "ts_utc": row.get("ts_utc"),
                "actor": row.get("actor"),
                "reason": row.get("reason"),
                "before_sha": before,
                "after_sha": row.get("after_sha"),
                "changed_paths": _json_list(row.get("changed_paths_json")),
                "effects": _json_list(row.get("effects_json")),
                "protected_changed": bool(row.get("protected_changed")),
                "applied": bool(row.get("applied")),
                "git_commit": row.get("git_commit"),
                "diff": row.get("diff"),
                "revertable": bool(before)
                and snapshot_text(file_id, str(before), fmt=spec.fmt) is not None,
            }
        )
    return out


def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def revert(
    file_id: str,
    audit_id: int,
    *,
    conn: Any,
    actor: str,
    root: Path | None = None,
    step_up: bool = False,
    confirm_phrase: str | None = None,
    commit: bool = False,
    secret: str | None = None,
) -> SaveResult:
    """Restore the file exactly as it stood before ``audit_id``, through the normal save path."""
    spec = file_by_id(file_id)
    row = next((r for r in audit_lib.config_history(conn, spec.rel, limit=500)
                if int(r.get("id") or 0) == int(audit_id)), None)
    if row is None:
        raise ConfigStoreError(f"no config_audit row {audit_id} for {spec.rel}")
    before = row.get("before_sha")
    if not before:
        raise ConfigStoreError(f"config_audit row {audit_id} has no before_sha to revert to")
    text = snapshot_text(file_id, str(before), fmt=spec.fmt)
    if text is None:
        raise ConfigStoreError(f"no snapshot for {spec.rel}@{str(before)[:12]}")
    doc = _read_doc(spec, root)
    return save(
        file_id,
        raw=text,
        base_sha=doc.sha,
        reason=f"revert to {str(before)[:12]} (config_audit {audit_id})",
        actor=actor,
        conn=conn,
        root=root,
        commit=commit,
        step_up=step_up,
        confirm_phrase=confirm_phrase,
        secret=secret,
        allow_read_only=True,
    )


# --------------------------------------------------------------------------- blame


def blame(conn: Any, rel: str, *, limit: int = 500) -> dict[str, dict[str, Any]]:
    """Dotted path → who last changed it and when, from ``config_audit.changed_paths_json``."""
    if conn is None:
        return {}
    try:
        rows = audit_lib.config_history(conn, rel, limit=limit)
    except Exception:  # noqa: BLE001 - blame is decoration; never fail a page over it
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:  # newest first: first writer wins
        for path in _json_list(row.get("changed_paths_json")):
            if path not in out:
                out[str(path)] = {
                    "actor": row.get("actor"),
                    "ts_utc": row.get("ts_utc"),
                    "audit_id": row.get("id"),
                    "reason": row.get("reason"),
                }
    return out


def default_for(file_id: str, path: str) -> Any:
    """The schema default for one dotted path — what "revert to default" writes."""
    meta = resolve_meta(index_for(file_id), path)
    return None if meta is None else meta.default


# --------------------------------------------------------------------------- drift


def drift(root: Path | None = None) -> dict[str, Any]:
    """``gen_freqtrade_config --check`` (and ``gen_ops_files --check`` when P1 has landed)."""
    results: list[dict[str, Any]] = []
    base = root or paths.REPO_ROOT
    for module, args in (
        ("ops.gen_freqtrade_config", ["--check"]),
        ("ops.gen_ops_files", ["--check"]),
    ):
        try:
            proc = subprocess.run(
                # `sys.executable`, never a bare "python": the console runs from the venv
                # and its systemd unit inherits a PATH that need not have `python` on it
                # at all, which turned every drift report into "not runnable".
                [sys.executable, "-m", module, *args],
                cwd=base,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as e:
            results.append({"generator": module, "ok": None, "detail": f"not runnable: {e}"})
            continue
        missing = "No module named" in (proc.stderr or "")
        results.append(
            {
                "generator": module,
                "ok": None if missing else proc.returncode == 0,
                "detail": (proc.stdout or proc.stderr or "").strip()[:4000],
            }
        )
    bless = config_guard.verify(root=root)
    return {
        "ok": all(r["ok"] is not False for r in results) and bless.ok,
        "generators": results,
        "bless": {"ok": bless.ok, "reason": bless.reason, "changed": bless.changed},
    }


def load_yaml(path: Path) -> Any:
    """Small helper for callers that only need the values of a plain YAML file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))
