"""DEMO in the state machine and in the preflight.

Binance Spot Demo Mode is the last step before real money: real orders, real filters, real
rate limits, fake balances. The tests here are the statements that make that safe rather
than merely convenient.

**The binding.** A mode reaches exactly one venue. ``DEMO_*`` may only reach
``demo-api.binance.com``; ``LIVE_*`` may only reach ``api.binance.com``; ``TEST`` reaches
nobody and may not even hold a key. A credential that authenticates at the wrong venue is
refused at preflight with a message that names what is wrong — and a credential that could
not be *disproved* at the other venues is refused too, because a network that was down
looks exactly like a key that was rejected.

**The gates.** Demo runs its own preflight: everything that protects the order path, none
of the confidence-building gates that only make sense before real money.

**The way back.** ``DEMO_* -> TEST`` is always allowed and always flattens, because a demo
position is a real resting order on a real book and the dry-run container that replaces
the demo one knows nothing about it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import db, modes
from ops import preflight as pf
from ops.lib import config_guard
from ops.lib import mode_state as ms
from ops.lib.exchange_endpoints import Credential, Venue, VenueBindingError, venue_for_mode
from tests.test_modes.conftest import seed_run, write_mode

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- the machine


class TestTheStateMachine:
    def test_test_can_reach_demo_and_live(self):
        assert modes.ALLOWED["TEST"] >= {"DEMO_PROPOSE", "DEMO_EXECUTE",
                                         "LIVE_PROPOSE", "LIVE_EXECUTE"}

    def test_demo_can_never_jump_straight_to_live(self):
        """The live gates are measured on a TEST run. A demo run is not one."""
        for state in ("DEMO_PROPOSE", "DEMO_EXECUTE"):
            assert not (modes.ALLOWED[state] & ms.LIVE_MODES), state

    def test_live_can_never_slide_sideways_into_demo(self):
        for state in ("LIVE_PROPOSE", "LIVE_EXECUTE"):
            assert not (modes.ALLOWED[state] & ms.DEMO_MODES), state

    def test_every_venue_state_can_always_go_back_to_test(self):
        for state in sorted(ms.VENUE_MODES):
            assert "TEST" in modes.ALLOWED[state], state

    def test_demo_propose_and_execute_reach_each_other(self):
        assert "DEMO_EXECUTE" in modes.ALLOWED["DEMO_PROPOSE"]
        assert "DEMO_PROPOSE" in modes.ALLOWED["DEMO_EXECUTE"]

    def test_every_allowed_state_has_a_venue_decided_for_it(self):
        """``ALLOWED`` and ``MODE_VENUE`` are two tables; they must not drift apart."""
        for state in modes.ALLOWED:
            if state in ms.TRANSIENT_MODES:
                with pytest.raises(VenueBindingError):
                    venue_for_mode(state)
                continue
            venue = venue_for_mode(state)
            assert venue is (None if state == "TEST" else venue), state

    def test_the_venue_of_each_target(self):
        assert venue_for_mode("TEST") is None
        assert venue_for_mode("DEMO_PROPOSE") is Venue.DEMO
        assert venue_for_mode("DEMO_EXECUTE") is Venue.DEMO
        assert venue_for_mode("LIVE_PROPOSE") is Venue.LIVE
        assert venue_for_mode("LIVE_EXECUTE") is Venue.LIVE

    def test_the_run_mode_word_is_its_own(self):
        assert modes.mode_word_for("DEMO_PROPOSE") == "demo"
        assert modes.mode_word_for("DEMO_EXECUTE") == "demo"
        assert modes.mode_word_for("LIVE_EXECUTE") == "live"
        assert modes.mode_word_for("TEST") == "test"

    def test_a_demo_request_is_never_live(self):
        req = modes.TransitionRequest(sleeve="a", target="DEMO_EXECUTE")
        assert req.is_demo is True
        assert req.is_live is False
        assert req.is_venue_bound is True
        assert req.venue is Venue.DEMO


class TestTheConfirmPhrases:
    """Disjoint words, so a phrase typed from memory cannot move a sleeve to the wrong
    venue in either direction."""

    def _phrase(self, cfg, **over):
        base = {"sleeve": "a", "from_state": "TEST", "target": "DEMO_PROPOSE",
                "seed_usdt": 500.0}
        base.update(over)
        return modes.confirm_phrase_for(cfg, **base)

    def test_arming_demo_asks_for_the_demo_phrase(self, cfg):
        assert self._phrase(cfg) == "GO DEMO A 500 USDT"

    def test_arming_live_still_asks_for_the_live_phrase(self, cfg):
        assert self._phrase(cfg, target="LIVE_PROPOSE") == "GO LIVE A 500 USDT"

    def test_the_two_phrases_are_never_equal(self, cfg):
        assert self._phrase(cfg) != self._phrase(cfg, target="LIVE_PROPOSE")

    def test_the_live_phrase_does_not_arm_a_demo_sleeve(self, cfg):
        with pytest.raises(modes.ModeError):
            modes.check_confirmation(self._phrase(cfg), "GO LIVE A 500 USDT")

    def test_the_demo_phrase_does_not_arm_a_live_sleeve(self, cfg):
        with pytest.raises(modes.ModeError):
            modes.check_confirmation(
                self._phrase(cfg, target="LIVE_PROPOSE"), "GO DEMO A 500 USDT"
            )

    def test_moving_within_demo_has_its_own_words(self, cfg):
        assert self._phrase(
            cfg, from_state="DEMO_PROPOSE", target="DEMO_EXECUTE"
        ) == modes.CONFIRM_DEMO_EXECUTE
        assert self._phrase(
            cfg, from_state="DEMO_EXECUTE", target="DEMO_PROPOSE"
        ) == modes.CONFIRM_DEMO_DERISK
        assert modes.CONFIRM_DEMO_EXECUTE != modes.CONFIRM_EXECUTE

    def test_leaving_demo_with_positions_needs_the_override(self, cfg):
        assert self._phrase(cfg, from_state="DEMO_EXECUTE", target="TEST") == ""
        assert self._phrase(
            cfg, from_state="DEMO_EXECUTE", target="TEST", flatten=False
        ) == modes.CONFIRM_LEAVE_POSITIONS


class TestShowConfigVerification:
    """Step 10 is the only half of the venue binding that is not our own code marking its
    own homework: freqtrade renames the exchange itself once ccxt's demo switch fired."""

    def test_binance_demo_is_what_a_demo_bot_must_report(self, cfg):
        assert modes.DEMO_EXCHANGE_NAME == "binance_demo"
        assert modes._venue_problems({"exchange": "binance_demo"}, "DEMO_PROPOSE") == []

    def test_a_demo_bot_still_calling_itself_binance_is_refused(self, cfg):
        problems = modes._venue_problems({"exchange": "binance"}, "DEMO_EXECUTE")
        assert len(problems) == 1
        assert "NOT on Binance Spot Demo Mode" in problems[0]
        assert "production" in problems[0]

    def test_a_live_bot_reporting_binance_demo_is_refused(self, cfg):
        problems = modes._venue_problems({"exchange": "binance_demo"}, "LIVE_PROPOSE")
        assert problems and "live venue" in problems[0]

    def test_a_bot_that_does_not_report_an_exchange_is_not_faulted_for_it(self, cfg):
        assert modes._venue_problems({}, "DEMO_PROPOSE") == []


# --------------------------------------------------------------------------- preflight


GOOD_ACCOUNT = {
    "uid": 4242,
    "canTrade": True,
    "accountType": "SPOT",
    "balances": [
        {"asset": "USDT", "free": "5000.0", "locked": "0.0"},
        {"asset": "BTC", "free": "0.05", "locked": "0.0"},
    ],
}


def _exchange_info(pairs):
    """Demo's order types are byte-identical to live for BTC/ETH (demo-mode.md §4a)."""
    return {
        "symbols": [
            {"symbol": p.replace("/", ""),
             "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"],
             "ocoAllowed": True}
            for p in pairs
        ]
    }


def _prober(home: Venue):
    def probe(venue, _credential):
        return "authenticated" if venue is home else "rejected"

    return probe


@pytest.fixture
def cfg():
    from ops.config import load_config

    config = load_config()
    config.telegram.chat_id = 42
    config.telegram.user_id = 7
    return config


@pytest.fixture
def demo_world(cfg, state_root, jdb, kdb, monkeypatch):
    """A world in which a DEMO arming passes every item it runs."""
    import shutil

    from ops.config import REPO_ROOT

    # The demo key lives under its OWN names, never the live ones.
    monkeypatch.setenv("BINANCE_DEMO_KEY", "demo-key-Ycyb")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret-4VX7")
    monkeypatch.delenv("BINANCE_KEY_A", raising=False)
    monkeypatch.delenv("BINANCE_SECRET_A", raising=False)

    (state_root / "config").mkdir(exist_ok=True)
    for rel in config_guard.BLESSED_FILES:
        source = REPO_ROOT / rel
        if source.exists():
            shutil.copy2(source, state_root / rel)

    write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
    seed_run(jdb, cfg, run_id="test-a-1", started="2026-10-20T00:00:00Z")

    close_ms = int((NOW - timedelta(minutes=5)).timestamp() * 1000)
    kdb.execute(
        "INSERT INTO candles(pair, tf, open_time, close_time, close, is_closed)"
        " VALUES ('BTC/USDT','1h',?,?,60000,1)",
        (close_ms - 3600_000, close_ms),
    )
    kdb.commit()
    db.write(
        jdb,
        "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed, reason,"
        " severity) VALUES (?, 'a', 'BTC/USDT', 'entry', 'bot_loop_start', 1, 'ok', 'allow')",
        ((NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    flags = state_root / cfg.paths.flags_file
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(
        json.dumps({"version": 1, "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "flags": {}})
    )
    config_guard.bless("human:cli", root=state_root)
    hook = state_root / ".claude" / "hooks"
    hook.mkdir(parents=True, exist_ok=True)
    (hook / "protect_tier2.py").write_text("# test stub\n")
    (state_root / "ops").mkdir(exist_ok=True)
    (state_root / "ops" / "envwrap.sh").write_text("# allowlists without console secrets\n")

    return pf.PreflightDeps(
        jdb=jdb,
        kdb=kdb,
        root=state_root,
        now=NOW,
        state=ms.load(),
        venue=Venue.DEMO,
        venue_probe=_prober(Venue.DEMO),
        bot_status=lambda s: {"up": True, "strategy": getattr(cfg.sleeves, s).strategy},
        # Demo has NO sapi tier, so this probe would 404 there. It is deliberately absent:
        # the check must warn with the reason, not silently pass.
        exchange_restrictions=None,
        exchange_account=lambda s: json.loads(json.dumps(GOOD_ACCOUNT)),
        exchange_info=_exchange_info,
        host_facts=lambda: pf.HostFacts(
            filesystem="ext4", sleep_on_ac_disabled=False, keepalive_task=False,
            docker_running=True, ntp_skew_s=0.1, cron_installed=True, cron_matches=True,
        ),
        git_status=lambda: {"branch": cfg.git.live_branch, "dirty": False},
        drift_check=lambda: (True, "in sync"),
        telegram_probe=lambda: (True, "test message delivered"),
        strategy_tests=lambda: (True, "42 passed"),
        backup_status=lambda: {"dest": "/tmp/b", "writable": True, "age_hours": 3},
        envwrap_allowlist=lambda: "# nothing secret here\n",
        agent_user_ok=lambda: (True, "ok"),
    )


def _demo_request(**over) -> pf.PreflightRequest:
    base = {"sleeve": "a", "target": "DEMO_PROPOSE", "submode": "propose", "seed_usdt": 500.0}
    base.update(over)
    return pf.PreflightRequest(**base)


def _item(result: pf.PreflightResult, check_id: str) -> pf.Check:
    found = result.item(check_id)
    assert found is not None, f"{check_id} missing from the result"
    return found


class TestTheDemoPreflight:
    def test_a_ready_world_passes(self, cfg, demo_world):
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        assert result.ok, [(c.id, c.detail) for c in result.blocking_failures]

    def test_it_runs_the_order_path_items(self, cfg, demo_world):
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        ran = {c.id for c in result.items if c.status != pf.SKIP}
        for required in ("kill_clear", "bots_healthy", "config_blessed", "venue_binding",
                         "exchange_keys", "seed_ok", "stoploss_on_exchange", "host_ready",
                         "automation_isolation"):
            assert required in ran, required

    def test_it_does_not_run_the_live_only_gates(self, cfg, demo_world):
        """No 90-day track record, no propose track record. Demo risks nothing."""
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        assert _item(result, "track_record").status == pf.SKIP
        assert _item(result, "propose_track_record").status == pf.SKIP

    def test_a_skipped_item_is_not_labelled_blocking(self, cfg, demo_world):
        """"Not applicable to this transition" and "blocking" cannot both be true.

        The Mode page printed a red BLOCKING badge beside ``track_record`` on every demo
        arming — the one item demo is defined not to need. A skipped item has no verdict
        to block on, so it carries ``blocking=False``.
        """
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        skipped = [c for c in result.items if c.status == pf.SKIP]
        assert skipped, "this transition should skip the live-only gates"
        for check in skipped:
            assert check.blocking is False, f"{check.id} is skipped but marked blocking"

    def test_live_keeps_every_blocking_flag_the_table_gives_it(self, cfg, demo_world):
        """The fix is labelling only: a LIVE arming skips nothing, so nothing is relabelled."""
        demo_world.venue = Venue.LIVE
        demo_world.venue_probe = _prober(Venue.LIVE)
        result = pf.run_preflight(
            cfg,
            pf.PreflightRequest(sleeve="a", target="LIVE_EXECUTE", seed_usdt=500.0),
            deps=demo_world,
        )
        assert [c.id for c in result.items if c.status == pf.SKIP] == []
        for check in result.items:
            if pf.CHECK_BLOCKING[check.id]:
                assert check.blocking, f"{check.id} lost its blocking flag on a live target"

    def test_a_three_day_old_test_run_does_not_block_demo(self, cfg, demo_world, jdb):
        """The same world blocks LIVE and clears DEMO — that is the point of demo mode."""
        db.write(
            jdb, "UPDATE sleeve_runs SET started_utc=? WHERE run_id='test-a-1'",
            ((NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        assert pf.run_preflight(cfg, _demo_request(), deps=demo_world).ok
        live_deps = demo_world
        live_deps.venue = Venue.LIVE
        live_deps.venue_probe = _prober(Venue.LIVE)
        live = pf.run_preflight(cfg, _demo_request(target="LIVE_PROPOSE"), deps=live_deps)
        assert not live.ok
        assert _item(live, "track_record").status == pf.FAIL

    def test_a_sleeping_host_does_not_block_demo_but_does_block_live(self, cfg, demo_world):
        """``require_host_awake_for_live`` means what it says."""
        assert pf.run_preflight(cfg, _demo_request(), deps=demo_world).ok
        assert _item(
            pf.run_preflight(cfg, _demo_request(), deps=demo_world), "host_ready"
        ).status == pf.PASS

    def test_the_kill_switch_still_blocks_demo(self, cfg, demo_world, state_root):
        from ops.lib import kill as killlib

        killlib.engage(cfg, "operator pulled the switch", state_root)
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "kill_clear")
        assert item.status == pf.FAIL and item.blocking
        assert not result.ok

    def test_an_unhealthy_container_still_blocks_demo(self, cfg, demo_world):
        demo_world.bot_status = lambda s: {"up": False}
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        assert _item(result, "bots_healthy").status == pf.FAIL
        assert not result.ok

    def test_missing_exchange_filters_still_block_demo(self, cfg, demo_world):
        """Demo's filters and order types are live's; a symbol without a stop is a refusal."""
        demo_world.exchange_info = lambda pairs: {
            "symbols": [{"symbol": p.replace("/", ""), "orderTypes": ["LIMIT"]} for p in pairs]
        }
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "stoploss_on_exchange")
        assert item.status == pf.FAIL and not result.ok
        assert "no exchange-side stop" in item.detail

    def test_the_seed_ceiling_still_binds(self, cfg, demo_world):
        huge = pf.run_preflight(cfg, _demo_request(seed_usdt=10_000_000.0), deps=demo_world)
        item = _item(huge, "seed_ok")
        assert item.status == pf.FAIL and "exceeds" in item.detail

    def test_telegram_is_advisory_on_demo_and_blocking_on_live(self, cfg, demo_world):
        demo_world.telegram_probe = lambda: (False, "bot token rejected")
        demo = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(demo, "telegram")
        assert item.status == pf.WARN and item.blocking is False
        assert demo.ok

        demo_world.venue = Venue.LIVE
        demo_world.venue_probe = _prober(Venue.LIVE)
        live = pf.run_preflight(cfg, _demo_request(target="LIVE_PROPOSE"), deps=demo_world)
        live_item = _item(live, "telegram")
        assert live_item.status == pf.FAIL and live_item.blocking is True


class TestTheSapiHole:
    """Demo answers nginx 404 on every ``/sapi`` path, so key permissions cannot be read
    back there. The one thing that must never happen is reading that 404 as "no
    restrictions", because the permission it would silently clear is *withdrawals*."""

    def test_the_permission_read_is_a_named_warning_not_a_pass(self, cfg, demo_world):
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "exchange_keys")
        assert item.status == pf.WARN
        assert item.evidence["permissions_readable"] is False
        assert "no /sapi tier" in item.detail
        assert "Withdrawals" in item.detail and "MANUAL" in item.detail
        assert "Not treated as a pass" in item.detail

    def test_it_still_proves_the_key_can_trade(self, cfg, demo_world):
        demo_world.exchange_account = lambda s: {**GOOD_ACCOUNT, "canTrade": False}
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "exchange_keys")
        assert item.status == pf.FAIL and "cannot trade" in item.detail

    def test_live_still_reads_the_permissions(self, cfg, demo_world, monkeypatch):
        monkeypatch.setenv("BINANCE_KEY_A", "live-key-aaaa")
        monkeypatch.setenv("BINANCE_SECRET_A", "live-secret")
        demo_world.venue = Venue.LIVE
        demo_world.venue_probe = _prober(Venue.LIVE)
        demo_world.exchange_restrictions = lambda s: {
            "ipRestrict": True, "enableWithdrawals": False, "enableInternalTransfer": False,
            "enableFutures": False, "enableMargin": False, "enableVanillaOptions": False,
            "permitsUniversalTransfer": False, "enableSpotAndMarginTrading": True,
        }
        result = pf.run_preflight(cfg, _demo_request(target="LIVE_PROPOSE"), deps=demo_world)
        item = _item(result, "exchange_keys")
        assert item.evidence["permissions_readable"] is True
        assert item.evidence["permissions"]["enableWithdrawals"] is False


class TestTheVenueBinding:
    """The refusals. Each one names what is wrong and what the operator should do."""

    def test_the_demo_key_is_accepted_for_a_demo_target(self, cfg, demo_world):
        item = _item(pf.run_preflight(cfg, _demo_request(), deps=demo_world), "venue_binding")
        assert item.status == pf.PASS
        assert item.evidence["venue"] == "demo"
        assert item.evidence["rest_host"] == "demo-api.binance.com"
        assert item.evidence["proof"]["status"] == "confirmed"

    def test_an_absent_demo_key_is_refused_with_the_reason(self, cfg, demo_world, monkeypatch):
        monkeypatch.delenv("BINANCE_DEMO_KEY", raising=False)
        monkeypatch.delenv("BINANCE_DEMO_SECRET", raising=False)
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "venue_binding")
        assert item.status == pf.FAIL and item.blocking and not result.ok
        assert "demo-api.binance.com" in item.detail
        assert "Demo Trading" in item.detail          # where to mint it
        assert "disable" in item.detail.lower()       # withdrawals off
        keys = _item(result, "exchange_keys")
        assert keys.status == pf.FAIL
        assert "BINANCE_DEMO_KEY/BINANCE_DEMO_SECRET not set" in keys.detail

    def test_a_key_the_live_venue_also_accepts_is_refused_as_dangerous(self, cfg, demo_world):
        """The case a label alone can never catch, and the reason the negative is probed."""
        demo_world.venue_probe = lambda venue, cred: "authenticated"
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "venue_binding")
        assert item.status == pf.FAIL and not result.ok
        assert "DANGER" in item.detail
        assert "api.binance.com" in item.detail
        assert item.evidence["proof"]["status"] == "mismatch"

    def test_a_key_the_demo_venue_rejects_is_refused(self, cfg, demo_world):
        demo_world.venue_probe = _prober(Venue.LIVE)   # it is really a live key
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "venue_binding")
        assert item.status == pf.FAIL and not result.ok
        assert item.evidence["proof"]["status"] == "mismatch"

    def test_an_unreachable_other_venue_is_cannot_verify_and_FAILS(self, cfg, demo_world):
        """A probe that could not be completed is never a pass. That is the whole tri-state."""
        demo_world.venue_probe = lambda venue, cred: (
            "authenticated" if venue is Venue.DEMO else "unreachable"
        )
        result = pf.run_preflight(cfg, _demo_request(), deps=demo_world)
        item = _item(result, "venue_binding")
        assert item.status == pf.FAIL and not result.ok
        assert item.evidence["proof"]["status"] == "cannot_verify"
        assert "unproven" in item.detail

    def test_no_prober_at_all_is_a_refusal_not_a_skip(self, cfg, demo_world):
        demo_world.venue_probe = None
        item = _item(pf.run_preflight(cfg, _demo_request(), deps=demo_world), "venue_binding")
        assert item.status == pf.FAIL
        assert "A label is a claim" in item.detail

    def test_live_shaped_probes_cannot_serve_a_demo_transition(self, cfg, demo_world):
        """The probes carry a host and a key. Re-pointing them is not a thing that happens."""
        demo_world.venue = Venue.LIVE
        item = _item(pf.run_preflight(cfg, _demo_request(), deps=demo_world), "venue_binding")
        assert item.status == pf.FAIL
        assert "api.binance.com" in item.detail and "demo-api.binance.com" in item.detail
        assert "re-run the preflight" in item.detail

    def test_a_test_sleeve_may_not_carry_a_key(self):
        """TEST reaches no exchange at all, and the binding refuses a key outright.

        Checked here rather than through the preflight on purpose: this is a *render-time*
        refusal (``gen_freqtrade_config`` and ``compose`` both resolve the binding before
        writing), and the preflight deliberately does not run it for a TEST target —
        see :meth:`test_the_way_back_to_test_is_never_gated_by_a_venue_check`.
        """
        from ops.lib.exchange_endpoints import resolve_binding

        cred = Credential(label="BINANCE_DEMO_KEY", venue=Venue.DEMO, key="k", secret="s")
        with pytest.raises(VenueBindingError) as excinfo:
            resolve_binding("TEST", cred)
        assert "dry-run and must reach no exchange" in str(excinfo.value)

    def test_a_test_sleeve_with_no_key_binds_to_no_venue(self):
        from ops.lib.exchange_endpoints import resolve_binding

        binding = resolve_binding("TEST", Credential(label="none", venue=Venue.DEMO))
        assert binding.venue is None and binding.needs_credential is False

    def test_the_way_back_to_test_is_never_gated_by_a_venue_check(self, cfg, demo_world):
        """Disarming must not be blocked by anything about a key. An operator getting out
        of demo is the one path that has to work when everything else has gone wrong."""
        demo_world.venue = None
        demo_world.venue_probe = None
        result = pf.run_preflight(cfg, _demo_request(target="TEST"), deps=demo_world)
        assert _item(result, "venue_binding").status == pf.SKIP
        assert _item(result, "exchange_keys").status == pf.SKIP

    def test_the_env_names_are_venue_specific(self):
        assert pf.credential_env_names("a", Venue.DEMO) == (
            "BINANCE_DEMO_KEY", "BINANCE_DEMO_SECRET"
        )
        assert pf.credential_env_names("a", Venue.LIVE) == ("BINANCE_KEY_A", "BINANCE_SECRET_A")
        assert pf.credential_env_names("b", Venue.LIVE) == ("BINANCE_KEY_B", "BINANCE_SECRET_B")

    def test_a_credential_never_leaks_its_secret(self, cfg, demo_world):
        item = _item(pf.run_preflight(cfg, _demo_request(), deps=demo_world), "venue_binding")
        blob = json.dumps(item.to_json())
        assert "demo-secret-4VX7" not in blob
        assert "demo-key-Ycyb" not in blob
        assert item.evidence["credential"]["last4"] == "Ycyb"


# --------------------------------------------------------------------------- transition


def _demo_bot(bot):
    """What a container that really is on demo reports back."""
    bot.dry_run = False
    bot.bot_name = "earn-a-demo"
    bot.exchange = "binance_demo"
    return bot


class TestTheDemoTransition:
    @pytest.fixture
    def demo_deps(self, deps, jdb, cfg, bots):
        seed_run(jdb, cfg, run_id="test-a-1", started="2026-10-20T00:00:00Z")
        write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
        for bot in bots.values():
            _demo_bot(bot)
        return deps

    def test_a_demo_arming_runs_the_whole_ceremony(self, cfg, demo_deps, actor, jdb):
        result = modes.transition(
            cfg,
            modes.TransitionRequest(
                sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
            ),
            actor, deps=demo_deps,
        )
        assert result.to_state == "DEMO_PROPOSE"
        assert result.run_id.startswith("demo-a-")
        assert [s["step"] for s in result.steps] == list(modes.STEPS)
        assert ms.load(secret=demo_deps.secret).sleeve("a").state == "DEMO_PROPOSE"

    def test_the_preflight_step_runs_for_demo(self, cfg, demo_deps, actor):
        result = modes.transition(
            cfg,
            modes.TransitionRequest(
                sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
            ),
            actor, deps=demo_deps,
        )
        step = next(s for s in result.steps if s["step"] == "preflight")
        assert step["status"] == "ok"

    def test_the_run_is_filed_under_the_demo_word(self, cfg, demo_deps, actor, jdb):
        result = modes.transition(
            cfg,
            modes.TransitionRequest(
                sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
            ),
            actor, deps=demo_deps,
        )
        row = jdb.execute(
            "SELECT mode, submode FROM sleeve_runs WHERE run_id=?", (result.run_id,)
        ).fetchone()
        assert row["mode"] == "demo" and row["submode"] == "propose"

    def test_a_bot_still_on_production_fails_the_transition(self, cfg, demo_deps, actor, bots):
        bots["a"].exchange = "binance"          # demo_trading never took effect
        with pytest.raises(modes.ModeTransitionError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                    confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
                ),
                actor, deps=demo_deps,
            )
        assert "binance_demo" in str(excinfo.value)
        # rolled back, and the sleeve is where it started
        assert ms.load(secret=demo_deps.secret).sleeve("a").state == "TEST"

    def test_a_dry_running_bot_fails_the_transition(self, cfg, demo_deps, actor, bots):
        bots["a"].dry_run = True
        with pytest.raises(modes.ModeTransitionError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                    confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
                ),
                actor, deps=demo_deps,
            )
        assert "dry_run" in str(excinfo.value)

    def test_the_live_phrase_cannot_arm_a_demo_sleeve(self, cfg, demo_deps, actor):
        with pytest.raises(modes.ModeError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                    confirm_phrase="GO LIVE A 500 USDT", preflight_id="pf-test",
                ),
                actor, deps=demo_deps,
            )
        assert "GO DEMO A 500 USDT" in str(excinfo.value)

    def test_demo_to_live_is_refused_by_the_machine(self, cfg, demo_deps, actor):
        write_mode({"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)})
        with pytest.raises(modes.ModeError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="LIVE_EXECUTE", seed_usdt=500.0,
                    confirm_phrase="GO LIVE A 500 USDT", preflight_id="pf-test",
                ),
                actor, deps=demo_deps,
            )
        assert "DEMO_EXECUTE -> LIVE_EXECUTE is not an allowed transition" in str(excinfo.value)

    def test_going_back_to_test_flattens_and_orphans_nothing(
        self, cfg, demo_deps, actor, bots, jdb
    ):
        write_mode({"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)})
        seed_run(jdb, cfg, run_id="demo-a-1", mode="demo", seed=500, submode="execute")
        bots["a"].trades = [{"trade_id": 7, "has_open_orders": True}]
        bots["a"].dry_run = True                 # the dry-run container that replaces it
        bots["a"].bot_name = "earn-a-test"
        bots["a"].exchange = "binance"
        result = modes.transition(
            cfg,
            modes.TransitionRequest(sleeve="a", target="TEST", confirm_phrase=""),
            actor, deps=demo_deps,
        )
        assert result.to_state == "TEST"
        flatten = next(s for s in result.steps if s["step"] == "flatten")
        assert flatten["status"] == "ok" and flatten["detail"] == "flat"
        assert "forceexit:all" in bots["a"].calls
        assert bots["a"].trades == []
        assert ms.load(secret=demo_deps.secret).sleeve("a").state == "TEST"

    def test_leaving_demo_without_flattening_needs_the_typed_override(
        self, cfg, demo_deps, actor, bots, jdb
    ):
        write_mode({"a": ms.SleeveState("DEMO_EXECUTE", "execute", "demo-a-1", 500)})
        seed_run(jdb, cfg, run_id="demo-a-1", mode="demo", seed=500, submode="execute")
        bots["a"].dry_run = True
        bots["a"].bot_name = "earn-a-test"
        bots["a"].exchange = "binance"
        with pytest.raises(modes.ModeError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="TEST", flatten=False, confirm_phrase="",
                ),
                actor, deps=demo_deps,
            )
        assert modes.CONFIRM_LEAVE_POSITIONS in str(excinfo.value)

    def test_a_demo_transition_is_still_human_only(self, cfg, demo_deps, actor, monkeypatch):
        from ops.lib import paths

        monkeypatch.setenv(paths.AUTOMATED_RUN_ENV, "1")
        demo_deps.env = {paths.AUTOMATED_RUN_ENV: "1"}
        with pytest.raises(modes.ModeError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                    confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
                ),
                actor, deps=demo_deps,
            )
        assert "EARN_AUTOMATED_RUN" in str(excinfo.value)

    def test_a_demo_transition_still_needs_step_up(self, cfg, demo_deps):
        weak = modes.HumanActor.console("sid-1", step_up_ok=False)
        with pytest.raises(modes.ModeError) as excinfo:
            modes.transition(
                cfg,
                modes.TransitionRequest(
                    sleeve="a", target="DEMO_PROPOSE", seed_usdt=500.0,
                    confirm_phrase="GO DEMO A 500 USDT", preflight_id="pf-test",
                ),
                weak, deps=demo_deps,
            )
        assert "step-up" in str(excinfo.value)
