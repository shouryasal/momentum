"""The secret store, seen from the console: write-only, with a catalogue.

Everything here is built around one rule — **no code path returns a secret value**. The
console can say a name is present, show its last four characters, say which jobs use it,
and overwrite it. It cannot read one back, and neither can a response, a log line or an
SSE frame. The only reader of raw values is :mod:`console.services.credential_tests`,
which uses them to make a live probe and returns a redacted verdict.

The catalogue is the second half of the design. A free-form ``.env`` editor in a web UI is
a footgun; :data:`CATALOGUE` fixes which names exist, what each one is for, which jobs get
it through ``ops/envwrap.sh`` and whether it is required. A name outside the catalogue is
refused, so the console cannot be talked into writing ``PATH`` or ``LD_PRELOAD``.

``set_auth_mode`` is the one place two files move together: ``models.yaml:
auth.claude_mode`` (through :mod:`ops.config_io`, so the comments survive) and
``.env: EARN_CLAUDE_AUTH_MODE`` (which is what ``ops/envwrap.sh`` actually reads when it
builds a job's environment). Saving one without the other is how a job silently keeps the
old credential, so they are one operation with one audit row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ops import config_io, db
from ops.lib import audit, claude_auth, envfile

__all__ = [
    "AUTH_MODES",
    "CATALOGUE",
    "SecretMeta",
    "SecretsError",
    "auth_mode_state",
    "delete_secret",
    "known",
    "list_secrets",
    "set_auth_mode",
    "set_secret",
]

AUTH_MODES: tuple[str, ...] = ("subscription", "api_key", "auto")


class SecretsError(Exception):
    """A refused secret operation. The message never contains a value."""

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SecretMeta:
    """What a secret *is*. Static; the value lives only in ``.env``."""

    name: str
    label: str
    group: str
    used_by: tuple[str, ...] = ()
    required: bool = False
    help: str = ""


def _m(name: str, label: str, group: str, used_by: tuple[str, ...], **kwargs: Any) -> SecretMeta:
    return SecretMeta(name=name, label=label, group=group, used_by=used_by, **kwargs)


#: Every secret the console may touch, in display order.
CATALOGUE: tuple[SecretMeta, ...] = (
    _m("CLAUDE_CODE_OAUTH_TOKEN", "Claude subscription token", "claude",
       ("research", "review", "daily_review", "ingest", "signals", "maintenance"),
       required=True,
       help="From `claude setup-token` on this machine. The Claude Max subscription "
            "credential; envwrap gives it to every model job unless auth mode is api_key."),
    _m("ANTHROPIC_API_KEY", "Anthropic API key", "claude",
       ("research", "review", "daily_review", "ingest", "signals", "maintenance"),
       help="Metered Console key. In `auto` mode envwrap renames it to "
            "EARN_FALLBACK_ANTHROPIC_API_KEY so the CLI can never pick it up implicitly."),
    _m("TELEGRAM_BOT_TOKEN", "Telegram bot token", "telegram", ("telegram",),
       help="The ops bot. Freqtrade's own Telegram stays disabled."),
    _m("TELEGRAM_CHAT_ID", "Telegram chat id", "telegram", ("telegram",)),
    _m("FT_API_PASSWORD_A", "Freqtrade A API password", "freqtrade",
       ("console", "preflight", "reconcile")),
    _m("FT_API_PASSWORD_B", "Freqtrade B API password", "freqtrade",
       ("console", "preflight", "reconcile")),
    _m("FT_JWT_SECRET", "Freqtrade JWT secret", "freqtrade", ("console",)),
    _m("BINANCE_KEY_A", "Binance key (sleeve A)", "exchange", ("reconcile", "preflight"),
       help="Permissions: Enable Reading + Enable Spot & Margin Trading ONLY. "
            "Withdrawals disabled, IP-restricted, rotated every 90 days. Never reaches a "
            "model job — envwrap allowlists it for reconcile and preflight only."),
    _m("BINANCE_SECRET_A", "Binance secret (sleeve A)", "exchange",
       ("reconcile", "preflight")),
    _m("BINANCE_KEY_B", "Binance key (sleeve B)", "exchange", ("reconcile", "preflight")),
    _m("BINANCE_SECRET_B", "Binance secret (sleeve B)", "exchange",
       ("reconcile", "preflight")),
    _m("EARN_APPROVAL_KEY", "Proposal approval HMAC key", "console", ("telegram",),
       help="Signs proposals/approved/*.json; the in-container loader verifies it with "
            "stdlib hmac only."),
    _m("HEALTHCHECKS_URL", "Dead-man's-switch URL", "ops", ("healthcheck",)),
    _m("BACKUP_RCLONE_REMOTE", "rclone remote", "ops", ("backup",)),
)

_BY_NAME = {m.name: m for m in CATALOGUE}


def known(name: str) -> SecretMeta:
    meta = _BY_NAME.get(name)
    if meta is None:
        raise SecretsError(f"unknown secret {name!r}", reason="not_found")
    return meta


def env_path(root: Path | None = None) -> Path:
    """Where ``.env`` lives: beside the live data, not beside the source.

    In the normal checkout the state root *is* the repo root and this is the familiar
    ``<repo>/.env``. Under ``$EARN_STATE_ROOT`` — a worktree session, or a test — it
    follows the data, so a console started against an isolated root can neither read nor
    rewrite the real credentials.
    """
    from ops.lib.paths import state_root

    return (root or state_root()) / ".env"


# --------------------------------------------------------------------------- reads


def list_secrets(*, path: Path | None = None) -> list[dict[str, Any]]:
    """One row per catalogued secret: present, last4, updated_at, used-by jobs."""
    p = path or env_path()
    infos = {i.name: i for i in envfile.infos([m.name for m in CATALOGUE], path=p)}
    out: list[dict[str, Any]] = []
    for meta in CATALOGUE:
        info = infos[meta.name]
        out.append({
            "name": meta.name,
            "label": meta.label,
            "group": meta.group,
            "used_by": list(meta.used_by),
            "required": meta.required,
            "help": meta.help,
            "present": info.present,
            "last4": info.last4,
            "updated_at": info.updated_at,
        })
    return out


# --------------------------------------------------------------------------- writes


def set_secret(
    name: str,
    value: str,
    *,
    actor: str,
    path: Path | None = None,
    journal: Path | None = None,
) -> dict[str, Any]:
    """Write one catalogued secret. Returns present/last4 — never the value."""
    meta = known(name)
    if not value or not value.strip():
        raise SecretsError(f"{name}: a value is required (use delete to clear it)")
    try:
        info = envfile.set_value(name, value, path=path or env_path())
    except envfile.EnvFileError as e:
        _audit(journal, actor=actor, action="secret.set", target=name, result="denied",
               detail={"reason": str(e)})
        raise SecretsError(str(e), reason="denied") from e
    _audit(journal, actor=actor, action="secret.set", target=name, result="ok",
           detail={"last4": info.last4, "group": meta.group})
    return info.as_dict()


def delete_secret(
    name: str,
    *,
    actor: str,
    path: Path | None = None,
    journal: Path | None = None,
) -> dict[str, Any]:
    known(name)
    try:
        info = envfile.delete(name, path=path or env_path())
    except envfile.EnvFileError as e:
        _audit(journal, actor=actor, action="secret.delete", target=name, result="denied",
               detail={"reason": str(e)})
        raise SecretsError(str(e), reason="denied") from e
    _audit(journal, actor=actor, action="secret.delete", target=name, result="ok")
    return info.as_dict()


# --------------------------------------------------------------------------- auth mode


def auth_mode_state(
    *, models_path: Path | None = None, path: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """What the two sources of truth say, and whether they agree.

    ``models.yaml`` is what a human configured; ``.env`` is what ``ops/envwrap.sh`` will
    act on. When they disagree the jobs follow ``.env``, so the UI has to show both.
    """
    from ops.models_config import DEFAULT_MODELS_CONFIG, load_models_cfg

    mp = models_path or DEFAULT_MODELS_CONFIG
    try:
        configured = load_models_cfg(mp, overlay=None).auth
        configured_mode: str | None = configured.claude_mode
        source = configured.subscription_source
    except Exception:  # noqa: BLE001 - a broken models.yaml is the config page's problem
        configured_mode, source = None, "token"
    env_mode = envfile.value_of(claude_auth.AUTH_MODE_VAR, path=path or env_path())
    effective = claude_auth.auth_mode({claude_auth.AUTH_MODE_VAR: env_mode or ""},
                                      configured=configured_mode or "subscription")
    session = claude_auth.login_session(home=home)
    return {
        "configured": configured_mode,
        "env": env_mode,
        "effective": effective,
        "in_sync": (env_mode or configured_mode) == configured_mode,
        "subscription_source": source,
        "login_session": {
            "present": session.present,
            "expires_at": session.expires_at,
            "expired": session.expired,
            "subscription_type": session.subscription_type,
        },
    }


def set_auth_mode(
    mode: Literal["subscription", "api_key", "auto"],
    *,
    actor: str,
    models_path: Path | None = None,
    path: Path | None = None,
    journal: Path | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move ``models.yaml`` and ``.env`` together. One operation, one audit trail."""
    if mode not in AUTH_MODES:
        raise SecretsError(f"unknown auth mode {mode!r}; expected one of {AUTH_MODES}")
    from ops.models_config import DEFAULT_MODELS_CONFIG

    mp = Path(models_path or DEFAULT_MODELS_CONFIG)
    doc = config_io.read_document(mp, rel="config/models.yaml")
    before_sha = doc.sha
    try:
        result = config_io.apply_patch(
            doc, [{"op": "replace", "path": "/auth/claude_mode", "value": mode}])
    except config_io.PatchError as e:
        raise SecretsError(f"could not update models.yaml: {e}", reason="failed") from e
    config_io.atomic_write(mp, result.text)
    try:
        envfile.set_value(claude_auth.AUTH_MODE_VAR, mode, path=path or env_path())
    except envfile.EnvFileError as e:
        config_io.atomic_write(mp, doc.text)      # keep the two files in step
        raise SecretsError(str(e), reason="denied") from e

    _audit(journal, actor=actor, action="secret.auth_mode", target=mode, result="ok",
           detail={"changed": result.changed})
    _audit_config(journal, actor=actor, before_sha=before_sha, after_sha=result.sha,
                  changed=result.changed,
                  diff=config_io.diff_text(doc.text, result.text, rel="config/models.yaml"),
                  reason=reason)
    return auth_mode_state(models_path=mp, path=path)


# --------------------------------------------------------------------------- audit


def _audit(journal: Path | None, **kwargs: Any) -> None:
    if journal is None or not Path(journal).exists():
        return
    try:
        with db.opened(journal) as conn:
            audit.try_record(conn, **kwargs)
    except Exception:  # noqa: BLE001 - auditing never vetoes the action it records
        pass


def _audit_config(journal: Path | None, *, actor: str, before_sha: str, after_sha: str,
                  changed: list[str], diff: str, reason: str | None) -> None:
    if journal is None or not Path(journal).exists():
        return
    try:
        with db.opened(journal) as conn:
            audit.record_config(
                conn, actor=actor, file="config/models.yaml", before_sha=before_sha,
                after_sha=after_sha, changed_paths=changed, diff=diff, reason=reason,
                protected_changed=False, effects=["crontab"], applied=True,
            )
    except Exception:  # noqa: BLE001
        pass
