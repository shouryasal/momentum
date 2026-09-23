"""Turning our JSON Schemas into something the agent CLI's validator will accept.

Every structured task in this system — ``classify``, ``scan``/screen, ``validate``,
``flags``, ``decide`` — hands a schema to the model and then validates the answer against
that **same** schema. The two uses have different audiences and, it turns out, different
requirements:

* **the result check** is ours. It runs under ``jsonschema`` with the full document,
  ``$schema: draft/2020-12`` included, and it is the reason a malformed proposal is
  rejected rather than traded. It must not change.
* **the wire copy** is the model provider's. The Claude CLI compiles ``--json-schema``
  with **Ajv in strict mode against draft-07**, and Ajv refuses anything it does not
  recognise *before* the request is authenticated, let alone sent::

      $ claude -p --json-schema "$(cat schemas/proposal.json)"
      Error: --json-schema is not a valid JSON Schema:
             no schema with key or ref "https://json-schema.org/draft/2020-12/schema"

  That is not a model refusal or a bad prompt: the call dies on the host. Since every
  schema in ``schemas/`` declares the 2020-12 meta-schema, *every* structured call was
  rejected — the classifier, the screener, the validator and the decide stage alike.

Probed against the real CLI (2.1.280), Ajv rejects: a ``$schema`` that is not draft-07,
``$anchor``, any ``x-`` vendor keyword, the 2020-12-only keywords (``prefixItems``,
``unevaluated*``, ``dependent*``, ``min/maxContains``), and any ``$ref`` it cannot resolve
— external URIs and dangling local pointers both. It accepts local ``$ref`` into ``$defs``
or ``definitions``, ``$ref`` with sibling annotations, ``format``, ``const``,
``patternProperties``, union ``type`` arrays and an empty ``{}`` subschema.

:func:`wire_schema` therefore returns a **copy** narrowed to what Ajv compiles, and the
caller keeps the original for validation. The rule is deliberately an allow-list: Ajv's
strict mode fails on *any* unknown keyword, so a keyword this module has never heard of
would break the call if it were passed through, and dropping it only ever loosens the
constraint the model is generated under — never the constraint the answer is judged by.

Dropping is safe precisely because it is one-sided. A looser wire schema can let a bad
answer *out of the model*; it cannot let a bad answer *past the gate*, because the gate
still validates against the untouched original.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

__all__ = [
    "DRAFT07_KEYWORDS",
    "SchemaRewrite",
    "rewrite",
    "wire_schema",
]

#: Annotation keywords Ajv knows in draft-07. They carry meaning for the model (a
#: ``description`` is half the prompt) so they are kept.
_ANNOTATIONS = frozenset({
    "title", "description", "default", "examples", "readOnly", "writeOnly",
    "deprecated", "$comment",
})

#: Assertion keywords whose values are data, never subschemas — copied verbatim.
_ASSERTIONS = frozenset({
    "type", "enum", "const", "multipleOf", "maximum", "exclusiveMaximum", "minimum",
    "exclusiveMinimum", "maxLength", "minLength", "pattern", "maxItems", "minItems",
    "uniqueItems", "maxProperties", "minProperties", "required", "format",
    "contentMediaType", "contentEncoding",
})

#: Keywords whose value is a single subschema (``additionalProperties`` and
#: ``additionalItems`` may also be a bare boolean, which is copied as-is).
_SUBSCHEMA = frozenset({
    "additionalProperties", "additionalItems", "contains", "propertyNames", "not",
    "if", "then", "else",
})

#: Keywords whose value is a list of subschemas.
_SUBSCHEMA_LIST = frozenset({"allOf", "anyOf", "oneOf"})

#: Keywords whose value is a *map* of arbitrary names to subschemas. The names are data
#: (a property called ``type`` is a property, not a keyword), so only the values recurse.
_SUBSCHEMA_MAP = frozenset({"properties", "patternProperties", "$defs", "definitions"})

#: ``items`` is a subschema or a list of them; ``dependencies`` maps a name to either a
#: subschema or a list of property names; ``$ref`` is handled on its own.
_SPECIAL = frozenset({"items", "dependencies", "$ref"})

#: Everything Ajv compiles in draft-07. Anything else is dropped from the wire copy.
DRAFT07_KEYWORDS: frozenset[str] = (
    _ANNOTATIONS | _ASSERTIONS | _SUBSCHEMA | _SUBSCHEMA_LIST | _SUBSCHEMA_MAP | _SPECIAL
)

#: Where a local ``$ref`` may point and still be resolvable after the rewrite.
_REF_CONTAINERS = ("$defs", "definitions", "properties", "patternProperties")


@dataclass
class SchemaRewrite:
    """The wire copy plus what had to go, so a caller can say why in a log line."""

    schema: dict[str, Any]
    dropped_keywords: list[str] = field(default_factory=list)
    dropped_refs: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped_keywords or self.dropped_refs)

    def summary(self) -> str:
        parts = []
        if self.dropped_keywords:
            parts.append("dropped keywords: " + ", ".join(sorted(set(self.dropped_keywords))))
        if self.dropped_refs:
            parts.append("dropped refs: " + ", ".join(sorted(set(self.dropped_refs))))
        return "; ".join(parts)


def _pointer_resolves(root: Any, ref: str) -> bool:
    """Does ``ref`` name something that still exists in the rewritten document?

    Only same-document pointers can: an absolute URI has nothing to resolve against once
    the schema is a bare ``--json-schema`` argument, which is exactly what Ajv says.
    """
    if ref == "#":
        return True
    if not ref.startswith("#/"):
        return False
    node = root
    for raw in ref[2:].split("/"):
        token = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                return False
            node = node[token]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return True


def _node(value: Any, out: SchemaRewrite, root: Mapping[str, Any]) -> Any:
    """Rewrite one subschema. ``True``/``False`` are valid schemas and pass through."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, Mapping):
        # Not a schema at all — a caller handed us something odd. Refuse to guess: an
        # empty schema accepts anything on the wire and the result check still bites.
        return {}
    clean: dict[str, Any] = {}
    for key, val in value.items():
        if key not in DRAFT07_KEYWORDS:
            out.dropped_keywords.append(key)
            continue
        if key == "$ref":
            if isinstance(val, str) and _pointer_resolves(root, val):
                clean[key] = val
            else:
                out.dropped_refs.append(str(val))
            continue
        if key in _SUBSCHEMA:
            clean[key] = val if isinstance(val, bool) else _node(val, out, root)
        elif key in _SUBSCHEMA_LIST:
            if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
                clean[key] = [_node(v, out, root) for v in val]
        elif key in _SUBSCHEMA_MAP:
            if isinstance(val, Mapping):
                clean[key] = {k: _node(v, out, root) for k, v in val.items()}
        elif key == "items":
            if isinstance(val, Sequence) and not isinstance(val, (str, bytes)):
                clean[key] = [_node(v, out, root) for v in val]
            else:
                clean[key] = _node(val, out, root)
        elif key == "dependencies":
            if isinstance(val, Mapping):
                clean[key] = {
                    k: (list(v)
                        if isinstance(v, Sequence) and not isinstance(v, (str, bytes))
                        else _node(v, out, root))
                    for k, v in val.items()
                }
        else:
            clean[key] = val
    return clean


def _prune_root(root: Mapping[str, Any]) -> dict[str, Any]:
    """The document a ``$ref`` will be resolved against: keyword-filtered, one level of
    containers deep. Pointers into ``$defs``/``properties`` are checked against this so a
    ref that survives really does still land on something."""
    pruned: dict[str, Any] = {}
    for key, val in root.items():
        if key not in DRAFT07_KEYWORDS:
            continue
        if key in _REF_CONTAINERS and isinstance(val, Mapping):
            pruned[key] = dict(val)
        else:
            pruned[key] = val
    return pruned


def rewrite(schema: Mapping[str, Any]) -> SchemaRewrite:
    """The wire copy of ``schema`` and the record of what was removed to get it."""
    out = SchemaRewrite(schema={})
    out.schema = _node(schema, out, _prune_root(schema))
    return out


def wire_schema(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A copy of ``schema`` the agent CLI will compile; ``None`` passes through.

    The argument is never mutated — the caller keeps the full document for the strict
    check on the model's answer.
    """
    if schema is None:
        return None
    return rewrite(schema).schema
