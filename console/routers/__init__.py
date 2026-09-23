"""One module per area; every module is auto-mounted under ``/api``.

**The convention P1…P7 follow** (``console.app.discover_routers`` implements it):

1. The file is ``console/routers/<area>.py`` where ``<area>`` is the first path segment
   of the endpoints in spec §5.2 — ``mode.py`` for ``/api/mode/*``, ``bots.py`` for
   ``/api/bots/*``, ``config.py`` for ``/api/config/*``, and so on. One area, one file,
   one owner: no two packages edit the same router module.
2. It exports exactly one module-level object named ``router``::

       router = APIRouter(prefix="/mode", tags=["mode"])

   The prefix carries the leading slash and no ``/api`` (that is added on mount), and the
   tag is the area name. A module whose routes span several top-level paths (``decisions``
   serves ``/runs`` and ``/proposals``) instead exports ``APIRouter(tags=["decisions"])``
   with no prefix and spells the full path on each route. Two modules must never serve the
   same path.
3. Auth is declared per route with the dependencies in :mod:`console.deps`:
   ``Depends(current_actor)`` for ``S`` in the spec's table and
   ``Depends(require_step_up)`` for ``SU``. A route with neither is public and must say
   why in its docstring (only ``/api/health`` and ``/api/auth/login`` qualify).
4. Response models come from :mod:`console.contracts`, which is also where the DTO is
   registered for the no-secret-leak scan. Errors are raised with ``deps.http_error``.
5. Business logic lives in :mod:`console.services`; a router only translates.

Discovery imports every module in this package at startup, so an import error in one area
fails the console loudly rather than silently dropping routes. F0 ships ``auth``,
``health``, ``meta`` and ``kill``; the rest arrive with their packages and need no change
here — this package deliberately has no ``__all__`` to keep out of their way.
"""

from __future__ import annotations
