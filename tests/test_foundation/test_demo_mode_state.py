"""DEMO as a first-class state in the signed authority and in the read-only view.

Binance Spot Demo Mode is real orders, on a real matching engine, with fake money. That
makes it neither of the two things the codebase already knew about, and the tests here are
statements about *both* halves of that:

* it is **not TEST** — no permissive "it's only paper" branch may fire for a sleeve that is
  placing genuine orders (``is_test`` false, ``assume_live`` true, ``needs_exchange`` true);
* it is **not LIVE** — no real-money guard, report or P&L column may count it
  (``is_live`` false, ``any_live`` false, the run's mode word is ``demo``, the computed
  ``phase`` stays ``paper``).

Getting either half wrong is a specific, nameable accident: the first lets a demo sleeve
skip the order-path protections, the second presents a free-money rehearsal as live
performance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import db
from ops.config import load_config
from ops.lib import mode_state as ms
from ops.lib import mode_view
from tests.test_foundation.test_mode_view import (  # reuse the overlay writers
    SECRET,
    open_run,
    write_current_runtime,
    write_runtime,
)

DEMO_STATES = ("DEMO_PROPOSE", "DEMO_EXECUTE")


# --------------------------------------------------------------------------- mode_state


class TestTheSets:
    def test_demo_states_are_parseable_and_listed(self):
        assert set(DEMO_STATES) <= set(ms.MODES)
        assert ms.DEMO_MODES == frozenset(DEMO_STATES)

    def test_live_modes_did_not_grow(self):
        """The one set every real-money guard keys on. Demo must never join it."""
        assert ms.LIVE_MODES == frozenset({"LIVE_PROPOSE", "LIVE_EXECUTE"})
        assert not (ms.LIVE_MODES & ms.DEMO_MODES)

    def test_venue_modes_is_the_union_and_test_is_in_neither(self):
        assert ms.VENUE_MODES == ms.LIVE_MODES | ms.DEMO_MODES
        assert "TEST" not in ms.VENUE_MODES
        assert not (ms.VENUE_MODES & ms.TRANSIENT_MODES)

    @pytest.mark.parametrize("state", DEMO_STATES)
    def test_a_demo_sleeve_is_not_live_and_not_test(self, state):
        sleeve = ms.SleeveState(state)
        assert sleeve.is_demo is True
        assert sleeve.is_live is False
        assert sleeve.is_test is False
        assert sleeve.needs_exchange is True

    def test_propose_needs_approval_and_execute_does_not(self):
        assert ms.SleeveState("DEMO_PROPOSE").requires_approval is True
        assert ms.SleeveState("DEMO_PROPOSE").executes is False
        assert ms.SleeveState("DEMO_EXECUTE").requires_approval is False
        assert ms.SleeveState("DEMO_EXECUTE").executes is True


class TestTheSignedFile:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path: Path, monkeypatch):
        from ops.lib import paths, signing

        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
        monkeypatch.setenv(signing.SECRET_ENV, SECRET)
        monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)

    def _write(self, state: str) -> ms.ModeState:
        built = ms.build({"a": ms.SleeveState(state, "propose", "demo-a-1", 500.0)},
                         set_by="human:cli")
        ms.write(built, secret=SECRET)
        return ms.load(secret=SECRET)

    def test_a_demo_state_round_trips_through_the_signature(self):
        loaded = self._write("DEMO_PROPOSE")
        assert loaded.verified is True
        assert loaded.sleeve("a").state == "DEMO_PROPOSE"
        assert loaded.is_demo("a") is True
        assert loaded.is_live("a") is False
        assert loaded.needs_exchange("a") is True

    def test_a_demo_sleeve_never_counts_as_live_anywhere(self):
        loaded = self._write("DEMO_EXECUTE")
        assert loaded.any_live() is False
        assert loaded.live_sleeves() == []
        assert loaded.any_demo() is True
        assert loaded.demo_sleeves() == ["a"]

    def test_the_computed_phase_stays_paper(self):
        """``phase`` answers "how much real capital is at stake". On demo: none."""
        assert self._write("DEMO_EXECUTE").phase == "paper"

    def test_an_unsigned_demo_file_still_reads_as_test(self, tmp_path: Path):
        """The fail-closed invariant is untouched by the new states."""
        path = tmp_path / "var" / "state" / "mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "sleeves": {"a": {"state": "DEMO_EXECUTE", "submode": "execute"}},
            "sig": "hmac-sha256:not-a-real-signature",
        }))
        loaded = ms.load(path, secret=SECRET)
        assert loaded.verified is False and loaded.reason == ms.REASON_BAD_SIGNATURE
        assert loaded.sleeve("a").state == "TEST"
        assert loaded.is_demo("a") is False


# --------------------------------------------------------------------------- mode_view


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    return tmp_path / "var" / "runtime"


@pytest.fixture
def cfg():  # noqa: ANN201 - EarnConfig
    return load_config()


@pytest.fixture
def jdb(cfg, tmp_path):  # noqa: ANN001, ANN201
    journal, _knowledge = db.init_all(cfg, root=tmp_path)
    conn = db.connect(journal)
    yield conn
    conn.close()


class TestTheView:
    def test_demo_is_its_own_liveness_value(self):
        assert mode_view.DEMO == "demo"
        assert mode_view.DEMO in mode_view.LIVENESS

    @pytest.mark.parametrize("state", DEMO_STATES)
    def test_the_overlay_proves_demo_not_live(self, runtime_dir, state):
        # dry_run is false for demo exactly as it is for live — freqtrade refuses
        # demo_trading together with dry_run — so the state word is what separates them.
        write_runtime(runtime_dir, "b", state=state, mode="demo", dry_run=False)
        view = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert view.liveness == mode_view.DEMO
        assert view.is_demo and not view.is_live and not view.is_test
        assert view.label() == "demo"

    def test_a_demo_sleeve_takes_every_restrictive_branch(self, runtime_dir):
        write_runtime(runtime_dir, "b", state="DEMO_EXECUTE", mode="demo", dry_run=False)
        view = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        # ``assume_live`` is "not provably TEST" — the predicate every restrictive caller
        # in the module docstring's table uses. Demo must satisfy it.
        assert view.assume_live is True
        assert view.needs_exchange is True

    def test_a_demo_sleeve_is_never_counted_as_live_by_the_view(self, runtime_dir):
        write_runtime(runtime_dir, "b", state="DEMO_PROPOSE", mode="demo", dry_run=False)
        view = mode_view.load(runtime_dir=runtime_dir)
        assert view.any_live() is False
        assert view.any_demo() is True
        assert view.any_assume_live() is True

    def test_demo_propose_requires_approval_and_demo_execute_does_not(self, runtime_dir):
        write_runtime(runtime_dir, "b", state="DEMO_PROPOSE", mode="demo", dry_run=False)
        assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").requires_approval is True
        write_runtime(runtime_dir, "b", state="DEMO_EXECUTE", mode="demo", dry_run=False,
                      require_approval=False)
        assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").requires_approval is False

    def test_a_demo_state_with_dry_run_true_is_a_conflict(self, runtime_dir):
        """A demo container that is dry-running is one of the two files being stale."""
        write_runtime(runtime_dir, "b", state="DEMO_EXECUTE", mode="demo", dry_run=True)
        view = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert view.unknown and view.reason == mode_view.REASON_RUNTIME_CONFLICT
        assert view.assume_live

    def test_a_demo_state_filed_under_the_live_mode_word_is_a_conflict(self, runtime_dir):
        """``mode: live`` on a ``DEMO_*`` state is exactly how demo P&L becomes live P&L."""
        write_runtime(runtime_dir, "b", state="DEMO_EXECUTE", mode="live", dry_run=False)
        assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").unknown

    def test_a_live_state_is_still_live(self, runtime_dir):
        """The change must not have moved live: the existing proof still holds."""
        write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", dry_run=False)
        view = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert view.is_live and not view.is_demo

    def test_the_journal_mode_word_demo_proves_demo(self, jdb, cfg, runtime_dir):
        open_run(jdb, cfg, "b", mode="demo", run_id="demo-b-01", submode="propose")
        view = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert view.is_demo and view.run_id == "demo-b-01"
        assert view.source == mode_view.SOURCE_JOURNAL

    def test_the_signed_authority_still_outranks_the_overlays(
        self, runtime_dir, tmp_path, monkeypatch
    ):
        from ops.lib import paths, signing

        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
        monkeypatch.setenv(signing.SECRET_ENV, SECRET)
        monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
        built = ms.build({"b": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-b-9", 500)},
                         set_by="human:cli")
        ms.write(built, secret=SECRET)
        write_current_runtime(runtime_dir, "b", state="TEST")
        view = mode_view.load(state=ms.load(secret=SECRET), runtime_dir=runtime_dir).sleeve("b")
        assert view.is_demo and view.state == "DEMO_EXECUTE"
        assert view.source == mode_view.SOURCE_MODE_STATE
        assert view.corroborated is True
