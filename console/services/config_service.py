"""Config service: what the config router calls, with no FastAPI in sight.

Everything here is a plain function over :mod:`ops.config_store` and
:mod:`console.services.effects`, so the whole save pipeline — preview, optimistic
concurrency, protected-path refusal, bless, audit, effects — is unit-testable without an
HTTP client.

It also owns two small seams the rest of P7 shares:

* :func:`resolve_dep` looks a dependency up in ``console.deps`` / ``console.security`` by
  any of several plausible names and falls back to a local no-op, so a router written
  against the foundation still imports while the foundation is landing next door;
* :func:`journal` opens the journal database, or yields ``None`` when there is not one yet,
  so a page renders on a fresh checkout instead of 500-ing.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from console.services import effects as effects_engine
from ops import config_store, db
from ops.config import EarnConfig, load_config
from ops.config_store import (
    ConfigStoreError,
    ConflictError,
    LiveLockedError,
    StepUpRequired,
    ValidationFailed,
)
from ops.lib import paths

__all__ = [
    "ConfigStoreError",
    "ConflictError",
    "LiveLockedError",
    "StepUpRequired",
    "ValidationFailed",
    "apply_pending_effects",
    "drift_report",
    "effects_banner",
    "get_cfg",
    "get_file",
    "journal",
    "list_files",
    "preview_change",
    "resolve_dep",
    "revert_change",
    "save_change",
]


# --------------------------------------------------------------------------- seams


def resolve_dep(*names: str, fallback: Any, modules: Sequence[str] = ("console.deps",
                                                                     "console.security")) -> Any:
    """First attribute named ``names`` found on ``modules``, else ``fallback``.

    The foundation owns ``console/deps.py``; P7's routers are written against it but must
    not fail to import before it exists, nor care whether the dependency ended up called
    ``require_session`` or ``session_required``.
    """
    for mod_name in modules:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for name in names:
            found = getattr(mod, name, None)
            if found is not None:
                return found
    return fallback


_CFG_CACHE: dict[str, tuple[str, EarnConfig]] = {}


def get_cfg(root: Path | None = None) -> EarnConfig:
    """The loaded config, cached on the file's sha so an edit is picked up immediately.

    Deliberately local rather than delegating to ``console.deps``: this cache has to follow
    ``root``, which the foundation's request-scoped accessor knows nothing about.
    """
    path = (root or paths.REPO_ROOT) / "config" / "earn.yaml"
    try:
        sha = config_store.signing.sha256_file(path)
    except OSError:
        sha = ""
    cached = _CFG_CACHE.get(str(path))
    if cached and cached[0] == sha:
        return cached[1]
    cfg = load_config(path, root=root)
    _CFG_CACHE[str(path)] = (sha, cfg)
    return cfg


@contextmanager
def journal(*, readonly: bool = False, root: Path | None = None) -> Iterator[
    sqlite3.Connection | None
]:
    """The journal connection, or ``None`` when the database has not been created yet."""
    try:
        cfg = get_cfg(root)
        path = db.journal_path(cfg, root)
    except Exception:  # noqa: BLE001 - a page must render without a journal
        yield None
        return
    if not Path(path).exists():
        yield None
        return
    with db.opened(path, readonly=readonly) as conn:
        yield conn


# --------------------------------------------------------------------------- reads


def list_files(root: Path | None = None) -> dict[str, Any]:
    return {
        "files": config_store.registry(root),
        "effects": effects_engine.catalogue(),
        "banner": effects_engine.banner(),
    }


def get_file(file_id: str, *, root: Path | None = None) -> dict[str, Any]:
    with journal(readonly=True, root=root) as conn:
        payload = config_store.read(file_id, root=root, conn=conn)
    payload["banner"] = effects_engine.banner()
    return payload


def file_history(file_id: str, *, limit: int = 50, root: Path | None = None) -> dict[str, Any]:
    with journal(readonly=True, root=root) as conn:
        rows = config_store.history(conn, file_id, limit=limit)
    return {"file_id": file_id, "entries": rows}


def defaults_for(file_id: str, paths_: Sequence[str]) -> dict[str, Any]:
    """"Revert to default" values, straight from the schema."""
    return {p: config_store.default_for(file_id, p) for p in paths_}


# --------------------------------------------------------------------------- writes


def preview_change(
    file_id: str,
    *,
    patch: Sequence[Mapping[str, Any]] | None = None,
    raw: str | None = None,
    base_sha: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    try:
        pv = config_store.preview(file_id, patch=patch, raw=raw, base_sha=base_sha, root=root)
    except ValidationFailed as e:
        # A preview never fails: an unparseable paste or a bad pointer *is* the answer.
        return {
            "file_id": file_id,
            "valid": False,
            "errors": [i.as_dict() for i in e.issues],
            "diff": "",
            "changed_paths": [],
            "protected_changed": [],
            "locked_paths": [],
            "effects": [],
            "effect_details": [],
            "restarts": [],
            "requires_stepup": False,
            "requires_confirm": False,
            "confirm_phrase": config_store.confirm_phrase_for(file_id),
            "reflowed": False,
            "base_sha": base_sha or "",
            "new_sha": "",
            "new_text": "",
        }
    # `new_text` is what the Raw tab and the diff viewer render; it is the operator's own
    # candidate, so returning it leaks nothing they did not just send.
    out = pv.as_dict(include_text=True)
    out["effect_details"] = [effects_engine.describe(e) for e in pv.effects]
    return out


def _effects_default(root: Path | None) -> str:
    try:
        return str(get_cfg(root).console.effects_default)
    except Exception:  # noqa: BLE001 - a broken config still has to be savable
        return "apply_now"


def save_change(
    file_id: str,
    *,
    base_sha: str,
    reason: str,
    actor: str,
    patch: Sequence[Mapping[str, Any]] | None = None,
    raw: str | None = None,
    commit: bool | None = None,
    apply_effects: bool | None = None,
    step_up: bool = False,
    confirm_phrase: str | None = None,
    root: Path | None = None,
    runner: effects_engine.Runner | None = None,
) -> dict[str, Any]:
    """Save, then either run the effects or queue them for the banner."""
    cfg_commit = commit
    if cfg_commit is None:
        try:
            cfg_commit = bool(get_cfg(root).console.git_commit_on_save)
        except Exception:  # noqa: BLE001
            cfg_commit = False
    do_apply = apply_effects
    if do_apply is None:
        do_apply = _effects_default(root) == "apply_now"

    with journal(root=root) as conn:
        result = config_store.save(
            file_id,
            patch=patch,
            raw=raw,
            base_sha=base_sha,
            reason=reason,
            actor=actor,
            conn=conn,
            root=root,
            commit=bool(cfg_commit),
            step_up=step_up,
            confirm_phrase=confirm_phrase,
        )
        payload = _finish(result, apply_now=bool(do_apply), root=root, runner=runner)
        if payload["effects_applied"] and conn is not None and result.audit_id:
            from ops.lib import audit as audit_lib

            audit_lib.try_record(
                conn,
                actor=actor,
                action="config.effects.apply",
                target=result.rel,
                detail={"effects": result.effects, "config_audit_id": result.audit_id},
            )
            try:
                audit_lib.mark_config_applied(conn, int(result.audit_id))
            except Exception:  # noqa: BLE001 - the save itself already succeeded
                pass
    return payload


def _finish(
    result: config_store.SaveResult,
    *,
    apply_now: bool,
    root: Path | None,
    runner: effects_engine.Runner | None,
) -> dict[str, Any]:
    payload = result.as_dict()
    applied: list[dict[str, Any]] = []
    if result.effects:
        if apply_now:
            cfg = None
            try:
                cfg = get_cfg(root)
            except Exception:  # noqa: BLE001 - restarts only need the compose project name
                cfg = None
            results = effects_engine.apply_effects(
                [e for e in result.effects if e in effects_engine.AUTO_APPLICABLE],
                cfg=cfg,
                root=root,
                runner=runner,
            )
            applied = [r.as_dict() for r in results]
            manual = [e for e in result.effects if e not in effects_engine.AUTO_APPLICABLE]
            if manual:
                effects_engine.queue_pending(
                    manual, source=f"config:{result.file_id}", reason=result.rel,
                    audit_id=result.audit_id,
                )
        else:
            effects_engine.queue_pending(
                result.effects, source=f"config:{result.file_id}", reason=result.rel,
                audit_id=result.audit_id,
            )
    payload["effects_applied"] = bool(applied) and all(
        r["status"] == "applied" for r in applied
    )
    payload["effects_result"] = applied
    payload["banner"] = effects_engine.banner()
    return payload


def revert_change(
    file_id: str,
    audit_id: int,
    *,
    actor: str,
    step_up: bool = False,
    confirm_phrase: str | None = None,
    apply_effects: bool | None = None,
    commit: bool | None = None,
    root: Path | None = None,
    runner: effects_engine.Runner | None = None,
) -> dict[str, Any]:
    do_apply = apply_effects
    if do_apply is None:
        do_apply = _effects_default(root) == "apply_now"
    with journal(root=root) as conn:
        if conn is None:
            raise ConfigStoreError("no journal database: nothing to revert to")
        result = config_store.revert(
            file_id,
            audit_id,
            conn=conn,
            actor=actor,
            root=root,
            step_up=step_up,
            confirm_phrase=confirm_phrase,
            commit=bool(commit),
        )
        return _finish(result, apply_now=bool(do_apply), root=root, runner=runner)


# --------------------------------------------------------------------------- effects


def effects_banner() -> dict[str, Any]:
    return effects_engine.banner()


def apply_pending_effects(
    *,
    actor: str,
    only: Sequence[str] | None = None,
    root: Path | None = None,
    runner: effects_engine.Runner | None = None,
) -> dict[str, Any]:
    queued = [p.effect for p in effects_engine.pending()]
    wanted = [e for e in (only or queued) if e in queued and e in effects_engine.AUTO_APPLICABLE]
    cfg = None
    try:
        cfg = get_cfg(root)
    except Exception:  # noqa: BLE001
        cfg = None
    results = effects_engine.apply_effects(wanted, cfg=cfg, root=root, runner=runner)
    with journal(root=root) as conn:
        if conn is not None:
            from ops.lib import audit as audit_lib

            audit_lib.try_record(
                conn,
                actor=actor,
                action="config.effects.apply",
                target="pending",
                detail={"requested": wanted, "results": [r.as_dict() for r in results]},
                result="ok" if all(r.status == "applied" for r in results) else "failed",
            )
    return {"results": [r.as_dict() for r in results], "banner": effects_engine.banner()}


def drift_report(root: Path | None = None) -> dict[str, Any]:
    report = config_store.drift(root)
    report["banner"] = effects_engine.banner()
    return report


# --------------------------------------------------------------------------- errors


STATUS_FOR: dict[type[Exception], int] = {
    ConflictError: 409,
    ValidationFailed: 422,
    StepUpRequired: 403,
    LiveLockedError: 403,
}


def error_payload(exc: ConfigStoreError) -> tuple[int, dict[str, Any]]:
    """`{"error": {...}}` plus the status code, exactly as spec §5.2 defines the shape."""
    detail: Any = None
    if isinstance(exc, ValidationFailed):
        detail = [i.as_dict() for i in exc.issues]
    elif isinstance(exc, StepUpRequired):
        detail = {"paths": exc.paths, "confirm_phrase": exc.confirm_phrase}
    elif isinstance(exc, LiveLockedError):
        detail = {"blocked": exc.blocked, "live_sleeves": exc.sleeves}
    elif isinstance(exc, ConflictError):
        detail = {"expected": exc.expected, "actual": exc.actual}
    status = getattr(exc, "status", 400)
    return status, {"error": {"code": exc.code, "message": str(exc), "detail": detail}}


Dep = Callable[..., Any]
