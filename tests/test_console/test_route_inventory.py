"""The mounted HTTP surface, pinned against ``docs/contracts.md``.

Nine packages mount routers into one app through ``console.app.discover_routers``. Nothing
in that seam notices a router that silently failed to declare a route, a path that moved,
or an endpoint that arrived with no auth on it — the app just starts, smaller or larger
than anybody meant. So the whole table is asserted here in both directions:

* every route the app mounts is listed in ``docs/contracts.md`` §9.6;
* every line in that block is actually mounted;
* the ``/api`` prefix is added exactly once, by ``register_router``, and no router repeats
  it (the seam tolerates one that does, so that a package's documented path keeps working,
  but the convention is a bare area prefix);
* no two routes collide, and no wildcard route shadows a literal one declared after it;
* every route carries a session dependency except the three that cannot;
* the step-up set is exactly spec §5.2's ``SU`` column.

Adding an endpoint means adding a line to contracts.md. That is the point.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from console import deps
from console.app import create_app, discover_routers, iter_routes
from console.settings import API_PREFIX
from ops.lib import paths

CONTRACTS = paths.REPO_ROOT / "docs" / "contracts.md"

#: Routes that may be reached without a session, and why.
PUBLIC: frozenset[str] = frozenset(
    {
        "GET /api/health",        # the liveness probe; it carries no data
        "POST /api/auth/login",   # there is no session yet
        "GET /api/openapi.json",  # the schema of the surface, not the surface
    }
)

#: Spec §5.2's ``SU`` column: a session AND a fresh re-entry of the console token.
#: Everything that moves money, changes what the bots do, rewrites a secret, or edits a
#: tier-2 file. ``PUT /api/config/{file_id}`` is deliberately absent: it decides per save,
#: from the ``x-protected`` annotation on the paths that actually changed.
STEP_UP: frozenset[str] = frozenset(
    {
        "POST /api/auth/rotate-token",
        "PUT /api/autonomy",
        "POST /api/bots/{sleeve}/forceexit",
        "DELETE /api/bots/{sleeve}/locks/{lock_id}",
        "DELETE /api/bots/{sleeve}/orders/{trade_id}",
        "POST /api/bots/{sleeve}/restart",
        "POST /api/changes/{change_id}/attach",
        "POST /api/changes/{change_id}/revert",
        # The control surface. Arming and resuming raise how much the system does by
        # itself, and flatten sells — so all three step up. Pause and stop deliberately do
        # not: the brake is never behind a door.
        "PUT /api/control/level",
        "POST /api/control/resume",
        "POST /api/control/flatten",
        "DELETE /api/kill",
        "POST /api/mode/recover",
        "POST /api/mode/transition",
        "POST /api/mode/transitions/{transition_id}/rollback",
        "POST /api/ops/containers/{service}/restart",
        "POST /api/ops/schedules/install",
        "POST /api/portfolio/{sleeve}/orders/{trade_id}/cancel",
        "POST /api/llm/claude/signin",
        "PUT /api/prompts/active",
        "POST /api/risk/{sleeve}/resume-monthly",
        "PUT /api/secrets/auth-mode",
        "DELETE /api/secrets/{name}",
        "PUT /api/secrets/{name}",
        "POST /api/skills",
        "POST /api/skills/{name}/archive",
        "PUT /api/skills/{name}/bindings",
        "PUT /api/skills/{name}/policy",
        "POST /api/testruns/{sleeve}/reset",
    }
)

IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _documented() -> set[str]:
    """``METHOD /api/...`` from the ```routes``` block in docs/contracts.md §9.6."""
    text = CONTRACTS.read_text(encoding="utf-8")
    blocks = re.findall(r"```routes\n(.*?)```", text, re.S)
    assert len(blocks) == 1, "docs/contracts.md must hold exactly one ```routes``` block"
    out = set()
    for line in blocks[0].splitlines():
        line = line.strip()
        if not line:
            continue
        method, path = line.split(None, 1)
        out.add(f"{method} {path.strip()}")
    return out


def _mounted(app) -> set[str]:  # noqa: ANN001
    out = set()
    for methods, path in iter_routes(app):
        if not path.startswith(API_PREFIX) or path == f"{API_PREFIX}/openapi.json":
            continue
        for method in methods or ():
            if method not in IGNORED_METHODS:
                out.add(f"{method} {path}")
    return out


@pytest.fixture(scope="module")
def mounted(app_module) -> set[str]:  # noqa: ANN001
    return _mounted(app_module)


@pytest.fixture(scope="module")
def app_module():  # noqa: ANN201
    return create_app(start_pollers=False)


# --------------------------------------------------------------------------- the table


def test_every_mounted_route_is_documented(mounted: set[str]):
    undocumented = sorted(mounted - _documented())
    assert not undocumented, (
        "these routes are mounted but missing from docs/contracts.md §9.6:\n  "
        + "\n  ".join(undocumented)
    )


def test_every_documented_route_is_mounted(mounted: set[str]):
    missing = sorted(_documented() - mounted)
    assert not missing, (
        "docs/contracts.md §9.6 lists routes the app does not mount "
        "(a router failed to import, or an endpoint moved):\n  " + "\n  ".join(missing)
    )


def test_the_documented_surface_is_not_empty():
    """A parse that silently matched nothing would make both tests above vacuous."""
    assert len(_documented()) > 150


# --------------------------------------------------------------------------- the seam


def test_no_router_repeats_the_api_prefix():
    """``register_router`` survives one that does, but the convention is a bare prefix."""
    offenders = [name for name, router in discover_routers()
                 if str(getattr(router, "prefix", "") or "").startswith(API_PREFIX)]
    assert not offenders, (
        f"{offenders} declare an /api prefix; the app adds it on mount "
        "(see docs/contracts.md §9.1)"
    )


def test_every_router_is_tagged_with_one_area():
    for name, router in discover_routers():
        assert router.tags, f"{name} must carry a tag"
        assert len(router.tags) == 1, f"{name} must name exactly one area"


def test_no_two_routes_collide(mounted: set[str]):
    """``iter_routes`` walks included routers, so a duplicate would appear twice."""
    app = create_app(start_pollers=False)
    seen: list[str] = []
    for methods, path in iter_routes(app):
        for method in methods or ():
            if method not in IGNORED_METHODS:
                seen.append(f"{method} {path}")
    duplicates = sorted({key for key in seen if seen.count(key) > 1})
    assert not duplicates, f"two routers claim: {duplicates}"


def _as_regex(path: str) -> re.Pattern[str]:
    pattern = re.sub(r"\{[^}]+:path\}", "(.+)", path)
    pattern = re.sub(r"\{[^}/]+\}", "([^/]+)", pattern)
    return re.compile(f"^{pattern}$")


def test_no_wildcard_route_shadows_a_literal_one():
    """``/api/proposals/{run_id:path}`` declared before ``/api/proposals/pending`` would
    swallow it, and the only symptom is a 404 with a confusing body."""
    app = create_app(start_pollers=False)
    ordered = [
        (path, frozenset(m for m in (methods or ()) if m not in IGNORED_METHODS))
        for methods, path in iter_routes(app)
    ]
    shadowed: list[str] = []
    for index, (path, methods) in enumerate(ordered):
        if "{" in path:
            continue
        for earlier, earlier_methods in ordered[:index]:
            if "{" not in earlier or not (methods & earlier_methods):
                continue
            if _as_regex(earlier).match(path):
                shadowed.append(f"{path} is shadowed by {earlier}")
    assert not shadowed, shadowed


# --------------------------------------------------------------------------- auth


def _auth_marks(app) -> dict[str, set[str]]:  # noqa: ANN001
    callables = {deps.current_actor: "S", deps.require_step_up: "SU"}
    marks: dict[str, set[str]] = {}

    def walk(routes, prefix: str) -> None:  # noqa: ANN001
        for route in routes:
            context = getattr(route, "include_context", None)
            original = getattr(route, "original_router", None)
            if context is not None and original is not None:
                walk(original.routes, prefix + str(getattr(context, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            if path is None or not (prefix + str(path)).startswith(API_PREFIX):
                continue
            found: set[str] = set()
            stack = [route.dependant] if getattr(route, "dependant", None) else []
            while stack:
                node = stack.pop()
                name = callables.get(getattr(node, "call", None))
                if name:
                    found.add(name)
                stack.extend(getattr(node, "dependencies", []) or [])
            for method in getattr(route, "methods", ()) or ():
                if method not in IGNORED_METHODS:
                    marks[f"{method} {prefix}{path}"] = found

    walk(app.routes, "")
    return marks


def test_every_route_needs_a_session_except_the_three_that_cannot(app_module):  # noqa: ANN001
    marks = _auth_marks(app_module)
    open_routes = sorted(key for key, found in marks.items() if not found)
    assert set(open_routes) <= PUBLIC, (
        f"these routes are reachable with no session: {sorted(set(open_routes) - PUBLIC)}"
    )


def test_the_step_up_set_is_exactly_the_specs_su_column(app_module):  # noqa: ANN001
    marks = _auth_marks(app_module)
    actual = {key for key, found in marks.items() if "SU" in found}
    assert actual == STEP_UP, (
        f"gained step-up: {sorted(actual - STEP_UP)}\n"
        f"lost step-up: {sorted(STEP_UP - actual)}"
    )


def test_contracts_md_is_where_the_inventory_lives():
    assert Path(CONTRACTS).is_file()


# --------------------------------------------------------------------------- SSE topics


def test_every_sse_topic_has_a_publisher():
    """A topic the UI can subscribe to but nothing ever publishes is a page that never
    updates and never says why. Each one is sourced by a poller, a route, or both."""
    from console import sse
    from console.events import default_pollers
    from console.settings import ConsoleSettings
    from ops.config import load_config

    cfg = load_config()
    pollers = default_pollers(ConsoleSettings.from_config(cfg), cfg=cfg, factory=object)
    from_pollers = {getattr(p, "topic", None) for p in pollers}

    #: Topics that have no table or file to watch, with what publishes them instead.
    #: Everything else must come from ``default_pollers`` — a DB cursor is what makes a
    #: page update when a *job*, not a request, wrote the row.
    published_in_process = {
        "job": "console.services.jobs.JobRunner._emit, on every status change",
        "backtest": "console.services.backtest_service.BacktestQueue._emit",
        "claude_auth": "console.services.claude_signin_service.SignInManager._emit",
    }
    uncovered = sorted(sse.TOPICS - from_pollers - set(published_in_process))
    assert not uncovered, (
        "these SSE topics have no source; either add a poller in console/events.py or "
        f"name what publishes them: {uncovered}"
    )
