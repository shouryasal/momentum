"""``/api/skills`` — the Skills page's backend.

The load-bearing behaviours: a write is linted server side and a lint *error* rolls it back,
``scripts/**`` needs step-up, a new skill lands incubating (bound to nothing), and nothing
here can promote a skill into production on its own.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ops import db
from ops.config import REPO_ROOT, load_config

from .conftest import step_up

CLEAN_SKILL = (
    "---\n"
    "name: demo-skill\n"
    "description: Reconstructs one trade end to end from the journal and refuses to guess a"
    " price. Triggers on trade forensics.\n"
    "allowed-tools: Read, Grep, Glob, Skill, Write(knowledge/**)\n"
    "---\n\n"
    "# Demo skill\n\n## When this runs\n\nOn the words above.\n\n"
    "## Hard stops\n\nNever estimates a number it did not read from a script.\n"
)


def await_check(client: TestClient, url: str, *, json: dict | None = None,
                timeout: float = 30.0) -> dict[str, Any]:
    """POST one of the four check buttons and wait for its job to reach a verdict.

    The buttons are asynchronous now: the POST hands back a ``job_id`` and the work
    happens on a thread. Every test that used to read the result straight out of the
    response goes through here, so what is asserted is still the check's own answer.
    """
    started = client.post(url, json=json or {})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    assert job_id, started.text
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/skills/jobs/{job_id}")
        assert state.status_code == 200, state.text
        body = state.json()
        if body["terminal"]:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{url} never finished")


@pytest.fixture
def repo(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root with the real template and one demo skill."""
    root = env / "repo"
    (root / ".claude" / "skills").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / ".claude" / "skills" / "_template",
                    root / ".claude" / "skills" / "_template")
    demo = root / ".claude" / "skills" / "demo-skill"
    (demo / "tests").mkdir(parents=True)
    (demo / "scripts").mkdir()
    (demo / "SKILL.md").write_text(CLEAN_SKILL, encoding="utf-8")
    (demo / "tests" / "test_demo.py").write_text("def test_demo():\n    assert True\n")
    (demo / "scripts" / "run.py").write_text("print('rows=1')\n")
    db.init_all(load_config(), root=env)
    for module in ("console.routers.skills", "console.services.skills_service"):
        monkeypatch.setattr(f"{module}._root", lambda root=root: root)
    return root


def test_the_listing_shows_status_bindings_and_policy(auth_client: TestClient, repo: Path):
    response = auth_client.get("/api/skills")
    assert response.status_code == 200, response.text
    items = {row["name"]: row for row in response.json()["items"]}
    assert "demo-skill" in items
    demo = items["demo-skill"]
    assert demo["has_tests"] is True and demo["has_scripts"] is True
    assert demo["bindings"] == []          # nothing loads it yet
    assert demo["policy"]["scripts"] == "human"
    assert items["_template"]["status"] == "template"


def test_the_tree_marks_the_executable_parts_as_tier_two(auth_client: TestClient,
                                                         repo: Path):
    """``tests/**`` joined ``scripts/**``: it is what the Test button and the change gate
    execute, so a session that can write it has arbitrary code execution."""
    response = auth_client.get("/api/skills/demo-skill/tree")
    assert response.status_code == 200, response.text
    tiers = {row["path"]: row["tier"] for row in response.json()["files"]}
    assert tiers["SKILL.md"] == "tier1"
    assert tiers["scripts/run.py"] == "tier2"
    assert tiers["tests/test_demo.py"] == "tier2"


def test_reading_and_writing_a_body_round_trips(auth_client: TestClient, repo: Path):
    current = auth_client.get("/api/skills/demo-skill/files/SKILL.md").json()
    assert current["tier"] == "tier1" and current["requires_step_up"] is False

    updated = CLEAN_SKILL.replace("On the words above.", "On the words above, only.")
    response = auth_client.put("/api/skills/demo-skill/files/SKILL.md",
                               json={"content": updated, "base_sha": current["sha"]})
    assert response.status_code == 200, response.text
    assert response.json()["lint_ok"] is True
    assert "only." in (repo / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text()


def test_a_stale_base_sha_is_a_conflict(auth_client: TestClient, repo: Path):
    response = auth_client.put("/api/skills/demo-skill/files/SKILL.md",
                               json={"content": CLEAN_SKILL, "base_sha": "0" * 64})
    assert response.status_code == 409


def test_a_lint_error_rolls_the_write_back_and_returns_the_findings(
        auth_client: TestClient, repo: Path):
    before = (repo / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text()
    broken = CLEAN_SKILL.replace("allowed-tools: Read", "allowed-tools: WebSearch, Read")
    response = auth_client.put("/api/skills/demo-skill/files/SKILL.md",
                               json={"content": broken, "base_sha": None})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "lint_failed"
    codes = {f["code"] for f in body["detail"]["findings"]}
    assert "tools.network" in codes
    assert (repo / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text() == before


def test_a_banned_import_in_a_script_is_refused(auth_client: TestClient, repo: Path,
                                                token: str):
    step_up(auth_client, token)
    response = auth_client.put("/api/skills/demo-skill/files/scripts/run.py",
                               json={"content": "import socket\n", "base_sha": None})
    assert response.status_code == 422, response.text
    assert (repo / ".claude" / "skills" / "demo-skill" / "scripts" / "run.py").read_text() \
        == "print('rows=1')\n"


def test_writing_a_script_needs_step_up(auth_client: TestClient, repo: Path, token: str):
    response = auth_client.put("/api/skills/demo-skill/files/scripts/run.py",
                               json={"content": "print('rows=2')\n", "base_sha": None})
    assert response.status_code == 403
    step_up(auth_client, token)
    response = auth_client.put("/api/skills/demo-skill/files/scripts/run.py",
                               json={"content": "print('rows=2')\n", "base_sha": None})
    assert response.status_code == 200, response.text


def test_a_path_cannot_escape_the_skill_folder(repo: Path):
    """The HTTP client normalises ``..`` away, so this is asserted where it is enforced."""
    from console.services import skills_service
    from console.services.skills_service import SkillError

    with pytest.raises(SkillError) as excinfo:
        skills_service.write_file("demo-skill", "../../../config/earn.yaml", "x",
                                  actor="human:console:t", root=repo)
    assert excinfo.value.code == "forbidden"


def test_writing_a_skill_test_needs_step_up(auth_client: TestClient, repo: Path,
                                            token: str):
    """``tests/**`` is executed, so it is the human's — a session alone cannot plant it."""
    payload = {"content": "def test_x():\n    assert True\n", "base_sha": None}
    assert auth_client.put("/api/skills/demo-skill/files/tests/test_new.py",
                           json=payload).status_code == 403
    assert not (repo / ".claude" / "skills" / "demo-skill" / "tests" / "test_new.py").exists()
    step_up(auth_client, token)
    assert auth_client.put("/api/skills/demo-skill/files/tests/test_new.py",
                           json=payload).status_code == 200


def test_a_percent_encoded_traversal_cannot_dodge_the_step_up_gate(
        auth_client: TestClient, repo: Path):
    """Reproduced on a live app before the fix: the router decided step-up from the RAW
    ``{path:path}`` string, and Starlette decodes ``%2F`` *after* routing. So
    ``tests/..%2Fscripts%2Fzz.py`` had a first segment of ``tests``, took the tier-1
    branch, and landed in ``scripts/`` — the one directory automated runs execute.
    """
    target = repo / ".claude" / "skills" / "demo-skill" / "scripts" / "zz.py"
    for path in ("tests/..%2Fscripts%2Fzz.py",
                 "%2E%2E/scripts/zz.py",
                 "evals/../scripts/zz.py"):
        response = auth_client.put(f"/api/skills/demo-skill/files/{path}",
                                   json={"content": "print('pwned')\n", "base_sha": None})
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert not target.exists(), path


def test_a_skill_check_subprocess_inherits_no_console_secret(repo: Path,
                                                             monkeypatch: pytest.MonkeyPatch):
    """The console's own environment used to be the starting point, minus ten known
    credential names — so a secret added to ``.env`` tomorrow was inherited by default."""
    from console.services import skills_service

    monkeypatch.setenv("EARN_CONSOLE_SECRET", "console-should-not-leak")
    monkeypatch.setenv("EARN_TOMORROWS_SECRET", "brand-new-should-not-leak")
    monkeypatch.setenv("EARN_LIVE_ROOT", "/home/shourya/earn")
    env = skills_service._clean_env(repo)
    assert "EARN_CONSOLE_SECRET" not in env
    assert "EARN_TOMORROWS_SECRET" not in env
    assert "EARN_LIVE_ROOT" not in env
    assert env["PYTHONPATH"] == str(repo)


def test_the_tier_reported_to_the_browser_is_the_tier_that_will_apply(repo: Path):
    from console.services import skills_service

    assert skills_service.file_tier("tests/../scripts/zz.py") == "tier2"
    assert skills_service.file_tier("./scripts/zz.py") == "tier2"
    assert skills_service.file_tier("evals/cases.yaml") == "tier1"
    # unclassifiable ⇒ human-only, never "free"
    assert skills_service.file_tier("../../config/earn.yaml") == "tier2"
    assert skills_service.file_tier("/etc/passwd") == "tier2"
    assert skills_service.requires_step_up("tests/..%2Fscripts%2Fzz.py") is True


def test_creating_a_skill_scaffolds_it_incubating(auth_client: TestClient, repo: Path,
                                                  token: str):
    body = {"name": "trade-forensics",
            "description": "Reconstructs one trade end to end from the journal and refuses"
                           " to guess. Triggers on trade forensics.",
            "title": "Trade forensics"}
    assert auth_client.post("/api/skills", json=body).status_code == 403
    step_up(auth_client, token)
    response = auth_client.post("/api/skills", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "incubating"

    created = repo / ".claude" / "skills" / "trade-forensics"
    assert (created / "SKILL.md").exists()
    assert any((created / "tests").glob("test_*.py"))
    assert "{{SKILL_NAME}}" not in (created / "SKILL.md").read_text()

    listing = {row["name"]: row for row in auth_client.get("/api/skills").json()["items"]}
    assert listing["trade-forensics"]["status"] == "incubating"
    assert listing["trade-forensics"]["bindings"] == []    # loaded by nothing


def test_a_thin_description_is_refused(auth_client: TestClient, repo: Path, token: str):
    step_up(auth_client, token)
    response = auth_client.post("/api/skills",
                                json={"name": "thin", "description": "too short"})
    assert response.status_code == 422       # pydantic min_length


def test_a_duplicate_name_is_a_conflict(auth_client: TestClient, repo: Path, token: str):
    step_up(auth_client, token)
    response = auth_client.post("/api/skills", json={
        "name": "demo-skill",
        "description": "Reconstructs one trade end to end. Triggers on trade forensics."})
    assert response.status_code == 409


def test_archive_and_restore_move_the_overlay_status(auth_client: TestClient, repo: Path,
                                                     token: str):
    step_up(auth_client, token)
    assert auth_client.post("/api/skills/demo-skill/archive",
                            json={"restore": False}).json()["status"] == "archived"
    listing = {r["name"]: r for r in auth_client.get("/api/skills").json()["items"]}
    assert listing["demo-skill"]["status"] == "archived"
    assert auth_client.post("/api/skills/demo-skill/archive",
                            json={"restore": True}).json()["status"] == "incubating"


def test_lint_runs_on_demand(auth_client: TestClient, repo: Path):
    lint = await_check(auth_client, "/api/skills/demo-skill/lint")
    assert lint["status"] == "ok"
    assert lint["result"]["ok"] is True

    evals = await_check(auth_client, "/api/skills/demo-skill/eval")
    payload = evals["result"]
    assert payload["total"] == 0 and payload["ok"] is False   # no cases declared yet


def test_the_test_button_refuses_when_nothing_can_contain_the_suite(
        auth_client: TestClient, repo: Path):
    """This test used to assert ``result.ok is True`` — i.e. that the console happily ran
    a skill's model-authored pytest suite as the owner, with the console process's own
    environment. That was arbitrary code execution for anyone holding a session cookie.

    ``security.agent_user`` / ``security.agent_cli_wrapper`` ship as ``null``, so the
    only honest answer is a refusal, the same one the change gate gives.
    """
    body = await_check(auth_client, "/api/skills/demo-skill/test")
    result = body["result"]
    assert result["ok"] is False
    assert result["refused"].startswith("refused: "), result
    assert "agent_user" in result["refused"]
    assert result["sandbox"]["kind"] == "refused"


def test_a_literal_payload_in_a_skill_test_is_refused_by_the_lint(
        auth_client: TestClient, repo: Path, token: str):
    """The write half. The lint walked ``scripts/`` only, so ``tests/**`` — the file a
    session *can* write — was never inspected at all."""
    step_up(auth_client, token)
    written = repo / ".claude" / "skills" / "demo-skill" / "tests" / "test_zz_pwn.py"
    response = auth_client.put("/api/skills/demo-skill/files/tests/test_zz_pwn.py", json={
        "content": "def test_pwn():\n"
                   "    print(open('/home/shourya/earn/.env').read())\n",
        "base_sha": None})
    assert response.status_code == 422, response.text
    codes = {f["code"] for f in response.json()["error"]["detail"]["findings"]}
    assert "script.protected_path" in codes, codes
    assert not written.exists()


def test_a_planted_test_is_not_executed_by_the_test_button(auth_client: TestClient,
                                                           repo: Path, token: str):
    """The execute half, on the review's own reproduction: plant a payload the lint
    cannot refuse (a *computed* destination is a warning, not an error), press Test, and
    check nothing ran."""
    step_up(auth_client, token)          # tests/** is tier 2 now; get past that honestly
    payload = ("import os\n"
               "from pathlib import Path\n\n\n"
               "def test_pwn():\n"
               "    Path(os.getcwd()).joinpath('PWNED').write_text('x')\n")
    response = auth_client.put("/api/skills/demo-skill/files/tests/test_zz_pwn.py",
                               json={"content": payload, "base_sha": None})
    assert response.status_code == 200, response.text
    body = await_check(auth_client, "/api/skills/demo-skill/test")
    assert body["result"]["ok"] is False
    assert not (repo / "PWNED").exists()
    assert not (Path.cwd() / "PWNED").exists()


def test_a_check_returns_a_job_id_and_streams_it_on_the_bus(auth_client: TestClient,
                                                            repo: Path, app):
    """The point of the move: the POST returns at once and the answer arrives on SSE."""
    seen: list[dict] = []
    real = app.state.bus.publish

    def spy(topic: str, payload: dict | None = None, **kw):
        if topic == "job":
            seen.append(dict(payload or {}))
        return real(topic, payload, **kw)

    app.state.bus.publish = spy            # type: ignore[method-assign]
    try:
        body = auth_check_start(auth_client, "/api/skills/demo-skill/lint")
        # The response carries a handle, never the findings: a fast lint may already be
        # ``ok`` by the time the POST returns, but the client still follows the job.
        assert set(body) == {"job_id", "skill", "kind", "status", "topic"}
        assert body["topic"] == "job"
        final = _wait(auth_client, body["job_id"])
        # The status becomes terminal in the runner a hair before the event leaves the
        # bus, so wait for the event rather than assume the GET implies it.
        mine = _wait_for_event(seen, body["job_id"], lambda e: e.get("status") == "ok")
    finally:
        app.state.bus.publish = real       # type: ignore[method-assign]

    assert final["status"] == "ok"
    assert {e.get("event") for e in mine} == {"status", "output"}
    assert any(e.get("labels", {}).get("skill") == "demo-skill" for e in mine)
    assert any(e.get("status") == "ok" for e in mine)


def auth_check_start(client: TestClient, url: str, json: dict | None = None) -> dict:
    response = client.post(url, json=json or {})
    assert response.status_code == 200, response.text
    return response.json()


def _wait_for_event(seen: list[dict], job_id: str, predicate, timeout: float = 10.0
                    ) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        mine = [e for e in seen if e.get("id") == job_id]
        if any(predicate(e) for e in mine):
            return mine
        time.sleep(0.02)
    raise AssertionError(f"no matching job event for {job_id}: {seen}")


def _wait(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/skills/jobs/{job_id}").json()
        if body["terminal"]:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def test_a_running_check_can_be_cancelled(auth_client: TestClient, repo: Path,
                                          monkeypatch: pytest.MonkeyPatch):
    """Cancel must reach the work, not just set a flag the check never reads."""
    from console.services import skills_service

    started = __import__("threading").Event()

    def slow_eval(name, cfg, *, root=None, session_runner=None, progress=None):
        started.set()
        for _ in range(600):
            if progress is not None and progress.cancelled:
                raise skills_service.CheckCancelled("stopped between cases")
            time.sleep(0.01)
        return {"name": name, "total": 0, "cases": []}

    monkeypatch.setattr(skills_service, "run_eval", slow_eval)
    body = auth_check_start(auth_client, "/api/skills/demo-skill/eval")
    assert started.wait(timeout=10)
    cancel = auth_client.post(f"/api/skills/jobs/{body['job_id']}/cancel", json={})
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["cancelled"] is True
    assert _wait(auth_client, body["job_id"])["status"] == "cancelled"


def test_an_unknown_check_job_is_a_404(auth_client: TestClient, repo: Path):
    assert auth_client.get("/api/skills/jobs/deadbeef").status_code == 404
    assert auth_client.post("/api/skills/jobs/deadbeef/cancel", json={}).status_code == 404


def test_a_check_on_a_missing_skill_fails_before_a_job_exists(auth_client: TestClient,
                                                              repo: Path):
    response = auth_client.post("/api/skills/nope/test")
    assert response.status_code == 404, response.text


def test_the_strict_lint_is_what_the_change_gate_would_say(auth_client: TestClient,
                                                           repo: Path, token: str):
    step_up(auth_client, token)
    broad = CLEAN_SKILL.replace("allowed-tools: Read",
                                "allowed-tools: Bash(docker compose *), Read")
    saved = auth_client.put("/api/skills/demo-skill/files/SKILL.md",
                            json={"content": broad, "base_sha": None})
    assert saved.status_code == 200, saved.text      # a human may do this
    assert saved.json()["lint_ok"] is True

    strict = await_check(auth_client, "/api/skills/demo-skill/lint")
    assert strict["result"]["ok"] is False
    lenient = await_check(auth_client, "/api/skills/demo-skill/lint?strict=false")
    assert lenient["result"]["ok"] is True


def test_an_unknown_skill_is_a_404(auth_client: TestClient, repo: Path):
    assert auth_client.get("/api/skills/nope/tree").status_code == 404


def test_every_route_needs_a_session(client: TestClient, repo: Path):
    assert client.get("/api/skills").status_code == 401
    # non-GET is refused by the CSRF guard before the session check even runs
    assert client.post("/api/skills/demo-skill/lint").status_code in (401, 403)
