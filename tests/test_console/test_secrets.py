"""``/api/secrets``: write-only, step-up-protected, and it never echoes a value.

Every test runs against an injected ``.env`` under the temporary state root, so nothing
here can read or rewrite the real credential file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console.services import credential_tests
from ops import db
from ops.config import load_config
from ops.lib import claude_auth, envfile

from .conftest import step_up

SECRET_VALUE = "sk-ant-api03-supersecretvalue7890"


@pytest.fixture
def env_file(app, env: Path) -> Path:  # noqa: ANN001
    """An isolated ``.env`` with one existing entry, wired into the app."""
    path = env / ".env"
    path.write_text("# comment kept\nANTHROPIC_API_KEY=sk-ant-existing1234\n")
    app.state.env_file = path
    return path


@pytest.fixture
def models_file(app, env: Path) -> Path:  # noqa: ANN001
    """A copy of the committed models.yaml the auth-mode write may edit."""
    from ops.models_config import DEFAULT_MODELS_CONFIG

    path = env / "models.yaml"
    path.write_text(DEFAULT_MODELS_CONFIG.read_text())
    app.state.models_path = path
    return path


@pytest.fixture
def journal(env: Path) -> Path:
    journal_path, _ = db.init_all(load_config(), root=env)
    return journal_path


def audit_rows(journal: Path, action: str) -> list[dict]:
    with db.opened(journal, readonly=True) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


# --------------------------------------------------------------------------- reads


def test_listing_reports_presence_and_last4_only(auth_client: TestClient, env_file: Path):
    body = auth_client.get("/api/secrets").json()
    rows = {row["name"]: row for row in body["secrets"]}
    assert rows["ANTHROPIC_API_KEY"]["present"] is True
    assert rows["ANTHROPIC_API_KEY"]["last4"] == "1234"
    assert rows["BINANCE_KEY_A"]["present"] is False
    assert "sk-ant-existing1234" not in auth_client.get("/api/secrets").text


def test_every_row_says_which_jobs_use_it(auth_client: TestClient, env_file: Path):
    rows = {row["name"]: row for row in auth_client.get("/api/secrets").json()["secrets"]}
    assert "reconcile" in rows["BINANCE_KEY_A"]["used_by"]
    assert "research" in rows["CLAUDE_CODE_OAUTH_TOKEN"]["used_by"]
    assert rows["CLAUDE_CODE_OAUTH_TOKEN"]["required"] is True


def test_there_is_no_route_that_returns_a_value(auth_client: TestClient, env_file: Path):
    """The named-secret route is write-only: there is no GET that could return one."""
    schema = auth_client.get("/api/openapi.json").json()
    secret_paths = {p: set(ops) for p, ops in schema["paths"].items() if "/secrets" in p}
    assert secret_paths, "the secrets router must be mounted"
    named = [p for p in secret_paths if p.endswith("{name}")]
    assert named, f"no named-secret route in {sorted(secret_paths)}"
    for path in named:
        assert secret_paths[path] == {"put", "delete"}, path
    # the only readable secrets route is the list, which carries presence and last4
    readable = [p for p, ops in secret_paths.items() if "get" in ops]
    assert all(p.endswith("/secrets") for p in readable), readable


def test_a_session_is_required(client: TestClient, env_file: Path):
    assert client.get("/api/secrets").status_code == 401


# --------------------------------------------------------------------------- writes


def test_writing_needs_step_up(auth_client: TestClient, env_file: Path):
    response = auth_client.put("/api/secrets/BINANCE_KEY_A", json={"value": "abcd1234"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "step_up_required"
    assert not envfile.info("BINANCE_KEY_A", path=env_file).present


def test_a_round_trip_never_echoes_the_value(auth_client: TestClient, token: str,
                                             env_file: Path, journal: Path):
    step_up(auth_client, token)
    response = auth_client.put("/api/secrets/ANTHROPIC_API_KEY",
                               json={"value": SECRET_VALUE})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "ANTHROPIC_API_KEY", "present": True,
                               "last4": "7890",
                               "updated_at": response.json()["updated_at"]}
    assert SECRET_VALUE not in response.text
    # it really landed, comments intact, and the audit row carries no value either
    assert envfile.value_of("ANTHROPIC_API_KEY", path=env_file) == SECRET_VALUE
    assert "# comment kept" in env_file.read_text()
    rows = audit_rows(journal, "secret.set")
    assert rows and SECRET_VALUE not in str(rows[-1])
    assert '"last4": "7890"' in rows[-1]["detail_json"]


def test_deleting_removes_the_entry(auth_client: TestClient, token: str, env_file: Path,
                                   journal: Path):
    step_up(auth_client, token)
    response = auth_client.delete("/api/secrets/ANTHROPIC_API_KEY")
    assert response.status_code == 200
    assert response.json()["present"] is False
    assert not envfile.info("ANTHROPIC_API_KEY", path=env_file).present
    assert audit_rows(journal, "secret.delete")


def test_a_name_outside_the_catalogue_is_refused(auth_client: TestClient, token: str,
                                                 env_file: Path):
    step_up(auth_client, token)
    response = auth_client.put("/api/secrets/LD_PRELOAD", json={"value": "/tmp/evil.so"})
    assert response.status_code == 404
    assert "LD_PRELOAD" not in env_file.read_text()


def test_an_empty_value_is_refused(auth_client: TestClient, token: str, env_file: Path):
    step_up(auth_client, token)
    assert auth_client.put("/api/secrets/BINANCE_KEY_A",
                           json={"value": "  "}).status_code == 400


# --------------------------------------------------------------------------- auth mode


def test_auth_mode_moves_models_yaml_and_dotenv_together(
    auth_client: TestClient, token: str, env_file: Path, models_file: Path, journal: Path
):
    step_up(auth_client, token)
    response = auth_client.put("/api/secrets/auth-mode",
                               json={"mode": "auto", "reason": "rate limits at 16:00"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] == "auto" and body["env"] == "auto"
    assert body["effective"] == "auto" and body["in_sync"] is True
    # models.yaml changed surgically: the migration note above it survives
    text = models_file.read_text()
    assert "claude_mode: auto" in text
    assert "which Claude credential a job gets" in text
    assert envfile.value_of(claude_auth.AUTH_MODE_VAR, path=env_file) == "auto"
    # and it is a config_audit row, not just an audit_log row
    with db.opened(journal, readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM config_audit WHERE file='config/models.yaml'")]
    assert rows and rows[-1]["reason"] == "rate limits at 16:00"
    assert "auth.claude_mode" in rows[-1]["changed_paths_json"]


def test_auth_mode_needs_step_up(auth_client: TestClient, env_file: Path,
                                 models_file: Path):
    assert auth_client.put("/api/secrets/auth-mode",
                           json={"mode": "api_key"}).status_code == 403
    assert "claude_mode: subscription" in models_file.read_text()


def test_an_unknown_auth_mode_is_rejected_by_the_dto(auth_client: TestClient, token: str,
                                                     env_file: Path, models_file: Path):
    step_up(auth_client, token)
    assert auth_client.put("/api/secrets/auth-mode",
                           json={"mode": "whatever"}).status_code == 422


def test_the_listing_reports_the_login_session_state(auth_client: TestClient,
                                                     env_file: Path, models_file: Path,
                                                     monkeypatch):
    auth = auth_client.get("/api/secrets").json()["auth"]
    assert set(auth["login_session"]) == {"present", "expires_at", "expired",
                                          "subscription_type"}
    assert auth["subscription_source"] in ("token", "login")


# --------------------------------------------------------------------------- probes


def test_every_documented_target_exists(auth_client: TestClient):
    assert set(credential_tests.TARGETS) == {
        "claude_subscription", "claude_login", "claude_api_key", "ollama", "telegram",
        "binance_a", "binance_b", "freqtrade_a", "freqtrade_b"}


def test_an_unknown_target_is_a_404(auth_client: TestClient):
    response = auth_client.post("/api/secrets/test/nonsense")
    assert response.status_code == 404
    assert "claude_subscription" in response.text


def test_a_missing_credential_is_a_clean_failure_not_an_exception(
    auth_client: TestClient, env_file: Path
):
    for target in ("claude_subscription", "telegram", "binance_a"):
        body = auth_client.post(f"/api/secrets/test/{target}").json()
        assert body["ok"] is False and body["target"] == target
        assert "not set" in body["detail"], target


def test_the_probe_verdict_is_redacted(auth_client: TestClient, env_file: Path,
                                       monkeypatch):
    """A probe that manages to quote a key still cannot leak it."""
    envfile.set_value("TELEGRAM_BOT_TOKEN", "123456:AAbbCCddEEffGG", path=env_file)
    envfile.set_value("TELEGRAM_CHAT_ID", "42", path=env_file)

    def boom(request, **kwargs):  # noqa: ANN001
        raise RuntimeError("upstream said 123456:AAbbCCddEEffGG is invalid")

    class Client:
        def get(self, url, **kwargs):  # noqa: ANN001
            return boom(url, **kwargs)

    auth_client.app.state.http_client = Client()
    body = auth_client.post("/api/secrets/test/telegram").json()
    assert body["ok"] is False
    assert "AAbbCCddEEffGG" not in body["detail"]


def test_the_claude_login_probe_reads_the_credentials_file(monkeypatch, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 99999999999999,'
        ' "subscriptionType": "max"}}')
    result = credential_tests.run_test("claude_login", home=tmp_path)
    assert result.ok and "login session valid" in result.detail
    assert "tok" not in result.detail
