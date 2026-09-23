"""``GET /api/llm/local-model`` — the operator never types a URL again.

The owner's rule for this surface is one sentence long: *local models are a backend
concern*. So the tests here are less about numbers and more about what the answer is
**shaped** like —

* one ``line`` in plain words, and nothing in the payload that could be a form field
  (``configurable`` is false, and there is no write route on the path at all);
* the base URL is **detected**, not configured: the probe list expands to the WSL2
  addresses that can actually reach a Windows-hosted daemon, not just ``127.0.0.1``;
* the model is whatever ``models.yaml`` already routes to, and when it is missing the
  backend starts the pull itself and says so;
* when nothing is reachable, the reason says why in WSL terms and carries exactly one
  command that fixes it.

Every probe goes through an injected ``httpx.MockTransport``; nothing here opens a socket.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from console.services import local_model_service as lm
from ops import db
from ops.config import load_config

OLLAMA = "http://127.0.0.1:11434"
MODEL = "llama3.1:8b"


@pytest.fixture(autouse=True)
def _fresh_caches():
    lm.clear_cache()
    yield
    lm.clear_cache()


@pytest.fixture
def knowledge(env: Path) -> Path:
    _, knowledge_path = db.init_all(load_config(), root=env)
    return knowledge_path


def transport(*, models: list[str] | None = None, up: bool = True,
              eval_count: int = 8, eval_ns: int = 200_000_000) -> httpx.Client:
    """A daemon that answers on 127.0.0.1 only — exactly like Ollama bound to loopback."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not up or not url.startswith(OLLAMA):
            raise httpx.ConnectError("connection refused", request=request)
        if url.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.34.1"})
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={
                "models": [{"name": name}
                           for name in ([MODEL] if models is None else models)]})
        if url.endswith("/api/generate"):
            return httpx.Response(200, json={
                "response": "pong", "eval_count": eval_count,
                "eval_duration": eval_ns})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def cfg_with(model: str | None = MODEL, *, enabled: bool = True) -> SimpleNamespace:
    """The shape ``local_model_service`` reads out of ``models.yaml``."""
    models = {}
    if model is not None:
        models["local_small"] = SimpleNamespace(provider="ollama", id=model)
    return SimpleNamespace(
        providers={"ollama": SimpleNamespace(enabled=enabled, probe=None)},
        models=models,
    )


class FakeRun:
    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.progress = 0.37
        self.message = "pulling manifest"
        self.terminal = False


class FakeJobs:
    """Enough of ``console.services.jobs`` for the auto-pull to be observable."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.submitted: list[str] = []
        self.runs: dict[str, FakeRun] = {}

    def register(self, spec) -> None:  # noqa: ANN001
        self.registered.append(spec.name)

    def submit(self, name: str, **_kwargs) -> FakeRun:  # noqa: ANN003
        run = FakeRun(f"job-{len(self.submitted)}")
        self.submitted.append(name)
        self.runs[run.id] = run
        return run

    def get(self, run_id: str) -> FakeRun | None:
        return self.runs.get(run_id)


# --------------------------------------------------------------------------- the service


def test_a_reachable_daemon_with_the_model_reads_as_one_connected_line(knowledge: Path):
    with transport() as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, measure=False)
    assert body["state"] == "connected"
    assert body["model"] == MODEL
    assert body["base_url"] == OLLAMA
    assert body["line"].startswith(f"local model: connected, {MODEL}")
    assert body["fix_command"] is None
    assert body["configurable"] is False


def test_the_rate_is_measured_not_guessed(knowledge: Path):
    """8 tokens in 0.2s is 40 tok/s, and the number in the line comes from the daemon."""
    with transport(eval_count=8, eval_ns=200_000_000) as client:
        assert lm.measure_throughput(OLLAMA, MODEL, client=client) == pytest.approx(40.0)

        lm.status(cfg_with(), knowledge=knowledge, client=client)  # kicks off the thread
        deadline = time.monotonic() + 5
        body = {}
        while time.monotonic() < deadline:
            body = lm.status(cfg_with(), knowledge=knowledge, client=client)
            if body["tok_per_s"]:
                break
            time.sleep(0.05)
    assert body["tok_per_s"] == pytest.approx(40.0)
    assert "40 tok/s" in body["line"]


def test_an_unreachable_daemon_explains_wsl_and_carries_one_fix(knowledge: Path):
    with transport(up=False) as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, measure=False)
    assert body["state"] == "unreachable"
    assert body["base_url"] is None
    assert "unreachable" in body["line"]
    assert "WSL2" in body["reason"] and "127.0.0.1" in body["reason"]
    assert body["fix_command"] == lm.FIX_COMMAND
    assert "PowerShell" in body["fix_shell"]
    assert body["fix_command"].count(";") == 2, "one paste, not a tutorial"


def test_detection_tries_the_addresses_wsl_can_actually_reach(knowledge: Path):
    """127.0.0.1 inside WSL2 is not the Windows host, so it cannot be the only candidate."""
    with transport(up=False) as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, measure=False)
    tried = body["tried"]
    assert OLLAMA in tried
    assert len(tried) > 1, tried
    assert any("host.docker.internal" in t or t != OLLAMA for t in tried)


def test_a_missing_model_is_pulled_by_the_backend_not_asked_for(knowledge: Path):
    jobs = FakeJobs()
    with transport(models=["some-other-model:latest"]) as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, jobs=jobs,
                         measure=False)
    assert body["state"] == "pulling"
    assert body["pull"]["job_id"] == "job-0"
    assert body["line"] == f"local model: downloading {MODEL} (37%) — nothing for you to do"
    assert jobs.submitted == [f"ollama.pull:{MODEL}"]


def test_the_same_pull_is_never_submitted_twice(knowledge: Path):
    jobs = FakeJobs()
    with transport(models=[]) as client:
        for _ in range(3):
            lm.status(cfg_with(), knowledge=knowledge, client=client, jobs=jobs,
                      measure=False)
    assert jobs.submitted == [f"ollama.pull:{MODEL}"]


def test_without_a_job_runner_the_line_says_it_will_retry(knowledge: Path):
    with transport(models=[]) as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, measure=False)
    assert body["state"] == "missing"
    assert "not downloaded yet" in body["line"]


def test_a_bare_model_name_matches_the_latest_tag(knowledge: Path):
    with transport(models=["mistral:latest"]) as client:
        body = lm.status(cfg_with("mistral"), knowledge=knowledge, client=client,
                         measure=False)
    assert body["state"] == "connected"


def test_a_disabled_provider_is_off_not_broken(knowledge: Path):
    body = lm.status(cfg_with(enabled=False), knowledge=knowledge, client=None,
                     measure=False)
    assert body["state"] == "disabled"
    assert body["line"] == "local model: off (no local provider is enabled)"
    assert body["fix_command"] is None


def test_no_alias_routes_locally_is_said_plainly(knowledge: Path):
    with transport() as client:
        body = lm.status(cfg_with(None), knowledge=knowledge, client=client,
                         measure=False)
    assert body["state"] == "connected"
    assert body["model"] is None
    assert "no local model is routed to" in body["line"]


def test_a_daemon_that_dies_mid_answer_is_a_state_not_a_crash(knowledge: Path):
    """``status`` is called from a page poll; it must never raise into the router."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.34.1"})
        raise httpx.ConnectError("gone", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        body = lm.status(cfg_with(), knowledge=knowledge, client=client, measure=False)
    assert body["state"] in ("connected", "missing", "unreachable")
    assert body["line"]


# --------------------------------------------------------------------------- the route


def test_the_route_answers_with_the_line_and_nothing_to_configure(
    auth_client: TestClient, app, knowledge: Path,  # noqa: ANN001
):
    app.state.http_client = transport()
    body = auth_client.get("/api/llm/local-model").json()
    assert body["state"] in ("connected", "pulling", "missing")
    assert body["line"]
    assert body["configurable"] is False
    assert "base_url" in body, "the URL is shown as a fact, not offered as a field"
    app.state.http_client.close()


def test_the_route_is_read_only(auth_client: TestClient, app):  # noqa: ANN001
    """There is no way to set a base URL or a model from the browser. That is the point."""
    app.state.http_client = transport()
    for call in (auth_client.post, auth_client.put, auth_client.delete):
        assert call("/api/llm/local-model").status_code in (404, 405)
    app.state.http_client.close()


def test_the_route_needs_a_session(client: TestClient):
    assert client.get("/api/llm/local-model").status_code == 401
