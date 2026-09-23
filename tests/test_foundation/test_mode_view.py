"""``ops.lib.mode_view`` — the tri-state an unattended job can prove.

The whole point of this module is the third value. ``mode_state.load()`` fails closed to
all-TEST because it is the *writer's* authority; six jobs then read that back as a *proof*
of TEST while ``EARN_CONSOLE_SECRET`` was absent by design, and acted on it. So the tests
here are statements about the distinction:

* with the secret, the signed file still wins outright;
* without it, the rendered ``var/runtime`` overlays and the journal's own records are
  enough to prove LIVE — the case that used to be invisible;
* with nothing, or with evidence that disagrees, the answer is ``UNKNOWN`` and never TEST;
* ``assume_live`` is the predicate a restrictive branch uses, and it is true on UNKNOWN.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import db
from ops.config import load_config
from ops.lib import mode_state as ms
from ops.lib import mode_view, paths, signing

SECRET = "mode-view-tests-secret-0123456789"


# --------------------------------------------------------------------------- helpers


def write_runtime(runtime_dir: Path, sleeve: str, *, state: str, run_id: str = "",
                  submode: str | None = None, seed: float | None = None,
                  require_approval: bool | None = None,
                  dry_run: bool | None = None, mode: str | None = None,
                  generated_at: str | None = None, config_sha: str | None = None,
                  version: int = 1, names_sleeve: str | None = None) -> None:
    """The two overlays ``ops.gen_freqtrade_config.write_runtime`` renders, by hand.

    Hand-written rather than generated so a test can make them *disagree*, which is the
    corruption case the merge has to refuse. ``generated_at``/``config_sha`` default to
    absent, which is the ``unverified`` provenance: readable evidence that can never
    corroborate. :func:`write_current_runtime` writes the provable version.
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    live = state in ms.LIVE_MODES
    body: dict = {
        "version": version,
        "sleeve": names_sleeve if names_sleeve is not None else sleeve,
        "mode": mode if mode is not None else ("live" if live else "test"),
        "state": state, "submode": submode,
        "run_id": run_id or f"{'live' if live else 'test'}-{sleeve}-01",
        "seed_usdt": seed if seed is not None else 10000.0,
    }
    if require_approval is not None:
        body["require_approval"] = require_approval
    if generated_at is not None:
        body["generated_at"] = generated_at
    if config_sha is not None:
        body["config_sha"] = config_sha
    (runtime_dir / f"runtime-{sleeve}.json").write_text(json.dumps(body))
    (runtime_dir / f"freqtrade-{sleeve}.mode.json").write_text(
        json.dumps({"dry_run": (not live) if dry_run is None else dry_run})
    )


def write_current_runtime(runtime_dir: Path, sleeve: str, *, state: str,
                          generated_at: str = "2026-10-27T06:00:00Z", **over) -> None:
    """An overlay whose provenance a reader can actually check.

    The digest is the real one of the config on disk, because that is what
    ``ops.gen_freqtrade_config`` stamps and what ``mode_view`` recomputes.
    """
    write_runtime(runtime_dir, sleeve, state=state, generated_at=generated_at,
                  config_sha=mode_view.config_sha(), **over)


def completed_transition(jdb, sleeve: str, to_state: str, *, status: str = "completed",
                         from_state: str = "TEST") -> None:
    db.write(
        jdb,
        "INSERT INTO mode_transitions(sleeve, from_state, to_state, started_utc, status,"
        " actor) VALUES (?,?,?,?,?,?)",
        (sleeve, from_state, to_state, "2026-10-27T05:00:00Z", status, "human:cli"),
    )


def open_run(jdb, cfg, sleeve: str, *, mode: str, run_id: str,
             submode: str | None = None) -> None:
    from ops import modes

    modes.open_run(jdb, cfg, run_id=run_id, sleeve=sleeve, mode=mode, submode=submode,
                   seed_usdt=10000.0, started_utc="2026-10-27T05:00:00Z")


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def jdb(cfg, tmp_path):
    journal, _knowledge = db.init_all(cfg, root=tmp_path)
    conn = db.connect(journal)
    yield conn
    conn.close()


@pytest.fixture
def runtime_dir(tmp_path):
    return tmp_path / "var" / "runtime"


# --------------------------------------------------------------------------- no evidence


def test_nothing_at_all_is_unknown_not_test(runtime_dir):
    """The regression in one line. Six callers read this state as "the mode is TEST"."""
    view = mode_view.load(runtime_dir=runtime_dir)
    for sleeve in ("a", "b"):
        mv = view.sleeve(sleeve)
        assert mv.liveness == mode_view.UNKNOWN
        assert mv.unknown and not mv.is_test and not mv.is_live
        assert mv.reason == mode_view.REASON_NO_EVIDENCE
    assert view.any_assume_live() and not view.any_live()
    assert not view.all_test()


def test_an_unlisted_sleeve_is_unknown(runtime_dir):
    view = mode_view.load(runtime_dir=runtime_dir, sleeves=("a",))
    assert view.sleeve("zz").unknown
    assert view.sleeve("zz").assume_live


# --------------------------------------------------------------------------- runtime


def test_the_rendered_overlay_proves_live_without_the_secret(runtime_dir, monkeypatch):
    """The core fix: a job with no key can still see that sleeve b is armed."""
    monkeypatch.delenv(signing.SECRET_ENV, raising=False)
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", run_id="live-b-01",
                  submode="execute")
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.liveness == mode_view.LIVE
    assert mv.state == "LIVE_EXECUTE" and mv.submode == "execute"
    assert mv.run_id == "live-b-01"
    assert mv.source == mode_view.SOURCE_RUNTIME
    assert mv.assume_live


def test_the_rendered_overlay_proves_test(runtime_dir, monkeypatch):
    monkeypatch.delenv(signing.SECRET_ENV, raising=False)
    write_runtime(runtime_dir, "a", state="TEST")
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("a")
    assert mv.is_test and not mv.assume_live


def test_a_runtime_that_contradicts_its_own_dry_run_is_unknown(runtime_dir):
    """``mode: live`` with ``dry_run: true`` means one file is stale. Refuse to choose."""
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", dry_run=True)
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
    assert mv.unknown and mv.reason == mode_view.REASON_RUNTIME_CONFLICT
    assert mv.assume_live


def test_a_runtime_whose_mode_word_contradicts_its_state_is_unknown(runtime_dir):
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", mode="test", dry_run=False)
    assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").unknown


def test_only_the_freqtrade_overlay_still_reports_dry_run(runtime_dir):
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "freqtrade-b.mode.json").write_text(json.dumps({"dry_run": False}))
    assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").is_live


def test_a_corrupt_runtime_file_is_unknown_not_a_crash(runtime_dir):
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime-b.json").write_text("{not json")
    assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").unknown


def test_a_transient_state_is_unknown(runtime_dir):
    """ARMING is neither. A half-finished transition must not read as TEST."""
    write_runtime(runtime_dir, "b", state="ARMING", dry_run=True)
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
    assert mv.unknown and mv.assume_live


# --------------------------------------------------------------------------- journal


def test_the_active_run_row_proves_live(jdb, cfg, runtime_dir):
    open_run(jdb, cfg, "b", mode="live", run_id="live-b-07", submode="propose")
    mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.run_id == "live-b-07" and mv.submode == "propose"
    assert mv.source == mode_view.SOURCE_JOURNAL


def test_the_last_completed_transition_proves_live(jdb, runtime_dir):
    completed_transition(jdb, "b", "LIVE_PROPOSE")
    mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.state == "LIVE_PROPOSE"


def test_a_running_transition_is_not_evidence(jdb, runtime_dir):
    completed_transition(jdb, "b", "LIVE_EXECUTE", status="running")
    assert mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b").unknown


def test_the_transition_lookup_is_per_sleeve(jdb, runtime_dir):
    completed_transition(jdb, "b", "LIVE_EXECUTE")
    view = mode_view.load(jdb=jdb, runtime_dir=runtime_dir)
    assert view.sleeve("b").is_live
    assert view.sleeve("a").unknown          # sleeve a said nothing; it does not inherit


def test_journal_records_that_disagree_are_unknown(jdb, cfg, runtime_dir):
    open_run(jdb, cfg, "b", mode="test", run_id="test-b-01")
    completed_transition(jdb, "b", "LIVE_EXECUTE")
    mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
    assert mv.unknown and mv.reason == mode_view.REASON_EVIDENCE_CONFLICT


# --------------------------------------------------------------------------- merge


def test_runtime_and_journal_corroborate(jdb, cfg, runtime_dir):
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", run_id="live-b-09")
    open_run(jdb, cfg, "b", mode="live", run_id="live-b-09")
    mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.source == mode_view.SOURCE_BOTH


def test_runtime_and_journal_that_disagree_are_unknown(jdb, cfg, runtime_dir):
    """A stale overlay must not be able to demote a live sleeve to TEST."""
    write_runtime(runtime_dir, "b", state="TEST")
    open_run(jdb, cfg, "b", mode="live", run_id="live-b-09")
    mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
    assert mv.unknown and mv.reason == mode_view.REASON_EVIDENCE_CONFLICT
    assert mv.assume_live


def test_the_journal_fills_in_a_run_id_the_overlay_lacks(jdb, cfg, runtime_dir):
    """The risk-state namespace depends on the run id, so it must survive the merge."""
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "freqtrade-b.mode.json").write_text(json.dumps({"dry_run": False}))
    open_run(jdb, cfg, "b", mode="live", run_id="live-b-11")
    assert mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b").run_id == "live-b-11"


# --------------------------------------------------------------------------- signed state


def test_a_verified_signed_state_outranks_the_overlays(runtime_dir, monkeypatch, tmp_path):
    """A caller that HAS the secret (the console, the CLI) keeps the real authority."""
    monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(signing.SECRET_ENV, SECRET)
    monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
    paths.ensure_var_layout()
    ms.write(ms.build({"b": ms.SleeveState("LIVE_PROPOSE", submode="propose",
                                           run_id="live-b-42", seed_usdt=500.0)},
                      set_by="human:cli"), secret=SECRET)
    write_runtime(runtime_dir, "b", state="TEST")          # a stale overlay
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.source == mode_view.SOURCE_MODE_STATE
    assert mv.run_id == "live-b-42" and mv.requires_approval


def test_an_explicit_state_is_used_without_touching_the_disk(runtime_dir):
    state = ms.build({"a": ms.SleeveState("LIVE_EXECUTE", submode="execute")},
                     set_by="human:test")
    view = mode_view.load(state=state, runtime_dir=runtime_dir)
    assert view.sleeve("a").is_live and view.sleeve("b").is_test


def test_an_unverified_state_does_not_prove_test(runtime_dir):
    """``default_state`` is every sleeve TEST — and it proves nothing."""
    view = mode_view.load(state=ms.default_state(ms.REASON_NO_SECRET),
                          runtime_dir=runtime_dir)
    assert view.sleeve("a").unknown and view.sleeve("b").unknown
    assert view.state_reason == ms.REASON_NO_SECRET
    assert view.sleeve("a").state_reason == ms.REASON_NO_SECRET


# --------------------------------------------------------------------------- approval


def test_requires_approval_comes_from_the_overlay_the_container_enforces(runtime_dir):
    write_runtime(runtime_dir, "b", state="LIVE_PROPOSE", require_approval=True)
    assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").requires_approval


def test_requires_approval_is_true_when_nothing_is_provable(runtime_dir):
    assert mode_view.load(runtime_dir=runtime_dir).sleeve("b").requires_approval


def test_a_proven_test_sleeve_does_not_require_approval(runtime_dir):
    write_runtime(runtime_dir, "a", state="TEST", require_approval=False)
    assert not mode_view.load(runtime_dir=runtime_dir).sleeve("a").requires_approval


def test_live_execute_does_not_require_per_proposal_approval(runtime_dir):
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", require_approval=False)
    assert not mode_view.load(runtime_dir=runtime_dir).sleeve("b").requires_approval


def test_an_unknown_sleeve_requires_approval_whatever_else_it_carries(runtime_dir):
    """The ordering bug, at the level of the property itself.

    An UNKNOWN sleeve that kept a state word answered ``state == 'LIVE_PROPOSE'`` —
    ``False`` for every other word — and an UNKNOWN carrying an explicit
    ``require_approval: false`` would have answered ``False`` too. Both are the overlay we
    just failed to prove; neither may speak for it.
    """
    for state in ("TEST", "LIVE_EXECUTE", "ARMING", ""):
        sleeve = mode_view.SleeveView(sleeve="b", liveness=mode_view.UNKNOWN, state=state,
                                      approval_flag=False,
                                      reason=mode_view.REASON_EVIDENCE_CONFLICT)
        assert sleeve.requires_approval is True, state


def test_a_live_sleeve_with_no_proven_submode_requires_approval(runtime_dir):
    """``dry_run: false`` alone proves *liveness*, never the submode.

    That branch returns ``state=""`` and no approval flag, and LIVE_PROPOSE is a live
    submode — so "no human needed" would have been a guess about real money.
    """
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "freqtrade-b.mode.json").write_text(json.dumps({"dry_run": False}))
    mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
    assert mv.is_live and mv.state == "" and mv.requires_approval


# --------------------------------------------------------------- the overlay's trust


class TestOverlayTrust:
    """``docs/contracts.md`` §1.2 — what an unsigned ``var/runtime`` file may license.

    The overlay cannot be verified by the jobs that read it (they hold no secret, by
    design), so its provenance is checked instead: the ``version`` the renderer stamps,
    the ``config_sha`` of the config it rendered from, and a ``generated_at`` that must not
    predate the human's newest completed transition. Only a ``current`` overlay may
    corroborate, and only a corroborated answer may license a destructive branch.
    """

    def test_a_current_overlay_plus_the_journal_corroborates(self, jdb, cfg, runtime_dir):
        write_current_runtime(runtime_dir, "b", state="TEST", run_id="test-b-01")
        open_run(jdb, cfg, "b", mode="test", run_id="test-b-01")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.is_test and mv.overlay == mode_view.OVERLAY_CURRENT
        assert mv.source == mode_view.SOURCE_BOTH
        assert mv.corroborated

    def test_one_overlay_alone_never_corroborates(self, runtime_dir):
        """Liveness proven by a single unsigned file: enough to refuse, not to flatten."""
        write_current_runtime(runtime_dir, "b", state="TEST")
        mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert mv.is_test and not mv.corroborated

    def test_an_overlay_with_no_provenance_can_never_corroborate(self, jdb, cfg,
                                                                 runtime_dir):
        """No ``generated_at``/``config_sha``: readable, but nothing can be checked."""
        write_runtime(runtime_dir, "b", state="TEST", run_id="test-b-01")
        open_run(jdb, cfg, "b", mode="test", run_id="test-b-01")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.is_test and mv.source == mode_view.SOURCE_BOTH
        assert mv.overlay == mode_view.OVERLAY_UNVERIFIED and not mv.corroborated

    def test_a_tampered_config_sha_is_unverified(self, jdb, cfg, runtime_dir):
        """The digest names the config the overlay was rendered from. It has to match."""
        write_runtime(runtime_dir, "b", state="TEST", run_id="test-b-01",
                      generated_at="2026-10-27T06:00:00Z", config_sha="0" * 64)
        open_run(jdb, cfg, "b", mode="test", run_id="test-b-01")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.overlay == mode_view.OVERLAY_UNVERIFIED and not mv.corroborated

    def test_an_unknown_version_is_unverified(self, runtime_dir):
        write_current_runtime(runtime_dir, "b", state="TEST", version=99)
        mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert mv.overlay == mode_view.OVERLAY_UNVERIFIED

    def test_an_overlay_that_names_another_sleeve_is_refused(self, runtime_dir):
        """``runtime-b.json`` claiming ``"sleeve": "a"`` is not sleeve b's file."""
        write_current_runtime(runtime_dir, "b", state="LIVE_EXECUTE", names_sleeve="a")
        mv = mode_view.load(runtime_dir=runtime_dir).sleeve("b")
        assert mv.unknown and mv.reason == mode_view.REASON_RUNTIME_CONFLICT
        assert not mv.corroborated

    def test_an_overlay_older_than_the_last_transition_is_stale(self, jdb, runtime_dir):
        """The file the container was started from, after the human moved on.

        It agrees with the journal here — both say TEST — and it is *still* discarded:
        the journal alone speaks, so nothing is corroborated and no destructive branch
        opens.
        """
        write_current_runtime(runtime_dir, "b", state="TEST",
                              generated_at="2026-10-26T00:00:00Z")
        completed_transition(jdb, "b", "TEST", from_state="LIVE_EXECUTE")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.overlay == mode_view.OVERLAY_STALE
        assert mv.is_test and mv.source == mode_view.SOURCE_JOURNAL
        assert not mv.corroborated

    def test_a_stale_overlay_with_nothing_else_is_unknown_and_says_so(self, jdb,
                                                                      runtime_dir):
        """A completed transition with no ``to_state`` liveness left: no proof at all."""
        write_current_runtime(runtime_dir, "b", state="LIVE_EXECUTE",
                              generated_at="2026-10-26T00:00:00Z")
        completed_transition(jdb, "b", "ARMING")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.unknown and mv.overlay == mode_view.OVERLAY_STALE
        assert not mv.corroborated

    def test_an_overlay_newer_than_the_transition_is_current(self, jdb, cfg, runtime_dir):
        """A genuine render: step 1 stamps ``started_utc``, step 7 the ``set_at`` copied
        into ``generated_at``, so the overlay is always the newer of the two."""
        write_current_runtime(runtime_dir, "b", state="LIVE_EXECUTE", run_id="live-b-09")
        completed_transition(jdb, "b", "LIVE_EXECUTE")
        open_run(jdb, cfg, "b", mode="live", run_id="live-b-09")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.is_live and mv.overlay == mode_view.OVERLAY_CURRENT and mv.corroborated

    def test_the_signed_authority_needs_no_corroboration(self, runtime_dir, monkeypatch,
                                                         tmp_path):
        monkeypatch.setenv(paths.STATE_ROOT_ENV, str(tmp_path))
        monkeypatch.setenv(signing.SECRET_ENV, SECRET)
        monkeypatch.delenv(paths.AUTOMATED_RUN_ENV, raising=False)
        paths.ensure_var_layout()
        ms.write(ms.build({"a": ms.SleeveState("TEST", run_id="test-a-1")},
                          set_by="human:cli"), secret=SECRET)
        mv = mode_view.load(runtime_dir=runtime_dir).sleeve("a")
        assert mv.is_test and mv.source == mode_view.SOURCE_MODE_STATE and mv.corroborated

    def test_an_unknown_answer_is_never_corroborated(self, jdb, cfg, runtime_dir):
        write_current_runtime(runtime_dir, "b", state="TEST")
        open_run(jdb, cfg, "b", mode="live", run_id="live-b-09")
        mv = mode_view.load(jdb=jdb, runtime_dir=runtime_dir).sleeve("b")
        assert mv.unknown and not mv.corroborated

    def test_an_unreadable_config_skips_the_digest_check(self, runtime_dir, tmp_path):
        """A reader that cannot read the repo config has lost the ability to say anything
        about integrity; it must not therefore call every overlay a forgery."""
        assert mode_view.config_sha(tmp_path / "nope.yaml") == ""
        write_runtime(runtime_dir, "b", state="TEST",
                      generated_at="2026-10-27T06:00:00Z", config_sha="0" * 64)
        mv = mode_view.load(runtime_dir=runtime_dir,
                            config_path=tmp_path / "nope.yaml").sleeve("b")
        assert mv.overlay == mode_view.OVERLAY_CURRENT


# --------------------------------------------------------------------------- risk_state


class TestRiskState:
    """The gate namespaces every key with ``run:<run_id>:``; host readers did not."""

    def _set(self, jdb, sleeve, key, value):
        db.write(jdb, "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
                      " VALUES (?,?,?,?)", (sleeve, key, value, "2026-10-27T05:00:00Z"))

    def test_the_run_scoped_row_is_found(self, jdb):
        self._set(jdb, "b", "run:live-b-03:day_anchor_nav", "10000")
        got = mode_view.risk_state(jdb, "b", ("day_anchor_nav",), run_id="live-b-03")
        assert got == {"day_anchor_nav": "10000"}

    def test_a_bare_row_is_still_the_fallback(self, jdb):
        self._set(jdb, "b", "day_anchor_nav", "9000")
        got = mode_view.risk_state(jdb, "b", ("day_anchor_nav",), run_id="live-b-03")
        assert got == {"day_anchor_nav": "9000"}

    def test_the_run_scoped_row_wins_over_the_bare_one(self, jdb):
        self._set(jdb, "b", "day_anchor_nav", "9000")
        self._set(jdb, "b", "run:live-b-03:day_anchor_nav", "10000")
        got = mode_view.risk_state(jdb, "b", ("day_anchor_nav",), run_id="live-b-03")
        assert got == {"day_anchor_nav": "10000"}

    def test_a_missing_key_is_absent_not_empty(self, jdb):
        assert mode_view.risk_state(jdb, "b", ("nope",), run_id="r") == {}

    def test_risk_flag_reads_the_run_scoped_lock(self, jdb):
        self._set(jdb, "a", "run:live-a-01:monthly_locked", "1")
        assert mode_view.risk_flag(jdb, "a", "monthly_locked", run_id="live-a-01")

    def test_risk_flag_is_the_union_because_a_lock_fails_closed(self, jdb):
        """A lock recorded under a *different* run id still blocks: it is a lock."""
        self._set(jdb, "a", "monthly_locked", "1")
        assert mode_view.risk_flag(jdb, "a", "monthly_locked", run_id="live-a-99")

    def test_risk_flag_is_false_when_nothing_is_set(self, jdb):
        assert not mode_view.risk_flag(jdb, "a", "monthly_locked", run_id="live-a-01")

    def test_risk_flag_reads_zero_as_unlocked(self, jdb):
        self._set(jdb, "a", "run:live-a-01:monthly_locked", "0")
        assert not mode_view.risk_flag(jdb, "a", "monthly_locked", run_id="live-a-01")

    def test_active_run_id_prefers_the_view(self, jdb, cfg, runtime_dir):
        write_runtime(runtime_dir, "b", state="LIVE_EXECUTE", run_id="live-b-overlay")
        open_run(jdb, cfg, "b", mode="live", run_id="live-b-overlay")
        view = mode_view.load(jdb=jdb, runtime_dir=runtime_dir)
        assert mode_view.active_run_id(jdb, "b", view=view) == "live-b-overlay"

    def test_active_run_id_falls_back_to_the_active_run_row(self, jdb, cfg):
        open_run(jdb, cfg, "b", mode="live", run_id="live-b-row")
        assert mode_view.active_run_id(jdb, "b") == "live-b-row"

    def test_active_run_id_is_empty_with_no_run(self, jdb):
        assert mode_view.active_run_id(jdb, "b") == ""


# --------------------------------------------------------------------------- describe


def test_describe_names_the_reason_so_an_alert_can_carry_it(runtime_dir):
    write_runtime(runtime_dir, "b", state="LIVE_EXECUTE")
    view = mode_view.load(runtime_dir=runtime_dir)
    text = mode_view.describe(view)
    assert "b=live" in text
    assert f"a={mode_view.UNKNOWN}:{mode_view.REASON_NO_EVIDENCE}" in text
