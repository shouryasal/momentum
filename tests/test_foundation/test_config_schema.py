"""The console generates its config forms from the pydantic JSON Schema, so a new key must
arrive fully annotated or the build goes red.

Walk every property of the earn.yaml and models.yaml schemas and require ``description``,
``x-tier`` and ``x-group`` on each one. That is the whole contract behind "a new field
appears in the UI with no frontend change": SchemaForm needs to know what to call it,
where to put it and who is allowed to touch it.
"""

from __future__ import annotations

import pytest
import yaml

from console.schema_meta import ARRAY_SUFFIX, MAP_KEY, flatten
from ops.config import CONFIG_VERSION, DEFAULT_CONFIG, config_schema
from ops.models_config import models_schema

TIERS = {"human", "tier1", "generated", "invariant"}
EFFECTS = {
    "regen", "restart:freqtrade-a", "restart:freqtrade-b", "crontab",
    "restart:telegram", "restart:console", "reset_required",
}
UNITS = {"fraction", "pct", "usdt", "minutes", "hours", "days", "bps"}
WIDGETS = {"slider", "cron", "time", "duration", "path", "model-ref", "skill-ref", "pair",
           "secret-ref"}


def _walk(schema: dict, defs: dict, path: str = "", seen: set[str] | None = None):
    """Yield (dotted path, property schema) for every property in the tree."""
    seen = seen if seen is not None else set()
    for name, prop in (schema.get("properties") or {}).items():
        here = f"{path}.{name}" if path else name
        yield here, prop
        for child in _targets(prop, defs):
            key = f"{here}:{id(child)}"
            if key in seen:
                continue
            seen.add(key)
            yield from _walk(child, defs, here, seen)


def _targets(prop: dict, defs: dict) -> list[dict]:
    """Resolve the object schemas a property can expand into."""
    out: list[dict] = []
    for node in _candidates(prop):
        ref = node.get("$ref")
        if ref:
            out.append(defs[ref.rsplit("/", 1)[-1]])
        elif node.get("properties"):
            out.append(node)
    return out


def _candidates(prop: dict) -> list[dict]:
    nodes = [prop]
    for key in ("additionalProperties", "items"):
        value = prop.get(key)
        if isinstance(value, dict):
            nodes.append(value)
    for key in ("anyOf", "allOf", "oneOf"):
        for value in prop.get(key) or []:
            if isinstance(value, dict):
                nodes.append(value)
                inner = value.get("items") or value.get("additionalProperties")
                if isinstance(inner, dict):
                    nodes.append(inner)
    return nodes


@pytest.fixture(scope="module")
def earn():
    s = config_schema()
    return s, s.get("$defs", {})


@pytest.fixture(scope="module")
def models():
    s = models_schema()
    return s, s.get("$defs", {})


def _properties(schema_and_defs):
    schema, defs = schema_and_defs
    return list(_walk(schema, defs))


def test_every_earn_property_is_annotated(earn):
    props = _properties(earn)
    assert len(props) > 200, "the walk must reach the whole tree, not just the top level"
    missing = [
        (path, sorted({"description", "x-tier", "x-group"} - set(prop)))
        for path, prop in props
        if not {"description", "x-tier", "x-group"} <= set(prop)
    ]
    assert not missing, f"unannotated config keys: {missing[:10]}"


def test_every_models_property_is_annotated(models):
    missing = [
        path for path, prop in _properties(models)
        if not {"description", "x-tier", "x-group"} <= set(prop)
    ]
    assert not missing, f"unannotated models.yaml keys: {missing[:10]}"


@pytest.mark.parametrize("fixture", ["earn", "models"])
def test_annotation_values_are_from_the_allowed_sets(fixture, request):
    for path, prop in _properties(request.getfixturevalue(fixture)):
        assert prop["x-tier"] in TIERS, f"{path}: x-tier {prop['x-tier']!r}"
        assert prop["description"].strip(), f"{path}: empty description"
        if "x-unit" in prop:
            assert prop["x-unit"] in UNITS, f"{path}: x-unit {prop['x-unit']!r}"
        if "x-widget" in prop:
            assert prop["x-widget"] in WIDGETS, f"{path}: x-widget {prop['x-widget']!r}"
        if "x-effects" in prop:
            unknown = set(prop["x-effects"]) - EFFECTS
            assert not unknown, f"{path}: unknown effects {unknown}"
        if "x-protected" in prop:
            assert prop["x-protected"] is True


def test_human_only_sections_are_protected(earn):
    """Spec section 3.1: risk, bounds, universe, modes.live, trading, autonomy, security,
    git, console, runtime and paths are tier-2 and need step-up plus a typed confirm."""
    schema, _ = earn
    for section in ("risk", "bounds", "universe", "trading", "autonomy"):
        prop = schema["properties"][section]
        assert prop["x-tier"] == "human", section


def test_tier1_params_are_marked_tier1(earn):
    schema, defs = earn
    tier1 = {path for path, prop in _walk(schema, defs) if prop["x-tier"] == "tier1"}
    assert any(p.startswith("sleeve_a.") for p in tier1)
    assert any(p.startswith("research.stage_prompts") for p in tier1)


def test_schema_is_json_serialisable_and_stamped(earn):
    import json

    schema, _ = earn
    json.dumps(schema)                       # the console ships this to the browser verbatim
    assert schema["title"] == "EarnConfig"
    assert CONFIG_VERSION == 2


# --------------------------------------------------------------- every YAML key is addressable


def _yaml_leaves(node, segments=None, out=None):
    """Every leaf of the live ``earn.yaml`` as a list of segments (ints index a list).

    Segments, not a dotted string: ``bounds`` and ``skills.bindings`` are keyed by strings
    that *contain* dots (``"sleeve_a.trend.ma_days"``, ``"research.flags"``), so splitting a
    joined path would invent segments that were never in the file. An empty dict or list is
    a leaf too — the container itself still has to be an addressable schema node.
    """
    segments = [] if segments is None else segments
    out = [] if out is None else out
    if isinstance(node, dict) and node:
        for key, child in node.items():
            _yaml_leaves(child, [*segments, str(key)], out)
    elif isinstance(node, list) and node:
        for i, child in enumerate(node):
            _yaml_leaves(child, [*segments, i], out)
    else:
        out.append(segments)
    return out


def _resolve(index, segments):
    """Walk the flattened schema one segment at a time: exact, then ``<key>``, then ``[]``."""
    prefix = ""
    for segment in segments:
        if isinstance(segment, int):
            candidates = [f"{prefix}{ARRAY_SUFFIX}"]
        else:
            candidates = [
                f"{prefix}.{segment}" if prefix else segment,
                f"{prefix}.{MAP_KEY}" if prefix else MAP_KEY,
            ]
        for candidate in candidates:
            if candidate in index:
                prefix = candidate
                break
        else:
            return None
    return index.get(prefix)


def test_every_earn_yaml_leaf_resolves_to_an_annotated_schema_node(earn):
    """"Everything is configurable" has to be literally true, with no exceptions list.

    Walk the file the system actually loads and demand that every leaf lands on a node of
    the flattened schema carrying ``x-tier`` and ``x-group``. A leaf that resolves to
    nothing gets no form field, no Ctrl-K entry and no protected/tier badge — which is how
    ``trading.sleeves.<sleeve>`` stayed invisible while it was typed ``dict[str, Any]``.
    """
    schema, _ = earn
    index = flatten(schema)
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    leaves = _yaml_leaves(raw)
    assert len(leaves) > 300, "the walk must reach the whole file"

    unaddressable = []
    for segments in leaves:
        meta = _resolve(index, segments)
        dotted = ".".join(str(s) for s in segments)
        if meta is None:
            unaddressable.append(f"{dotted}: no schema node")
        elif not meta.tier or not meta.group:
            unaddressable.append(f"{dotted}: x-tier={meta.tier!r} x-group={meta.group!r}")
    assert not unaddressable, f"config keys the UI cannot reach: {unaddressable[:10]}"


def test_per_sleeve_trading_overrides_are_typed_leaves(earn):
    """The specific subtree the walk above used to miss, pinned by name."""
    index = flatten(earn[0])
    for sleeve in ("a", "b"):
        meta = index[f"trading.sleeves.{sleeve}.dca.enabled"]
        assert meta.kind == "leaf" and meta.type == "boolean"
        assert meta.tier == "human" and meta.group == "trading.adds"
        assert meta.protected is True
        # An override inherits the section's effects, so saving one regenerates and restarts.
        assert "regen" in meta.effects
    # …and the whole defaults tree is mirrored, not a hand-picked subset.
    defaults = {p[len("trading.defaults."):] for p in index if p.startswith("trading.defaults.")}
    for sleeve in ("a", "b"):
        prefix = f"trading.sleeves.{sleeve}."
        assert {p[len(prefix):] for p in index if p.startswith(prefix)} == defaults


def test_deprecated_keys_are_flagged_not_hidden(earn):
    schema, defs = earn
    deprecated = {path for path, prop in _walk(schema, defs) if prop.get("x-deprecated")}
    assert "sleeves.a.capital_usdt" in deprecated
    assert "triggers.max_per_day" in deprecated
