"""Sleeve B may only trade what a proposal told it to trade.

RUNTIME DEFECT (8h dry-run, 2026-09-23): with ``proposals/`` empty and no proposal ever
adopted, sleeve B opened BTC/USDT 0.04618 (~3 960 USDT) and ETH/USDT 1.306 (~3 550 USDT)
tagged ``enter_tag="proposal"`` — roughly 4x what sleeve A took on the same candles.
``bot_loop_start`` treated "no proposal has ever arrived" as "the 48h drift window has
elapsed" (the drift clock had no start), so the sleeve fell straight through to sleeve
A's FULL rules targets and tagged the trades as if a proposal had produced them.

The four states and their tags are asserted here:
  none      -> zero targets, no entry candidate, no stake, reason journalled
  proposal  -> the proposal's targets, tag ``proposal``
  hold      -> the last targets stand while a stale file / no file leaves them, tag
               ``proposal_hold``
  drift_a   -> sleeve A's rules targets after ``drift_to_a_after_h``, tag
               ``drift_sleeve_a``
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

pytest.importorskip("freqtrade")

from strategies import SleeveB as sleeve_b  # noqa: E402
from strategies import _journal  # noqa: E402

from .test_ledger_nav import FakeWallets, with_trades  # noqa: E402
from .test_sleeves import _flat, _make, _proposal  # noqa: E402

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
RUN_ID = "2026-09-22T08:30+04:00"
BTC, ETH = "BTC/USDT", "ETH/USDT"

DRIFT_TARGETS = {BTC: 0.40, ETH: 0.30}


def _sleeve(monkeypatch, tmp_path, *, journal=False, mutate=None):
    s = _make(monkeypatch, tmp_path, "b", mutate=mutate)
    (tmp_path / "proposals").mkdir(exist_ok=True)
    monkeypatch.setattr(type(s), "_rules_targets", lambda self: dict(DRIFT_TARGETS))
    with_trades(monkeypatch, s, [])
    s.wallets = FakeWallets(start=10_000.0, free=10_000.0)
    s._journal_on = journal
    return s


def _rows(monkeypatch):
    """Collect the gate rows SleeveB writes (journalling is off in backtest mode)."""
    seen: list[dict] = []

    def fake(sleeve, pair, callback, intent, allowed, reason, **kw):
        seen.append({"pair": pair, "callback": callback, "allowed": allowed,
                     "reason": reason, **kw})
        return 1

    monkeypatch.setattr(_journal, "record_gate_decision", fake)
    return seen


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"close": [100.0, 101.0, 102.0]})


def _tag(strategy) -> str | None:
    df = strategy.populate_entry_trend(_frame(), {"pair": BTC})
    if not int(df["enter_long"].iloc[-1]):
        return None
    return str(df["enter_tag"].iloc[-1])


# --------------------------------------------------------------- 1. no proposal, ever

class TestNoProposalEverMeansNoTrade:
    def test_an_empty_proposals_dir_leaves_no_targets_and_no_candidate(
            self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)

        assert s._target_source == sleeve_b.SOURCE_NONE
        # Every pair on the whitelist at zero — a wide universe does not weaken the
        # mandate rule, it only makes the dict longer.
        assert set(s._targets) == set(s.gate_cfg.pairs)
        assert set(s._targets.values()) == {0.0}
        assert _tag(s) is None, "sleeve B raised an entry candidate with no proposal"

    def test_no_stake_is_sized_with_no_proposal(self, monkeypatch, tmp_path):
        """The defect in money terms: this used to size ~40% of NAV into BTC."""
        s = _sleeve(monkeypatch, tmp_path)
        ps = _flat(monkeypatch, s)
        s.bot_loop_start(NOW)
        assert s._desired_stake(BTC, ps, 0.0, "proposal") == 0.0
        assert s._desired_stake(BTC, ps, 0.0, None) == 0.0

    def test_an_invalid_proposal_is_not_a_proposal(self, monkeypatch, tmp_path):
        """A file the loader refuses must not start the drift clock either."""
        _proposal(tmp_path, targets={"BTC": 0.9, "ETH": 0.9, "USDT": 0.9})
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s._target_source == sleeve_b.SOURCE_NONE
        assert _tag(s) is None

    def test_the_reason_is_journalled_once_per_state_change(self, monkeypatch, tmp_path):
        s = _sleeve(monkeypatch, tmp_path, journal=True)
        rows = _rows(monkeypatch)
        s.bot_loop_start(NOW)
        s.bot_loop_start(NOW + timedelta(seconds=5))     # the loop runs constantly

        idle = [r for r in rows if r["reason"] == "targets:no_proposal_ever"]
        assert len(idle) == 1, "the idle reason was written on every loop"
        assert idle[0]["allowed"] is False and idle[0]["severity"] == "reject"
        assert idle[0]["checks"]["has_mandate"] is False
        assert idle[0]["callback"] == "bot_loop_start"


# ------------------------------------------------------------------- 2. a real proposal

class TestAProposalTrades:
    def test_a_valid_proposal_sets_its_targets_and_the_proposal_tag(
            self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)

        assert s._target_source == sleeve_b.SOURCE_PROPOSAL
        assert s._targets[BTC] == pytest.approx(0.36)     # 0.45 * exposure_scale 0.8
        assert _tag(s) == "proposal"


# ------------------------------------------- 3. stale-but-valid proposal => hold targets

class TestStaleProposalHolds:
    def _adopted(self, monkeypatch, tmp_path, **kw):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path, **kw)
        s.bot_loop_start(NOW)
        return s

    @staticmethod
    def _short_max_age(raw):
        """Stale (loader) well before drift (sleeve): that gap IS the hold window."""
        raw["proposal"]["max_age_hours"] = 6

    def test_targets_are_held_while_the_file_goes_stale(self, monkeypatch, tmp_path):
        s = self._adopted(monkeypatch, tmp_path, mutate=self._short_max_age)
        s.bot_loop_start(NOW + timedelta(hours=10))       # file stale, drift clock at 13.5h

        assert s._target_source == sleeve_b.SOURCE_HOLD
        assert s._targets[BTC] == pytest.approx(0.36)     # unchanged: held, not re-derived
        assert s._targets != DRIFT_TARGETS
        assert _tag(s) == "proposal_hold"

    def test_a_deleted_proposal_still_holds_inside_the_window(self, monkeypatch, tmp_path):
        s = self._adopted(monkeypatch, tmp_path)
        for f in (tmp_path / "proposals").glob("*.json"):
            f.unlink()
        s.bot_loop_start(NOW + timedelta(hours=1))
        assert s._target_source == sleeve_b.SOURCE_HOLD
        assert s._targets[BTC] == pytest.approx(0.36)

    def test_an_abstain_only_history_holds_nothing_and_trades_nothing(
            self, monkeypatch, tmp_path):
        """``abstain`` means hold — and with nothing yet to hold that is no trade."""
        _proposal(tmp_path, module="hold", abstain=True)
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s._target_source == sleeve_b.SOURCE_NONE
        assert _tag(s) is None


# ------------------------------------------------ 4. the window elapses => sleeve A drift

class TestDriftToSleeveA:
    def test_after_the_window_the_rules_targets_take_over_with_their_own_tag(
            self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s.gate_cfg.drift_to_a_after_h == 48

        s.bot_loop_start(NOW + timedelta(hours=49))
        assert s._target_source == sleeve_b.SOURCE_DRIFT_A
        assert s._targets == DRIFT_TARGETS
        assert _tag(s) == "drift_sleeve_a", "a drift trade must not claim to be a proposal"

    def test_the_drift_is_journalled(self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path, journal=True)
        rows = _rows(monkeypatch)
        s.bot_loop_start(NOW)
        s.bot_loop_start(NOW + timedelta(hours=49))
        assert [r for r in rows if r["reason"] == "targets:drift_to_sleeve_a"]


# ------------------------------------------------------ 5. the tag tells the truth

class TestTheTagTellsTheTruth:
    def test_every_state_has_its_own_tag(self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path)
        seen = {}
        s.bot_loop_start(NOW)                             # adopted
        seen[s._target_source] = _tag(s)
        for f in (tmp_path / "proposals").glob("*.json"):
            f.unlink()                                    # nothing usable: hold
        s.bot_loop_start(NOW + timedelta(hours=1))
        seen[s._target_source] = _tag(s)
        s.bot_loop_start(NOW + timedelta(hours=49))       # window elapsed: drift
        seen[s._target_source] = _tag(s)

        assert seen == {
            sleeve_b.SOURCE_PROPOSAL: "proposal",
            sleeve_b.SOURCE_HOLD: "proposal_hold",
            sleeve_b.SOURCE_DRIFT_A: "drift_sleeve_a",
        }
        assert len(set(seen.values())) == 3   # distinguishable in the journal and the UI

    def test_a_candidate_tagged_by_an_earlier_state_is_not_sized(
            self, monkeypatch, tmp_path):
        """The dataframe is tagged once per candle; the state may have moved on."""
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path)
        ps = _flat(monkeypatch, s)
        s.bot_loop_start(NOW)                             # adopted, tagged "proposal"
        s.bot_loop_start(NOW + timedelta(hours=49))       # now drifting
        assert s._desired_stake(BTC, ps, 0.0, "proposal") == 0.0
        assert s._desired_stake(BTC, ps, 0.0, "drift_sleeve_a") > 0.0

    def test_the_run_id_is_still_recorded_for_a_traded_proposal(self, monkeypatch, tmp_path):
        _proposal(tmp_path)
        s = _sleeve(monkeypatch, tmp_path)
        s.bot_loop_start(NOW)
        assert s.gate.store.get("sleeveb_run_id") == RUN_ID
        assert json.loads(s.gate.store.get("sleeveb_targets"))[BTC] == pytest.approx(0.36)
