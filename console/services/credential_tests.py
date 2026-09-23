"""Live credential probes: "does this key actually work", answered without echoing it.

Nine targets, one shape: ``CredentialTest(ok, detail, latency_ms)`` where ``detail`` has
already been through :func:`console.security.redact`. A probe is allowed to read a value
out of ``.env`` (that is the point) but nothing it returns may contain one, and every
probe is bounded by a short timeout — a hung exchange must not hang the UI.

What each probe actually proves:

``claude_subscription``  a ``CLAUDE_CODE_OAUTH_TOKEN`` exists and the CLI accepts it —
                         one minimal turn through the provider, so a revoked token shows
                         up here rather than at 08:30 in a research run.
``claude_login``         ``~/.claude/.credentials.json`` exists and has not expired.
``claude_api_key``       ``models.list(limit=1)`` on the Anthropic API — cheap, metered
                         at zero, and it distinguishes "wrong key" from "no credit".
``ollama``               ``GET /api/version`` on the detected endpoint, plus the model
                         list, so the page can say *which* local models exist.
``telegram``             ``getMe`` on the bot token.
``binance_a|b``          public server time plus a **signed** account call, which is the
                         only thing that proves the key/secret pair and its permissions.
                         Read-only: nothing here places or cancels an order.
``freqtrade_a|b``        ``/ping`` then ``/show_config`` on the local bot API.

Every network client is injectable, so the tests exercise the branching without a socket.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from console import security
from ops.lib import claude_auth, envfile

__all__ = ["TARGETS", "CredentialTest", "run_test"]

#: Every probe the console exposes. The UI renders a Test button per row from this list.
TARGETS: tuple[str, ...] = (
    "claude_subscription", "claude_login", "claude_api_key", "ollama", "telegram",
    "binance_a", "binance_b", "freqtrade_a", "freqtrade_b",
)

DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CredentialTest:
    target: str
    ok: bool
    detail: str
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        # `detail` was already redacted in run_test, with the values this probe read as
        # extra sentinels; this pass catches anything constructed by another caller.
        return {"target": self.target, "ok": self.ok,
                "detail": security.redact(self.detail), "latency_ms": self.latency_ms}


def run_test(
    target: str,
    *,
    cfg: Any = None,
    models_cfg: Any = None,
    env_file: Path | None = None,
    kdb: Any = None,
    client: Any = None,
    provider_factory: Callable[..., Any] | None = None,
    home: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CredentialTest:
    """Run one probe. Never raises: a failure is a result, not an exception."""
    if target not in TARGETS:
        return CredentialTest(target=target, ok=False, detail=f"unknown target {target!r}")
    started = time.monotonic()
    ctx = _Ctx(cfg=cfg, models_cfg=models_cfg, env_file=env_file, kdb=kdb,
               client=client, provider_factory=provider_factory, home=home,
               timeout_s=timeout_s)
    try:
        ok, detail = _PROBES[target](ctx)
    except Exception as e:  # noqa: BLE001 - a probe failure is information, not a crash
        ok, detail = False, f"{type(e).__name__}: {e}"
    # Every value this probe read becomes a redaction sentinel: an upstream error that
    # quotes the credential back at us must not carry it into the response.
    detail = security.redact(detail, extra=ctx.seen)
    return CredentialTest(target=target, ok=ok, detail=detail,
                          latency_ms=int((time.monotonic() - started) * 1000))


@dataclass
class _Ctx:
    cfg: Any = None
    models_cfg: Any = None
    env_file: Path | None = None
    kdb: Any = None
    client: Any = None
    provider_factory: Callable[..., Any] | None = None
    home: Path | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    seen: list[str] = field(default_factory=list)

    def value(self, name: str) -> str:
        """Read one credential — and remember it, so the verdict can be redacted."""
        value = envfile.value_of(name, path=self.env_file) or ""
        if value and value not in self.seen:
            self.seen.append(value)
        return value

    def http(self):
        if self.client is not None:
            return self.client, False
        import httpx

        return httpx.Client(timeout=self.timeout_s), True


# --------------------------------------------------------------------------- claude


def _probe_claude_subscription(ctx: _Ctx) -> tuple[bool, str]:
    token = ctx.value(claude_auth.OAUTH_VAR)
    if not token:
        return False, "CLAUDE_CODE_OAUTH_TOKEN is not set (run `claude setup-token`)"
    factory = ctx.provider_factory
    if factory is None:
        from runs.llm.providers.claude_sdk import ClaudeSDKProvider

        factory = ClaudeSDKProvider
    provider = factory(claude_auth.PROVIDER_SUBSCRIPTION,
                       environ={claude_auth.OAUTH_VAR: token})
    return _one_turn(provider, ctx, "subscription")


def _probe_claude_api_key(ctx: _Ctx) -> tuple[bool, str]:
    key = ctx.value(claude_auth.API_KEY_VAR)
    if not key:
        return False, "ANTHROPIC_API_KEY is not set"
    factory = ctx.provider_factory
    if factory is None:
        return _anthropic_models_list(key, ctx)
    provider = factory(claude_auth.PROVIDER_API_KEY,
                       environ={claude_auth.API_KEY_VAR: key})
    return _one_turn(provider, ctx, "api_key")


def _anthropic_models_list(key: str, ctx: _Ctx) -> tuple[bool, str]:
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=ctx.timeout_s)
    page = client.models.list(limit=1)
    names = [getattr(m, "id", "?") for m in getattr(page, "data", [])]
    return True, f"API key accepted; catalogue reachable ({names[0] if names else 'empty'})"


def _one_turn(provider: Any, ctx: _Ctx, label: str) -> tuple[bool, str]:
    """One minimal, schema-constrained turn. Cheap, and it proves the credential."""
    from runs.llm.types import LLMRequest, ModelRef

    alias, model_id = _cheapest_claude(ctx.models_cfg)
    req = LLMRequest(
        task="playground",
        prompt='Reply with exactly {"ok": true} and nothing else.',
        model=ModelRef(alias=alias, provider="claude", model_id=model_id, tier=2),
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                       "required": ["ok"]},
        tools_profile="none", max_turns=1, max_usd=0.05,
        deadline_s=min(ctx.timeout_s * 3, 60.0),
    )
    result = provider.run(req)
    served = result.meta.served_model or model_id
    return True, f"{label}: one turn served by {served}"


def _cheapest_claude(models_cfg: Any) -> tuple[str, str]:
    """The lowest-tier Claude alias declared, so a probe costs as little as possible."""
    default = ("haiku", "claude-haiku-4-5-20251001")
    if models_cfg is None:
        return default
    candidates = [
        (entry.tier, alias, entry.id)
        for alias, entry in (models_cfg.models or {}).items()
        if entry is not None and entry.provider != "ollama"
    ]
    if not candidates:
        return default
    _, alias, model_id = min(candidates)
    return alias, model_id


def _probe_claude_login(ctx: _Ctx) -> tuple[bool, str]:
    session = claude_auth.login_session(home=ctx.home)
    if not session.present:
        return False, f"no CLI login session at {claude_auth.credentials_path(ctx.home)}"
    if session.error:
        return False, f"credentials file present but unreadable ({session.error})"
    if session.expired:
        return False, f"login session expired at {session.expires_at}; run `claude login`"
    plan = session.subscription_type or "unknown plan"
    return True, f"login session valid until {session.expires_at} ({plan})"


# --------------------------------------------------------------------------- ollama


def _probe_ollama(ctx: _Ctx) -> tuple[bool, str]:
    from ops.lib import ollama as ollama_lib
    from runs.llm import health as health_mod

    provider_cfg = (getattr(ctx.models_cfg, "providers", None) or {}).get("ollama")
    if provider_cfg is None:
        return False, "providers.ollama is not declared in models.yaml"
    base_url = health_mod.ollama_base_url(provider_cfg, kdb=ctx.kdb, client=ctx.client,
                                          force=True)
    if not base_url:
        return False, ("no Ollama endpoint answered; see the WSL networking guidance on "
                       "this page")
    models = ollama_lib.list_models(base_url, client=ctx.client, timeout=ctx.timeout_s)
    names = ", ".join(str(m["name"]) for m in models[:5]) or "no models pulled"
    return True, f"{base_url} — {names}"


# --------------------------------------------------------------------------- telegram


def _probe_telegram(ctx: _Ctx) -> tuple[bool, str]:
    token = ctx.value("TELEGRAM_BOT_TOKEN")
    chat = ctx.value("TELEGRAM_CHAT_ID")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN is not set"
    client, owned = ctx.http()
    try:
        resp = client.get(f"https://api.telegram.org/bot{token}/getMe",
                          timeout=ctx.timeout_s)
        payload = resp.json() if resp.status_code == 200 else {}
    finally:
        if owned:
            client.close()
    if not payload.get("ok"):
        return False, f"getMe failed (HTTP {resp.status_code})"
    username = (payload.get("result") or {}).get("username", "?")
    suffix = "" if chat else "; TELEGRAM_CHAT_ID is not set"
    return bool(chat), f"bot @{username} reachable{suffix}"


# --------------------------------------------------------------------------- binance


def _probe_binance(ctx: _Ctx, sleeve: str) -> tuple[bool, str]:
    key = ctx.value(f"BINANCE_KEY_{sleeve.upper()}")
    secret = ctx.value(f"BINANCE_SECRET_{sleeve.upper()}")
    if not key or not secret:
        return False, f"BINANCE_KEY_{sleeve.upper()}/SECRET is not set"
    base = "https://api.binance.com"
    client, owned = ctx.http()
    try:
        query = urllib.parse.urlencode(
            {"timestamp": int(time.time() * 1000), "recvWindow": 5000})
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        resp = client.get(f"{base}/api/v3/account?{query}&signature={signature}",
                          headers={"X-MBX-APIKEY": key}, timeout=ctx.timeout_s)
        if resp.status_code != 200:
            body = (resp.json() or {}) if resp.headers.get("content-type", "").startswith(
                "application/json") else {}
            return False, (f"account call refused (HTTP {resp.status_code}"
                           f"{', ' + str(body.get('msg')) if body.get('msg') else ''})")
        account = resp.json() or {}
    finally:
        if owned:
            client.close()
    perms = account.get("permissions") or []
    can_trade = bool(account.get("canTrade"))
    can_withdraw = bool(account.get("canWithdraw"))
    problems = []
    if not can_trade:
        problems.append("spot trading is NOT enabled")
    if can_withdraw:
        problems.append("WITHDRAWALS ARE ENABLED — revoke that permission")
    detail = f"account uid {account.get('uid', '?')}, permissions {perms or '[]'}"
    if problems:
        return False, f"{detail}; {'; '.join(problems)}"
    return True, detail


# --------------------------------------------------------------------------- freqtrade


def _probe_freqtrade(ctx: _Ctx, sleeve: str) -> tuple[bool, str]:
    if ctx.cfg is None:
        return False, "config/earn.yaml is not loaded"
    from ops.lib.freqtrade_api import BotApi

    factory = ctx.provider_factory or BotApi.for_sleeve
    bot = factory(ctx.cfg, sleeve)
    ping = bot.ping() if hasattr(bot, "ping") else None
    config = bot.show_config() if hasattr(bot, "show_config") else {}
    dry = config.get("dry_run")
    strategy = config.get("strategy")
    state = config.get("state")
    return True, (f"ping {ping or 'ok'}; strategy {strategy}, state {state}, "
                  f"dry_run {dry}")


_PROBES: dict[str, Callable[[_Ctx], tuple[bool, str]]] = {
    "claude_subscription": _probe_claude_subscription,
    "claude_login": _probe_claude_login,
    "claude_api_key": _probe_claude_api_key,
    "ollama": _probe_ollama,
    "telegram": _probe_telegram,
    "binance_a": lambda ctx: _probe_binance(ctx, "a"),
    "binance_b": lambda ctx: _probe_binance(ctx, "b"),
    "freqtrade_a": lambda ctx: _probe_freqtrade(ctx, "a"),
    "freqtrade_b": lambda ctx: _probe_freqtrade(ctx, "b"),
}
