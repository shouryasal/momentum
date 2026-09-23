"""Console settings — and the one place the bind address lives.

``BIND_HOST`` is a module constant typed as a ``Literal``. :class:`ConsoleSettings` has no
host field at all, so there is nothing for a config key, an environment variable or a CLI
flag to override: ``python -m console serve --host 0.0.0.0`` is rejected by the CLI, and a
programmatic caller that wants a different host has nowhere to put it.

Everything else (port, session lifetime, step-up window) comes from ``config/earn.yaml``
``console:`` through :meth:`ConsoleSettings.from_config`, and falls back to the defaults
here when the config cannot be loaded, so the console still starts to show the error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from ops.lib import paths

#: The console bind address. Code, not config (docs/contracts.md §8).
BIND_HOST: Final[Literal["127.0.0.1"]] = "127.0.0.1"

DEFAULT_PORT: Final[int] = 8765
DEFAULT_SESSION_HOURS: Final[int] = 12
DEFAULT_STEPUP_MINUTES: Final[int] = 10

#: Every route the console serves lives under this prefix.
API_PREFIX: Final[str] = "/api"

#: Where the one-time login token is kept (0600). ``EARN_CONSOLE_TOKEN_FILE`` overrides it
#: for tests and for an operator who keeps their config elsewhere.
TOKEN_FILE_ENV: Final[str] = "EARN_CONSOLE_TOKEN_FILE"
XDG_CONFIG_ENV: Final[str] = "XDG_CONFIG_HOME"
TOKEN_FILE_NAME: Final[str] = "console-token"
AUTH_STATE_NAME: Final[str] = "console_auth.json"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def config_home(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/earn`` or ``~/.config/earn``."""
    raw = _env(env).get(XDG_CONFIG_ENV, "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "earn"


def token_path(env: Mapping[str, str] | None = None) -> Path:
    """The 0600 file holding the console login token."""
    raw = _env(env).get(TOKEN_FILE_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return config_home(env) / TOKEN_FILE_NAME


def auth_state_path(env: Mapping[str, str] | None = None) -> Path:
    """``var/state/console_auth.json`` — the salted hash, never the token."""
    return paths.state_dir(dict(env) if env is not None else None) / AUTH_STATE_NAME


@dataclass(frozen=True)
class ConsoleSettings:
    """Everything the app needs to boot. Frozen: nothing reconfigures a running console."""

    port: int = DEFAULT_PORT
    session_hours: int = DEFAULT_SESSION_HOURS
    stepup_minutes: int = DEFAULT_STEPUP_MINUTES
    repo_root: Path = paths.REPO_ROOT
    state_root: Path = paths.REPO_ROOT
    token_file: Path = paths.REPO_ROOT / TOKEN_FILE_NAME
    auth_state_file: Path = paths.REPO_ROOT / AUTH_STATE_NAME
    static_dir: Path = paths.REPO_ROOT / "console" / "static"

    # ---- the invariant -----------------------------------------------------------

    @property
    def host(self) -> str:
        """Always ``127.0.0.1``. There is no field and no setter behind this."""
        return BIND_HOST

    @property
    def base_url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}/"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host headers the app answers; anything else is a 421 (DNS rebinding)."""
        return frozenset(
            {
                BIND_HOST,
                f"{BIND_HOST}:{self.port}",
                "localhost",
                f"localhost:{self.port}",
                "[::1]",
                f"[::1]:{self.port}",
            }
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Origins allowed on a mutating request. No CORS middleware exists."""
        return frozenset({f"http://{BIND_HOST}:{self.port}", f"http://localhost:{self.port}"})

    @property
    def session_max_age_s(self) -> int:
        return int(self.session_hours) * 3600

    @property
    def stepup_max_age_s(self) -> int:
        return int(self.stepup_minutes) * 60

    # ---- construction ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: Any | None = None,
        *,
        env: Mapping[str, str] | None = None,
        port: int | None = None,
    ) -> ConsoleSettings:
        """Build settings from an ``EarnConfig`` (loaded on demand) and the environment.

        A config that will not load is not fatal: the console starts on the defaults so a
        human can see and fix the error in the UI.
        """
        console_cfg = getattr(cfg, "console", None) if cfg is not None else None
        if console_cfg is None:
            console_cfg = _console_section_or_none()
        state = paths.state_root(dict(env) if env is not None else None)
        return cls(
            port=int(port if port is not None else getattr(console_cfg, "port", DEFAULT_PORT)),
            session_hours=int(getattr(console_cfg, "session_hours", DEFAULT_SESSION_HOURS)),
            stepup_minutes=int(getattr(console_cfg, "stepup_minutes", DEFAULT_STEPUP_MINUTES)),
            repo_root=paths.REPO_ROOT,
            state_root=state,
            token_file=token_path(env),
            auth_state_file=auth_state_path(env),
            static_dir=paths.REPO_ROOT / "console" / "static",
        )


def _console_section_or_none() -> Any | None:
    """``cfg.console`` when earn.yaml loads, else ``None`` (defaults win)."""
    try:
        from console.deps import get_cfg

        return get_cfg().console
    except Exception:  # noqa: BLE001 - a broken config must not stop the console booting
        return None
