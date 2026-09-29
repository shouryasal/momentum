"""ModelsConfig: the typed loader for config/models.yaml (``version: 2``).

Where ``earn.yaml`` says what Earn may risk, this file says *who thinks*. It declares the
providers, the model aliases with their capability tier, the ordered fallback chain per
task, and how the router reacts to an error, a timeout, a rate limit or an exhausted
budget. ``runs/llm/chain.py`` consumes it; ``runs/router.py`` keeps working through the
v1 compatibility shim.

Three things it must get right:

* **A v1 file still loads.** :func:`load_models_cfg` converts ``tasks.<t>.model`` /
  ``escalation`` / ``fallback`` into a chain, so the committed v1 file and the existing
  router tests keep working while the rest of the system migrates.
* **min_tier is a floor nothing can lower.** ``decide`` needs tier 4 and ``validate``
  tier 3 as *code* rules (``runs.llm.types.MIN_TIER``), so no config edit and no tier-1
  overlay can let a local model write a proposal or a validation.
* **The tier-1 overlay is narrow.** ``config/models-auto.yaml`` may *add* a
  ``models.<alias>`` declaration (never redefine one the human file owns), move
  ``tasks.<t>.chain[0]`` to a declared model of the same or higher tier, and own the
  ``shadow`` block. Anything else is rejected at load with the offending paths named.
  The addition is what auto-shadow needs: it discovers a model ``models.yaml`` has never
  heard of and must name it before ``shadow.model`` can point at it.

Every field carries the same ``x-*`` annotations as ``ops.config`` so the console can
generate the Models page with no frontend change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ops.config import ConfigError, F
from ops.lib.paths import REPO_ROOT

DEFAULT_MODELS_CONFIG = REPO_ROOT / "config" / "models.yaml"

#: The tier-1 overlay's file name. It always sits beside the models config it overlays —
#: a worktree's overlay must never be the live checkout's.
OVERLAY_NAME = "models-auto.yaml"
OVERLAY_PATH = REPO_ROOT / "config" / OVERLAY_NAME


class _Sibling:
    """Sentinel: "the overlay beside whichever models.yaml you gave me"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<overlay sibling {OVERLAY_NAME}>"


SIBLING = _Sibling()

MODELS_VERSION = 2

#: Top-level overlay keys accepted wholesale. ``models`` and ``tasks`` are accepted too
#: but only under the narrow rules in :func:`_overlay_violations` — ``models`` may add an
#: alias and never redefine one; ``tasks.<t>.chain[0]`` may move to a same-or-higher tier.
OVERLAY_ALLOWED: tuple[str, ...] = ("shadow",)

ToolsProfile = Literal["none", "read_only", "skill_rw"]
SwitchAction = Literal["next", "skip", "retry_then_next", "stop"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Auth(_Model):
    claude_mode: Literal["subscription", "api_key", "auto"] = F(
        "subscription",
        desc="Which Claude credential jobs get. 'auto' adds the API key as a named fallback.",
        group="auth", effects=["crontab"],
    )
    subscription_source: Literal["token", "login"] = F(
        "token",
        desc="token = $CLAUDE_CODE_OAUTH_TOKEN; login = the CLI's own ~/.claude credentials.",
        group="auth",
    )
    prefer: Literal["subscription", "api_key"] = F(
        "subscription", desc="In 'auto' mode, which credential is tried first.", group="auth",
    )
    fallback_on: list[str] = F(
        default_factory=lambda: ["auth_error", "rate_limited", "quota_exhausted", "overloaded"],
        desc="Failure classes that switch to the other credential in 'auto' mode.",
        group="auth",
    )
    return_after_min: int = F(
        60, desc="Retry the preferred credential after this long.", group="auth",
        unit="minutes", ge=1,
    )
    api_key_monthly_cap_usd: float = F(
        30, desc="Hard cap on metered spend whenever the API key serves, whatever budget.mode says.",
        group="auth", unit="usdt", protected=True, ge=0,
    )


class ProviderCfg(_Model):
    kind: Literal["claude_sdk", "ollama"] = F(..., desc="Provider implementation.",
                                              group="providers")
    enabled: bool = F(True, desc="Whether the router may use this provider.", group="providers")
    base_url: str | None = F(
        None, desc="'auto' probes for a local endpoint; explicit URLs must be loopback/private.",
        group="providers", widget="path",
    )
    probe: list[str] = F(default_factory=list,
                         desc="Ordered base-URL candidates tried by the health probe.",
                         group="providers")
    timeout_s: int = F(120, desc="Per-request timeout for this provider.", group="providers",
                       ge=1)
    keep_alive: str | None = F(None, desc="How long a local model stays resident.",
                               group="providers")
    options: dict[str, Any] = F(default_factory=dict,
                                desc="Provider-specific request options (temperature, num_ctx).",
                                group="providers")


class ModelEntry(_Model):
    provider: str = F(..., desc="Key into providers.", group="models")
    id: str = F(..., desc="Exact model id sent to the provider.", group="models",
                widget="model-ref")
    tier: int = F(..., desc="Capability tier 1-5; task min_tier floors are enforced in code.",
                  group="models", ge=1, le=5)


class Capability(_Model):
    tools: bool = F(True, desc="Can run tools.", group="capabilities")
    skills: bool = F(True, desc="Can load repo skills.", group="capabilities")
    structured_output: bool = F(True, desc="Can be constrained to a JSON schema.",
                                group="capabilities")
    max_ctx: int | None = F(None, desc="Context window, when smaller than the default.",
                            group="capabilities")


class PanelPassCfg(_Model):
    """One pass of a consensus panel: which model, at which effort, in which role."""

    model: str = F(..., desc="Model alias this pass runs on.", group="tasks",
                   widget="model-ref")
    effort: str = F(..., desc="Reasoning effort for this pass; the code floor still clamps.",
                    group="tasks")
    label: str = F("", desc="Short name for this pass in the journal and on the console.",
                   group="tasks")
    role: Literal["vote", "adjudicator"] = F(
        "vote",
        desc="'vote' passes are counted; the single 'adjudicator' runs only when they "
             "disagree and its verdict decides.",
        group="tasks",
    )


class PanelCfg(_Model):
    """Multi-pass consensus for one task. Agreement acts; disagreement escalates."""

    enabled: bool = F(False, desc="Run this task as a panel instead of a single call.",
                      group="tasks")
    passes: list[PanelPassCfg] = F(default_factory=list,
                                   desc="Independent passes, run in order.", group="tasks")
    quorum: int = F(2, desc="Valid passes required before any verdict may be acted on.",
                    group="tasks", ge=1)
    min_confidence: float = F(
        0.6,
        desc="Unanimous passes below this confidence escalate to the adjudicator instead "
             "of acting.",
        group="tasks", unit="fraction", ge=0, le=1,
    )
    verdict_key: str = F("verdict", desc="JSON key each pass returns its verdict under.",
                         group="tasks")
    confidence_key: str = F("confidence",
                            desc="JSON key each pass returns its 0-1 confidence under.",
                            group="tasks")
    verdicts: list[str] = F(default_factory=list,
                            desc="Allowed verdict values; anything else is an invalid pass.",
                            group="tasks")


class TaskCfg(_Model):
    chain: list[str] = F(..., desc="Ordered model aliases; the first that can serve, does.",
                         group="tasks", widget="model-ref")
    why: str | None = F(
        None,
        desc="One line saying why this task sits at this model and this effort. Shown on "
             "the console's AI & Models page beside the row it explains.",
        group="tasks",
    )
    escalation_effort: str | None = F(
        None,
        desc="On escalation, re-run the CHAIN HEAD at this effort before moving to the "
             "escalation MODEL. Effort is the cheaper lever, so it is pulled first.",
        group="tasks",
    )
    panel: PanelCfg | None = F(
        None, desc="Multi-pass consensus configuration for this task.", group="tasks",
    )
    escalation: str | None = F(None, desc="Model prepended on a hard case or forced escalation.",
                               group="tasks", widget="model-ref")
    tools: ToolsProfile = F("none", desc="Tool profile this task runs with.", group="tasks")
    min_tier: int = F(1, desc="Lowest model tier allowed; code floors override upwards.",
                      group="tasks", ge=1, le=5)
    allow_local: bool = F(True, desc="May a local (Ollama) model serve this task at all.",
                          group="tasks", protected=True)
    retry: int = F(0, desc="Retries on the same model before moving down the chain.",
                   group="tasks", ge=0)
    effort: str | None = F(None, desc="Reasoning effort; the code floor 'high' still clamps.",
                           group="tasks")
    # ge=2, not ge=1: every call here asks for structured output, and the SDK spends one
    # turn answering and one emitting the JSON. A cap of 1 is therefore not "strict", it is
    # guaranteed failure — `Reached maximum number of turns (1)`, charged for in full. Four
    # tasks shipped with 1 and every cloud attempt at them failed, silently, for weeks.
    max_turns: int | None = F(None, desc="Turn cap for one call; 2 is the floor because a "
                                         "structured answer costs a turn to write and a "
                                         "turn to emit.", group="tasks", ge=2)
    max_usd_per_run: float | None = F(None, desc="Spend cap for one call.", group="tasks",
                                      unit="usdt", ge=0)
    monthly_budget_usd: float | None = F(None, desc="Monthly spend cap for this task.",
                                         group="tasks", unit="usdt", ge=0)
    deadline_s: float | None = F(None, desc="Wall-clock budget for one call.", group="tasks",
                                 ge=1)
    skills_from: str | None = F(None, desc="Config path the skill list is read from.",
                                group="tasks", widget="skill-ref")
    local_mode: Literal["context_pack"] | None = F(
        None,
        desc="How a local model may serve a tool-using task: 'context_pack' pre-assembles the "
             "inputs and the host writes any output file.",
        group="tasks",
    )
    short_on_fallback: bool = F(False, desc="Use the short prompt variant when falling back.",
                                group="tasks")
    prompt: str | None = F(None, desc="Config path of the prompt file for this task.",
                           group="tasks", widget="path")
    on_all_failed: str | None = F(
        None,
        desc="What happens when every chain entry failed: keep_last, hold_last, rule, "
             "drop_signal, skip_screen or abstain.",
        group="tasks",
    )


class CircuitBreaker(_Model):
    failures: int = F(3, desc="Consecutive failures that open the breaker.", group="switching",
                      ge=1)
    window_min: int = F(15, desc="Window the failures are counted over.", group="switching",
                        unit="minutes", ge=1)
    open_min: int = F(15, desc="How long the breaker stays open.", group="switching",
                      unit="minutes", ge=1)


class Switching(_Model):
    on: dict[str, SwitchAction] = F(
        default_factory=dict,
        desc="Failure class to action: next, skip, retry_then_next or stop.",
        group="switching",
    )
    prefer_local_when_rate_limited: list[str] = F(
        default_factory=list,
        desc="Tasks whose local candidates move to the front while rate-limited.",
        group="switching",
    )
    escalate_on: list[str] = F(default_factory=list,
                               desc="Conditions that prepend the escalation model.",
                               group="switching")
    circuit_breaker: CircuitBreaker = F(default_factory=CircuitBreaker,
                                        desc="Per-provider breaker, persisted in provider_health.",
                                        group="switching")
    max_attempts_per_call: int = F(4, desc="Hard ceiling on attempts across the whole chain.",
                                   group="switching", ge=1)
    held_authorship_for_fallback: bool = F(
        True, desc="A change authored by a fallback model is always HELD for a human.",
        group="switching", protected=True,
    )


class Budget(_Model):
    mode: Literal["telemetry", "hard"] = F("telemetry",
                                           desc="telemetry reports; hard blocks calls.",
                                           group="budget")
    monthly_total_usd: float = F(150, desc="Monthly ceiling across every task.", group="budget",
                                 unit="usdt", ge=0)
    throttle_at_pct: int = F(80, desc="Percentage of the ceiling at which the brief degrades.",
                             group="budget", unit="pct", ge=0, le=100)


class Shadow(_Model):
    enabled: bool = F(False, desc="Run a shadow model beside the decision model.",
                      group="shadow")
    model: str | None = F(None, desc="Alias of the shadow model.", group="shadow",
                          widget="model-ref")
    started: str | None = F(None, desc="Date the shadow window opened (YYYY-MM-DD).",
                            group="shadow", widget="time")
    days: int = F(30, desc="Length of the shadow window.", group="shadow", unit="days", ge=1)
    monthly_budget_usd: float = F(20, desc="Monthly ceiling for shadow spend.", group="shadow",
                                  unit="usdt", ge=0)


class ModelsConfig(_Model):
    version: int = F(2, desc="Schema version of this file.", group="meta", tier="generated")
    auth: Auth = F(default_factory=Auth, desc="Which Claude credential serves, and when.",
                   group="auth")
    providers: dict[str, ProviderCfg] = F(default_factory=dict,
                                          desc="Declared providers by key.", group="providers")
    models: dict[str, ModelEntry | None] = F(default_factory=dict,
                                             desc="Alias to provider, id and tier. null = unset.",
                                             group="models")
    capabilities: dict[str, Capability] = F(
        default_factory=dict,
        desc="Per-alias capability overrides; Claude defaults to all true, Ollama to "
             "structured output only.",
        group="capabilities",
    )
    tasks: dict[str, TaskCfg] = F(default_factory=dict,
                                  desc="Per-task chain, tool profile, tier floor and budgets.",
                                  group="tasks")
    switching: Switching = F(default_factory=Switching,
                             desc="How the router reacts to each failure class.",
                             group="switching")
    budget: Budget = F(default_factory=Budget, desc="Global spend policy.", group="budget")
    shadow: Shadow = F(default_factory=Shadow, desc="Shadow-model window.", group="shadow")

    # ---- accessors -------------------------------------------------------------

    def model_ref(self, alias: str) -> ModelEntry:
        entry = self.models.get(alias)
        if entry is None:
            raise ConfigError(f"models.yaml: model alias {alias!r} is not declared")
        return entry

    def task(self, name: str) -> TaskCfg:
        cfg = self.tasks.get(name)
        if cfg is None:
            raise ConfigError(f"models.yaml: task {name!r} is not declared")
        return cfg

    def tier_of(self, alias: str) -> int:
        return self.model_ref(alias).tier

    def panel_for(self, name: str) -> PanelCfg | None:
        """The enabled panel for a task, or ``None`` when it runs as a single call."""
        panel = self.task(name).panel
        return panel if (panel is not None and panel.enabled and panel.passes) else None

    def caps_for(self, alias: str) -> Capability:
        if alias in self.capabilities:
            return self.capabilities[alias]
        provider = self.providers.get(self.model_ref(alias).provider)
        if provider is not None and provider.kind == "ollama":
            return Capability(tools=False, skills=False, structured_output=True, max_ctx=None)
        return Capability()


# --------------------------------------------------------------------------- v1 -> v2


def _v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate the v1 shape (``tasks.<t>.model/escalation/fallback``) into chains."""
    models = {
        alias: {"provider": "claude", "id": model_id, "tier": _v1_tier(alias)}
        for alias, model_id in (raw.get("models") or {}).items()
    }
    tasks: dict[str, Any] = {}
    for name, t in (raw.get("tasks") or {}).items():
        t = dict(t)
        chain = [t.pop("model", None)]
        fallback = t.pop("fallback", None)
        if fallback in models:
            chain.append(fallback)
        elif fallback is not None:
            t.setdefault("on_all_failed", str(fallback))
        escalation = t.pop("escalation", None)
        tasks[name] = {
            "chain": [c for c in chain if c],
            "escalation": escalation if escalation in models else None,
            "tools": "read_only",
            "min_tier": 1,
            **{k: v for k, v in t.items() if k in TaskCfg.model_fields},
        }
    providers = dict(raw.get("providers") or {})
    providers.setdefault("claude", {"kind": "claude_sdk", "enabled": True})
    out: dict[str, Any] = {
        "version": MODELS_VERSION,
        "providers": providers,
        "models": models,
        "tasks": tasks,
    }
    # v2-only sections may already sit in a v1 file: they are read through this module
    # only, so they do not disturb the direct readers of the v1 models/tasks shape.
    for key in ("auth", "capabilities", "switching", "budget", "shadow"):
        if key in raw:
            out[key] = raw[key]
    return out


#: v1 had no tiers; this is the historical ordering the router relied on.
_V1_TIERS = {"fable": 5, "opus": 4, "sonnet": 3, "haiku": 2}


def _v1_tier(alias: str) -> int:
    return _V1_TIERS.get(alias, 2)


# --------------------------------------------------------------------------- overlay


def _overlay_violations(base: ModelsConfig, overlay: dict[str, Any]) -> list[str]:
    """Which overlay keys step outside ``models`` (additions only), ``tasks.<t>.chain[0]``
    and ``shadow``."""
    bad: list[str] = []
    # An alias declared in this same overlay counts as declared for the chain[0] and
    # shadow.model checks below — otherwise opening a shadow window and pointing at it
    # could never be one atomic edit.
    added = overlay.get("models") if isinstance(overlay.get("models"), dict) else {}
    added_tiers = {
        alias: int(entry.get("tier", 1))
        for alias, entry in added.items()
        if isinstance(entry, dict) and base.models.get(alias) is None
    }

    def declared(alias: str) -> bool:
        return alias in added_tiers or base.models.get(alias) is not None

    def tier_of(alias: str) -> int:
        return added_tiers.get(alias) if alias in added_tiers else base.tier_of(alias)

    for key, value in overlay.items():
        if key in OVERLAY_ALLOWED:
            continue
        if key == "models":
            bad.extend(_model_addition_violations(base, value))
            continue
        if key != "tasks":
            bad.append(key)
            continue
        for task_name, patch in (value or {}).items():
            if not isinstance(patch, dict):
                bad.append(f"tasks.{task_name}")
                continue
            for field in patch:
                if field != "chain":
                    bad.append(f"tasks.{task_name}.{field}")
            chain = patch.get("chain")
            if chain is None:
                continue
            if not isinstance(chain, list) or not chain:
                bad.append(f"tasks.{task_name}.chain")
                continue
            head = chain[0]
            current = base.tasks.get(task_name)
            if current is None:
                bad.append(f"tasks.{task_name}.chain (unknown task)")
                continue
            if list(chain[1:]) != list(current.chain[1:]):
                bad.append(f"tasks.{task_name}.chain (only chain[0] may change)")
            if not declared(head):
                bad.append(f"tasks.{task_name}.chain[0]={head!r} (undeclared model)")
            elif current.chain and tier_of(head) < base.tier_of(current.chain[0]):
                bad.append(f"tasks.{task_name}.chain[0]={head!r} (lower tier than the base)")
    return bad


def _model_addition_violations(base: ModelsConfig, value: Any) -> list[str]:
    """``models:`` in the overlay may only DECLARE a new alias, never redefine one.

    ``runs/maintenance.start_shadow`` discovers a model the human file has never heard of
    and has to name it before ``shadow.model`` (and, after a clean window,
    ``tasks.decide.chain[0]``) can point at it. Declaring an alias is safe: it is inert
    until something references it, and the two things that can reference it are already
    policed — ``chain[0]`` must be the same tier or higher, and ``MIN_TIER_FLOOR`` /
    ``ALWAYS_LOCAL_FORBIDDEN`` in ``runs/llm/types.py`` are code, not config.
    *Re*defining an alias is not safe: it would silently repoint ``decide`` at another
    model while every chain still reads the same, so it is refused.
    """
    bad: list[str] = []
    if not isinstance(value, dict):
        return ["models"]
    for alias, entry in value.items():
        if base.models.get(alias) is not None:
            bad.append(f"models.{alias} (already declared; the overlay may only add)")
        elif not isinstance(entry, dict) or not entry.get("id"):
            bad.append(f"models.{alias} (needs provider/id/tier, not {type(entry).__name__})")
    return bad


def apply_overlay(base: ModelsConfig, overlay: dict[str, Any] | None) -> ModelsConfig:
    """Merge the tier-1 overlay, refusing anything outside its narrow remit."""
    if not overlay:
        return base
    bad = _overlay_violations(base, overlay)
    if bad:
        raise ConfigError(
            "config/models-auto.yaml may only add models.<alias> and change "
            f"tasks.<t>.chain[0] and shadow; rejected: {sorted(set(bad))}"
        )
    data = base.model_dump(by_alias=True)
    # Added aliases first: a chain[0] or shadow.model in the same overlay may name one.
    for alias, entry in (overlay.get("models") or {}).items():
        data["models"][alias] = dict(entry)
    for task_name, patch in (overlay.get("tasks") or {}).items():
        if "chain" in patch:
            data["tasks"][task_name]["chain"] = list(patch["chain"])
    if "shadow" in overlay:
        data["shadow"] = {**data.get("shadow", {}), **(overlay["shadow"] or {})}
    return ModelsConfig.model_validate(data)


# --------------------------------------------------------------------------- validation


def _validate_panel(cfg: ModelsConfig, name: str, task: TaskCfg) -> None:
    """A panel must name declared models, clear the task's own tier floor on every pass,
    and be able to reach its quorum.

    The tier check is repeated here so a misconfiguration is a load error rather than a
    run-time skip. ``runs.llm.types.chain_for`` is still the thing that *enforces* it on
    every pass — this only makes the failure visible before a panel is ever run.
    """
    from runs.llm.types import ALWAYS_LOCAL_FORBIDDEN, MIN_TIER_FLOOR

    panel = task.panel
    if panel is None or not panel.enabled:
        return
    floor = max(task.min_tier, MIN_TIER_FLOOR.get(name, 1))
    local_ok = task.allow_local and name not in ALWAYS_LOCAL_FORBIDDEN
    votes = 0
    adjudicators = 0
    for p in panel.passes:
        entry = cfg.models.get(p.model)
        if entry is None:
            raise ConfigError(f"tasks.{name}.panel: model alias {p.model!r} is not declared")
        if entry.tier < floor:
            raise ConfigError(
                f"tasks.{name}.panel: pass on {p.model!r} is tier {entry.tier}, below the "
                f"tier {floor} floor for this task"
            )
        if not local_ok and cfg.providers.get(entry.provider, None) is not None \
                and cfg.providers[entry.provider].kind == "ollama":
            raise ConfigError(
                f"tasks.{name}.panel: {p.model!r} is local and {name!r} may not be served "
                "by a local model"
            )
        votes += p.role == "vote"
        adjudicators += p.role == "adjudicator"
    if adjudicators > 1:
        raise ConfigError(f"tasks.{name}.panel: at most one adjudicator pass")
    if votes < panel.quorum:
        raise ConfigError(
            f"tasks.{name}.panel: {votes} voting pass(es) cannot reach quorum {panel.quorum}"
        )


def _cross_validate(cfg: ModelsConfig) -> None:
    for alias, entry in cfg.models.items():
        if entry is None:
            continue
        if entry.provider not in cfg.providers:
            raise ConfigError(f"models.{alias}.provider {entry.provider!r} is not declared")
    for name, task in cfg.tasks.items():
        if not task.chain:
            raise ConfigError(f"tasks.{name}.chain must not be empty")
        for alias in [*task.chain, *( [task.escalation] if task.escalation else [] )]:
            if alias not in cfg.models or cfg.models[alias] is None:
                raise ConfigError(f"tasks.{name}: model alias {alias!r} is not declared")
        best = max(cfg.tier_of(a) for a in task.chain)
        if best < task.min_tier:
            raise ConfigError(
                f"tasks.{name}: no chain entry reaches min_tier {task.min_tier} (best {best})"
            )
        _validate_panel(cfg, name, task)
    for alias in cfg.capabilities:
        if alias not in cfg.models:
            raise ConfigError(f"capabilities.{alias} is not a declared model alias")
    if cfg.shadow.enabled and (cfg.shadow.model is None or cfg.shadow.model not in cfg.models):
        raise ConfigError("shadow.enabled requires shadow.model to be a declared alias")


def load_models_cfg(
    path: Path | str | None = None, *, overlay: Path | str | None | _Sibling = SIBLING
) -> ModelsConfig:
    """Load config/models.yaml (v1 or v2) and merge the tier-1 overlay if present.

    ``overlay`` defaults to :data:`OVERLAY_NAME` **beside the given path**, not to the
    live checkout's copy. The old default was the absolute ``OVERLAY_PATH``, so loading a
    worktree's ``config/models.yaml`` through this strict loader silently merged the LIVE
    checkout's overlay — precisely the code path that is supposed to prove a proposed
    model change is safe before a human applies it. ``runs.router.load_models_cfg``
    already resolved the sibling correctly; the two now agree.

    Pass an explicit path to override, or ``None`` for "no overlay at all".
    """
    p = Path(path) if path else DEFAULT_MODELS_CONFIG
    if isinstance(overlay, _Sibling):
        overlay = p.parent / OVERLAY_NAME
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"models config not found: {p}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: expected a mapping at the top level")
    if int(raw.get("version", 1)) < MODELS_VERSION:
        raw = _v1_to_v2(raw)  # supported input shape, not an error
    try:
        cfg = ModelsConfig.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise ConfigError(f"models.yaml invalid at '{loc}': {first['msg']}") from e

    overlay_data: dict[str, Any] | None = None
    if overlay is not None:
        op = Path(overlay)
        if op.exists():
            loaded = yaml.safe_load(op.read_text())
            overlay_data = loaded if isinstance(loaded, dict) else None
    cfg = apply_overlay(cfg, overlay_data)
    _cross_validate(cfg)
    return cfg


def models_schema() -> dict[str, Any]:
    """The JSON Schema the console generates the Models page from."""
    return ModelsConfig.model_json_schema(by_alias=True)


__all__ = [
    "DEFAULT_MODELS_CONFIG",
    "MODELS_VERSION",
    "OVERLAY_NAME",
    "OVERLAY_PATH",
    "SIBLING",
    "Capability",
    "ModelEntry",
    "ModelsConfig",
    "PanelCfg",
    "PanelPassCfg",
    "TaskCfg",
    "apply_overlay",
    "load_models_cfg",
    "models_schema",
]
