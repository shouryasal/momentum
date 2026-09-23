"""The watcher's capability firewall — what raising a hand is *allowed* to touch.

The watcher runs every few minutes, unattended, on the strength of an 8B model's opinion.
That is fine precisely because it can do exactly one thing: append a row to
``watch_events`` and, at most, ask for a human-or-Claude escalation. It can never place,
size or cancel an order, never write a proposal, never edit config, never move a flag.

This module makes that a property of the code rather than a promise in a docstring:

* :class:`HandRaiseOnly` wraps the journal connection. ``SELECT`` is free; ``INSERT`` and
  ``UPDATE`` are permitted only against :data:`WRITABLE_TABLES`; every other statement —
  ``DELETE``, ``DROP``, ``ATTACH``, a write to ``orders``, ``proposals``, ``gate_decisions``
  or ``signals`` — raises :class:`WatchForbidden` before SQLite sees it. The watcher holds
  no other handle on the journal, so there is no path around it.
* :func:`assert_no_order_path` walks this package's own source and refuses any import of
  the order, proposal, approval, flag or config-writing modules. It is called by the test
  suite, and it is cheap enough to call at import time of the runner.

Both are deliberately blunt. A watcher that needs to be clever about what it may write is
a watcher that has outgrown its job description; the answer to that is a proposal written
by a tier-4 model, which is the thing this package exists to *request* and never to do.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "FORBIDDEN_IMPORTS",
    "WRITABLE_TABLES",
    "HandRaiseOnly",
    "WatchForbidden",
    "assert_no_order_path",
    "forbidden_imports_in",
]


class WatchForbidden(RuntimeError):
    """The watcher tried to do something only a decision run may do."""


#: The only table the watcher may write. It is its own, and it holds hand-raises.
WRITABLE_TABLES: frozenset[str] = frozenset({"watch_events"})

#: Modules that can move money, change what the system may do, or speak for a decision.
#: An import of any of these from inside ``runs.watch`` is a bug, not a feature.
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "runs.approvals",
    "runs.apply_changes",
    "runs.research_run",
    "schemas.proposal",
    "strategies",
    "ops.config_store",
    "ops.lib.flags",
    "ops.lib.kill",
    "ops.modes",
    "freqtrade",
)

_STATEMENT = re.compile(r"^\s*(?:/\*.*?\*/\s*)?([a-zA-Z]+)", re.DOTALL)
_TARGET = re.compile(
    r"^\s*(?:insert\s+(?:or\s+\w+\s+)?into|update|replace\s+into)\s+"
    r"[\"'`\[]?(\w+)",
    re.IGNORECASE,
)
#: Read-only verbs. ``PRAGMA`` is here because ``opened()`` issues them on every handle.
_READ_VERBS = frozenset({"select", "with", "explain", "pragma", "begin", "commit",
                         "rollback"})
_WRITE_VERBS = frozenset({"insert", "update", "replace"})


class HandRaiseOnly:
    """A journal connection that can read anything and write only the watch tables.

    It is not a subclass of :class:`sqlite3.Connection` on purpose: subclassing would keep
    the parent's ``execute`` reachable through ``super()`` and through any helper that
    takes a connection and calls it directly. Wrapping means the real connection is a
    private attribute and the only way through is :meth:`execute`.
    """

    def __init__(self, conn: sqlite3.Connection,
                 *, writable: Iterable[str] = WRITABLE_TABLES) -> None:
        self._conn = conn
        self._writable = frozenset(w.lower() for w in writable)

    # -- the gate --------------------------------------------------------------

    def check(self, sql: str) -> None:
        """Raise :class:`WatchForbidden` unless ``sql`` is a read or an allowed write."""
        match = _STATEMENT.match(sql or "")
        verb = (match.group(1).lower() if match else "")
        if verb in _READ_VERBS:
            return
        if verb == "create":
            # The watcher creates its own tables on first use and nothing else; the table
            # name has to be one it owns.
            name = re.search(r"create\s+(?:table|index)\s+(?:if\s+not\s+exists\s+)?"
                             r"[\"'`\[]?(\w+)", sql, re.IGNORECASE)
            target = (name.group(1).lower() if name else "")
            if target in self._writable or target.startswith("idx_watch"):
                return
            raise WatchForbidden(f"the watcher may not create {target or sql[:40]!r}")
        if verb in _WRITE_VERBS:
            target = _TARGET.match(sql)
            table = (target.group(1).lower() if target else "")
            if table in self._writable:
                return
            raise WatchForbidden(
                f"the watcher may only write {sorted(self._writable)}, not {table!r}: "
                "raising a hand is the whole of its authority"
            )
        raise WatchForbidden(
            f"statement {verb or sql[:40]!r} is not available to the watcher"
        )

    # -- the narrow connection API the watcher uses ----------------------------

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        self.check(sql)
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Any]) -> sqlite3.Cursor:
        self.check(sql)
        return self._conn.executemany(sql, seq)

    def executescript(self, sql: str) -> sqlite3.Cursor:
        raise WatchForbidden("executescript bypasses the statement check")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    @property
    def in_transaction(self) -> bool:
        return bool(self._conn.in_transaction)

    @property
    def raw_for_reads(self) -> sqlite3.Connection:
        """The underlying connection, for a helper that only ever selects.

        Used by the position and thesis lookups, which issue nothing but ``SELECT``. The
        name is the review trigger: a writer reached through this property is a bug.
        """
        return self._conn

    @property
    def raw_for_llm_journal(self) -> sqlite3.Connection:
        """The one deliberate hole, and it is an audit trail rather than an action.

        ``runs.llm.chain`` writes an ``llm_calls`` row per attempt and a
        ``provider_switches`` row per move, and it has to, or a watch cycle would be the
        only model traffic in the system that nothing records — no circuit breaker, no
        budget, nothing on the AI & Models page. Those two tables describe *who was asked
        what*; they cannot place an order, size a position or approve anything.

        This is the only unguarded handle the watcher hands out, it is handed only to the
        router, and it is named so that anybody passing it anywhere else has to type the
        reason out loud.
        """
        return self._conn


# --------------------------------------------------------------------------- imports


def forbidden_imports_in(path: Path) -> list[str]:
    """Every forbidden module this file imports, with the line, as readable strings."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as e:  # pragma: no cover — a broken file fails elsewhere
        return [f"{path.name}: unreadable ({e})"]
    found: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            for bad in FORBIDDEN_IMPORTS:
                if name == bad or name.startswith(bad + "."):
                    found.append(f"{path.name}:{node.lineno} imports {name}")
    return found


def assert_no_order_path(package_dir: Path | None = None) -> None:
    """Refuse to run if this package imports anything that can act on the market."""
    root = package_dir or Path(__file__).resolve().parent
    offences: list[str] = []
    for file in sorted(root.glob("*.py")):
        offences.extend(forbidden_imports_in(file))
    if offences:
        raise WatchForbidden(
            "runs.watch may only raise a hand; these imports would let it act: "
            + "; ".join(offences)
        )
