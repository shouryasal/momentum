"""``/api/skills`` — the Skills page.

Step-up is required for anything that changes what an automated run can execute:
``scripts/**`` writes, creating or archiving a skill, and editing bindings or policy.
Reading, linting, testing and evaluating are session actions.

Every write is linted server side before it lands; a lint error rolls the file back and
comes back as the refusal with its findings, so a broken skill never sits on disk waiting
for the next review run to load it.

**Lint, Test, Eval and Trial are jobs, not requests.** A pytest suite or a routed session
takes seconds to minutes; running one inside the POST held the UI open and put the answer
at the mercy of whichever timeout fired first. Each button now registers its check on
``console.services.jobs.JobRunner`` and returns a ``job_id`` immediately. Progress, the
output lines and the final status arrive on the ``job`` SSE topic — every event labelled
with the skill and the kind, so a page can pick out its own — and ``GET
/api/skills/jobs/{job_id}`` is the same state for a client that reconnected or never
subscribed. ``POST /api/skills/jobs/{job_id}/cancel`` stops one: for Test and Eval that
terminates the child process, not just a flag nobody reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from console.deps import (
    HumanActor,
    audit_event,
    cfg_dep,
    current_actor,
    get_jdb,
    http_error,
    require_step_up,
)
from console.services import jobs as jobs_service
from console.services import skills_service
from console.services.skills_service import SkillError
from ops.config import EarnConfig
from ops.lib import paths

router = APIRouter(prefix="/skills", tags=["skills"])

#: Module-level dependency singletons — FastAPI resolves them per request exactly as an
#: inline Depends() would, and ruff B008 stays quiet.
ACTOR = Depends(current_actor)
STEP_UP = Depends(require_step_up)
CFG = Depends(cfg_dep)
JDB = Depends(get_jdb)

_HTTP_FOR = {"not_found": 404, "conflict": 409, "forbidden": 403, "too_large": 413,
             "lint_failed": 422, "invalid": 400}


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteBody(_Dto):
    content: str = Field(description="The whole file — writes are atomic, not patches.")
    base_sha: str | None = Field(default=None, description="sha256 of what you loaded.")


class CreateBody(_Dto):
    name: str = Field(min_length=2, max_length=48)
    description: str = Field(min_length=40)
    title: str | None = None


class ArchiveBody(_Dto):
    restore: bool = False


class BindingsBody(_Dto):
    tasks: list[str] = Field(default_factory=list)


class PolicyBody(_Dto):
    policy: dict[str, str]


class TrialBody(_Dto):
    prompt: str = Field(min_length=10)


def _root() -> Path:
    return paths.REPO_ROOT


def _fail(exc: SkillError) -> Exception:
    detail = {"findings": exc.detail} if exc.detail is not None else None
    return http_error(_HTTP_FOR.get(exc.code, 400), exc.code, str(exc), detail=detail)


def _publish(request: Request, name: str, action: str) -> None:
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        bus.publish("config", {"skill": name, "action": action})


# --------------------------------------------------------------------------- check jobs


def _runner(request: Request) -> Any:
    return getattr(request.app.state, "jobs", None)


def _check_job(cfg: EarnConfig) -> Any:
    """The body every check job runs.

    It takes everything from ``progress.args`` rather than a closure so the registered
    spec is the same object whatever the request said — two tabs linting the same skill at
    once cannot end up running each other's arguments.
    """

    def run(progress: jobs_service.JobProgress) -> Any:
        args = progress.args
        try:
            return skills_service.run_check(
                str(args.get("kind")), str(args.get("skill")), cfg, root=_root(),
                progress=progress, prompt=str(args.get("prompt") or ""),
                strict_tools=bool(args.get("strict", True)))
        except skills_service.CheckCancelled as e:
            raise jobs_service.JobCancelled(str(e)) from e

    return run


def _submit_check(request: Request, kind: str, name: str, *, actor: HumanActor,
                  cfg: EarnConfig, prompt: str = "", strict: bool = True
                  ) -> dict[str, Any]:
    try:
        skills_service.skill_dir(name, root=_root())          # 404 now, not inside a job
    except SkillError as e:
        raise _fail(e) from e
    runner = _runner(request)
    args = {"skill": name, "kind": kind, "prompt": prompt, "strict": strict}
    if runner is None:  # pragma: no cover - create_app always sets app.state.jobs
        result = skills_service.run_check(kind, name, cfg, root=_root(), prompt=prompt,
                                          strict_tools=strict)
        return {"job_id": None, "skill": name, "kind": kind, "status": "ok",
                "result": result}
    job_name = f"skills.{kind}:{name}"
    runner.register(jobs_service.JobSpec(
        name=job_name, fn=_check_job(cfg),
        description=f"{kind} the skill {name}"))
    run = runner.submit(job_name, actor=actor.actor, args=args,
                        labels={"area": "skills", "kind": kind, "skill": name})
    audit_event(actor=actor.actor, action=f"skill.{kind}", target=name,
                detail={"job_id": run.id})
    return {"job_id": run.id, "skill": name, "kind": kind, "status": run.status,
            "topic": jobs_service.JOB_TOPIC}


@router.get("/jobs/{job_id}", summary="One check job: status, progress, output, result")
def check_status(
    job_id: str,
    request: Request,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    runner = _runner(request)
    run = runner.get(job_id) if runner is not None else None
    if run is None:
        raise http_error(404, "not_found", f"no job {job_id}")
    return run.detail()


@router.post("/jobs/{job_id}/cancel", summary="Stop a running check")
def cancel_check(
    job_id: str,
    request: Request,
    actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    runner = _runner(request)
    run = runner.get(job_id) if runner is not None else None
    if run is None:
        raise http_error(404, "not_found", f"no job {job_id}")
    cancelled = bool(runner.cancel(job_id))
    audit_event(actor=actor.actor, action="skill.check.cancel", target=run.job,
                detail={"job_id": job_id})
    return {"id": job_id, "cancelled": cancelled, "status": run.status}


@router.get("", summary="Every skill: policy, bindings, status, tests")
def list_skills(
    _actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
    jdb: Any = JDB,
) -> dict[str, Any]:
    return {"items": skills_service.list_skills(cfg, root=_root(), jdb=jdb)}


@router.get("/{name}/tree", summary="The skill's files, with their tier")
def tree(
    name: str,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    try:
        return {"skill": name, "files": skills_service.tree(name, root=_root())}
    except SkillError as e:
        raise _fail(e) from e


@router.get("/{name}/files/{path:path}", summary="One file")
def read_file(
    name: str,
    path: str,
    _actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    try:
        return skills_service.read_file(name, path, root=_root())
    except SkillError as e:
        raise _fail(e) from e


@router.put("/{name}/files/{path:path}", summary="Write one file (lint decides)")
def write_file(
    request: Request,
    name: str,
    path: str,
    body: WriteBody,
    actor: HumanActor = ACTOR,
) -> dict[str, Any]:
    if skills_service.requires_step_up(path):
        actor.require_step_up()          # scripts/** is tier 2
    try:
        result = skills_service.write_file(name, path, body.content, actor=actor.actor,
                                           base_sha=body.base_sha, root=_root())
    except SkillError as e:
        audit_event(actor=actor.actor, action="skill.write", target=f"{name}/{path}",
                    detail={"code": e.code}, result="denied")
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="skill.write", target=f"{name}/{path}",
                detail={"lint_ok": result["lint_ok"]})
    _publish(request, name, "write")
    return result


@router.post("", summary="Create a skill from the template (step-up; lands incubating)")
def create_skill(
    request: Request,
    body: CreateBody,
    actor: HumanActor = STEP_UP,
) -> dict[str, Any]:
    try:
        result = skills_service.create_skill(body.name, description=body.description,
                                             title=body.title, actor=actor.actor,
                                             root=_root())
    except SkillError as e:
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="skill.create", target=body.name)
    _publish(request, body.name, "create")
    return result


@router.post("/{name}/archive", summary="Archive or restore a skill (step-up)")
def archive(
    request: Request,
    name: str,
    body: ArchiveBody,
    actor: HumanActor = STEP_UP,
) -> dict[str, Any]:
    try:
        result = skills_service.archive_skill(name, actor=actor.actor, root=_root(),
                                              restore=body.restore)
    except SkillError as e:
        raise _fail(e) from e
    audit_event(actor=actor.actor, action="skill.archive", target=name,
                detail={"restore": body.restore})
    _publish(request, name, "archive")
    return result


@router.post("/{name}/lint", summary="Lint as the change gate would (job)")
def lint(
    name: str,
    request: Request,
    strict: bool = Query(default=True, description="False shows a human save's verdict."),
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return _submit_check(request, "lint", name, actor=actor, cfg=cfg, strict=strict)


@router.post("/{name}/test", summary="Run the skill's own pytest suite (job)")
def test(
    name: str,
    request: Request,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return _submit_check(request, "test", name, actor=actor, cfg=cfg)


@router.post("/{name}/eval", summary="Run the skill's eval cases and score them (job)")
def evaluate(
    name: str,
    request: Request,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return _submit_check(request, "eval", name, actor=actor, cfg=cfg)


@router.post("/{name}/trial",
             summary="One routed session with this skill, in a throwaway worktree (job)")
def trial(
    name: str,
    request: Request,
    body: TrialBody,
    actor: HumanActor = ACTOR,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    return _submit_check(request, "trial", name, actor=actor, cfg=cfg, prompt=body.prompt)


@router.put("/{name}/bindings", summary="Which tasks load this skill (step-up; earn.yaml)")
def set_bindings(
    name: str,
    body: BindingsBody,
    actor: HumanActor = STEP_UP,
    cfg: EarnConfig = CFG,
) -> dict[str, Any]:
    try:
        ops = skills_service.bindings_patch(name, body.tasks, cfg)
    except SkillError as e:
        raise _fail(e) from e
    return _save_earn(ops, actor, f"skill bindings for {name}", f"skill.bindings:{name}")


@router.put("/{name}/policy", summary="Who may edit this skill's parts (step-up; earn.yaml)")
def set_policy(
    name: str,
    body: PolicyBody,
    actor: HumanActor = STEP_UP,
) -> dict[str, Any]:
    try:
        ops = skills_service.policy_patch(name, body.policy)
    except SkillError as e:
        raise _fail(e) from e
    return _save_earn(ops, actor, f"skill policy for {name}", f"skill.policy:{name}")


def _save_earn(ops: list[dict[str, Any]], actor: HumanActor, reason: str,
               action: str) -> dict[str, Any]:
    from console.services import config_service

    base_sha = str(config_service.get_file("earn").get("sha") or "")
    try:
        result = config_service.save_change("earn", base_sha=base_sha, reason=reason,
                                            actor=actor.actor, patch=ops, step_up=True)
    except Exception as e:  # noqa: BLE001 - config_store raises its own typed errors
        raise http_error(409, "conflict", str(e)) from e
    audit_event(actor=actor.actor, action=action, target="config/earn.yaml",
                detail={"ops": ops})
    return {"saved": result, "ops": ops}
