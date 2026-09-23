"""The security matrix of spec §5.4, one assertion per control.

The sentence under test: **only a human at this machine, holding the token, in a browser
tab this console served, can change anything.** Everything below is one way that could be
false — a wrong Host, a foreign Origin, a missing CSRF token, a forged cookie, a brute
forced token, a dangerous action without step-up, or an automated Claude run finding the
port — and the assertion that it is not.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import security
from console.app import SECURITY_HEADERS, create_app
from console.cli import main as cli_main
from console.settings import BIND_HOST, ConsoleSettings
from ops.lib import paths

from .conftest import BASE_URL, PORT, step_up

# --------------------------------------------------------------------------- the bind address


def test_bind_host_is_a_code_constant_with_no_way_in():
    """No field, no env var and no config key can move the console off loopback."""
    assert BIND_HOST == "127.0.0.1"
    fields = {f.name for f in dataclasses.fields(ConsoleSettings)}
    assert "host" not in fields and "bind" not in fields
    settings = ConsoleSettings()
    assert settings.host == "127.0.0.1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 9999  # type: ignore[misc]


def test_settings_ignore_a_host_in_the_environment(env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EARN_CONSOLE_HOST", "0.0.0.0")
    monkeypatch.setenv("HOST", "0.0.0.0")
    assert ConsoleSettings.from_config(port=PORT).host == "127.0.0.1"


def test_cli_refuses_any_other_host(env: Path, capsys: pytest.CaptureFixture[str]):
    assert cli_main(["serve", "--host", "0.0.0.0"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err


# --------------------------------------------------------------------------- automated runs


def test_create_app_refuses_under_automated_run(settings: ConsoleSettings,
                                                monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
    with pytest.raises(RuntimeError, match="EARN_AUTOMATED_RUN"):
        create_app(settings, start_pollers=False)


def test_mutating_routes_refuse_once_automated_run_appears(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Defence in depth: the flag can appear after the app was built."""
    monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
    response = auth_client.post("/api/kill", json={"reason": "automated attempt"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "automated_run"
    # reads still work: an automated run may look, never touch
    assert auth_client.get("/api/health").status_code == 200


def test_cli_refuses_under_automated_run(env: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
    assert cli_main(["create-token"]) == 3
    assert "human-only" in capsys.readouterr().err


# --------------------------------------------------------------------------- transport guards


def test_foreign_host_header_is_421(app):  # noqa: ANN001
    with TestClient(app, base_url="http://earn.example.com") as c:
        response = c.get("/api/health")
    assert response.status_code == 421
    assert response.json()["error"]["code"] == "bad_host"


def test_localhost_and_loopback_are_both_allowed(app):  # noqa: ANN001
    for base in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        with TestClient(app, base_url=base) as c:
            assert c.get("/api/health").status_code == 200


def test_security_headers_on_every_response(client: TestClient):
    response = client.get("/api/health")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_no_cors_headers_are_ever_sent(client: TestClient):
    response = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


# --------------------------------------------------------------------------- session & CSRF


def test_protected_route_needs_a_session(client: TestClient):
    response = client.get("/api/meta")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_sets_an_httponly_samesite_strict_cookie(client: TestClient, token: str):
    response = client.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200
    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{security.COOKIE_NAME}=")
    assert "HttpOnly" in raw and "SameSite=strict" in raw.replace("Strict", "strict")
    assert "Path=/" in raw
    assert "Secure" not in raw  # loopback http: a Secure cookie would never be sent
    body = response.json()
    assert body["csrf"] and "token" not in body


def test_login_rejects_a_wrong_token(client: TestClient, token: str):
    response = client.post("/api/auth/login", json={"token": token + "x"})
    assert response.status_code == 401
    assert security.COOKIE_NAME not in response.cookies


def test_mutating_call_needs_an_allowed_origin(auth_client: TestClient):
    response = auth_client.post(
        "/api/kill", json={"reason": "csrf probe"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "bad_origin"


def test_mutating_call_needs_the_csrf_header(client: TestClient, token: str):
    client.post("/api/auth/login", json={"token": token})
    response = client.post("/api/kill", json={"reason": "no csrf"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_mutating_call_rejects_a_stale_csrf_token(auth_client: TestClient):
    auth_client.headers[security.CSRF_HEADER] = "not-the-session-csrf"
    response = auth_client.post("/api/kill", json={"reason": "stale csrf"})
    assert response.status_code == 403


def test_mutating_call_must_be_json(auth_client: TestClient):
    response = auth_client.post(
        "/api/kill", content=b"reason=x", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 415


def test_login_is_the_only_csrf_exempt_route(app):  # noqa: ANN001
    from console.app import CSRF_EXEMPT_PATHS

    assert CSRF_EXEMPT_PATHS == frozenset({"/api/auth/login"})


def test_a_tampered_cookie_is_not_a_session(client: TestClient, token: str):
    response = client.post("/api/auth/login", json={"token": token})
    cookie = response.cookies[security.COOKIE_NAME]
    body, sig = cookie.split(".", 1)
    client.cookies.set(security.COOKIE_NAME, f"{body}.{sig[:-2]}xx")
    assert client.get("/api/meta").status_code == 401


def test_an_expired_session_is_not_a_session(settings: ConsoleSettings):
    signer = security.SessionSigner("unit-test-secret-value")
    session = security.new_session(max_age_s=1, now=time.time() - 10)
    assert signer.loads(signer.dumps(session)) is None


def test_a_session_signed_with_another_key_is_refused():
    session = security.new_session(max_age_s=60)
    raw = security.SessionSigner("key-one").dumps(session)
    assert security.SessionSigner("key-two").loads(raw) is None


# --------------------------------------------------------------------------- rate limiting


def test_five_bad_logins_lock_the_client_out(client: TestClient, token: str):
    for _ in range(security.LOGIN_MAX_FAILURES):
        assert client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    locked = client.post("/api/auth/login", json={"token": "wrong"})
    assert locked.status_code == 429
    assert locked.json()["error"]["detail"]["retry_after_s"] > 0
    # even the correct token is refused while locked out
    assert client.post("/api/auth/login", json={"token": token}).status_code == 429


def test_rate_limiter_windows_and_lockout():
    limiter = security.RateLimiter(max_failures=3, window_s=10, lockout_s=60)
    now = 1000.0
    assert limiter.retry_after("a", now=now) == 0.0
    for i in range(2):
        assert limiter.record_failure("a", now=now + i) == 0.0
    assert limiter.record_failure("a", now=now + 2) == 60
    assert limiter.retry_after("a", now=now + 30) == pytest.approx(32, abs=1)
    assert limiter.retry_after("a", now=now + 200) == 0.0
    limiter.reset("a")
    assert limiter.retry_after("a", now=now) == 0.0


# --------------------------------------------------------------------------- step-up


def test_dangerous_action_needs_step_up(auth_client: TestClient, token: str):
    engaged = auth_client.post("/api/kill", json={"reason": "stop now"})
    assert engaged.status_code == 200, engaged.text  # engaging needs no step-up

    denied = auth_client.request(
        "DELETE", "/api/kill", json={"confirm_phrase": "RESUME TRADING"}
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "step_up_required"

    step_up(auth_client, token)
    allowed = auth_client.request(
        "DELETE", "/api/kill", json={"confirm_phrase": "RESUME TRADING"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["engaged"] is False


def test_step_up_needs_the_real_token(auth_client: TestClient):
    response = auth_client.post("/api/auth/step-up", json={"token": "nope"})
    assert response.status_code == 401


def test_step_up_expires(settings: ConsoleSettings):
    session = security.new_session(max_age_s=3600)
    stepped = session.with_step_up(seconds=600, now=time.time())
    assert stepped.step_up_ok() is True
    stale = session.with_step_up(seconds=600, now=time.time() - 3600)
    assert stale.step_up_ok() is False


def test_typed_phrase_is_required_to_resume(auth_client: TestClient, token: str):
    auth_client.post("/api/kill", json={"reason": "stop"})
    step_up(auth_client, token)
    response = auth_client.request("DELETE", "/api/kill", json={"confirm_phrase": "resume"})
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["expected"] == "RESUME TRADING"


# --------------------------------------------------------------------------- the token store


def test_token_is_stored_as_a_salted_hash_at_0600(settings: ConsoleSettings):
    token, record = security.create_token(
        token_file=settings.token_file, auth_state_file=settings.auth_state_file
    )
    assert oct(settings.token_file.stat().st_mode)[-3:] == "600"
    assert oct(settings.auth_state_file.stat().st_mode)[-3:] == "600"
    stored = settings.auth_state_file.read_text(encoding="utf-8")
    assert token not in stored
    assert record.hash in stored and record.salt in stored
    assert security.verify_token(token, record) is True
    assert security.verify_token(token + "a", record) is False
    assert security.verify_token(token, None) is False


def test_rotation_invalidates_the_old_token(auth_client: TestClient, token: str,
                                            settings: ConsoleSettings):
    step_up(auth_client, token)
    response = auth_client.post("/api/auth/rotate-token", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == f"http://127.0.0.1:{PORT}/"
    assert body["token_path"] == str(settings.token_file)
    assert token not in response.text
    new_token = settings.token_file.read_text(encoding="utf-8").strip()
    assert new_token and new_token != token
    record = security.load_record(settings.auth_state_file)
    assert security.verify_token(token, record) is False
    assert security.verify_token(new_token, record) is True


# --------------------------------------------------------------------------- redaction


def test_redact_blanks_known_shapes_and_env_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BINANCE_SECRET_A", "super-secret-value-123456")
    monkeypatch.setenv("EARN_CONSOLE_TOKEN", "tok-abcdefghijklmnop")
    text = (
        "key=super-secret-value-123456 token=tok-abcdefghijklmnop "
        "anthropic=sk-ant-api03-AAAABBBBCCCCDDDD "
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g "
        f"exchange={'a1b2c3d4' * 8}"
    )
    cleaned = security.redact(text)
    assert "super-secret-value-123456" not in cleaned
    assert "tok-abcdefghijklmnop" not in cleaned
    assert "sk-ant-" not in cleaned
    assert "eyJhbGciOiJIUzI1NiJ9." not in cleaned
    assert "a1b2c3d4a1b2c3d4" not in cleaned
    assert cleaned.count(security.REDACTED) >= 5


def test_redact_response_walks_nested_structures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOME_API_KEY", "leaked-value-abcdef")
    payload = {"a": ["leaked-value-abcdef", {"b": "sk-ant-api03-ZZZZYYYYXXXX"}], "n": 3}
    cleaned = security.redact_response(payload)
    assert cleaned == {"a": [security.REDACTED, {"b": security.REDACTED}], "n": 3}


def test_env_sentinels_skip_paths_and_short_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EARN_CONSOLE_TOKEN_FILE", "/home/me/.config/earn/console-token")
    monkeypatch.setenv("SHORT_SECRET", "abc")
    monkeypatch.setenv("REAL_SECRET", "long-enough-secret")
    sentinels = security.env_sentinels()
    assert "/home/me/.config/earn/console-token" not in sentinels
    assert "abc" not in sentinels
    assert "long-enough-secret" in sentinels


def test_last4_is_all_a_response_may_show():
    assert security.last4("binance-key-ABCD") == "ABCD"
    assert security.last4("ab") == ""


# --------------------------------------------------------------------------- helpers


def test_host_and_origin_helpers():
    allowed = ConsoleSettings(port=PORT).allowed_hosts
    assert security.host_allowed("127.0.0.1:8765", allowed)
    assert security.host_allowed("LOCALHOST:8765", allowed)
    assert not security.host_allowed("earn.example.com", allowed)
    assert not security.host_allowed(None, allowed)

    origins = ConsoleSettings(port=PORT).allowed_origins
    assert security.origin_allowed(BASE_URL, None, origins)
    assert security.origin_allowed(None, f"{BASE_URL}/settings", origins)
    assert not security.origin_allowed("http://evil.example", None, origins)
    assert not security.origin_allowed(None, None, origins)


def test_is_mutating_covers_the_safe_methods():
    assert not any(security.is_mutating(m) for m in ("GET", "head", "OPTIONS"))
    assert all(security.is_mutating(m) for m in ("POST", "put", "PATCH", "DELETE"))
