"""``pyproject.toml`` must ship every package that is on disk.

Tests run from the repo root, so an omission here is invisible until somebody builds a
wheel — at which point the console, or the signal pipeline, simply is not in it. That is
exactly the kind of failure nobody notices until a deploy, so it is asserted instead.
"""

from __future__ import annotations

import tomllib

from ops.lib import paths

PYPROJECT = paths.REPO_ROOT / "pyproject.toml"

#: Directories that hold ``__init__.py`` files but are not shipped: tests are not a
#: package of the distribution, and ``.claude`` is agent configuration.
NOT_SHIPPED: frozenset[str] = frozenset({"tests", ".claude", "console.web"})


def _declared() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["tool"]["setuptools"]["packages"])


def _on_disk() -> set[str]:
    out: set[str] = set()
    for init in paths.REPO_ROOT.rglob("__init__.py"):
        rel = init.parent.relative_to(paths.REPO_ROOT)
        parts = rel.parts
        if any(part.startswith(".") or part in ("node_modules", "web") for part in parts):
            continue
        name = ".".join(parts)
        if name and name.split(".")[0] not in NOT_SHIPPED:
            out.add(name)
    return out


def test_every_package_on_disk_is_declared():
    missing = sorted(_on_disk() - set(_declared()))
    assert not missing, (
        "these packages exist but pyproject would not ship them: " + ", ".join(missing)
    )


def test_no_declared_package_is_missing_from_disk():
    stale = sorted(set(_declared()) - _on_disk())
    assert not stale, f"pyproject declares packages that do not exist: {stale}"


def test_the_declared_list_is_sorted():
    """Alphabetical, so a merge conflict here is a conflict about content, not order."""
    declared = _declared()
    assert declared == sorted(declared)


def test_the_console_is_shipped():
    """The one that actually went missing: a wheel with no console serves no UI."""
    declared = set(_declared())
    assert {"console", "console.routers", "console.services"} <= declared
