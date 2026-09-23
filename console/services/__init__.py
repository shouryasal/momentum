"""Console business logic — importable without FastAPI, therefore unit-testable.

Nothing in this package imports ``fastapi`` or ``starlette``: a router translates these
functions' return values and exceptions into HTTP, and a test calls them directly. Each
service raises its own typed error (``QueryError``, ``JobError``, ``GitError``, …) and the
router maps it to a status code and the ``{"error": {...}}`` envelope.

F0 ships the three every package needs:

* :mod:`console.services.queries` — read-only SQLite helpers with busy-timeout and retry
* :mod:`console.services.jobs` — background jobs with progress on the SSE bus
* :mod:`console.services.git_service` — branch/commit/dirty, file history, commits

Other packages add their own module beside them (``mode_service``, ``config_service``, …);
this file stays empty of exports so no package has to edit it.
"""

from __future__ import annotations
