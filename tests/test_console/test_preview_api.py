"""The preview: the real decision path, and the four writes that are stubbed.

The value of "what would it do right now" is entirely in it being the *same* path. A mock
would answer the question the owner is not asking. So what these tests hold are the two
halves of that claim:

* it really runs the decision — the same prompt builder, the same routing, the same
  read-only tool set, the same schema check, and the real risk gate loaded from the real
  ``config/riskgate.json``;
* and it really writes nothing — no plan file, no ``proposals`` row, no ``runs`` row, and
  not one byte into the gate's own counters, which it reads from the live journal.

Plus the two things that keep it from being expensive: a cost ceiling and a rate limit that
survives a restart because the marker is a file.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from console.services import preview_service
from console.services.preview_service import PreviewError
from ops import db
from ops.lib import paths

REPO = Path(__file__).resolve().parents[2]
CONFIG_FILES = (
    "earn.yaml", "models.yaml", "backtest.yaml", "macro_calendar.yaml",
    "params-sleeve-a.json", "params-sleeve-b.json", "freqtrade-a.json", "freqtrade-b.json",
    "riskgate.json",
)


@dataclass
class FakeMeta:
    cost_usd: float = 0.31
    served_model: str = "claude-opus-5"
    applied_effort: str = "high"
    error: str | None = None
    subtype: str | None = None


@dataclass
class FakeResult:
    ok: bool
    text: str
    meta: FakeMeta = field(default_factory=FakeMeta)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    for name in CONFIG_FILES:
        shutil.copy(REPO / "config" / name, root / "config" / name)
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(root))
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    return root


@pytest.fixture
def cfg(repo: Path):  # noqa: ANN201
    from ops.config import load_config

    return load_config(repo / "config" / "earn.yaml")


def _plan(run_id: str, snapshot: dict[str, str], *, abstain: bool = False) -> str:
    """A schema-conforming plan — the same shape the live decision stage has to return."""
    return json.dumps({
        "schema_version": 4,
        "run_id": run_id,
        "prompt_version": "research.v4",
        "module": "hold" if abstain else "trend",
        "targets": {"USDT": 1.0} if abstain else {"BTC": 0.6, "USDT": 0.4},
        "exposure_scale": 0.0 if abstain else 1.0,
        "confidence": 0.2 if abstain else 0.6,
        "abstain": abstain,
        "horizon_days": 7,
        "rationale": ["The newest candle is a day old." if abstain
                      else "Trend is intact on the daily."],
        "invalidation": "a daily close below the fifty day average",
        "universe_snapshot": dict(snapshot),
    })


def _runner(text_for, calls: list[dict[str, Any]]):  # noqa: ANN001, ANN202
    def run(prompt: str, **kwargs: Any) -> FakeResult:
        calls.append({"prompt": prompt, **kwargs})
        return FakeResult(ok=True, text=text_for(prompt))
    return run


def _run_id_from(prompt: str) -> str:
    """The RUN header the builder put in the prompt — what the plan must echo back."""
    import re

    match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+04:00)", prompt)
    assert match, "the prompt carries no run id"
    return match.group(1)


# --------------------------------------------------------------------------- the budget


def test_a_preview_is_capped_below_a_real_run(cfg, repo: Path):
    bud = preview_service.budget(cfg, root=repo)
    assert bud["max_usd"] <= preview_service.MAX_PREVIEW_USD
    assert bud["ready"] is True
    assert bud["min_interval_s"] == preview_service.MIN_INTERVAL_S


def test_the_rate_limit_survives_a_restart_because_the_marker_is_a_file(cfg, repo: Path):
    now = datetime.now(UTC)
    preview_service._write_state({"started_utc": preview_service._iso(now)}, repo)

    # A brand-new process reads the same marker off disk.
    bud = preview_service.budget(cfg, root=repo, now=now + timedelta(seconds=30))
    assert bud["ready"] is False
    assert bud["wait_s"] > 0
    with pytest.raises(PreviewError) as excinfo:
        preview_service.run_preview(cfg, root=repo, now=now + timedelta(seconds=30),
                                    stage_runner=lambda *a, **k: pytest.fail("called"))
    assert excinfo.value.code == "rate_limited"


def test_the_rate_limit_lets_go_once_the_window_passes(cfg, repo: Path):
    now = datetime.now(UTC)
    preview_service._write_state({"started_utc": preview_service._iso(now)}, repo)
    later = now + timedelta(seconds=preview_service.MIN_INTERVAL_S + 1)
    assert preview_service.budget(cfg, root=repo, now=later)["ready"] is True


def test_a_failed_preview_still_spends_its_slot(cfg, repo: Path, monkeypatch):
    """Otherwise a failing preview can be retried in a loop at full model price."""
    db.init_all(cfg, repo)
    calls: list[dict[str, Any]] = []

    def boom(*_args: Any, **_kwargs: Any) -> FakeResult:
        calls.append({})
        return FakeResult(ok=False, text="", meta=FakeMeta(error="the model timed out"))

    now = datetime.now(UTC)
    with db.opened(db.journal_path(cfg, repo)) as jdb:
        out = preview_service.run_preview(cfg, jdb=jdb, root=repo, now=now, stage_runner=boom)
    assert out["ok"] is False
    assert "timed out" in str(out["error"])
    assert preview_service.budget(cfg, root=repo, now=now + timedelta(seconds=5))["ready"] is False


# --------------------------------------------------------------------------- the real path


@pytest.fixture
def journal(cfg, repo: Path):  # noqa: ANN201
    db.init_all(cfg, repo)
    return db.journal_path(cfg, repo)


def _preview(cfg, repo: Path, journal: Path, *, abstain: bool = False,
             calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    recorded = calls if calls is not None else []
    snapshot = dict(cfg.universe.snapshot_ref)
    runner = _runner(
        lambda prompt: _plan(_run_id_from(prompt), snapshot, abstain=abstain), recorded)
    with db.opened(journal) as jdb:
        out = preview_service.run_preview(cfg, jdb=jdb, root=repo, stage_runner=runner)
    assert out["ok"] is True, out.get("error")
    return out


def test_it_calls_the_decision_stage_the_way_a_real_run_does(cfg, repo: Path, journal: Path):
    calls: list[dict[str, Any]] = []
    out = _preview(cfg, repo, journal, calls=calls)
    assert out["ok"] is True, out.get("error")

    call = calls[0]
    # Read-only, with Write and Bash named as disallowed rather than merely left out.
    from runs import decision_core

    assert call["allowed_tools"] == decision_core.READ_ONLY_TOOLS
    assert call["extra_disallowed"] == ["Write", "Bash"]
    assert call["output_schema"], "the same schema the real run enforces"
    assert call["max_usd"] <= preview_service.MAX_PREVIEW_USD
    # The same routing, not a cheap stand-in.
    assert out["requested_model"]
    assert out["prompt_version"].startswith("research.v")


def test_it_returns_the_plan_it_would_make(cfg, repo: Path, journal: Path):
    out = _preview(cfg, repo, journal)
    plan = out["plan"]
    assert plan["abstain"] is False
    assert plan["targets"]["BTC"] == pytest.approx(0.6)
    assert plan["rationale"]
    assert plan["invalidation"]


def test_it_says_plainly_when_the_plan_is_to_do_nothing(cfg, repo: Path, journal: Path):
    out = _preview(cfg, repo, journal, abstain=True)
    assert out["plan"]["abstain"] is True


def test_it_runs_the_real_safety_checks_and_names_the_one_that_refuses(
    cfg, repo: Path, journal: Path,
):
    out = _preview(cfg, repo, journal)
    gate = out["gate"]
    assert gate["available"] is True
    # No recorded value for this bot yet, so the gate's very first check refuses — which is
    # exactly what would really happen, and is far more useful than an idealised "allowed".
    assert gate["nav_valid"] is False
    assert gate["verdicts"], "the targets were actually put through the checks"
    assert all(not v["allowed"] for v in gate["verdicts"])
    assert gate["verdicts"][0]["reason"].startswith("nav_valid")


def test_the_checks_see_the_book_the_bot_last_reported(cfg, repo: Path, journal: Path):
    """A preview against an empty book would be a preview of a different system."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.opened(journal) as jdb:
        jdb.execute(
            "INSERT INTO nav_points(ts_utc, sleeve, run_id, mode, nav_usdt, cash_usdt,"
            " reserved_usdt, positions_json, open_trades)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now, "b", "test-b-1", "test", 10000.0, 10000.0, 0.0, "{}", 0))
        jdb.commit()
    out = _preview(cfg, repo, journal)
    gate = out["gate"]
    assert gate["nav_valid"] is True
    assert gate["nav_usdt"] == pytest.approx(10000.0)
    # 60% of a $10,000 book is a $6,000 stake, sized exactly as the live path would size it.
    btc = next(v for v in gate["verdicts"] if v["pair"].startswith("BTC/"))
    assert btc["stake_usdt"] == pytest.approx(6000.0)


# --------------------------------------------------------------------------- writes nothing


def test_it_writes_no_plan_file_and_no_journal_row(cfg, repo: Path, journal: Path):
    before_proposals = sorted((repo / "proposals").rglob("*.json")) if (
        repo / "proposals").exists() else []
    out = _preview(cfg, repo, journal)
    assert out["wrote_nothing"] is True
    assert out["preview"] is True

    after = sorted((repo / "proposals").rglob("*.json")) if (repo / "proposals").exists() else []
    assert after == before_proposals, "a preview wrote a plan file"

    with db.opened(journal, readonly=True) as jdb:
        assert jdb.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"] == 0
        assert jdb.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0


def test_it_never_writes_the_safety_checks_own_counters(cfg, repo: Path, journal: Path):
    """Looking must not change the thing being looked at."""
    _preview(cfg, repo, journal)
    with db.opened(journal, readonly=True) as jdb:
        assert jdb.execute("SELECT COUNT(*) AS n FROM risk_state").fetchone()["n"] == 0


def test_the_read_through_store_reads_the_live_counters_and_swallows_writes():
    class Inner:
        def __init__(self) -> None:
            self.data = {"trades_today": "3"}
            self.writes = 0

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def set(self, key: str, value: str) -> None:  # pragma: no cover - must never run
            self.writes += 1
            self.data[key] = value

    inner = Inner()
    store = preview_service.ReadThroughStore(inner)
    assert store.get("trades_today") == "3", "the real counter, not a fresh zero"
    store.set("trades_today", "99")
    assert inner.writes == 0
    assert inner.data["trades_today"] == "3"
    assert store.get("trades_today") == "99", "the caller still sees its own write"


def test_an_unreadable_counter_reads_as_absent_rather_than_raising():
    class Broken:
        def get(self, key: str) -> str | None:
            raise RuntimeError("database is locked")

        def set(self, key: str, value: str) -> None:
            raise RuntimeError("database is locked")

    store = preview_service.ReadThroughStore(Broken())
    assert store.get("anything") is None


# --------------------------------------------------------------------------- the label


def test_the_run_id_is_real_but_the_result_is_labelled_a_preview(cfg, repo: Path,
                                                                 journal: Path):
    out = _preview(cfg, repo, journal)
    # Real, because the plan schema refuses anything that is not ISO-8601 with an offset.
    datetime.fromisoformat(out["run_id"])
    # And unmistakable.
    assert out["preview"] is True
    assert out["preview_id"].startswith("preview-")


def test_the_latest_view_costs_nothing_and_says_what_a_preview_is(cfg, repo: Path,
                                                                  journal: Path):
    _preview(cfg, repo, journal)
    latest = preview_service.latest(cfg, root=repo)
    assert latest["last"]["status"] == "ok"
    assert latest["budget"]["ready"] is False
    assert "places nothing" in latest["note"]
