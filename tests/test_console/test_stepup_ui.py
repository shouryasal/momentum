"""Every step-up-guarded endpoint has a UI path that actually performs the step-up.

``test_route_inventory`` already pins *which* endpoints require step-up. What nothing
checked was the other half: that the SPA can reach them. Five dialogs collected the console
token and threw it away — ``ConfirmDialog`` handed it to ``onConfirm({stepUpToken})`` and
each caller was expected to remember ``await session.stepUp(...)`` first — and two guarded
actions fired from a bare ``onClick`` with no dialog at all. The symptom was identical every
time: the operator types the right token, clicks the button, and gets
``403 step_up_required`` back.

So this file asserts three things.

1. :data:`UI` covers exactly the step-up set the running app declares. A new guarded
   endpoint fails here until somebody says where its affordance is.
2. Every endpoint marked ``DIALOG`` names a frontend file that renders a ``requireStepUp``
   dialog, and its path really is called from somewhere in ``console/web/src``.
3. Every endpoint marked ``NO_UI`` is called from *nowhere* in the SPA. That is what makes
   the list honest: wiring one up without declaring its affordance turns this test red
   instead of shipping a button that 403s.

The step-up itself lives in exactly one place — ``ConfirmDialog`` calls
``session.stepUp`` before ``onConfirm`` — which is what makes (2) a sufficient check for a
caller that routes its action through the dialog. That behaviour is pinned on the frontend
side (``src/test/components.test.tsx``: "opens the step-up window itself").
"""

from __future__ import annotations

import re

import pytest

from console import deps
from console.app import create_app
from console.settings import API_PREFIX
from ops.lib import paths

WEB = paths.REPO_ROOT / "console" / "web" / "src"

IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

#: The affordance kinds.
DIALOG = "dialog"      #: a ConfirmDialog with requireStepUp, which performs the step-up
NO_UI = "no-ui"        #: reachable only from the CLI / Telegram — the SPA never calls it

#: ``METHOD /path`` -> (kind, frontend file that owns the affordance or "").
#:
#: A file is named for DIALOG entries because that is where the guard has to live; the
#: transport wrapper in the page's ``api.ts`` is not enough on its own.
UI: dict[str, tuple[str, str]] = {
    # --- reachable from the console, behind ConfirmDialog(requireStepUp) ---------------
    "PUT /api/autonomy":
        (DIALOG, "pages/self-improvement/components/AutonomyMatrixCard.tsx"),
    "POST /api/changes/{change_id}/attach":
        (DIALOG, "pages/self-improvement/ChangeDetail.tsx"),
    "POST /api/changes/{change_id}/revert":
        (DIALOG, "pages/self-improvement/ChangeDetail.tsx"),
    "POST /api/control/flatten":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "PUT /api/control/level":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "POST /api/control/pause":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "POST /api/control/schedule":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "POST /api/control/start":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "POST /api/control/stop":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    # "Make it restart itself" on Home. The console died overnight with nothing to bring it
    # back, and the affordance for fixing that has to be on the screen that noticed.
    "POST /api/control/units":
        (DIALOG, "pages/overview/ControlCard.tsx"),
    "DELETE /api/kill":
        (DIALOG, "components/KillButton.tsx"),
    "POST /api/llm/claude/signin":
        (DIALOG, "pages/secrets/components/ClaudeSignInModal.tsx"),
    "POST /api/mode/recover":
        (DIALOG, "pages/mode-live/index.tsx"),
    "POST /api/mode/transition":
        (DIALOG, "pages/mode-live/index.tsx"),
    "POST /api/ops/containers/{service}/restart":
        (DIALOG, "pages/operations/components/SystemPanel.tsx"),
    "POST /api/ops/schedules/install":
        (DIALOG, "pages/operations/components/SchedulePanel.tsx"),
    "POST /api/portfolio/{sleeve}/orders/{trade_id}/cancel":
        (DIALOG, "pages/portfolio/PortfolioPage.tsx"),
    "PUT /api/prompts/active":
        (DIALOG, "pages/prompts/index.tsx"),
    "POST /api/risk/{sleeve}/resume-monthly":
        (DIALOG, "pages/risk/components/ResumeMonthlyModal.tsx"),
    "PUT /api/secrets/auth-mode":
        (DIALOG, "pages/secrets/SecretsPage.tsx"),
    "DELETE /api/secrets/{name}":
        (DIALOG, "pages/secrets/SecretsPage.tsx"),
    "PUT /api/secrets/{name}":
        (DIALOG, "pages/secrets/SecretsPage.tsx"),
    "POST /api/skills":
        (DIALOG, "pages/skills/components/NewSkillModal.tsx"),
    "POST /api/skills/{name}/archive":
        (DIALOG, "pages/skills/index.tsx"),
    "POST /api/testruns/{sleeve}/reset":
        (DIALOG, "pages/test-lab/index.tsx"),
    # --- no console affordance: the CLI or the Telegram bot is the only caller ----------
    # Each of these is a deliberate absence, not an oversight. Adding a caller without
    # moving the entry to DIALOG fails `test_a_no_ui_endpoint_is_called_from_nowhere`.
    "POST /api/auth/rotate-token": (NO_UI, ""),          # `earn-console rotate-token`
    "POST /api/bots/{sleeve}/forceexit": (NO_UI, ""),    # Telegram /forceexit
    "DELETE /api/bots/{sleeve}/locks/{lock_id}": (NO_UI, ""),
    "POST /api/bots/{sleeve}/restart": (NO_UI, ""),      # the Operations panel restarts
    "DELETE /api/bots/{sleeve}/orders/{trade_id}": (NO_UI, ""),   # portfolio cancel does
    "POST /api/mode/transitions/{transition_id}/rollback": (NO_UI, ""),
    "PUT /api/skills/{name}/bindings": (NO_UI, ""),
    "PUT /api/skills/{name}/policy": (NO_UI, ""),
}


@pytest.fixture(scope="module")
def app_module():  # noqa: ANN201
    return create_app(start_pollers=False)


def _step_up_routes(app) -> set[str]:  # noqa: ANN001
    """``METHOD /api/...`` for every route that depends on ``require_step_up``."""
    found: set[str] = set()

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
            stack = [route.dependant] if getattr(route, "dependant", None) else []
            guarded = False
            while stack:
                node = stack.pop()
                if getattr(node, "call", None) is deps.require_step_up:
                    guarded = True
                stack.extend(getattr(node, "dependencies", []) or [])
            if not guarded:
                continue
            for method in getattr(route, "methods", ()) or ():
                if method not in IGNORED_METHODS:
                    found.add(f"{method} {prefix}{path}")

    walk(app.routes, "")
    return found


def _sources() -> dict[str, str]:
    """Every non-test frontend source, by repo-relative-to-``src`` posix path."""
    out: dict[str, str] = {}
    for path in sorted(WEB.rglob("*")):
        if path.suffix not in (".ts", ".tsx") or not path.is_file():
            continue
        rel = path.relative_to(WEB).as_posix()
        if ".test." in path.name or rel.startswith("test/"):
            continue
        out[rel] = path.read_text(encoding="utf-8")
    return out


#: ``client.post<T>(`/x/${y}`, …)`` — the verb, then the first ~200 characters of the call.
_CALL_RE = re.compile(r"\.(post|put|del|delete)\s*(?:<[^()]*?>)?\s*\(", re.S)

_VERB = {"post": "POST", "put": "PUT", "del": "DELETE", "delete": "DELETE"}


def _call_pattern(route: str) -> re.Pattern[str]:
    """A regex matching how the SPA would spell this route's path.

    The client is mounted at ``/api``, so the frontend writes the path without it, with
    ``{param}`` replaced by a template hole. Matching the literal fragments in order is
    enough to find the call and is immune to how the hole is named.
    """
    path = route.split(" ", 1)[1][len(API_PREFIX):]
    literals = [re.escape(part) for part in re.split(r"\{[^}]*\}", path)]
    return re.compile("[^'\"`]*?".join(literals))


def _prefix_pattern(route: str) -> re.Pattern[str]:
    """Only the literal head of the path — used when the tail itself is interpolated.

    ``console/web/src/pages/self-improvement/api.ts`` posts to
    ``/changes/${id}/${action}``: the action *is* the parameter, so ``/attach`` never
    appears as a literal anywhere. The head still identifies the area.
    """
    path = route.split(" ", 1)[1][len(API_PREFIX):]
    return re.compile(re.escape(re.split(r"\{[^}]*\}", path)[0]))


def _callers(route: str, sources: dict[str, str]) -> list[str]:
    """Files that issue this route's METHOD to a path shaped like this route's path.

    Scoped to the verb and to the text of the call itself, so a router table entry or a
    comment mentioning ``/skills`` is not mistaken for ``POST /api/skills``.
    """
    method, pattern = route.split(" ", 1)[0], _call_pattern(route)
    out: list[str] = []
    for name, body in sources.items():
        for match in _CALL_RE.finditer(body):
            if _VERB.get(match.group(1)) != method:
                continue
            if pattern.search(body[match.end(): match.end() + 240]):
                out.append(name)
                break
    return sorted(out)


def test_the_manifest_covers_exactly_the_servers_step_up_set(app_module):  # noqa: ANN001
    actual = _step_up_routes(app_module)
    missing = sorted(actual - set(UI))
    stale = sorted(set(UI) - actual)
    assert not missing, (
        "these endpoints require step-up and this file does not say how the console "
        f"reaches them (add a DIALOG or NO_UI entry): {missing}"
    )
    assert not stale, f"these entries no longer require step-up: {stale}"


def test_the_manifest_is_not_vacuous(app_module):  # noqa: ANN001
    """A walk that silently found nothing would make every assertion here pass."""
    assert len(_step_up_routes(app_module)) > 20


@pytest.mark.parametrize(
    "route", sorted(k for k, (kind, _f) in UI.items() if kind == DIALOG)
)
def test_a_dialog_endpoint_is_guarded_where_it_is_triggered(route: str):
    kind, rel = UI[route]
    assert kind == DIALOG
    sources = _sources()
    assert rel in sources, f"{route}: {rel} does not exist"
    text = sources[rel]
    assert "requireStepUp" in text, (
        f"{route}: {rel} triggers a step-up-guarded action but renders no "
        "ConfirmDialog with requireStepUp, so the action goes out unauthenticated "
        "and comes back 403"
    )
    callers = _callers(route, sources)
    if not callers:
        # The tail of the path may itself be interpolated (`/changes/${id}/${action}`);
        # fall back to the area prefix, restricted to the affordance's own folder.
        folder = rel.rsplit("/", 1)[0]
        head = _prefix_pattern(route)
        callers = sorted(
            name for name, body in sources.items()
            if name.startswith(folder.split("/components")[0]) and head.search(body)
        )
    assert callers, f"{route}: nothing in console/web/src calls this path"


@pytest.mark.parametrize("route", sorted(k for k, (kind, _f) in UI.items() if kind == NO_UI))
def test_a_no_ui_endpoint_is_called_from_nowhere(route: str):
    """If a caller appears, the entry must move to DIALOG and get a real affordance.

    A transport wrapper with no component behind it counts: an exported mutation hook for
    a step-up route is a 403 waiting for whoever wires it to a button.
    """
    callers = _callers(route, _sources())
    assert not callers, (
        f"{route} is declared NO_UI but {callers} calls it. Route it through a "
        "ConfirmDialog with requireStepUp and move the entry to DIALOG."
    )


def test_the_step_up_lives_in_one_place():
    """``ConfirmDialog`` must be the thing that opens the window.

    Every DIALOG entry above is verified only by "the file renders requireStepUp", which
    is sound exactly because the dialog performs the step-up itself. If that moves back
    out to the callers, this file's guarantee evaporates silently — so pin it.
    """
    text = (WEB / "components" / "ConfirmDialog.tsx").read_text(encoding="utf-8")
    assert "useOptionalSession" in text
    assert re.search(r"if \(needsToken && stepUp\) await stepUp\(", text), (
        "ConfirmDialog no longer performs the step-up before onConfirm; every caller "
        "that relies on it now sends a guarded request with no step-up"
    )


def test_no_step_up_action_is_fired_from_a_bare_click():
    """A guarded path called from a file with no dialog at all is the medium finding.

    Covered per-route above, but asserted globally too so a *new* page cannot introduce
    the same shape without touching the manifest: any file that calls a step-up path and
    is not a pure transport module must render ``requireStepUp``.
    """
    sources = {
        name: body for name, body in _sources().items()
        # transport wrappers hold no UI; their page owns the guard
        if not (name.endswith("api.ts") or name.startswith("api/"))
    }
    offenders: list[str] = []
    for route in UI:
        for name in _callers(route, sources):
            if "requireStepUp" not in sources[name]:
                offenders.append(f"{name} calls {route} with no requireStepUp dialog")
    assert not offenders, sorted(offenders)
