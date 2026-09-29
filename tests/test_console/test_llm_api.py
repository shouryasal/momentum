"""``/api/llm``: provider cards, Ollama detection, the routing matrix, usage, playground.

Nothing here touches a network or the Claude SDK: the Ollama probes go through an injected
``httpx.MockTransport`` and the playground runs against a registered stub provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from console.services import llm_service
from ops import db
from ops.config import load_config
from ops.lib import claude_auth
from runs.llm import health as health_mod
from runs.llm.stub import StubProvider, scripted
from runs.llm.types import ProviderCaps

OLLAMA = "http://127.0.0.1:11434"

#: Every seeded ``llm_calls`` row is stamped here, and ``pinned_clock`` puts the service's
#: clock in the same month. ``month_totals`` buckets spend by the CURRENT calendar month,
#: so without the pin this file asserted "1.5 spent in September" against whatever month
#: the machine happened to be in — green in September, a KeyError from October onwards.
NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
def journal(env: Path) -> Path:
    journal_path, _ = db.init_all(load_config(), root=env)
    return journal_path


@pytest.fixture
def knowledge(env: Path) -> Path:
    _, knowledge_path = db.init_all(load_config(), root=env)
    return knowledge_path


@pytest.fixture
def ollama_client(app):  # noqa: ANN001
    """A mock transport that answers as the local daemon on 127.0.0.1 only."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not url.startswith(OLLAMA):
            raise httpx.ConnectError("connection refused", request=request)
        if url.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.34.1"})
        if url.endswith("/api/tags"):
            # Two pulled models: the one models.yaml declares as local_small (granite, since
            # 2026-09-29) and one it does not, so the "declared" flag is tested both ways.
            return httpx.Response(200, json={"models": [
                {"name": "granite4.2:3b", "size": 2920000000,
                 "details": {"parameter_size": "3B", "quantization_level": "Q4_K_M",
                             "family": "granite"}},
                {"name": "llama3.1:8b", "size": 4661224676,
                 "details": {"parameter_size": "8.0B", "quantization_level": "Q4_K_M",
                             "family": "llama"}}]})
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    app.state.http_client = client
    yield client
    client.close()


# --------------------------------------------------------------------------- providers


def test_provider_cards_cover_every_provider_key(auth_client: TestClient, journal: Path):
    body = auth_client.get("/api/llm/providers").json()
    keys = [card["key"] for card in body["providers"]]
    assert keys == ["claude:subscription", "claude:api_key", "ollama"]
    assert body["auth_mode"] in ("subscription", "api_key", "auto")
    assert set(body["month"]) == {"month", "total_usd", "by_provider"}
    assert set(body["rate_limit"]) == {"status", "utilization", "resets_at"}


def test_the_token_counts_as_present_even_though_the_console_cannot_see_it(
        auth_client: TestClient, app, env: Path, journal: Path,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch):
    """A configured token must read as present, and it is never in the console's own env.

    This is the bug the owner reported as "it says claude not connected": credentials are
    handed to model jobs by ``ops/envwrap.sh`` and the console is in no allowlist, so
    checking ``os.environ`` answered "no credential" on a system where every job
    authenticated fine — and where the Secrets page, which reads ``.env``, said "signed
    in" on the same screen refresh.
    """
    monkeypatch.delenv(claude_auth.OAUTH_VAR, raising=False)
    env_file = env / ".env"
    env_file.write_text(f"{claude_auth.OAUTH_VAR}=sk-ant-oat01-present1234\n")
    app.state.env_file = env_file

    cards = {c["key"]: c for c in auth_client.get("/api/llm/providers").json()["providers"]}
    assert cards["claude:subscription"]["credential_present"] is True
    assert cards["claude:api_key"]["credential_present"] is False   # absent from that .env


def test_a_reachable_local_model_is_reported_with_no_cache_to_read(
        auth_client: TestClient, ollama_client, journal: Path, knowledge: Path):  # noqa: ANN001
    """An empty cache must mean "ask", not "not detected".

    The detected-URL cache lives for ten minutes and only a model job ever refills it, so
    ten quiet minutes were enough for this page to report a local model that was up and
    answering as absent — the same mistake as judging Claude's credential by the console's
    own environment.
    """
    with db.opened(knowledge) as conn:                       # nothing has probed yet
        conn.execute("DELETE FROM ops_state WHERE key LIKE 'ollama_%'")
        conn.commit()
    cards = {c["key"]: c for c in auth_client.get("/api/llm/providers").json()["providers"]}
    assert cards["ollama"]["credential_present"] is True, "a running local model read as absent"
    assert cards["ollama"]["base_url"] == OLLAMA


def test_only_the_active_credential_is_enabled(auth_client: TestClient, journal: Path):
    cards = {c["key"]: c for c in auth_client.get("/api/llm/providers").json()["providers"]}
    assert cards["claude:subscription"]["enabled"] is True      # models.yaml: subscription
    assert cards["claude:api_key"]["enabled"] is False
    assert "monthly cap" in cards["claude:api_key"]["detail"]


def test_an_open_breaker_is_reported_with_its_error(auth_client: TestClient,
                                                    journal: Path):
    with db.opened(journal) as conn:
        for _ in range(3):
            health_mod.record_failure(conn, "ollama", "connection refused")
    cards = {c["key"]: c for c in auth_client.get("/api/llm/providers").json()["providers"]}
    assert cards["ollama"]["circuit"] == "open"
    assert cards["ollama"]["consecutive_failures"] == 3
    assert cards["ollama"]["last_error"] == "connection refused"


def test_resetting_a_circuit_closes_it_and_is_audited(auth_client: TestClient,
                                                      journal: Path):
    with db.opened(journal) as conn:
        for _ in range(3):
            health_mod.record_failure(conn, "ollama", "boom")
    response = auth_client.post("/api/llm/providers/ollama/circuit/reset")
    assert response.status_code == 200
    assert response.json()["state"] == "closed"
    with db.opened(journal, readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action='llm.circuit.reset'")]
    assert rows and rows[-1]["result"] == "ok" and rows[-1]["target"] == "ollama"


def test_an_unknown_provider_key_is_a_404(auth_client: TestClient, journal: Path):
    assert auth_client.post(
        "/api/llm/providers/nonsense/circuit/reset").status_code == 404


def test_providers_needs_a_session(client: TestClient):
    assert client.get("/api/llm/providers").status_code == 401


# --------------------------------------------------------------------------- ollama


def test_detection_returns_the_probe_table_and_the_wsl_guidance(
    auth_client: TestClient, ollama_client, knowledge: Path
):
    body = auth_client.get("/api/llm/ollama/detect").json()
    assert body["ok"] and body["base_url"] == OLLAMA and body["version"] == "0.34.1"
    guidance = body["guidance"]
    flat = " ".join(c for option in guidance["options"] for c in option["commands"])
    assert "OLLAMA_HOST" in flat and "networkingMode=mirrored" in flat


def test_detection_caches_the_url_for_the_cron_jobs(auth_client: TestClient,
                                                    ollama_client, knowledge: Path):
    auth_client.get("/api/llm/ollama/detect")
    with db.opened(knowledge, readonly=True) as conn:
        assert health_mod.cached_ollama_url(conn) == OLLAMA


def test_the_model_list_flags_what_models_yaml_declares(auth_client: TestClient,
                                                        ollama_client, knowledge: Path):
    auth_client.get("/api/llm/ollama/detect")            # seed the cache
    body = auth_client.get("/api/llm/ollama/models").json()
    by_name = {m["name"]: m for m in body["models"]}
    assert by_name["granite4.2:3b"]["declared_in_models_yaml"] is True
    assert by_name["granite4.2:3b"]["parameter_size"] == "3B"
    assert by_name["llama3.1:8b"]["declared_in_models_yaml"] is False, \
        "a pulled model nothing routes to must not read as declared"


def test_an_unreachable_daemon_is_a_503_with_the_guidance(auth_client: TestClient,
                                                          app, knowledge: Path):
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app.state.http_client = httpx.Client(transport=httpx.MockTransport(dead))
    detect = auth_client.get("/api/llm/ollama/detect").json()
    assert detect["ok"] is False
    assert detect["guidance"]["reachable"] is False
    assert auth_client.get("/api/llm/ollama/models").status_code == 503


# --------------------------------------------------------------------------- routing


def test_the_routing_matrix_shows_the_code_floors_as_facts(auth_client: TestClient):
    rows = {row["task"]: row for row in auth_client.get("/api/llm/routing").json()["tasks"]}
    assert rows["decide"]["code_min_tier"] == 4
    assert rows["decide"]["local_forbidden"] is True
    assert rows["validate"]["code_min_tier"] == 3
    assert rows["scan"]["code_min_tier"] == 1
    # a config that tried to lower the floor still reports the effective one
    assert rows["decide"]["effective_min_tier"] == 4


def test_the_matrix_carries_the_whole_chain_with_provider_and_tier(
    auth_client: TestClient
):
    rows = {row["task"]: row for row in auth_client.get("/api/llm/routing").json()["tasks"]}
    chain = rows["brief"]["chain"]
    assert [entry["alias"] for entry in chain] == ["sonnet", "haiku", "local_small"]
    assert chain[-1]["local"] is True and chain[-1]["id"] == "granite4.2:3b"
    assert rows["brief"]["local_mode"] == "context_pack"
    assert rows["decide"]["escalation"]["alias"] == "fable"


def test_the_matrix_reports_overlay_provenance(auth_client: TestClient, env: Path,
                                               monkeypatch):
    body = auth_client.get("/api/llm/routing").json()
    assert all(row["overlay_head"] is False for row in body["tasks"])
    assert body["switching"]["max_attempts_per_call"] == 4
    assert body["budget"]["mode"] == "telemetry"


# --------------------------------------------------------------------------- usage


@pytest.fixture
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin the service clock to ``NOW`` — the month the seeded rows belong to."""
    monkeypatch.setattr(llm_service, "_now", lambda: NOW)
    return NOW


def _call(conn, **kwargs):
    row = {"ts_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "task": "decide",
           "provider": "claude:subscription", "model": "claude-opus-5",
           "auth_source": "subscription", "attempt": 0, "status": "ok",
           "latency_ms": 1200, "input_tokens": 100, "output_tokens": 50,
           "cost_usd": 1.5, **kwargs}
    conn.execute(
        "INSERT INTO llm_calls(ts_utc, task, provider, model, auth_source, attempt,"
        " status, latency_ms, input_tokens, output_tokens, cost_usd)"
        " VALUES (:ts_utc,:task,:provider,:model,:auth_source,:attempt,:status,"
        ":latency_ms,:input_tokens,:output_tokens,:cost_usd)", row)
    conn.commit()


def test_usage_groups_and_reports_a_success_rate(auth_client: TestClient, journal: Path,
                                                 pinned_clock: datetime):
    with db.opened(journal) as conn:
        _call(conn)
        _call(conn, status="rate_limited", cost_usd=0.0)
        _call(conn, task="scan", provider="ollama", model="llama3.1:8b",
              auth_source="local", cost_usd=0.0)
    body = auth_client.get("/api/llm/usage?group=task").json()
    rows = {row["bucket"]: row for row in body["rows"]}
    assert rows["decide"]["calls"] == 2 and rows["decide"]["success_rate"] == 0.5
    assert rows["decide"]["cost_usd"] == 1.5
    assert rows["scan"]["success_rate"] == 1.0
    assert body["month"]["by_provider"]["claude:subscription"] == 1.5


def test_the_month_panel_follows_the_service_clock(auth_client: TestClient, journal: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    """Two clocks, two answers — so the assertion cannot quietly depend on the calendar.

    ``month_totals`` buckets by the current month and takes its clock from
    ``llm_service._now``. Pinned inside the seeded month the spend shows; pinned one month
    on, the same rows are last month's and the panel is empty. Both are asserted here, so
    neither September nor any later month can make this test mean something else.
    """
    with db.opened(journal) as conn:
        _call(conn)

    monkeypatch.setattr(llm_service, "_now", lambda: NOW)
    body = auth_client.get("/api/llm/usage?group=task").json()
    assert body["month"]["month"] == "2026-09"
    assert body["month"]["by_provider"]["claude:subscription"] == 1.5

    monkeypatch.setattr(llm_service, "_now", lambda: NOW.replace(month=10))
    later = auth_client.get("/api/llm/usage?group=task").json()
    assert later["month"]["month"] == "2026-10"
    assert later["month"]["by_provider"] == {} and later["month"]["total_usd"] == 0.0


@pytest.mark.parametrize("group", ["task", "model", "provider", "auth", "day"])
def test_every_documented_grouping_works(auth_client: TestClient, journal: Path, group):
    with db.opened(journal) as conn:
        _call(conn)
    body = auth_client.get(f"/api/llm/usage?group={group}").json()
    assert body["group"] == group and len(body["rows"]) == 1


def test_an_unknown_grouping_is_rejected(auth_client: TestClient, journal: Path):
    assert auth_client.get("/api/llm/usage?group=nonsense").status_code == 422


def test_the_switch_log_is_exposed(auth_client: TestClient, journal: Path):
    with db.opened(journal) as conn:
        conn.execute(
            "INSERT INTO provider_switches(ts_utc, task, from_provider, from_model,"
            " to_provider, to_model, reason) VALUES (?,?,?,?,?,?,?)",
            ("2026-09-22T08:00:00Z", "decide", "claude:subscription", "opus",
             "claude:api_key", "opus", "rate_limited"))
        conn.commit()
    rows = auth_client.get("/api/llm/switches").json()["switches"]
    assert rows[0]["reason"] == "rate_limited" and rows[0]["to_model"] == "opus"
    assert auth_client.get("/api/llm/switches?task=scan").json()["switches"] == []


# --------------------------------------------------------------------------- playground


@pytest.fixture
def stub_provider(monkeypatch):
    """Register a stub under both Claude keys so the playground never needs the SDK."""
    from runs.llm import chain as chain_mod

    stubs = {
        "claude:subscription": StubProvider(key="claude:subscription",
                                            caps=ProviderCaps(),
                                            responses=[scripted('{"answer": 42}',
                                                                 cost_usd=0.01)]),
        "claude:api_key": StubProvider(key="claude:api_key", caps=ProviderCaps()),
    }
    original = chain_mod.run_task

    def patched(*args, **kwargs):
        kwargs.setdefault("providers", stubs)
        return original(*args, **kwargs)

    monkeypatch.setattr(chain_mod, "run_task", patched)
    return stubs


def test_the_playground_runs_one_call_and_reports_the_attempts(
    auth_client: TestClient, journal: Path, knowledge: Path, stub_provider
):
    response = auth_client.post("/api/llm/playground",
                                json={"task": "decide", "prompt": "what is 6*7?"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["text"] == '{"answer": 42}'
    assert body["served"] == "opus" and body["attempts"][0]["status"] == "ok"
    assert body["max_usd_per_run"] == 4.0


def test_the_playground_never_runs_tools(auth_client: TestClient, journal: Path,
                                         knowledge: Path, stub_provider):
    auth_client.post("/api/llm/playground",
                     json={"task": "decide", "prompt": "hello"})
    request = stub_provider["claude:subscription"].requests[0]
    assert request.tools_profile == "none" and request.skills is None


def test_the_playground_writes_llm_calls_but_no_runs_row(
    auth_client: TestClient, journal: Path, knowledge: Path, stub_provider
):
    auth_client.post("/api/llm/playground", json={"task": "decide", "prompt": "hi"})
    with db.opened(journal, readonly=True) as conn:
        calls = [dict(r) for r in conn.execute("SELECT * FROM llm_calls")]
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs")]
    assert len(calls) == 1 and calls[0]["run_ref"] == "playground"
    assert runs == []            # a playground call is not a journalled research run


def test_an_unknown_task_or_alias_is_refused(auth_client: TestClient, journal: Path,
                                             knowledge: Path, stub_provider):
    assert auth_client.post("/api/llm/playground",
                            json={"task": "nope", "prompt": "x"}).status_code == 404
    assert auth_client.post(
        "/api/llm/playground",
        json={"task": "decide", "prompt": "x", "model_ref": "ghost"}).status_code == 400


def test_pinning_a_model_overrides_the_chain(auth_client: TestClient, journal: Path,
                                             knowledge: Path, stub_provider):
    body = auth_client.post(
        "/api/llm/playground",
        json={"task": "review", "prompt": "x", "model_ref": "opus"}).json()
    assert body["served"] == "opus"


# --------------------------------------------------------------------------- service


def test_the_service_needs_no_fastapi(journal: Path):
    """llm_service is plain functions over paths — P8 and the CLI reuse it directly."""
    from ops.models_config import load_models_cfg

    mc = load_models_cfg(overlay=None)
    cards = llm_service.provider_cards(mc, journal=journal)
    assert [c["key"] for c in cards][:2] == ["claude:subscription", "claude:api_key"]
    assert llm_service.usage(journal, group="task") == []
    assert llm_service.switches(journal) == []
