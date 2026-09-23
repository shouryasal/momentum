"""Fixtures for the mode machine: an isolated state root, fake bots and a fake docker.

Every test here runs with ``$EARN_STATE_ROOT`` pointing at a temporary directory, so the
signed mode file, the ops lock, ``var/runtime`` and the kill file are all throwaway. The
repo checkout is still the source of the compose template and the committed config, which
is exactly the split ``ops.lib.compose`` makes between ``source_root`` and ``root``.

Nothing in here opens a socket or runs docker: :class:`FakeBot` answers the freqtrade REST
calls the transition makes, and :class:`FakeDocker` records the argv it was handed.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ops import db, modes
from ops import preflight as pf
from ops.config import load_config
from ops.lib import compose as composelib
from ops.lib import mode_state as ms
from ops.lib import paths, signing

SECRET = "mode-tests-secret-0123456789abcdef"
APPROVAL_SECRET = "approval-tests-secret-0123456789"
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fakes


class FakeBot:
    """The freqtrade endpoints a transition touches, scriptable per test."""

    def __init__(
        self,
        *,
        up: bool = True,
        dry_run: bool = True,
        strategy: str = "SleeveA",
        bot_name: str | None = None,
        trades: list[dict[str, Any]] | None = None,
        stoploss_on_exchange: bool = True,
        fail: str | None = None,
    ) -> None:
        self.up = up
        self.dry_run = dry_run
        self.strategy = strategy
        self.bot_name = bot_name
        self.trades = trades or []
        self.stoploss_on_exchange = stoploss_on_exchange
        self.fail = fail
        self.calls: list[str] = []
        self.db_url: str | None = None

    def _maybe_fail(self, name: str) -> None:
        """``fail`` is a comma-separated list of call names that should blow up."""
        if self.fail and name in {f.strip() for f in self.fail.split(",")}:
            raise RuntimeError(f"fake bot failure in {name}")

    def ping(self) -> bool:
        return self.up

    def health(self) -> dict[str, Any] | None:
        return {"last_process_ts": 1} if self.up else None

    def _get(self, path: str) -> Any:
        self.calls.append(f"get:{path}")
        self._maybe_fail(f"get:{path}")
        if path == "show_config":
            return {
                "dry_run": self.dry_run,
                "strategy": self.strategy,
                "bot_name": self.bot_name,
                "db_url": self.db_url,
                "order_types": {"stoploss_on_exchange": self.stoploss_on_exchange},
            }
        if path == "locks":
            return {"locks": []}
        return {}

    def _post(self, path: str, payload: dict | None = None) -> Any:
        self.calls.append(f"post:{path}")
        self._maybe_fail(f"post:{path}")
        return {"status": path}

    def _delete(self, path: str) -> Any:
        self.calls.append(f"delete:{path}")
        self._maybe_fail(f"delete:{path}")
        return {"status": "deleted"}

    def stopbuy(self) -> dict[str, Any]:
        return self._post("stopbuy")

    def status(self) -> list[dict[str, Any]]:
        self.calls.append("status")
        self._maybe_fail("status")
        return list(self.trades)

    def balance(self) -> dict[str, Any]:
        return {"total": 10000.0, "currencies": [{"currency": "USDT", "free": 10000.0}]}

    def profit(self) -> dict[str, Any]:
        return {"profit_closed_coin": 0.0}

    def cancel_open_order(self, trade_id: int) -> dict[str, Any]:
        return self._delete(f"trades/{trade_id}/open-order")

    def forceexit(self, tradeid: str = "all") -> dict[str, Any]:
        self.calls.append(f"forceexit:{tradeid}")
        self._maybe_fail("forceexit")
        self.trades = []
        return {"status": "exiting"}


class FakeDocker:
    """A compose runner that records argv and can be told to fail."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, timeout_s: float
    ) -> composelib.CommandResult:
        self.calls.append(list(argv))
        if not self.ok:
            return composelib.CommandResult(list(argv), 1, "", "docker: no such service")
        return composelib.CommandResult(list(argv), 0, "recreated", "")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def cfg():  # noqa: ANN201 - EarnConfig
    return load_config()


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(root))
    monkeypatch.setenv(signing.SECRET_ENV, SECRET)
    monkeypatch.setenv(signing.APPROVAL_SECRET_ENV, APPROVAL_SECRET)
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    paths.ensure_var_layout()
    (root / "ops" / "locks").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "killdir").mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def jdb(cfg, state_root: Path) -> Iterator[Any]:
    journal, _knowledge = db.init_all(cfg, root=state_root)
    conn = db.connect(journal)
    yield conn
    conn.close()


@pytest.fixture
def kdb(cfg, state_root: Path) -> Iterator[Any]:
    _journal, knowledge = db.init_all(cfg, root=state_root)
    conn = db.connect(knowledge)
    yield conn
    conn.close()


@pytest.fixture
def bots() -> dict[str, FakeBot]:
    return {
        "a": FakeBot(strategy="SleeveA", bot_name="earn-a-test"),
        "b": FakeBot(strategy="SleeveB", bot_name="earn-b-test"),
    }


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def deps(cfg, jdb, state_root: Path, bots, docker) -> modes.TransitionDeps:
    """Transition deps wired to the fakes, with a passing preflight and no reconcile gap."""

    def bot(_cfg, sleeve: str) -> modes.BotControl:
        return modes.BotControl(bots[sleeve])

    def passing_preflight(request: pf.PreflightRequest) -> pf.PreflightResult:
        return pf.PreflightResult(
            preflight_id="pf-test",
            request=request,
            items=[pf.Check(cid, title, blocking, pf.PASS, "ok")
                   for cid, title, blocking in pf.CHECK_ORDER],
            created_utc="2026-10-27T05:00:00Z",
            expires_utc="2099-01-01T00:00:00Z",
            baseline={"BTC": 0.0},
        )

    return modes.TransitionDeps(
        jdb=jdb,
        root=state_root,
        secret=SECRET,
        bot=bot,
        compose_runner=docker,
        preflight=passing_preflight,
        reconcile=lambda sleeve, run_id: ("ok", "ledger matches exchange"),
        now=lambda: NOW,
        sleep=lambda _s: None,
        verify_timeout_s=1.0,
        flatten_timeout_s=1.0,
        lock_timeout_s=0.5,
    )


@pytest.fixture
def actor() -> modes.HumanActor:
    return modes.HumanActor.console("sid-1", step_up_ok=True)


def write_mode(sleeves: dict[str, ms.SleeveState], *, set_by: str = "human:cli") -> ms.ModeState:
    """Sign and write a mode file for the isolated state root."""
    built = ms.build(sleeves, set_by=set_by)
    ms.write(built, secret=SECRET)
    return built


def seed_run(
    conn: Any,
    cfg,
    *,
    run_id: str,
    sleeve: str = "a",
    mode: str = "test",
    seed: float = 10000.0,
    started: str = "2026-07-01T00:00:00Z",
    submode: str | None = None,
) -> str:
    return modes.open_run(
        conn, cfg, run_id=run_id, sleeve=sleeve, mode=mode, submode=submode,
        seed_usdt=seed, started_utc=started,
    )
