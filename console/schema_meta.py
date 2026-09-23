"""Turn a pydantic JSON Schema into the flat metadata the Settings form and Ctrl-K need.

``ops.config.config_schema()`` (and ``ops.models_config.models_schema()``) produce a nested
JSON Schema where every leaf carries ``description`` plus the ``x-*`` annotations
``ops.config.F`` enforces. The UI wants that as a flat, ordered list of paths:

    console.port                      leaf   group=console  protected  effects=[restart:console]
    universe.assets[]                 leaf   group=universe
    risk.max_weight.<key>             leaf   group=risk
    bounds.<key>.min                  leaf   group=bounds

Path grammar — three shapes, and nothing else:

* ``a.b``      an object property
* ``a.b[]``    the items of an array
* ``a.b.<key>``  the values of an ``additionalProperties`` map

``$ref``/``$defs`` are resolved, ``anyOf`` with a ``null`` branch collapses to the real
branch with ``nullable=True``, and recursion is bounded by ``max_depth`` so a self
referencing schema cannot hang the console.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from console.contracts import FieldMeta

X_TIER = "x-tier"
X_GROUP = "x-group"
X_UNIT = "x-unit"
X_WIDGET = "x-widget"
X_EFFECTS = "x-effects"
X_PROTECTED = "x-protected"
X_HELP = "x-help-md"
X_DEPRECATED = "x-deprecated"

#: JSON Schema keywords the form uses as input constraints.
CONSTRAINT_KEYS: tuple[str, ...] = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "multipleOf",
)

MAX_DEPTH = 24
ARRAY_SUFFIX = "[]"
MAP_KEY = "<key>"


class SchemaMetaError(ValueError):
    """The schema could not be walked (bad ``$ref``, wrong shape)."""


def _merge_all_of(node: Mapping[str, Any]) -> Mapping[str, Any]:
    """``{"allOf": [X], "description": ...}`` → ``X`` with the siblings kept.

    Pydantic wraps a model-typed field that also carries annotations this way in some
    versions and inlines the ``$ref`` in others; both shapes have to flatten the same.
    """
    branches = node.get("allOf")
    if not isinstance(branches, list) or len(branches) != 1 or not isinstance(branches[0], Mapping):
        return node
    merged = {k: v for k, v in branches[0].items()}
    merged.update({k: v for k, v in node.items() if k != "allOf"})
    return merged


def _resolve(node: Mapping[str, Any], root: Mapping[str, Any], _seen: int = 0) -> Mapping[str, Any]:
    """Follow ``$ref`` chains inside ``#/$defs/...`` and merge sibling keywords."""
    node = _merge_all_of(node)
    if "$ref" not in node:
        return node
    if _seen > MAX_DEPTH:
        raise SchemaMetaError("$ref chain too deep")
    ref = str(node["$ref"])
    if not ref.startswith("#/"):
        raise SchemaMetaError(f"unsupported $ref: {ref}")
    target: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(target, Mapping) or part not in target:
            raise SchemaMetaError(f"unresolvable $ref: {ref}")
        target = target[part]
    if not isinstance(target, Mapping):
        raise SchemaMetaError(f"unresolvable $ref: {ref}")
    merged = {**_resolve(target, root, _seen + 1)}
    merged.update({k: v for k, v in node.items() if k != "$ref"})
    return merged


def _collapse_nullable(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    """``anyOf: [X, null]`` → ``(X, True)``. Any other ``anyOf`` is kept as a union leaf."""
    branches = node.get("anyOf") or node.get("oneOf")
    if not isinstance(branches, list) or not branches:
        return node, False
    real = [b for b in branches if isinstance(b, Mapping) and b.get("type") != "null"]
    nullable = len(real) < len(branches)
    if len(real) == 1:
        merged = {k: v for k, v in node.items() if k not in ("anyOf", "oneOf")}
        merged.update({k: v for k, v in real[0].items() if k not in merged or k in ("type", "$ref")})
        return merged, nullable
    return node, nullable


def _kind(node: Mapping[str, Any]) -> str:
    if "properties" in node:
        return "object"
    node_type = node.get("type")
    if node_type == "array":
        return "array"
    if node_type == "object":
        return "map" if node.get("additionalProperties") not in (None, False) else "object"
    return "leaf"


def _type_name(node: Mapping[str, Any]) -> str | None:
    t = node.get("type")
    if isinstance(t, str):
        return t
    if "enum" in node:
        return "enum"
    if "const" in node:
        return "const"
    if "anyOf" in node or "oneOf" in node:
        return "union"
    return None


#: Annotations an array item or a map value inherits from its container when it has none
#: of its own — ``list[str]`` items and ``dict[str, float]`` values are never annotated by
#: pydantic, but the UI still needs their group, tier and unit.
INHERITED_KEYS: tuple[str, ...] = (X_TIER, X_GROUP, X_UNIT, X_WIDGET, X_EFFECTS, X_PROTECTED)


def _inheritable(node: Mapping[str, Any]) -> dict[str, Any]:
    return {k: node[k] for k in INHERITED_KEYS if k in node}


def _effects_only(inherited: Mapping[str, Any]) -> dict[str, Any] | None:
    """The one annotation an object property inherits from its parent.

    ``x-effects`` answers "what has to happen after this is saved", which is a property
    of the *subtree*: every leaf under ``risk`` regenerates the bot configs and restarts
    both bots, and annotating forty leaves identically would be a list to forget to
    extend. Tier, group and ``x-protected`` stay per-field — they describe the field, not
    the machinery behind it, and the schema-walk test already demands the first two on
    every leaf. A leaf that declares its own ``x-effects`` overrides the inherited value.
    """
    effects = inherited.get(X_EFFECTS)
    return {X_EFFECTS: effects} if effects else None


def _meta_for(
    path: str,
    node: Mapping[str, Any],
    *,
    required: bool,
    nullable: bool,
    inherit: Mapping[str, Any] | None = None,
) -> FieldMeta:
    base = {**(dict(inherit) if inherit else {}), **node}
    raw_effects = base.get(X_EFFECTS)
    effects = [str(e) for e in raw_effects] if isinstance(raw_effects, list) else []
    constraints = {k: node[k] for k in CONSTRAINT_KEYS if k in node}
    return FieldMeta(
        path=path,
        title=node.get("title"),
        type=_type_name(node),
        kind=_kind(node),  # type: ignore[arg-type]
        description=node.get("description"),
        tier=base.get(X_TIER),
        group=base.get(X_GROUP),
        unit=base.get(X_UNIT),
        widget=base.get(X_WIDGET),
        effects=effects,
        protected=bool(base.get(X_PROTECTED, False)),
        deprecated=bool(node.get(X_DEPRECATED, False)),
        help_md=node.get(X_HELP),
        enum=list(node["enum"]) if isinstance(node.get("enum"), list) else None,
        default=node.get("default"),
        nullable=nullable,
        required=required,
        constraints=constraints,
    )


def flatten(schema: Mapping[str, Any], *, max_depth: int = MAX_DEPTH) -> dict[str, FieldMeta]:
    """Every path in ``schema``, in declaration order, mapped to its metadata."""
    out: dict[str, FieldMeta] = {}
    _walk(schema, schema, "", out, required=True, depth=0, max_depth=max_depth, inherit=None)
    out.pop("", None)
    return out


def _walk(
    node: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    out: dict[str, FieldMeta],
    *,
    required: bool,
    depth: int,
    max_depth: int,
    inherit: Mapping[str, Any] | None,
) -> None:
    if depth > max_depth:
        return
    resolved = _resolve(node, root)
    resolved, nullable = _collapse_nullable(resolved)
    resolved = _resolve(resolved, root)
    if path:
        out[path] = _meta_for(path, resolved, required=required, nullable=nullable,
                              inherit=inherit)
    own = {**(dict(inherit) if inherit else {}), **_inheritable(resolved)}

    props = resolved.get("properties")
    if isinstance(props, Mapping):
        req = set(resolved.get("required") or ())
        for name, child in props.items():
            if not isinstance(child, Mapping):
                continue
            child_path = f"{path}.{name}" if path else str(name)
            _walk(child, root, child_path, out, required=name in req, depth=depth + 1,
                  max_depth=max_depth, inherit=_effects_only(own))

    items = resolved.get("items")
    if isinstance(items, Mapping):
        _walk(items, root, f"{path}{ARRAY_SUFFIX}", out, required=False, depth=depth + 1,
              max_depth=max_depth, inherit=own)

    extra = resolved.get("additionalProperties")
    if isinstance(extra, Mapping):
        _walk(extra, root, f"{path}.{MAP_KEY}" if path else MAP_KEY, out, required=False,
              depth=depth + 1, max_depth=max_depth, inherit=own)


def groups(index: Mapping[str, FieldMeta]) -> dict[str, list[str]]:
    """``x-group`` → the paths inside it, in schema order. Ungrouped paths land in ``other``."""
    out: dict[str, list[str]] = {}
    for path, meta in index.items():
        out.setdefault(meta.group or "other", []).append(path)
    return out


def field_at(index: Mapping[str, FieldMeta], path: str) -> FieldMeta | None:
    """Look a path up, tolerating concrete array indices and map keys.

    ``universe.assets.0`` and ``risk.max_weight.BTC`` resolve to ``universe.assets[]`` and
    ``risk.max_weight.<key>`` — what a diff or a blame line needs.
    """
    if path in index:
        return index[path]
    prefix = ""
    for segment in path.split("."):
        exact = f"{prefix}.{segment}" if prefix else segment
        wildcard_map = f"{prefix}.{MAP_KEY}" if prefix else MAP_KEY
        wildcard_arr = f"{prefix}{ARRAY_SUFFIX}"
        for candidate in (exact, wildcard_arr, wildcard_map):
            if candidate in index:
                prefix = candidate
                break
        else:
            return None
    return index.get(prefix)


def protected_paths(index: Mapping[str, FieldMeta]) -> list[str]:
    """Paths whose save needs step-up (``x-protected``)."""
    return [p for p, m in index.items() if m.protected]


def effects_for(index: Mapping[str, FieldMeta], changed: Iterable[str]) -> list[str]:
    """The union of ``x-effects`` implied by a set of changed paths, deduped and sorted."""
    out: set[str] = set()
    for path in changed:
        meta = field_at(index, path)
        if meta:
            out.update(meta.effects)
    return sorted(out)


def search_index(index: Mapping[str, FieldMeta], *, config_id: str = "earn") -> list[dict[str, Any]]:
    """Rows for the Ctrl-K palette: path, title, help text and where it lives."""
    rows: list[dict[str, Any]] = []
    for path, meta in index.items():
        if meta.kind != "leaf":
            continue
        rows.append(
            {
                "kind": "config",
                "config_id": config_id,
                "path": path,
                "title": meta.title or path.rsplit(".", 1)[-1],
                "group": meta.group,
                "text": meta.description or "",
                "protected": meta.protected,
                "tier": meta.tier,
            }
        )
    return rows
