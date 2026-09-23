"""Adversarial re-check of the mode-authority and Risk-page fixes.

Two defects that survived the Fix-A pass. Both are *fail-open* answers produced by code
whose own docstring promises the restrictive one, which is the shape that gets trusted.

Nothing here is a fix — these tests are red on purpose and name what has to change.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from console.services import risk_service as rsvc
from ops import db
from ops.config import load_config
from ops.lib import mode_view as mv

# --------------------------------------------------------------------------- mode_view

def _armed_then_disarmed(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """A sleeve whose two unsigned sources disagree: the classic stale-overlay state.

    ``var/runtime`` still describes the sleeve as ``LIVE_EXECUTE`` (the overlay the
    container was started from) while the journal's newest *completed* transition says the
    human has since taken it back to ``TEST``. ``mode_view`` correctly answers UNKNOWN for
    that — neither source can be trusted over the other.
    """
    cfg = load_config()
    journal, _ = db.init_all(cfg, root=tmp_path)
    rt = tmp_path / "var" / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "runtime-b.json").write_text(json.dumps({
        "version": 1, "sleeve": "b", "mode": "live", "state": "LIVE_EXECUTE",
        "submode": None, "run_id": "live-b-01", "seed_usdt": 5000.0,
        "require_approval": False,
    }), encoding="utf-8")
    (rt / "freqtrade-b.mode.json").write_text(
        json.dumps({"dry_run": False}), encoding="utf-8")
    conn = sqlite3.connect(journal)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
        " actor) VALUES (?,?,?,?,?,?)",
        ("b", "LIVE_EXECUTE", "TEST", "2026-09-01T00:00:00Z", "completed", "human"))
    conn.commit()
    return conn, tmp_path


class TestUnknownMustRequireApproval:
    """``SleeveView.requires_approval`` is the predicate ``runs.research_run`` gates on.

    ``ops/lib/mode_view.py``'s module table states the contract plainly —
    "``runs.research_run``: ``approval_status='pending'`` — a proposal is a request until a
    human signs it" — and the property's own docstring says "UNKNOWN does". It does not.
    The ``if self.state:`` branch is tested *before* the unknown branch, so any UNKNOWN
    that retained a state word which is not literally ``LIVE_PROPOSE`` answers **False**.
    """

    def test_an_unknown_sleeve_that_kept_a_state_word_still_needs_a_human(self, tmp_path,
                                                                         monkeypatch):
        monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
        conn, root = _armed_then_disarmed(tmp_path)
        view = mv.load(jdb=conn, root=root)
        sleeve = view.sleeve("b")
        assert sleeve.unknown, sleeve.describe()
        assert sleeve.reason == mv.REASON_EVIDENCE_CONFLICT
        # This is the predicate research_run.proposal_destination() evaluates.
        assert sleeve.requires_approval is True, (
            "an UNKNOWN sleeve answered requires_approval=False, so the proposal is"
            " journalled approval_status='n/a' while the container refuses it for want of"
            " a signed approval and the console's approval queue stays empty — the exact"
            " bug the mode_view rewrite was written to close"
        )

    @pytest.mark.parametrize("state", ["TEST", "LIVE_EXECUTE", "ARMING", "DISARMING", ""])
    def test_unknown_requires_approval_whatever_state_word_survived(self, state):
        sleeve = mv.SleeveView(sleeve="b", liveness=mv.UNKNOWN, state=state,
                               reason=mv.REASON_EVIDENCE_CONFLICT)
        assert sleeve.requires_approval is True, state

    def test_a_proven_live_sleeve_with_no_state_word_still_needs_a_human(self):
        """Only the ``freqtrade-<s>.mode.json`` overlay survived: ``dry_run: false``.

        ``_from_runtime``'s overlay-only branch returns ``state=""`` and
        ``approval_flag=None``, so a sleeve proven to be trading real money answers
        "no human needed". Liveness is proven; the *submode* is not, and LIVE_PROPOSE is a
        live submode.
        """
        sleeve = mv.SleeveView(sleeve="b", liveness=mv.LIVE, state="",
                               source=mv.SOURCE_RUNTIME, reason=mv.REASON_OK)
        assert sleeve.requires_approval is True


# --------------------------------------------------------------------------- risk NAV

class TestRiskMetersUseLedgerNav:
    """The Risk page's meters must divide by the SAME NAV the gate divides by.

    ``strategies.riskgate`` is handed ``PortfolioState.nav`` = ledger NAV — "the bot's own
    capital, never the whole exchange account" (``earn_base._portfolio_state``) — and
    ``day_anchor_nav`` / ``month_anchor_nav`` are stamped straight off it.
    ``risk_service.portfolio_view`` instead takes ``balance['total']``, which freqtrade
    defines as the whole account; it also returns ``total_bot``, the bot's own capital, and
    ``console.services.portfolio_service`` already treats the difference between the two as
    a reconciliation *delta*. ``ops.preflight`` records a per-sleeve ``baseline`` of
    pre-existing exchange balances precisely because that difference is not zero in LIVE.
    """

    def test_portfolio_view_does_not_report_the_whole_account_as_nav(self):
        cfg = load_config()
        view = rsvc.portfolio_view(
            cfg, "b",
            bot_status=[{"pair": "BTC/USDT", "amount": 0.04, "current_rate": 50_000.0}],
            # A 5 000 USDT sleeve on an account that also holds 5 000 of the operator's.
            balance={"total": 10_000.0, "total_bot": 5_000.0,
                     "currencies": [{"currency": "USDT", "free": 8_000.0}]},
        )
        assert view["nav"] == pytest.approx(5_000.0), (
            "NAV came back as the whole exchange account. Every NAV-derived meter is then"
            " halved (gross_cap reads 40% of the cap as 20%), and StopProximity computes"
            " 1 - nav/day_anchor_nav against a ledger anchor, so `Math.max(0, ...)` pins"
            " both loss-stop bars at 0% however far the sleeve has actually fallen — the"
            " very symptom the risk fix claims to have removed"
        )

    def test_the_stop_proximity_input_is_comparable_with_the_gate_anchor(self, tmp_path,
                                                                        monkeypatch):
        """A -4% ledger drawdown must not read as 0% of the 3% daily stop."""
        monkeypatch.setenv("EARN_STATE_ROOT", str(tmp_path))
        cfg = load_config()
        db.init_all(cfg, root=tmp_path)
        gate = rsvc.risk_resume.gate_for(cfg, "b", root=tmp_path)
        gate.store.set("day_anchor_nav", "5000")
        anchor = float(rsvc.anchors(cfg, "b", root=tmp_path)["day_anchor_nav"])
        view = rsvc.portfolio_view(
            cfg, "b",
            bot_status=[{"pair": "BTC/USDT", "amount": 0.04, "current_rate": 50_000.0}],
            balance={"total": 9_800.0, "total_bot": 4_800.0,
                     "currencies": [{"currency": "USDT", "free": 2_800.0}]},
        )
        used = max(0.0, (1 - view["nav"] / anchor) / cfg.risk.daily_loss_stop)
        assert used > 1.0, (
            f"nav={view['nav']} against anchor={anchor} reads {used:.2f} of the daily"
            " stop; the sleeve is 4% down on a 3% stop, so the meter must be over 1.0"
        )
