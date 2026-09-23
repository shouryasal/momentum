"""Wiring for ``ops.preflight``: real probes, and the short-lived preflight cache.

``ops/preflight.py`` is deliberately probe-free — it takes callables and returns a verdict.
This module is where those callables become real: the bots' REST API, Binance's signed
endpoints, the host checks P1 owns, git, the drift check and the backup destination. When a
probe cannot be built (no key in the environment, no docker on this host), the callable is
simply left out and the corresponding item fails with "no … probe available", which is the
honest answer.

A result lives for ``ops.preflight.PREFLIGHT_TTL_MINUTES`` in an in-process cache keyed by
``preflight_id``. It is deliberately **not** persisted: a preflight is evidence about this
moment, and a console restart should force a fresh one rather than let a ten-minute-old
snapshot authorise a go-live. The transition re-runs the checks anyway (step 2) — the cache
only carries the operator's request and the baseline balances between the two calls.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import db
from ops import preflight as pf
from ops.config import EarnConfig
from ops.lib import binance_check, paths
from ops.lib import mode_state as ms

CACHE_LIMIT = 20


# --------------------------------------------------------------------------- cache


@dataclass
class _Entry:
    result: pf.PreflightResult
    created: datetime


class PreflightCache:
    """In-process, TTL-bounded store of recent preflight results."""

    def __init__(self, *, limit: int = CACHE_LIMIT) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._limit = limit

    def put(self, result: pf.PreflightResult, *, now: datetime | None = None) -> None:
        with self._lock:
            self._entries[result.preflight_id] = _Entry(result, now or datetime.now(UTC))
            while len(self._entries) > self._limit:
                oldest = min(self._entries, key=lambda k: self._entries[k].created)
                self._entries.pop(oldest, None)

    def get(
        self, preflight_id: str, *, now: datetime | None = None
    ) -> pf.PreflightResult | None:
        with self._lock:
            entry = self._entries.get(preflight_id)
        if entry is None:
            return None
        if entry.result.expired(now):
            with self._lock:
                self._entries.pop(preflight_id, None)
            return None
        return entry.result

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


#: the console's single cache; ``console.routers.mode`` and the transition share it
cache = PreflightCache()


# --------------------------------------------------------------------------- probes


def bot_status_probe(cfg: EarnConfig, factory: Any) -> Any:
    """``sleeve -> {up, strategy, dry_run, state}`` from ``/ping`` and ``/show_config``."""

    def probe(sleeve: str) -> dict[str, Any]:
        bot = factory(cfg, sleeve)
        info: dict[str, Any] = {"up": bool(bot.ping())}
        if not info["up"]:
            return info
        getter = getattr(bot, "_get", None)
        show: Mapping[str, Any] = {}
        if callable(getter):
            try:
                show = getter("show_config") or {}
            except Exception:  # noqa: BLE001 - an older bot without the endpoint
                show = {}
        info.update(
            {
                "strategy": show.get("strategy"),
                "dry_run": show.get("dry_run"),
                "state": show.get("state"),
                "bot_name": show.get("bot_name"),
            }
        )
        return info

    return probe


def _client(sleeve: str, env: Mapping[str, str] | None = None) -> Any:
    keys = binance_check.keys_for(sleeve, env)
    if not keys.present:
        raise binance_check.BinanceCheckError(f"{keys.key_env} is not set")
    return binance_check.BinanceClient(keys)


def exchange_probes(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The three Binance probes, each built lazily so a missing key fails one item only."""

    def restrictions(sleeve: str) -> dict[str, Any]:
        return _client(sleeve, env).api_restrictions()

    def account(sleeve: str) -> dict[str, Any]:
        return _client(sleeve, env).account()

    def info(pairs: Sequence[str]) -> dict[str, Any]:
        for sleeve in paths.SLEEVES:
            try:
                return _client(sleeve, env).exchange_info(
                    [binance_check.symbol_of(p) for p in pairs]
                )
            except binance_check.BinanceCheckError:
                continue
        raise binance_check.BinanceCheckError("no usable exchange credentials for a public probe")

    return {
        "exchange_restrictions": restrictions,
        "exchange_account": account,
        "exchange_info": info,
    }


def host_facts_probe(cfg: EarnConfig | None = None) -> pf.HostFacts:
    """Preflight check 13 (``host_ready``), from ``console.services.host_checks``.

    ``host_checks`` is the one place that actually shells out to ``findmnt``,
    ``powercfg.exe``, ``schtasks.exe``, ``docker`` and ``timedatectl``, and it reports a
    list of ``HostCheck`` rows; ``pf.HostFacts`` is the flat shape the preflight reasons
    about. This is the adapter between them.

    ``None`` means "could not determine", and the preflight treats that differently from
    ``False`` for the two policy checks (`sleep_on_ac`, `keepalive_task`) — so a check
    that came back ``warn`` because interop is disabled must map to ``None``, never to a
    confident ``True``. An earlier version of this function looked for module-level names
    that ``host_checks`` never had, so every fact was ``None`` and the blocking check
    failed with "docker is not running" on a host where docker was fine.
    """
    facts = pf.HostFacts()
    try:
        from console.services import host_checks
    except Exception:  # noqa: BLE001 - a broken probe is "unknown", not a crash
        return facts

    try:
        rows = {check.name: check for check in host_checks.collect(cfg)}
    except Exception:  # noqa: BLE001
        return facts

    filesystem = rows.get("filesystem")
    if filesystem is not None:
        facts.filesystem = str(filesystem.data.get("fstype") or "") or None

    def tri(name: str) -> bool | None:
        """``ok`` ⇒ True, ``fail`` ⇒ False, ``warn`` (could not read) ⇒ unknown."""
        check = rows.get(name)
        if check is None:
            return None
        if check.status == host_checks.OK:
            return True
        return False if check.status == host_checks.FAIL else None

    facts.sleep_on_ac_disabled = tri("sleep_ac")
    facts.keepalive_task = tri("keepalive_task")
    facts.docker_running = tri("docker")

    ntp = rows.get("ntp")
    if ntp is not None:
        # host_checks reports synchronisation, not a skew in seconds; a synced clock is
        # 0s of skew for this purpose and an unsynced one is deliberately left unknown,
        # because "unsynchronised" is a warning, not a measured drift.
        facts.ntp_skew_s = 0.0 if ntp.status == host_checks.OK else None

    facts.cron_installed, facts.cron_matches = _cron_facts(cfg)
    return facts


def _cron_facts(cfg: EarnConfig | None) -> tuple[bool | None, bool | None]:
    """``(installed, matches the rendered crontab)`` — unknown on any failure."""
    try:
        from ops import gen_ops_files as gen

        installed = gen.installed_crontab()
        if not installed.strip():
            return False, False
        _rendered, diff = gen.crontab_diff(cfg)
        return True, not diff.strip()
    except Exception:  # noqa: BLE001 - no crontab binary, no config: unknown
        return None, None


def telegram_probe(cfg: EarnConfig, *, send: Any = None) -> Any:
    """Deliver one test message; approvals and alerts both ride this channel."""

    def probe() -> tuple[bool, str]:
        from ops.lib import tg

        sender = send or tg.send
        ok = sender(
            "Earn preflight: Telegram reachable.",
            "info",
            dedupe_key=None,
        )
        return bool(ok), "test message delivered" if ok else "test message not delivered"

    return probe


def strategy_tests_probe(root: Path) -> Any:
    """``pytest tests/strategies -q`` — a warning in PROPOSE, blocking for EXECUTE."""

    def probe() -> tuple[bool, str]:
        import subprocess
        import sys

        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "tests/strategies", "-q", "-p", "no:cacheprovider"],
            cwd=str(root), capture_output=True, text=True, timeout=900, check=False,
        )
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        return proc.returncode == 0, tail[-1] if tail else f"exit {proc.returncode}"

    return probe


# --------------------------------------------------------------------------- deps


def build_deps(
    cfg: EarnConfig,
    *,
    root: Path,
    bot_factory: Any,
    journal_path: Path | None = None,
    knowledge_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    state: ms.ModeState | None = None,
    now: datetime | None = None,
    jdb: Any = None,
    kdb: Any = None,
) -> pf.PreflightDeps:
    """Assemble the real :class:`ops.preflight.PreflightDeps` for this console process."""
    deps = pf.PreflightDeps(
        jdb=jdb,
        kdb=kdb,
        root=root,
        env=env,
        state=state if state is not None else ms.load(env=env),
        now=now,
        bot_status=bot_status_probe(cfg, bot_factory),
        host_facts=lambda: host_facts_probe(cfg),
        git_status=lambda: pf.default_git_status(root),
        drift_check=lambda: pf.default_drift_check(root),
        telegram_probe=telegram_probe(cfg),
        strategy_tests=strategy_tests_probe(root),
        backup_status=lambda: pf.default_backup_status(cfg),
        envwrap_allowlist=lambda: _envwrap_text(root),
        agent_user_ok=lambda: _agent_user_ok(cfg),
    )
    for name, probe in exchange_probes(env).items():
        setattr(deps, name, probe)
    return deps


def _envwrap_text(root: Path) -> str:
    try:
        return (root / "ops" / "envwrap.sh").read_text(encoding="utf-8")
    except OSError:
        return ""


def _agent_user_ok(cfg: EarnConfig) -> tuple[bool, str]:  # pragma: no cover - shells out
    import subprocess

    user = cfg.security.agent_user
    if not user:
        return False, "security.agent_user is not configured"
    proc = subprocess.run(  # noqa: S603
        ["sudo", "-n", "-u", user, "true"], capture_output=True, text=True, timeout=20, check=False
    )
    return proc.returncode == 0, (proc.stderr or "ok").strip()[:200]


# --------------------------------------------------------------------------- entry point


def run(
    cfg: EarnConfig,
    request: pf.PreflightRequest,
    *,
    root: Path,
    bot_factory: Any,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    store: PreflightCache | None = None,
    deps: pf.PreflightDeps | None = None,
) -> pf.PreflightResult:
    """Run a preflight with real (or injected) probes and cache the result."""
    journal = db.journal_path(cfg)
    knowledge = db.knowledge_path(cfg)
    if deps is not None:
        result = pf.run_preflight(cfg, request, deps=deps)
        (store or cache).put(result, now=now)
        return result

    jdb = kdb = None
    try:
        if journal.exists():
            jdb = db.connect(journal, readonly=True)
        if knowledge.exists():
            kdb = db.connect(knowledge, readonly=True)
        built = build_deps(
            cfg, root=root, bot_factory=bot_factory, env=env, now=now, jdb=jdb, kdb=kdb
        )
        result = pf.run_preflight(cfg, request, deps=built)
    finally:
        for conn in (jdb, kdb):
            if conn is not None:
                conn.close()
    (store or cache).put(result, now=now)
    return result


__all__ = [
    "CACHE_LIMIT",
    "PreflightCache",
    "bot_status_probe",
    "build_deps",
    "cache",
    "exchange_probes",
    "host_facts_probe",
    "run",
    "strategy_tests_probe",
    "telegram_probe",
]
