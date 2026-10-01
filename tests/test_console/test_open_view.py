"""`console.open_view` — the dashboard opens without a token, and nothing else does.

Asked for on 2026-10-01 ("remove token requirement to sign in for now so i can easily view
dashboard"). Implemented as a READ-ONLY bypass rather than removing auth, because
`console/security.py`'s threat model names three attackers and only one of them is actually
cheap to accept:

* a malicious **web page** gains little — there is no CORS middleware, so it cannot read a
  cross-origin response, and it still cannot mutate;
* an **automated Claude run** is refused the loopback console by the tier-2 hook regardless;
* another **local process** is the real exposure, and on a single-user laptop that is the
  deliberate trade being made.

So the properties that must hold, and are asserted here:

* a loopback GET with no cookie gets 200 instead of 401, and carries `X-Earn-Open-View: 1`;
* every mutation still 401s — the bypass only answers GET/HEAD/OPTIONS;
* step-up can NEVER be satisfied, so arming a mode, blessing config and flattening the book
  still demand the real token;
* the CSRF token is empty, so the double-submit check cannot be satisfied either — belt and
  braces if the method guard were ever loosened;
* with the flag OFF, nothing changes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from console import deps

from .conftest import BASE_URL


@pytest.fixture
def loopback(app):  # noqa: ANN001, ANN201 — FastAPI app from the shared fixture
    """A client whose PEER ADDRESS is loopback, which the shared ``client`` fixture's is not.

    Starlette's ``TestClient`` presents itself as ``testclient``, so the shared fixture is
    correctly refused by ``_is_loopback_client`` — the guard failing closed on an unknown peer
    is the behaviour we want in production, so the fixture is what has to change here, not the
    check.
    """
    with TestClient(app, base_url=BASE_URL, headers={"Origin": BASE_URL},
                    client=("127.0.0.1", 54321)) as c:
        yield c


def _settings(app, **kw):
    """Rebuild app.state.settings with overrides (ConsoleSettings is frozen)."""
    import dataclasses

    app.state.settings = dataclasses.replace(app.state.settings, **kw)
    return app.state.settings


# --------------------------------------------------------------------------- the bypass


def test_a_loopback_get_needs_no_token_when_open_view_is_on(loopback):
    _settings(loopback.app, open_view=True)
    loopback.cookies.clear()
    r = loopback.get("/api/overview")
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Earn-Open-View") == "1", "an unauthenticated read was silent"


def test_with_open_view_off_the_same_request_is_401(loopback):
    _settings(loopback.app, open_view=False)
    loopback.cookies.clear()
    r = loopback.get("/api/overview")
    assert r.status_code == 401
    assert "X-Earn-Open-View" not in r.headers


def test_a_signed_in_session_is_not_marked_open_view(auth_client):
    """A real login must not be labelled as a tokenless read."""
    _settings(auth_client.app, open_view=True)
    r = auth_client.get("/api/overview")
    assert r.status_code == 200
    assert "X-Earn-Open-View" not in r.headers


# --------------------------------------------------------------------------- what it may NOT do


@pytest.mark.parametrize("method,path", [
    ("post", "/api/kill"),
    ("post", "/api/mode"),
    ("post", "/api/config"),
    ("delete", "/api/kill"),
    ("put", "/api/config"),
])
def test_every_mutation_still_demands_the_token(loopback, method, path):
    _settings(loopback.app, open_view=True)
    loopback.cookies.clear()
    r = loopback.request(method.upper(), path, json={})
    assert r.status_code in (401, 403, 404, 405), f"{method} {path} -> {r.status_code}"
    if r.status_code == 401:
        assert "sign in" in r.text.lower() or "unauthorized" in r.text.lower()


def test_step_up_can_never_be_satisfied_by_an_open_view_actor():
    """The dangerous actions all route through require_step_up. It must always raise."""
    from fastapi import HTTPException

    s = deps._open_view_session(3600)
    actor = deps.HumanActor(sid=s.sid, session=s, stepped_up=False)
    assert actor.session.step_up_ok() is False
    with pytest.raises(HTTPException) as e:
        actor.require_step_up()
    assert e.value.status_code == 403


def test_the_open_view_session_carries_no_csrf_token():
    """So the double-submit check cannot be met even if the method guard were loosened."""
    s = deps._open_view_session(3600)
    assert s.csrf == ""


def test_the_open_view_sid_is_identifiable_in_the_audit_trail():
    """Every row these requests produce must be distinguishable from a real human's."""
    from ops.lib import audit

    assert deps.OPEN_VIEW_SID == "open-view"
    assert "open-view" in audit.actor_console(deps.OPEN_VIEW_SID)


# --------------------------------------------------------------------------- the client guard


@pytest.mark.parametrize("host,allowed", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True),
    ("10.0.0.5", False), ("192.168.1.9", False), ("", False), (None, False),
])
def test_only_a_loopback_client_gets_the_bypass(host, allowed):
    """Fails closed on an unknown peer. Second of two checks — the Host guard is the first."""
    class _R:
        method = "GET"
        client = type("C", (), {"host": host})()

    assert deps._is_loopback_client(_R()) is allowed


def test_a_request_with_no_client_at_all_is_refused():
    class _R:
        method = "GET"
        client = None

    assert deps._is_loopback_client(_R()) is False
