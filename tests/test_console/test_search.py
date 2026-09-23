"""Ctrl-K search: the page table must be the same table the frontend routes with.

`console/services/search_service.PAGES` is the server half of the palette. Its ids and
routes were hand-written from spec §12's prose while `console/web/src/routes.tsx` keys the
same pages by their route id (`ai-models`, `test-lab`, `mode-live`), so five of the
twenty-one server page hits pointed at paths React Router does not serve. The frontend
papered over it by dropping every `kind === 'page'` hit from the server, which also threw
away the hits that *were* correct.

These tests parse the registry and hold both halves to it, in both directions: a page the
frontend adds has to become searchable, and a page the server advertises has to be
navigable.
"""

from __future__ import annotations

import re

import pytest

from console.services import search_service
from ops.lib import paths

ROUTES_TSX = paths.REPO_ROOT / "console" / "web" / "src" / "routes.tsx"

#: One `{ … }` entry of the `ROUTES` array — matched by its `id`, so the fields inside may
#: be reordered or wrapped without turning this into a silent skip.
ENTRY_RE = re.compile(r"\{[^{}]*?\bid:\s*'[^']+'[^{}]*?\}", re.DOTALL)

needs_frontend = pytest.mark.skipif(
    not ROUTES_TSX.exists(), reason="console/web is not present in this checkout"
)


def _field(block: str, name: str) -> str:
    match = re.search(rf"\b{name}:\s*'([^']*)'", block)
    return match.group(1) if match else ""


def _registered() -> list[dict[str, str]]:
    source = ROUTES_TSX.read_text(encoding="utf-8")
    body = source.split("export const ROUTES", 1)[-1].split("export const NAV_GROUPS", 1)[0]
    out: list[dict[str, str]] = []
    for block in ENTRY_RE.findall(body):
        page = re.search(r"\bpage:\s*(\d+)", block)
        out.append(
            {
                "page": page.group(1) if page else "",
                "id": _field(block, "id"),
                "path": _field(block, "path"),
                "title": _field(block, "title"),
            }
        )
    return out


@needs_frontend
def test_the_route_registry_parses():
    routes = _registered()
    assert len(routes) == 21, "spec §12 has 21 pages; the parser must see all of them"
    assert all(r["id"] and r["path"].startswith("/") and r["title"] for r in routes)
    assert sorted(int(r["page"]) for r in routes) == list(range(1, 22))


@needs_frontend
def test_every_pages_entry_matches_a_registered_route():
    by_id = {r["id"]: r for r in _registered()}
    mismatched = []
    for page_id, title, route in search_service.PAGES:
        registered = by_id.get(page_id)
        if registered is None:
            mismatched.append(f"{page_id}: no route with that id")
            continue
        if registered["path"] != route:
            mismatched.append(f"{page_id}: route {route!r} != registry {registered['path']!r}")
        if registered["title"] != title:
            mismatched.append(f"{page_id}: title {title!r} != registry {registered['title']!r}")
    assert not mismatched, f"PAGES drifted from routes.tsx: {mismatched}"


@needs_frontend
def test_every_registered_route_is_advertised_by_the_server():
    """A page the shell ships but the server never indexes is unfindable in Ctrl-K."""
    served = {page_id for page_id, _, _ in search_service.PAGES}
    missing = sorted({r["id"] for r in _registered()} - served)
    assert not missing, f"routes.tsx has pages search_service.PAGES does not index: {missing}"


def test_page_ids_and_routes_are_unique():
    ids = [page_id for page_id, _, _ in search_service.PAGES]
    routes = [route for _, _, route in search_service.PAGES]
    assert len(set(ids)) == len(ids)
    assert len(set(routes)) == len(routes)


def test_page_hits_carry_a_navigable_route():
    """Every page entry reaches the palette with a route the shell can navigate to."""
    hits = search_service.page_entries()
    assert len(hits) == len(search_service.PAGES)
    assert all(hit.kind == "page" and hit.route.startswith("/") for hit in hits)


def test_search_returns_the_page_by_its_title():
    result = search_service.search("AI & Models", root=None, include_live=False, limit=10)
    pages = [hit for hit in result["results"] if hit["kind"] == "page"]
    assert pages, "the palette must find a page by its title"
    assert pages[0]["route"] == "/ai-models"


def test_empty_query_lists_the_pages():
    result = search_service.search("", include_live=False, limit=50)
    assert [hit["id"] for hit in result["results"]] == [p[0] for p in search_service.PAGES]
