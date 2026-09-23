"""No endpoint, no model and no event may carry a secret value.

Three complementary checks, because each one alone is easy to fool:

1. **A static scan of every response DTO.** ``console.contracts.RESPONSE_MODELS`` is walked
   for field names that promise a secret value (``token``, ``secret``, ``api_key``, …).
   A new endpoint that returns ``{"api_key": ...}`` fails here before it is ever called.
2. **A live crawl.** Sentinel values are planted in the environment, in the kill reason and
   on the event bus; then every GET route, the OpenAPI document and the SSE stream are
   fetched with a real session and searched for them.
3. **The token store.** Logging in and rotating the token must never echo the token, and
   the stored state must only ever hold its salted hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import security
from console.app import create_app, iter_routes
from console.contracts import RESPONSE_MODELS
from console.settings import ConsoleSettings

from .conftest import BASE_URL, PORT

#: Values planted in the environment before the app is built.
SENTINELS: dict[str, str] = {
    "EARN_CONSOLE_TOKEN": "sentinel-console-token-AAAA1111",
    "EARN_CONSOLE_SECRET": "sentinel-console-secret-BBBB2222",
    "BINANCE_KEY_A": "sentinel-binance-key-CCCC3333",
    "BINANCE_SECRET_A": "sentinel-binance-secret-DDDD4444",
    "ANTHROPIC_API_KEY": "sk-ant-api03-sentinelEEEE5555",
    "TELEGRAM_BOT_TOKEN": "sentinel-telegram-token-FFFF6666",
}

#: Field names that would promise a secret value in a response.
FORBIDDEN_FIELD_PARTS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "token",
)
#: …except these, which are a path, a double-submit nonce, a deadline or a presence flag.
#: ``credential_present`` is the shape spec §5.4 *demands*: a provider card says whether a
#: credential exists and never what it is, exactly as ``SecretRow`` reports ``present`` and
#: ``last4``. The live crawl below still greps the real responses for the sentinels, so the
#: exemption narrows the name heuristic, not the guarantee.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"token_path", "csrf", "step_up_until", "credential_present"}
)


def test_no_response_model_promises_a_secret():
    """Every DTO the app **serves**, not only the ones listed in ``RESPONSE_MODELS``.

    Three packages declare their response models in their own router modules rather than
    editing ``console/contracts.py``, which the hand-kept tuple never saw. Walking the
    mounted routes covers a model because it is served.
    """
    from console.contracts import mounted_response_models

    app = create_app(start_pollers=False)
    models = {id(m): m for m in (*RESPONSE_MODELS, *mounted_response_models(app))}.values()
    offenders: list[str] = []
    for model in models:
        for name, field in model.model_fields.items():
            if name in ALLOWED_FIELDS or _is_container(field.annotation):
                continue
            low = name.lower()
            if any(part in low for part in FORBIDDEN_FIELD_PARTS):
                offenders.append(f"{model.__name__}.{name}")
    assert offenders == [], f"response models must not carry secret values: {offenders}"


def _is_container(annotation: object) -> bool:
    """Is this field a nested DTO (or a list of them) rather than a scalar value?

    ``SecretList.secrets: list[SecretRow]`` is an inventory, not a secret — the name
    describes what the rows are *about*. The rows themselves are scanned in their own
    right (``SecretRow`` carries ``present`` and ``last4`` and nothing else), so the
    heuristic only has to apply where a value could actually live: a scalar.
    """
    from typing import get_args

    from pydantic import BaseModel

    def named(node: object) -> bool:
        if isinstance(node, type) and issubclass(node, BaseModel):
            return True
        return any(named(arg) for arg in get_args(node))

    return named(annotation)


def test_secrets_style_dtos_would_report_presence_only():
    """The shape P7's /secrets endpoint must use is already impossible to get wrong here."""
    from console.contracts import _Dto  # noqa: PLC2701 - the shared base is the contract

    assert _Dto.model_config["extra"] == "forbid"


@pytest.fixture
def seeded(env: Path, monkeypatch: pytest.MonkeyPatch) -> ConsoleSettings:
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    return ConsoleSettings.from_config(port=PORT)


def _get_routes(app) -> list[str]:  # noqa: ANN001
    """Every GET route with no path parameters — what a crawler can actually call.

    ``iter_routes`` walks the included routers too, so this covers every package's
    endpoints, not only F0's.
    """
    return sorted(
        path
        for methods, path in iter_routes(app)
        if "GET" in methods and "{" not in path and path.startswith("/api")
    )


def _assert_clean(text: str, where: str) -> None:
    for name, value in SENTINELS.items():
        assert value not in text, f"{where} leaked {name}"
    assert "sk-ant-" not in text, f"{where} leaked an Anthropic key shape"


def test_every_get_route_is_free_of_secrets(seeded: ConsoleSettings, env: Path):
    """Crawls every package's GET routes, not only F0's — a leak anywhere fails here.

    ``raise_server_exceptions=False`` so that another package's bug shows up as a 500 whose
    body is still scanned, instead of aborting the scan.
    """
    from ops import db
    from ops.config import load_config

    db.init_all(load_config(), root=env)
    token, _ = security.create_token(
        token_file=seeded.token_file, auth_state_file=seeded.auth_state_file
    )
    app = create_app(seeded, start_pollers=False)
    with TestClient(
        app, base_url=BASE_URL, headers={"Origin": BASE_URL}, raise_server_exceptions=False
    ) as client:
        login = client.post("/api/auth/login", json={"token": token})
        assert login.status_code == 200
        client.headers[security.CSRF_HEADER] = login.json()["csrf"]
        _assert_clean(login.text, "POST /api/auth/login")

        # plant a secret where a careless implementation would echo it back
        engaged = client.post(
            "/api/kill",
            json={"reason": f"leak probe {SENTINELS['BINANCE_KEY_A']}"},
        )
        assert engaged.status_code == 200
        _assert_clean(engaged.text, "POST /api/kill")

        paths = _get_routes(app)
        assert "/api/health" in paths and "/api/meta" in paths and "/api/kill" in paths
        for path in paths:
            if path.endswith("/stream"):
                continue  # covered by test_the_event_stream_is_redacted (it never ends)
            response = client.get(path)
            assert response.status_code != 401, f"{path} answered without a session"
            _assert_clean(response.text, f"GET {path}")
        for own in ("/api/health", "/api/meta", "/api/kill", "/api/auth/me"):
            assert client.get(own).status_code == 200, own

        openapi = client.get("/api/openapi.json")
        assert openapi.status_code == 200
        _assert_clean(openapi.text, "openapi.json")


def test_the_event_stream_is_redacted(seeded: ConsoleSettings):
    """A payload carrying a secret is scrubbed on publish, before any client sees it."""
    token, _ = security.create_token(
        token_file=seeded.token_file, auth_state_file=seeded.auth_state_file
    )
    app = create_app(seeded, start_pollers=False)
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}) as client:
        login = client.post("/api/auth/login", json={"token": token})
        assert login.status_code == 200
        event = app.state.bus.publish(
            "alert", {"message": f"boom {SENTINELS['BINANCE_SECRET_A']}"}
        )
        assert SENTINELS["BINANCE_SECRET_A"] not in json.dumps(event.payload)
        response = client.get(
            "/api/stream",
            params={"topics": "alert", "limit": 1, "last_event_id": int(event.id) - 1},
        )
        assert response.status_code == 200
    _assert_clean(response.text, "GET /api/stream")
    assert security.REDACTED in response.text


def test_login_and_rotation_never_echo_the_token(seeded: ConsoleSettings):
    token, _ = security.create_token(
        token_file=seeded.token_file, auth_state_file=seeded.auth_state_file
    )
    app = create_app(seeded, start_pollers=False)
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}) as client:
        login = client.post("/api/auth/login", json={"token": token})
        client.headers[security.CSRF_HEADER] = login.json()["csrf"]
        assert token not in login.text
        client.post("/api/auth/step-up", json={"token": token})
        rotated = client.post("/api/auth/rotate-token", json={})
        assert rotated.status_code == 200
        new_token = seeded.token_file.read_text(encoding="utf-8").strip()
        assert token not in rotated.text and new_token not in rotated.text
        record = json.loads(seeded.auth_state_file.read_text(encoding="utf-8"))
        assert new_token not in json.dumps(record)
        assert set(record) == {"version", "algo", "salt", "hash", "created_at", "rotated_at"}


def test_markdown_and_plain_bodies_are_redacted_too(monkeypatch: pytest.MonkeyPatch):
    """The middleware is the backstop for the routes the crawl above cannot reach.

    ``GET /api/knowledge/briefs/{date}``, ``/api/reports/{path}`` and
    ``/api/runs/{run_id}/trace`` hand back ``text/markdown`` read straight off disk, and
    every one of them takes a path parameter, so :func:`_get_routes` skips them. If the
    middleware only buffered JSON, a brief that quoted a key would go out verbatim.
    ``text/event-stream`` must still stream: it is redacted on publish instead.
    """
    from fastapi import FastAPI, Response
    from fastapi.responses import PlainTextResponse, StreamingResponse

    from console.app import REDACTED_MEDIA_TYPES, RedactionMiddleware

    secret = "sentinel-binance-key-CCCC3333"
    monkeypatch.setenv("BINANCE_KEY_A", secret)

    app = FastAPI()
    app.add_middleware(RedactionMiddleware)

    @app.get("/md")
    def md() -> PlainTextResponse:
        return PlainTextResponse(f"# brief\n\nkey {secret}\n", media_type="text/markdown")

    @app.get("/txt")
    def txt() -> PlainTextResponse:
        return PlainTextResponse(f"log line {secret}")

    @app.get("/bin")
    def binary() -> Response:
        return Response(content=b"\x00\x01binary", media_type="application/octet-stream")

    @app.get("/sse")
    def sse() -> StreamingResponse:
        return StreamingResponse(iter([b"data: hi\n\n"]), media_type="text/event-stream")

    assert "text/markdown" in REDACTED_MEDIA_TYPES and "text/plain" in REDACTED_MEDIA_TYPES
    assert "text/event-stream" not in REDACTED_MEDIA_TYPES

    with TestClient(app) as client:
        body = client.get("/md").text
        assert secret not in body and "# brief" in body
        assert secret not in client.get("/txt").text
        assert client.get("/bin").content == b"\x00\x01binary"
        assert client.get("/sse").text == "data: hi\n\n"
