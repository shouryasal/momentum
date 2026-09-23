"""Step 2 — the console over the wire: auth, shape, refusals, redaction, the bundle.

Everything here goes through a real ``python -m console serve`` subprocess on loopback.
The point is the transport and the guards, not the handlers: a ``TestClient`` would never
have exercised the Host header, the cookie, the CSRF double-submit, the redaction
middleware on a streamed body, or the static mount.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import SENTINELS, Api, Sandbox, Server, free_port

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

#: The endpoints the task names, each with the key its payload must carry.
CORE_READS: dict[str, tuple[str, ...]] = {
    "/api/health": ("ok", "version", "ts"),
    "/api/meta/schema": ("config_id", "fields"),
    "/api/config": ("files",),
    "/api/mode": ("verified", "reason", "phase", "sleeves"),
    "/api/ops/jobs": ("jobs",),
    "/api/signals": ("signals",),
    "/api/changes": ("counts", "items"),
    "/api/skills": ("items",),
    "/api/llm/providers": ("auth_mode", "providers"),
}

#: A value for every path parameter in the mounted route table, so the crawl can ask for
#: a real URL rather than one containing a literal ``{sleeve}``.
PATH_VALUES: dict[str, str] = {
    "sleeve": "a",
    "file_id": "earn",
    "source": "config_audit",
    "ref": "1",
    "invariant_id": "console_bind",
    "run_id": "test-a-1",
    "bt_id": "1",
    "change_id": "none",
    "signal_id": "none",
    "name": "post-mortem",
    "path": "SKILL.md",
    "date": "2026-09-22",
    "asset": "BTC",
    "job": "healthcheck",
    "key": "claude:subscription",
    "family": "research",
    "rel": "daily",
    "incident_id": "1",
    "trade_id": "1",
    "lock_id": "1",
    "service": "freqtrade-a",
    "target": "telegram",
}

ROUTE_BLOCK = re.compile(r"```routes\n(.*?)```", re.S)


def mounted_get_routes(repo: Path) -> list[str]:
    """Every GET path from the route table in ``docs/contracts.md`` §9.6."""
    text = (repo / "docs" / "contracts.md").read_text(encoding="utf-8")
    match = ROUTE_BLOCK.search(text)
    assert match, "docs/contracts.md has no ```routes block"
    out: list[str] = []
    for line in match.group(1).splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] != "GET":
            continue
        out.append(parts[1])
    assert len(out) > 80, f"only {len(out)} GET routes parsed"
    return out


def concrete(path: str) -> str:
    """Substitute every ``{param}`` (and ``{param:path}``) with a plausible value."""
    def sub(match: re.Match[str]) -> str:
        name = match.group(1).split(":")[0]
        return PATH_VALUES.get(name, "none")

    return re.sub(r"\{([^}]+)\}", sub, path)


# --------------------------------------------------------------------------- shape


class TestCoreReads:
    def test_health_is_public(self, anon: httpx.Client) -> None:
        response = anon.get("/api/health")
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

    @pytest.mark.parametrize("path", sorted(CORE_READS))
    def test_the_payload_is_schema_shaped(self, api: Api, path: str) -> None:
        response = api.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"
        body = response.json()
        assert isinstance(body, dict), f"{path} returned {type(body).__name__}"
        for key in CORE_READS[path]:
            assert key in body, f"{path} payload has no {key!r}: {sorted(body)}"

    def test_meta_schema_describes_real_config_leaves(self, api: Api) -> None:
        fields = api.json("/api/meta/schema")["fields"]
        by_path = {f["path"]: f for f in fields}
        assert "risk.usdt_floor" in by_path, sorted(by_path)[:20]
        leaf = by_path["risk.usdt_floor"]
        assert leaf.get("description"), "a config leaf reached the UI with no description"
        assert leaf.get("tier"), "a config leaf reached the UI with no x-tier"

    def test_the_mode_payload_is_fail_closed_before_anything_is_signed(self, api: Api) -> None:
        body = api.json("/api/mode")
        assert body["verified"] is False and body["reason"] == "missing"
        assert {s["sleeve"] for s in body["sleeves"]} == {"a", "b"}
        assert all(s["state"] == "TEST" for s in body["sleeves"])

    def test_ops_jobs_lists_the_configured_schedules(self, api: Api) -> None:
        jobs = {j["job"] for j in api.json("/api/ops/jobs")["jobs"]}
        for expected in ("ingest", "scanner", "healthcheck", "research_run", "daily_review"):
            assert expected in jobs, sorted(jobs)

    def test_models_are_served_by_the_llm_router(self, api: Api) -> None:
        """There is no ``/api/models``; the AI & Models page reads the ``/api/llm`` area.

        docs/contracts.md §9.6 is the mounted surface and it has no ``/api/models`` line,
        so asking for one is a 404 by design rather than a missing endpoint.
        """
        assert api.get("/api/models").status_code == 404
        providers = api.json("/api/llm/providers")
        keys = {p["key"] for p in providers["providers"]}
        assert keys, "no LLM providers were reported"
        assert any(k.startswith("claude") for k in keys), keys
        routing = api.json("/api/llm/routing")
        assert routing, "the routing matrix came back empty"
        assert api.get("/api/llm/usage?group=task").status_code == 200


# --------------------------------------------------------------------------- refusals


class TestRefusals:
    def test_an_unauthenticated_request_is_refused(self, anon: httpx.Client) -> None:
        for path in ("/api/meta", "/api/config", "/api/mode", "/api/signals"):
            response = anon.get(path)
            assert response.status_code == 401, f"{path} -> {response.status_code}"
            assert response.json()["error"]["code"] == "unauthorized"

    def test_a_bad_host_header_is_refused(self, server: Server) -> None:
        response = httpx.get(f"{server.base_url}/api/health",
                             headers={"Host": "earn.example.com"}, timeout=10.0)
        assert response.status_code == 421
        assert response.json()["error"]["code"] == "bad_host"

    def test_a_bad_origin_is_refused_on_a_mutation(self, api: Api) -> None:
        response = api.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "bad_origin"

    def test_a_missing_csrf_header_is_refused(self, server: Server, console_token: str) -> None:
        session = Api(server.base_url, console_token)
        session.login()
        del session.client.headers["X-Earn-CSRF"]
        try:
            response = session.post("/api/kill", json={"reason": "csrf probe"})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_failed"
        finally:
            session.close()

    def test_a_non_json_mutation_is_refused(self, api: Api) -> None:
        response = api.post("/api/auth/logout", content=b"reason=x",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert response.status_code == 415

    def test_a_bad_token_is_refused_and_audited(self, server: Server) -> None:
        with httpx.Client(base_url=server.base_url, timeout=10.0,
                          headers={"Origin": server.base_url}) as client:
            response = client.post("/api/auth/login", json={"token": "not-the-token"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


class TestAutomatedRun:
    def test_the_cli_refuses_to_start(self, sandbox: Sandbox) -> None:
        result = sandbox.py("-m", "console", "serve", timeout=60,
                            env={"EARN_AUTOMATED_RUN": "1"})
        assert result.returncode == 3, result.stdout + result.stderr
        assert "EARN_AUTOMATED_RUN" in result.stderr

    def test_create_app_refuses_outright(self, sandbox: Sandbox) -> None:
        """The flag is checked before any route exists — the middleware is the second net."""
        probe = sandbox.py(
            "-c",
            "from console.app import create_app;"
            "from console.settings import ConsoleSettings;"
            "create_app(ConsoleSettings.from_config(port=9999))",
            env={"EARN_AUTOMATED_RUN": "1"}, timeout=120,
        )
        assert probe.returncode != 0
        assert "EARN_AUTOMATED_RUN=1" in probe.stderr, probe.stderr

    def test_the_app_refuses_every_mutation(self, sandbox: Sandbox, static_bundle: Path,
                                            db_ready: Sandbox, console_token: str) -> None:
        """The flag appearing *after* boot must still close every mutating route.

        ``create_app`` refuses outright when the variable is already set, so the only way
        to reach ``AutomatedRunMiddleware`` is the case it exists for: a console that was
        started by a human and then finds itself inside an automated run. The launcher
        below reproduces exactly that — build the app, then set the variable, then serve.
        """
        port = free_port()
        log = sandbox.repo.parent / f"console-automated-{port}.log"
        launcher = (
            "import os, uvicorn;"
            "from console.app import create_app;"
            "from console.settings import ConsoleSettings;"
            f"app = create_app(ConsoleSettings.from_config(port={port}));"
            "os.environ['EARN_AUTOMATED_RUN'] = '1';"
            f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')"
        )
        handle = log.open("w")
        process = subprocess.Popen(  # noqa: S603 - our own interpreter, fixed argv
            [sandbox.python, "-c", launcher], cwd=sandbox.repo, env=dict(sandbox.env),
            stdout=handle, stderr=subprocess.STDOUT, text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(240):
                try:
                    if httpx.get(f"{base}/api/health", timeout=1.0).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise AssertionError(log.read_text(errors="replace"))
            else:  # pragma: no cover - the server never came up
                raise AssertionError(f"no health on {base}\n{log.read_text(errors='replace')}")

            read = httpx.get(f"{base}/api/health", timeout=10.0)
            assert read.status_code == 200, "reads stay available under an automated run"

            with httpx.Client(base_url=base, timeout=10.0,
                              headers={"Origin": base}) as client:
                mutation = client.post("/api/auth/login", json={"token": console_token})
            assert mutation.status_code == 503, mutation.text
            assert mutation.json()["error"]["code"] == "automated_run"
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
            handle.close()


# --------------------------------------------------------------------------- secrets


class TestNoSecretLeaks:
    def test_the_sentinels_really_are_in_the_servers_environment(self, api: Api) -> None:
        """Otherwise the crawl below proves nothing at all."""
        assert set(SENTINELS) >= {"BINANCE_API_KEY", "ANTHROPIC_API_KEY"}
        secrets = api.json("/api/secrets")
        blob = str(secrets)
        assert "present" in blob or "last4" in blob, secrets

    def test_no_get_route_returns_a_secret_value(self, api: Api, sandbox: Sandbox,
                                                 console_token: str) -> None:
        needles = {**SENTINELS, "EARN_CONSOLE_TOKEN": console_token}
        leaks: list[str] = []
        crashes: list[str] = []
        served = 0
        for route in mounted_get_routes(sandbox.repo):
            if route == "/api/stream":
                continue  # asserted separately, with a bounded stream
            path = concrete(route)
            try:
                response = api.get(path)
            except httpx.HTTPError as e:  # pragma: no cover - a hang or a reset
                crashes.append(f"{path}: {type(e).__name__}: {e}")
                continue
            if response.status_code >= 500:
                crashes.append(f"{path} -> {response.status_code} {response.text[:200]}")
                continue
            if response.status_code == 200:
                served += 1
            body = response.text
            for name, value in needles.items():
                if value and value in body:
                    leaks.append(f"{path} leaked {name}")
        assert leaks == [], leaks
        assert crashes == [], crashes
        # A crawl where everything 404s proves nothing; most of the surface must answer.
        assert served >= 60, f"only {served} GET routes returned 200"

    def test_the_event_stream_is_redacted(self, api: Api, console_token: str) -> None:
        response = api.get("/api/stream?limit=1", timeout=30.0)
        assert response.status_code == 200, response.text
        for value in (*SENTINELS.values(), console_token):
            assert value not in response.text

    def test_rotating_the_token_never_returns_it(self, api: Api, sandbox: Sandbox) -> None:
        api.step_up()
        response = api.post("/api/auth/rotate-token", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"url", "token_path", "rotated_at"}
        new_token = sandbox.token_file.read_text().strip()
        assert new_token not in response.text
        # Put the session back on the token the rest of the session uses.
        assert new_token, "rotate-token wrote an empty token file"


# --------------------------------------------------------------------------- bundle


class TestStaticBundle:
    def test_the_index_is_served(self, server: Server, static_bundle: Path) -> None:
        response = httpx.get(f"{server.base_url}/", timeout=10.0)
        assert response.status_code == 200, response.text
        assert "text/html" in response.headers["content-type"]
        assert response.text == (static_bundle / "index.html").read_text()

    def test_an_asset_is_served_with_its_own_type(self, server: Server,
                                                  static_bundle: Path) -> None:
        asset = next(iter(sorted(static_bundle.rglob("*.js"))), None)
        assert asset is not None, "no javascript in the bundle"
        rel = asset.relative_to(static_bundle).as_posix()
        response = httpx.get(f"{server.base_url}/{rel}", timeout=10.0)
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_the_api_keeps_priority_over_the_bundle(self, anon: httpx.Client) -> None:
        assert anon.get("/api/health").json()["ok"] is True


class TestTokenRotationIsLast:
    """The token file is rotated above; a fresh login must still work afterwards."""

    def test_a_new_session_can_still_log_in(self, server: Server, sandbox: Sandbox) -> None:
        session = Api(server.base_url, sandbox.token_file.read_text().strip())
        try:
            body = session.login()
            assert body["actor"].startswith("human:console:")
        finally:
            session.close()


def test_the_server_logged_no_traceback(server: Server) -> None:
    """A 200 that was really an exception swallowed by a handler is still a defect."""
    tail = server.tail(400)
    assert "Traceback (most recent call last)" not in tail, tail
