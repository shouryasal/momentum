"""Fixtures for the console tests: an isolated state root, a token and a logged-in client.

Every test here runs against a temporary ``$EARN_STATE_ROOT`` and a temporary
``$XDG_CONFIG_HOME``, so no test can write the real token file, the real kill file or the
real databases. ``EARN_AUTOMATED_RUN`` is cleared for the same reason: the console refuses
to exist under it, and a stray value in the developer's shell would fail every test with
the wrong message.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from console import security
from console.app import create_app
from console.settings import ConsoleSettings
from ops.lib import paths, signing

SECRET = "console-secret-for-tests-0123456789"
PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated state root, config home and signing secret."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv(signing.SECRET_ENV, SECRET)
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    return state


@pytest.fixture
def settings(env: Path) -> ConsoleSettings:
    return ConsoleSettings.from_config(port=PORT)


@pytest.fixture
def token(settings: ConsoleSettings) -> str:
    value, _record = security.create_token(
        token_file=settings.token_file, auth_state_file=settings.auth_state_file
    )
    return value


@pytest.fixture
def app(settings: ConsoleSettings):  # noqa: ANN201 - FastAPI app
    return create_app(settings, start_pollers=False)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # noqa: ANN001
    """A client whose Host and Origin are the allowed loopback pair."""
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL}) as c:
        yield c


@pytest.fixture
def auth_client(client: TestClient, token: str) -> TestClient:
    """Logged in, with the CSRF header pre-set on every request."""
    response = client.post("/api/auth/login", json={"token": token})
    assert response.status_code == 200, response.text
    client.headers[security.CSRF_HEADER] = response.json()["csrf"]
    return client


#: Reading an unbounded SSE stream through ``TestClient`` is impossible by construction:
#: its transport runs the app to completion before returning a response, so an endless
#: body hangs forever. Tests that need the wire format use ``?limit=N`` (a finite stream)
#: or drive ``console.sse.event_stream`` directly in an asyncio test.


def step_up(client: TestClient, token: str) -> None:
    """Re-authenticate so the next dangerous call is allowed."""
    response = client.post("/api/auth/step-up", json={"token": token})
    assert response.status_code == 200, response.text


class FakeBot:
    """Stands in for ``ops.lib.freqtrade_api.BotApi`` in the kill path."""

    def __init__(self, *, up: bool = True, trades: list[dict] | None = None) -> None:
        self.up = up
        self.trades = trades or []
        self.calls: list[str] = []

    def ping(self) -> bool:
        return self.up

    def stopbuy(self) -> dict:
        self.calls.append("stopbuy")
        if not self.up:
            raise RuntimeError("bot unreachable")
        return {"status": "no more entries will be placed"}

    def status(self) -> list[dict]:
        return list(self.trades)

    def cancel_open_order(self, trade_id: int) -> dict:
        self.calls.append(f"cancel:{trade_id}")
        return {"status": "cancelled"}

    def forceexit(self, tradeid: str = "all") -> dict:
        self.calls.append(f"forceexit:{tradeid}")
        return {"status": "exiting"}
