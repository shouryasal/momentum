"""The AUTHORITATIVE proposal validator (host-side). A proposal file only ever
reaches proposals/ after this passes — SleeveB's stdlib loader is defence in depth.

Sum-to-1 (±0.001, the canonical epsilon), abstain→hold, and cash-module consistency
live here (JSON Schema cannot express them); schemas/proposal.json is the same
contract in JSON Schema form and is handed to the SDK as output_format.

**v4** (docs/design/wide-universe.md §3.3): ``targets`` is a **sparse map**. Under a
wide universe a dense target block is N numbers the model must emit and N keys of schema
it must read, which does not scale past a handful of assets; so a proposal now names only
the assets it wants to hold, **an omitted asset means exactly zero**, and the rules JSON
Schema cannot express are enforced here:

* ``USDT`` (the quote) is always required — cash is never implicit;
* every other key must be in the **tradeable set** handed to :func:`build_models`
  (the tradeable tier of the point-in-time universe snapshot). There is no default
  membership: an asset nobody vouched for is rejected, never silently given a weight;
* at most ``max_assets`` non-quote keys (``risk.max_open_positions``), so one proposal
  cannot open a book the gate would have to unwind;
* ``universe_snapshot`` names the snapshot the model actually saw (date + sha256), which
  is what makes a proposal replayable after the universe has moved and what closes the
  hole where a model proposes a weight for a coin delisted between decision and execution.

**v3** kept: the optional ``plan`` block SleeveB clamps to ``trading.plan_bounds`` and the
``signal_id`` a proposal answers. **v2** (BTC/ETH, no ``schema_version``) is what a
historical snapshot looks like — :func:`parse_any` accepts v2, v3 and v4 so replay keeps
working, and :func:`to_file` stamps the LOWEST version a payload actually needs.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

SUM_TOLERANCE = 0.001  # keep equal to config/earn.yaml proposal.sum_tolerance
SCHEMA_VERSION = 4
SPARSE_SCHEMA_VERSION = 4  # the version that introduced sparse targets + universe_snapshot
PLAN_SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
JSON_SCHEMA_PATH = Path(__file__).resolve().parent / "proposal.json"

#: The default universe the module-level `Targets`/`Proposal` are built for. Callers that
#: know the configured universe should use `build_models(cfg.universe.assets)`.
DEFAULT_ASSETS: tuple[str, ...] = ("BTC", "ETH")
DEFAULT_QUOTE = "USDT"

#: How many NON-quote assets one proposal may name. Mirrors ``risk.max_open_positions``
#: (docs/design/wide-universe.md §2.3): 8 positions already buy 1.82 of the 1.95
#: independent bets a 20-name alt book can offer, so a proposal naming more than this is
#: not expressing a view, it is spraying. The gate enforces the real limit; this is the
#: schema refusing to carry a proposal the gate would have to reject whole.
DEFAULT_MAX_ASSETS = 8

#: A Binance base asset as it appears in a pair. Deliberately ASCII-only: two USDT pairs
#: on the live exchange have non-ASCII bases (wide-universe §1.2 filter 7) and nothing
#: downstream — journal, filenames, prompts, Telegram — is tested for them.
ASSET_PATTERN = r"^[A-Z0-9]{2,12}$"
SNAPSHOT_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

ENTRY_STYLES: tuple[str, ...] = ("passive", "cross")

__all__ = [
    "ASSET_PATTERN",
    "DEFAULT_ASSETS",
    "DEFAULT_MAX_ASSETS",
    "DEFAULT_QUOTE",
    "ENTRY_STYLES",
    "LEGACY_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SPARSE_SCHEMA_VERSION",
    "SUM_TOLERANCE",
    "Plan",
    "Proposal",
    "ProposalInvalid",
    "Targets",
    "UniverseRef",
    "build_models",
    "json_schema",
    "parse_any",
    "proposal_json_schema",
    "tradeable_from_snapshot",
    "universe_ref_from_snapshot",
    "validate_proposal",
]


class ProposalInvalid(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class Plan(BaseModel):
    """Optional execution hints. Advisory only: SleeveB clamps every field to
    ``trading.plan_bounds`` and ignores anything that would LOOSEN a stop."""

    model_config = ConfigDict(extra="forbid")
    entry_style: Literal["passive", "cross"] = "passive"
    stop_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=1)
    dca_allowed: bool = False
    valid_for_hours: int | None = Field(default=None, ge=1, le=720)


class UniverseRef(BaseModel):
    """The point-in-time universe snapshot a proposal was authored against.

    A proposal is validated against *that* snapshot, not against whatever the universe
    happens to be when the file is read — which is what keeps a decision replayable after
    the watchlist has rotated underneath it.
    """

    model_config = ConfigDict(extra="forbid")
    date: str = Field(pattern=SNAPSHOT_DATE_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)


_AssetKey = Annotated[str, Field(pattern=ASSET_PATTERN)]
_Weight = Annotated[float, Field(ge=0, le=1)]


class Targets(RootModel[dict[_AssetKey, _Weight]]):
    """Sparse target weights. **An absent asset means zero, explicitly.**

    It behaves as a read-only mapping *and* keeps attribute access (``targets.BTC``) and
    ``.model_dump()``, because both are how the rest of Earn already reads a proposal.
    """

    def __getitem__(self, key: str) -> float:
        return self.root[key]

    def __contains__(self, key: object) -> bool:
        return key in self.root

    def __iter__(self):  # noqa: D105 — mapping protocol
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)

    def get(self, key: str, default: float = 0.0) -> float:
        """The weight for ``key``; **absent means zero**, never "unknown"."""
        return self.root.get(key, default)

    def keys(self):
        return self.root.keys()

    def values(self):
        return self.root.values()

    def items(self):
        return self.root.items()

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("_"):
            root = self.__dict__.get("root")
            if isinstance(root, dict) and name in root:
                return root[name]
        return super().__getattr__(name)  # type: ignore[misc]


def build_models(assets: list[str] | tuple[str, ...] = DEFAULT_ASSETS,
                 quote: str = DEFAULT_QUOTE, *,
                 max_assets: int | None = None,
                 snapshot: UniverseRef | dict[str, Any] | None = None,
                 ) -> tuple[type[BaseModel], type[BaseModel]]:
    """(Targets, Proposal) for one universe. The ONE place the proposal shape is defined.

    ``assets`` is the **tradeable set** — the tier of the universe snapshot the gate will
    pass an order for. A proposal may name any subset of it and nothing outside it.
    ``max_assets`` caps how many non-quote assets one proposal may name.
    ``snapshot``, when given, is the snapshot the caller resolved the tradeable set from:
    the proposal must then carry a matching ``universe_snapshot``.
    """
    allowed = frozenset(assets)
    cap = DEFAULT_MAX_ASSETS if max_assets is None else int(max_assets)
    want_snapshot = None if snapshot is None else (
        snapshot if isinstance(snapshot, UniverseRef) else UniverseRef.model_validate(snapshot))

    class _Proposal(BaseModel):
        model_config = ConfigDict(extra="forbid")
        schema_version: int = Field(default=LEGACY_SCHEMA_VERSION, ge=2, le=SCHEMA_VERSION)
        run_id: str
        prompt_version: str = Field(pattern=r"^research\.v\d+$")
        module: Literal["trend", "dca", "cash", "hold"]
        targets: Targets
        exposure_scale: float = Field(ge=0, le=1)
        confidence: float = Field(ge=0, le=1)
        abstain: bool
        horizon_days: int = Field(ge=1, le=30)
        rationale: list[str] = Field(min_length=1, max_length=6)
        invalidation: str = Field(min_length=10, max_length=300)
        signal_id: str | None = Field(default=None, max_length=120)
        plan: Plan | None = None
        universe_snapshot: UniverseRef | None = None

        @model_validator(mode="after")
        def _consistent(self):
            t = self.targets.model_dump()
            if quote not in t:
                raise ValueError(f"targets must name {quote} explicitly (cash is never implicit)")
            named = [k for k in t if k != quote]
            unknown = sorted(k for k in named if k not in allowed)
            if unknown:
                raise ValueError(
                    f"targets name assets outside the tradeable universe: {unknown}")
            if len(named) > cap:
                raise ValueError(
                    f"targets name {len(named)} assets, max_assets is {cap}")
            total = sum(t.values())
            if abs(total - 1.0) > SUM_TOLERANCE:
                raise ValueError(f"targets sum {total:.4f} != 1 +/- {SUM_TOLERANCE}")
            if self.abstain and self.module != "hold":
                raise ValueError("abstain requires module=hold")
            crypto = total - t[quote]
            if self.module == "cash" and crypto > 0.10:
                raise ValueError("module=cash requires crypto <= 10%")
            for r in self.rationale:
                if len(r) > 200:
                    raise ValueError("rationale entry over 200 chars")
            try:
                dt = datetime.fromisoformat(self.run_id)
            except ValueError as e:
                raise ValueError(f"run_id not ISO-8601: {self.run_id}") from e
            if dt.tzinfo is None:
                raise ValueError("run_id must carry a UTC offset")
            if self.plan is not None and self.schema_version < PLAN_SCHEMA_VERSION:
                raise ValueError("plan requires schema_version 3")
            if self.universe_snapshot is not None \
                    and self.schema_version < SPARSE_SCHEMA_VERSION:
                raise ValueError("universe_snapshot requires schema_version 4")
            if self.schema_version >= SPARSE_SCHEMA_VERSION and self.universe_snapshot is None:
                raise ValueError("schema_version 4 requires universe_snapshot")
            if want_snapshot is not None:
                got = self.universe_snapshot
                if got is None:
                    raise ValueError(
                        "universe_snapshot required: this run resolved its universe from"
                        f" the {want_snapshot.date} snapshot")
                if (got.date, got.sha256) != (want_snapshot.date, want_snapshot.sha256):
                    raise ValueError(
                        f"universe_snapshot {got.date}/{got.sha256[:12]} is not the"
                        f" snapshot this run used ({want_snapshot.date}/"
                        f"{want_snapshot.sha256[:12]})")
            return self

    _Proposal.__name__ = "Proposal"
    return Targets, _Proposal


_, Proposal = build_models()


def _reasons(e: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
        for err in e.errors()
    ]


def validate_proposal(raw: str | dict,
                      assets: list[str] | tuple[str, ...] | None = None,
                      quote: str = DEFAULT_QUOTE, *,
                      max_assets: int | None = None,
                      snapshot: UniverseRef | dict[str, Any] | None = None):
    """Raises ProposalInvalid with human-readable reasons; never partially succeeds."""
    if assets is None and max_assets is None and snapshot is None:
        model = Proposal
    else:
        model = build_models(assets if assets is not None else DEFAULT_ASSETS, quote,
                             max_assets=max_assets, snapshot=snapshot)[1]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProposalInvalid([f"not JSON: {e}"]) from e
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ProposalInvalid(_reasons(e)) from e


def to_file(prop, *, signal_id: str | None = None) -> dict:
    """What actually gets written to ``proposals/``.

    Absent optional fields are omitted rather than written as ``null``, and
    ``schema_version`` carries the LOWEST version the payload actually needs: v4 when it
    names its universe snapshot, v3 when it carries a ``plan`` or a ``signal_id``, and
    nothing at all otherwise. A file that says nothing new stays byte-compatible with the
    v2 readers (the in-container loader, replay, historical tooling), which is what keeps
    the migration free.
    """
    payload = prop.model_dump(exclude_none=True)
    if signal_id:
        payload["signal_id"] = signal_id
    if payload.get("universe_snapshot"):
        payload["schema_version"] = SPARSE_SCHEMA_VERSION
    elif payload.get("signal_id") or payload.get("plan"):
        payload["schema_version"] = PLAN_SCHEMA_VERSION
    else:
        payload.pop("schema_version", None)
    return payload


def parse_any(raw: str | dict,
              assets: list[str] | tuple[str, ...] | None = None,
              quote: str = DEFAULT_QUOTE, *,
              max_assets: int | None = None):
    """Accept a v2 (BTC/ETH-only, no ``schema_version``), v3 or v4 proposal.

    Historical snapshots predate both the generated targets and the universe snapshot, so
    replay must be able to load them even when the universe has since moved on — including
    the case where an asset they held has left the tradeable set entirely. A payload whose
    targets do not validate against the configured universe is re-tried against exactly the
    keys it carries; the sum, abstain and cash rules still apply, and nothing is coerced.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProposalInvalid([f"not JSON: {e}"]) from e
    try:
        return validate_proposal(raw, assets, quote, max_assets=max_assets)
    except ProposalInvalid:
        targets = raw.get("targets") if isinstance(raw, dict) else None
        if not isinstance(targets, dict) or quote not in targets:
            raise
        legacy = tuple(k for k in targets if k != quote)
        if not legacy or set(legacy) == set(assets or DEFAULT_ASSETS):
            raise
        return validate_proposal(raw, legacy, quote, max_assets=max(len(legacy), 1))


# --------------------------------------------------------------- universe snapshot glue


def tradeable_from_snapshot(snapshot: dict[str, Any],
                            tiers: tuple[str, ...] = ("core", "major", "satellite"),
                            ) -> tuple[str, ...]:
    """The tradeable base assets of a universe snapshot (U1's ``knowledge/universe/*.json``).

    Tolerant on purpose — the resolver owns that file's shape, this is only a reader, and a
    reader that guesses would be worse than one that returns nothing. Two layouts are
    understood: ``{"assets": {"BTC": {"tier": "core"}, ...}}`` and a list of per-pair
    records carrying ``asset``/``base``/``pair`` plus ``tier``. Anything whose tier is not
    in ``tiers`` (``watchlist``, ``exit_only``, an unknown tier) is **not** tradeable.
    """
    wanted = frozenset(tiers)
    out: list[str] = []

    def _add(asset: Any, tier: Any) -> None:
        if not isinstance(asset, str) or str(tier) not in wanted:
            return
        base = asset.split("/")[0].strip().upper()
        if base and base not in out:
            out.append(base)

    for key in ("assets", "pairs", "universe", "members"):
        body = snapshot.get(key) if isinstance(snapshot, dict) else None
        if not isinstance(body, dict):
            continue
        for name, meta in body.items():
            if isinstance(meta, dict):
                _add(meta.get("base") or meta.get("asset") or name, meta.get("tier"))
            else:
                _add(name, meta)
        if out:
            return tuple(out)
    for key in ("assets", "pairs", "universe", "members"):
        rows = snapshot.get(key) if isinstance(snapshot, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                _add(row.get("asset") or row.get("base") or row.get("pair"), row.get("tier"))
            if out:
                return tuple(out)
    return tuple(out)


def universe_ref_from_snapshot(snapshot: dict[str, Any]) -> UniverseRef | None:
    """``{date, sha256}`` for a snapshot dict, or ``None`` when it carries neither."""
    if not isinstance(snapshot, dict):
        return None
    date = snapshot.get("date") or snapshot.get("asof") or snapshot.get("resolved_on")
    sha = snapshot.get("sha256") or snapshot.get("sha")
    if not isinstance(date, str) or not isinstance(sha, str):
        return None
    try:
        return UniverseRef(date=date[:10], sha256=sha.lower())
    except ValidationError:
        return None


# --------------------------------------------------------------------- rendered artefact


def proposal_json_schema(assets: list[str] | tuple[str, ...] = DEFAULT_ASSETS,
                         quote: str = DEFAULT_QUOTE, *,
                         max_assets: int | None = None) -> dict:
    """Render the JSON Schema for one universe — what regenerates proposal.json.

    Targets are **sparse**: JSON Schema pins the shape (a map of asset → weight, the quote
    required, at most ``max_assets`` others), and membership of the tradeable tier is
    enforced by :func:`validate_proposal`, because it depends on a snapshot that JSON
    Schema has no way to see. ``assets`` is therefore documentation here, not a constraint.
    """
    cap = DEFAULT_MAX_ASSETS if max_assets is None else int(max_assets)
    listed = ", ".join([*assets, quote])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Earn proposal (spec section 7, schema_version 4)",
        "type": "object",
        "additionalProperties": False,
        "required": ["run_id", "prompt_version", "module", "targets", "exposure_scale",
                     "confidence", "abstain", "horizon_days", "rationale", "invalidation"],
        "properties": {
            "schema_version": {"type": "integer", "minimum": 2, "maximum": SCHEMA_VERSION},
            "run_id": {"type": "string",
                       "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}$"},
            "prompt_version": {"type": "string", "pattern": r"^research\.v\d+$"},
            "module": {"enum": ["trend", "dca", "cash", "hold"]},
            "targets": {
                "description": (
                    "Sparse target weights. An asset you do not name is zero. "
                    f"{quote} is always required. Every other key must be in the tradeable "
                    f"tier of the universe snapshot you were given (today: {listed}); a key "
                    "outside it rejects the whole proposal."),
                "type": "object",
                "propertyNames": {"pattern": ASSET_PATTERN},
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                "required": [quote],
                "minProperties": 1,
                "maxProperties": cap + 1,
            },
            "exposure_scale": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "abstain": {"type": "boolean"},
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 30},
            "rationale": {"type": "array", "minItems": 1, "maxItems": 6,
                          "items": {"type": "string", "maxLength": 200}},
            "invalidation": {"type": "string", "minLength": 10, "maxLength": 300},
            "signal_id": {"type": ["string", "null"], "maxLength": 120},
            "universe_snapshot": {
                "description": ("The universe snapshot these targets were chosen against. "
                                "Copy it verbatim from the UNIVERSE block."),
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["date", "sha256"],
                "properties": {
                    "date": {"type": "string", "pattern": SNAPSHOT_DATE_PATTERN},
                    "sha256": {"type": "string", "pattern": SHA256_PATTERN},
                },
            },
            "plan": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "properties": {
                    "entry_style": {"enum": list(ENTRY_STYLES)},
                    "stop_pct": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                 "maximum": 1},
                    "take_profit_pct": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                        "maximum": 1},
                    "dca_allowed": {"type": "boolean"},
                    "valid_for_hours": {"type": ["integer", "null"], "minimum": 1,
                                        "maximum": 720},
                },
            },
        },
    }


def json_schema() -> dict:
    return json.loads(JSON_SCHEMA_PATH.read_text())
