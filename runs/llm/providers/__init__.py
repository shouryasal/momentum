"""Building the provider set for one process, from ``models.yaml``.

Three provider *keys* exist (``claude:subscription``, ``claude:api_key``, ``ollama``) but
which of them a given run actually gets depends on the auth mode: ``subscription`` builds
one Claude provider, ``api_key`` the other, ``auto`` both, so the router can fall back
between credentials without restarting anything.

:func:`build_registry` never raises on a missing local endpoint — an Ollama provider with
``base_url=None`` reports ``health() is False`` and gets skipped, which is the difference
between "the laptop is not running Ollama today" and "the run failed".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ops.lib import claude_auth
from runs.llm.base import BaseProvider, ProviderRegistry
from runs.llm.providers.claude_sdk import ClaudeSDKProvider
from runs.llm.providers.ollama import OllamaProvider

__all__ = ["ClaudeSDKProvider", "OllamaProvider", "build_registry", "build_providers"]


def build_providers(
    models_cfg: Any,
    *,
    kdb: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    cli_path: str | None = None,
    http_client: Any | None = None,
    detect_ollama: bool = True,
) -> dict[str, BaseProvider]:
    """Instantiate every provider this process may use, keyed by provider key."""
    from runs.llm import health as health_mod

    auth = models_cfg.auth
    mode = claude_auth.auth_mode(environ, configured=auth.claude_mode)
    out: dict[str, BaseProvider] = {}
    for key in claude_auth.provider_order(mode, prefer=auth.prefer, kdb=kdb, now=now):
        out[key] = ClaudeSDKProvider(
            key,
            subscription_source=auth.subscription_source,
            cli_path=cli_path,
            environ=environ,
        )
    provider_cfg = (models_cfg.providers or {}).get("ollama")
    if provider_cfg is not None and provider_cfg.enabled:
        base_url = None
        if detect_ollama:
            try:
                base_url = health_mod.ollama_base_url(
                    provider_cfg, kdb=kdb, client=http_client, now=now
                )
            except Exception:  # noqa: BLE001 — an unreachable local model is not an error
                base_url = None
        out["ollama"] = OllamaProvider(
            base_url,
            client=http_client,
            timeout_s=provider_cfg.timeout_s,
            keep_alive=provider_cfg.keep_alive,
            options=dict(provider_cfg.options or {}),
        )
    return out


def build_registry(models_cfg: Any, **kwargs: Any) -> ProviderRegistry:
    """The same set, wrapped in a registry the router can look keys up in."""
    reg = ProviderRegistry()
    for provider in build_providers(models_cfg, **kwargs).values():
        reg.register(provider, replace=True)
    return reg
