"""The Earn console: a local-only FastAPI app on 127.0.0.1.

The console is the one place a human drives Earn from: it reads the journal and knowledge
databases, shows the signed mode state, and performs the privileged actions no automated
run may perform. Three rules shape every module in here:

1. **Local only.** The bind address is a code constant (``console.settings.BIND_HOST``).
   No config key, no environment variable and no CLI flag can move it.
2. **Human only.** Under ``EARN_AUTOMATED_RUN=1`` the app refuses to start and, if the
   variable appears after start, every mutating route is refused too.
3. **No secret ever leaves.** No response model carries a secret value; the event bus and
   the log helpers run ``console.security.redact`` over everything they emit.

Layout (spec §5.1)::

    console/app.py          create_app(): middleware stack, router discovery, static mount
    console/settings.py     ConsoleSettings + the 127.0.0.1 constant
    console/security.py     token login, signed session, CSRF, host/origin guard, redact()
    console/deps.py         config cache, read-only DB handles, BotApi factory, HumanActor
    console/sse.py          EventBus + the /api/stream response
    console/events.py       DB-cursor / file-mtime / bot pollers that feed the bus
    console/schema_meta.py  pydantic JSON Schema -> the form metadata the UI needs
    console/contracts.py    pydantic DTOs (the source of the generated TS types)
    console/cli.py          python -m console: serve, create-token, print-url
    console/routers/        one module per area, auto-registered under /api
    console/services/       business logic with no FastAPI imports
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Console API version. Bumped when a DTO in ``console.contracts`` changes shape.
__version__ = "0.1.0"
