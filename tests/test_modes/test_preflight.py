"""The 13-item preflight: every blocking item, failing one at a time.

The shape of these tests mirrors the rule the preflight exists to enforce — *a check that
could not be completed is never a pass*. So the baseline fixture makes every probe succeed,
and each test breaks exactly one of them and asserts that this one item fails, that it is
blocking, and that the whole preflight is therefore not ``ok``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ops import db
from ops import preflight as pf
from ops.lib import config_guard
from ops.lib import mode_state as ms
from ops.lib.exchange_endpoints import Venue
from tests.test_modes.conftest import seed_run, write_mode

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)

GOOD_RESTRICTIONS = {
    "ipRestrict": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "enableFutures": False,
    "enableMargin": False,
    "enableVanillaOptions": False,
    "permitsUniversalTransfer": False,
    "enableSpotAndMarginTrading": True,
}
GOOD_ACCOUNT = {
    "uid": 4242,
    "balances": [
        {"asset": "USDT", "free": "5000.0", "locked": "0.0"},
        {"asset": "BTC", "free": "0.05", "locked": "0.0"},
    ],
}


def _exchange_info(pairs):
    return {
        "symbols": [
            {"symbol": p.replace("/", ""), "orderTypes": ["LIMIT", "STOP_LOSS_LIMIT"]}
            for p in pairs
        ]
    }


def venue_prober(home):
    """A prober that authenticates the key at ``home`` and is refused everywhere else.

    That is what ``verify_credential_venue`` demands for ``confirmed``: the positive AND
    every negative. A prober that simply said "authenticated" everywhere would be a key
    that works on production too, which is a ``mismatch``, not a pass.
    """

    def probe(venue, _credential):
        return "authenticated" if venue is home else "rejected"

    return probe


def _host_ok() -> pf.HostFacts:
    return pf.HostFacts(
        filesystem="ext4", sleep_on_ac_disabled=True, keepalive_task=True,
        docker_running=True, ntp_skew_s=0.1, cron_installed=True, cron_matches=True,
    )


@pytest.fixture
def cfg():
    """The repo config with Telegram wired up — an unconfigured bot is its own test."""
    from ops.config import load_config

    config = load_config()
    config.telegram.chat_id = 42
    config.telegram.user_id = 7
    return config


@pytest.fixture
def ready(cfg, state_root, jdb, kdb, monkeypatch):
    """A world where every blocking preflight item passes."""
    import shutil

    from ops.config import REPO_ROOT

    monkeypatch.setenv("BINANCE_KEY_A", "test-key-a")
    monkeypatch.setenv("BINANCE_SECRET_A", "test-secret-a")
    monkeypatch.setenv("BINANCE_KEY_B", "test-key-b")
    monkeypatch.setenv("BINANCE_SECRET_B", "test-secret-b")
    # the blessed files are digested relative to the root, so give the temp root a copy
    (state_root / "config").mkdir(exist_ok=True)
    for rel in config_guard.BLESSED_FILES:
        source = REPO_ROOT / rel
        if source.exists():
            shutil.copy2(source, state_root / rel)

    write_mode({"a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000)})
    seed_run(jdb, cfg, run_id="test-a-1", started="2026-01-01T00:00:00Z")

    # fresh data, an active gate and a long clean streak
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

    # a readable, non-stale flags file with nothing blocking
    flags = state_root / cfg.paths.flags_file
    flags.parent.mkdir(parents=True, exist_ok=True)
    flags.write_text(
        json.dumps({"version": 1, "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "flags": {}})
    )
    # a blessed config, and the tier-2 hook in place
    config_guard.bless("human:cli", root=state_root)
    hook = state_root / ".claude" / "hooks"
    hook.mkdir(parents=True, exist_ok=True)
    (hook / "protect_tier2.py").write_text("# test stub\n")
    (state_root / "ops").mkdir(exist_ok=True)
    (state_root / "ops" / "envwrap.sh").write_text("# allowlists without console secrets\n")

    deps = pf.PreflightDeps(
        jdb=jdb,
        kdb=kdb,
        root=state_root,
        now=NOW,
        state=ms.load(),
        venue=Venue.LIVE,
        venue_probe=venue_prober(Venue.LIVE),
        bot_status=lambda s: {"up": True, "strategy": getattr(cfg.sleeves, s).strategy},
        exchange_restrictions=lambda s: dict(GOOD_RESTRICTIONS),
        exchange_account=lambda s: json.loads(json.dumps(GOOD_ACCOUNT)),
        exchange_info=_exchange_info,
        host_facts=_host_ok,
        git_status=lambda: {"branch": cfg.git.live_branch, "dirty": False},
        drift_check=lambda: (True, "in sync"),
        telegram_probe=lambda: (True, "test message delivered"),
        strategy_tests=lambda: (True, "42 passed"),
        backup_status=lambda: {"dest": "/tmp/b", "writable": True, "age_hours": 3},
        envwrap_allowlist=lambda: "# nothing secret here\n",
        agent_user_ok=lambda: (True, "ok"),
    )
    return deps


def _request(**over) -> pf.PreflightRequest:
    base = {"sleeve": "a", "target": "LIVE_PROPOSE", "submode": "propose", "seed_usdt": 500.0}
    base.update(over)
    return pf.PreflightRequest(**base)


def _run(cfg, deps, **over) -> pf.PreflightResult:
    return pf.run_preflight(cfg, _request(**over), deps=deps)


def _item(result: pf.PreflightResult, check_id: str) -> pf.Check:
    found = result.item(check_id)
    assert found is not None, f"{check_id} missing from the result"
    return found


class TestBaseline:
    def test_everything_passes(self, cfg, ready):
        result = _run(cfg, ready)
        failed = [(c.id, c.detail) for c in result.items if c.status == pf.FAIL]
        assert failed == []
        assert result.ok
        assert len(result.items) == len(pf.CHECK_ORDER)
        # propose mode skips the EXECUTE-only item
        assert _item(result, "propose_track_record").status == pf.SKIP

    def test_the_id_expires_in_ten_minutes(self, cfg, ready):
        result = _run(cfg, ready)
        created = datetime.fromisoformat(result.created_utc.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(result.expires_utc.replace("Z", "+00:00"))
        assert expires - created == timedelta(minutes=pf.PREFLIGHT_TTL_MINUTES)
        assert not result.expired(NOW)
        assert result.expired(NOW + timedelta(minutes=11))

    def test_the_baseline_balances_are_captured(self, cfg, ready):
        result = _run(cfg, ready)
        assert result.baseline == {"BTC": 0.05}

    def test_disarming_skips_the_go_live_gauntlet(self, cfg, ready):
        result = _run(cfg, ready, target="TEST", submode=None)
        assert _item(result, "exchange_keys").status == pf.SKIP
        assert _item(result, "seed_ok").status == pf.SKIP
        assert _item(result, "kill_clear").status == pf.PASS


class TestEachBlockingItem:
    def test_kill_engaged(self, cfg, ready, state_root):
        from ops.lib import kill

        kill.engage(cfg, "manual test", state_root)
        result = _run(cfg, ready)
        assert _item(result, "kill_clear").status == pf.FAIL
        assert not result.ok

    def test_block_entries_flag(self, cfg, ready, state_root):
        from ops.lib import flags

        flags.set_flag(
            state_root / cfg.paths.flags_file, "reconcile_mismatch:a",
            severity="block_entries", reason="ledger drift", set_by="system:test", now=NOW,
        )
        assert _item(_run(cfg, ready), "kill_clear").status == pf.FAIL

    def test_stale_market_data(self, cfg, ready, kdb):
        kdb.execute("DELETE FROM candles")
        kdb.commit()
        item = _item(_run(cfg, ready), "kill_clear")
        assert item.status == pf.FAIL and "no candle data" in item.detail

    def test_monthly_lock(self, cfg, ready, jdb):
        db.write(
            jdb,
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','monthly_locked','1','2026-10-01T00:00:00Z')",
            (),
        )
        assert "monthly loss lock" in _item(_run(cfg, ready), "kill_clear").detail

    def test_a_run_scoped_monthly_lock_still_blocks(self, cfg, ready, jdb):
        """Verified HIGH: the blocking ``kill_clear`` monthly-lock check was INERT.

        ``RiskGate`` wraps its store in ``NamespacedStateStore`` whenever the runtime file
        names a run id — which it always does for a real run — so the gate writes
        ``run:<run_id>:monthly_locked``. The bare query found nothing, the evidence read
        ``monthly_locked: false``, the check passed, and a human was cleared to arm a
        sleeve sitting under its own monthly loss lock. ``ops.lib.risk_resume`` read the
        same value correctly, so the Risk page and the preflight disagreed about one fact.
        """
        db.write(
            jdb,
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','run:test-a-1:monthly_locked','1','2026-10-01T00:00:00Z')",
            (),
        )
        item = _item(_run(cfg, ready), "kill_clear")
        assert item.status == pf.FAIL
        assert "monthly loss lock" in item.detail
        assert item.evidence["monthly_locked"] is True
        assert item.evidence["risk_state_run_id"] == "test-a-1"

    def test_a_lock_from_a_different_run_does_not_block(self, cfg, ready, jdb):
        """Scoped, not just prefix-blind: a lock the live gate itself cannot see (it
        belongs to another run's namespace) is not this run's lock."""
        db.write(
            jdb,
            "INSERT INTO risk_state(sleeve, key, value, updated_utc)"
            " VALUES ('a','run:test-a-OLD:monthly_locked','1','2026-01-01T00:00:00Z')",
            (),
        )
        item = _item(_run(cfg, ready), "kill_clear")
        assert item.evidence["monthly_locked"] is False
        assert item.status == pf.PASS

    def test_an_unprovable_current_mode_refuses_to_arm(self, cfg, ready):
        """``mode_view``'s table has always said ``ops.preflight``: "refuse to arm".

        No such check existed, so a sleeve whose *present* mode could not be established
        at all — no verified mode file, nothing rendered in ``var/runtime``, nothing in
        the journal — could still be walked into LIVE. Arming is a transition *from* a
        state; a state the machine cannot name is not one to leave blind.
        """
        from ops.lib import mode_state as ms_

        ready.state = ms_.default_state(ms_.REASON_NO_SECRET)
        ready.jdb = None
        item = _item(_run(cfg, ready), "kill_clear")
        assert item.status == pf.FAIL
        assert "refusing to arm from an unprovable state" in item.detail
        assert item.evidence["mode_liveness"] == "unknown"

    def test_a_provable_mode_arms_normally(self, cfg, ready):
        """The console holds the secret, so the signed authority answers and this never
        fires in the normal case — the baseline fixture is that case."""
        item = _item(_run(cfg, ready), "kill_clear")
        assert item.status == pf.PASS
        assert item.evidence["mode_liveness"] == "test"

    def test_disarming_is_never_blocked_by_not_knowing(self, cfg, ready):
        """The way back to TEST must not depend on proving where we are."""
        from ops.lib import mode_state as ms_

        ready.state = ms_.default_state(ms_.REASON_NO_SECRET)
        ready.jdb = None
        item = _item(_run(cfg, ready, target="TEST", submode=None), "kill_clear")
        assert item.status == pf.PASS

    def test_bot_down(self, cfg, ready):
        ready.bot_status = lambda s: {"up": s != "a"}
        assert _item(_run(cfg, ready), "bots_healthy").status == pf.FAIL

    def test_scaffold_strategy_is_refused(self, cfg, ready):
        ready.bot_status = lambda s: {"up": True, "strategy": "Scaffold"}
        item = _item(_run(cfg, ready), "bots_healthy")
        assert item.status == pf.FAIL and "Scaffold" in item.detail

    def test_no_recent_gate_decision(self, cfg, ready, jdb):
        jdb.execute("DELETE FROM gate_decisions")
        jdb.commit()
        assert "no gate decision" in _item(_run(cfg, ready), "bots_healthy").detail

    def test_unblessed_config(self, cfg, ready, state_root):
        (state_root / "var" / "state" / "config.bless.json").unlink()
        item = _item(_run(cfg, ready), "config_blessed")
        assert item.status == pf.FAIL and "not blessed" in item.detail

    def test_dirty_git_tree(self, cfg, ready):
        ready.git_status = lambda: {"branch": cfg.git.live_branch, "dirty": True}
        assert "dirty" in _item(_run(cfg, ready), "config_blessed").detail

    def test_wrong_branch(self, cfg, ready):
        ready.git_status = lambda: {"branch": "some/other", "dirty": False}
        assert "expected" in _item(_run(cfg, ready), "config_blessed").detail

    def test_generated_config_drift(self, cfg, ready):
        ready.drift_check = lambda: (False, "config/riskgate.json")
        assert "drifted" in _item(_run(cfg, ready), "config_blessed").detail

    def test_withdrawal_enabled_key(self, cfg, ready):
        ready.exchange_restrictions = lambda s: {**GOOD_RESTRICTIONS, "enableWithdrawals": True}
        item = _item(_run(cfg, ready), "exchange_keys")
        assert item.status == pf.FAIL and "enableWithdrawals" in item.detail

    def test_spot_trading_disabled(self, cfg, ready):
        ready.exchange_restrictions = lambda s: {
            **GOOD_RESTRICTIONS, "enableSpotAndMarginTrading": False
        }
        assert "enableSpotAndMarginTrading" in _item(_run(cfg, ready), "exchange_keys").detail

    def test_futures_enabled(self, cfg, ready):
        ready.exchange_restrictions = lambda s: {**GOOD_RESTRICTIONS, "enableFutures": True}
        assert "enableFutures" in _item(_run(cfg, ready), "exchange_keys").detail

    def test_ip_restriction_off_is_only_a_warning(self, cfg, ready):
        ready.exchange_restrictions = lambda s: {**GOOD_RESTRICTIONS, "ipRestrict": False}
        item = _item(_run(cfg, ready), "exchange_keys")
        assert item.status == pf.WARN and "ipRestrict" in item.detail

    def test_one_live_sleeve_per_account(self, cfg, ready):
        write_mode(
            {
                "a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000),
                "b": ms.SleeveState("LIVE_PROPOSE", "propose", "live-b-1", 500),
            }
        )
        ready.state = ms.load()
        item = _item(_run(cfg, ready), "exchange_keys")
        assert item.status == pf.FAIL and "already live on account 4242" in item.detail

    def test_different_accounts_are_fine(self, cfg, ready):
        write_mode(
            {
                "a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000),
                "b": ms.SleeveState("LIVE_PROPOSE", "propose", "live-b-1", 500),
            }
        )
        ready.state = ms.load()
        ready.exchange_account = lambda s: {**GOOD_ACCOUNT, "uid": 4242 if s == "a" else 9999}
        assert _item(_run(cfg, ready), "exchange_keys").status in (pf.PASS, pf.WARN)

    def test_unreadable_uid_is_not_assumed_unique(self, cfg, ready):
        write_mode(
            {
                "a": ms.SleeveState("TEST", run_id="test-a-1", seed_usdt=10000),
                "b": ms.SleeveState("LIVE_PROPOSE", "propose", "live-b-1", 500),
            }
        )
        ready.state = ms.load()
        ready.exchange_account = lambda s: {"balances": GOOD_ACCOUNT["balances"]}
        assert "cannot read this account's UID" in _item(_run(cfg, ready), "exchange_keys").detail

    def test_seed_above_the_ceiling(self, cfg, ready):
        ceiling = cfg.modes.live.max_seed_usdt["a"]
        item = _item(_run(cfg, ready, seed_usdt=ceiling + 1), "seed_ok")
        assert item.status == pf.FAIL and "max_seed_usdt" in item.detail

    def test_seed_below_four_times_min_notional(self, cfg, ready):
        item = _item(_run(cfg, ready, seed_usdt=cfg.risk.min_notional_usdt), "seed_ok")
        assert item.status == pf.FAIL and "min_notional_usdt" in item.detail

    def test_not_enough_free_quote(self, cfg, ready):
        ready.exchange_account = lambda s: {
            "uid": 4242, "balances": [{"asset": "USDT", "free": "10.0", "locked": "0.0"}]
        }
        item = _item(_run(cfg, ready), "seed_ok")
        assert item.status == pf.FAIL and "< seed" in item.detail

    def test_track_record_too_short(self, cfg, ready, jdb):
        db.write(
            jdb, "UPDATE sleeve_runs SET started_utc=? WHERE run_id='test-a-1'",
            ((NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        item = _item(_run(cfg, ready), "track_record")
        assert item.status == pf.FAIL and "min_test_days" in item.detail

    def test_recent_breach_blocks(self, cfg, ready, jdb):
        db.write(
            jdb,
            "INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback, allowed, reason,"
            " severity) VALUES (?, 'a', 'BTC/USDT', 'entry', 'confirm_trade_entry', 0,"
            " 'daily_loss_stop', 'breach')",
            ((NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        item = _item(_run(cfg, ready), "track_record")
        assert item.status == pf.FAIL and "breach" in item.detail

    def test_override_needs_a_typed_reason(self, cfg, ready, jdb):
        db.write(
            jdb, "UPDATE sleeve_runs SET started_utc=? WHERE run_id='test-a-1'",
            ((NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        assert _item(_run(cfg, ready), "track_record").status == pf.FAIL
        overridden = _item(
            _run(cfg, ready, override_reason="G3 waived: 3 clean months on testnet"),
            "track_record",
        )
        assert overridden.status == pf.WARN
        assert overridden.overridden is True
        assert "G3 waived" in overridden.detail
        assert _run(cfg, ready, override_reason="G3 waived: 3 clean months on testnet").ok

    def test_telegram_unconfigured(self, cfg, ready):
        broken = cfg.model_copy(deep=True)
        broken.telegram.chat_id = 0
        assert _item(_run(broken, ready), "telegram").status == pf.FAIL

    def test_telegram_undelivered(self, cfg, ready):
        ready.telegram_probe = lambda: (False, "bot token rejected")
        assert _item(_run(cfg, ready), "telegram").status == pf.FAIL

    def test_pair_without_exchange_stops(self, cfg, ready):
        ready.exchange_info = lambda pairs: {
            "symbols": [{"symbol": "BTCUSDT", "orderTypes": ["LIMIT"]}]
        }
        item = _item(_run(cfg, ready), "stoploss_on_exchange")
        assert item.status == pf.FAIL and "BTC/USDT" in item.detail

    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("filesystem", "9p", "filesystem"),
            ("sleep_on_ac_disabled", False, "sleep"),
            ("keepalive_task", False, "keep-alive"),
            ("docker_running", False, "docker"),
            ("ntp_skew_s", 9.0, "skew"),
            ("cron_installed", False, "crontab is not installed"),
            ("cron_matches", False, "differs"),
        ],
    )
    def test_host_readiness(self, cfg, ready, field, value, needle):
        def facts() -> pf.HostFacts:
            f = _host_ok()
            setattr(f, field, value)
            return f

        ready.host_facts = facts
        item = _item(_run(cfg, ready), "host_ready")
        assert item.status == pf.FAIL and needle in item.detail

    def test_leaked_console_secret_in_envwrap(self, cfg, ready):
        ready.envwrap_allowlist = lambda: "EARN_CONSOLE_SECRET EARN_APPROVAL_KEY\n"
        item = _item(_run(cfg, ready), "automation_isolation")
        assert item.status == pf.FAIL and "EARN_CONSOLE_SECRET" in item.detail

    def test_missing_tier2_hook(self, cfg, ready, state_root):
        (state_root / ".claude" / "hooks" / "protect_tier2.py").unlink()
        assert "hook is not installed" in _item(_run(cfg, ready), "automation_isolation").detail

    def test_unwritable_backup_destination_blocks(self, cfg, ready):
        ready.backup_status = lambda: {"dest": "/mnt/d", "writable": False}
        item = _item(_run(cfg, ready), "backups")
        assert item.status == pf.FAIL and item.blocking is True

    def test_old_backup_only_warns(self, cfg, ready):
        ready.backup_status = lambda: {"dest": "/tmp/b", "writable": True, "age_hours": 99}
        item = _item(_run(cfg, ready), "backups")
        assert item.status == pf.WARN and item.blocking is False
        assert _run(cfg, ready).ok

    def test_red_strategy_tests_warn_in_propose(self, cfg, ready):
        ready.strategy_tests = lambda: (False, "3 failed")
        item = _item(_run(cfg, ready), "strategy_tests")
        assert item.status == pf.WARN and not item.blocking
        assert _run(cfg, ready).ok


class TestExecuteOnlyItems:
    @pytest.fixture
    def in_propose(self, cfg, ready, jdb):
        write_mode({"a": ms.SleeveState("LIVE_PROPOSE", "propose", "live-a-1", 500)})
        jdb.execute("DELETE FROM sleeve_runs")
        seed_run(
            jdb, cfg, run_id="live-a-1", mode="live", seed=500, submode="propose",
            started=(NOW - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for i in range(10):
            db.write(
                jdb,
                "INSERT INTO proposal_approvals(run_id, decision, decided_utc, actor, channel,"
                " sig, expires_utc) VALUES (?,?,?,'human:cli','console','sig','2099-01-01T00:00:00Z')",
                (
                    f"p{i}",
                    "approve" if i < 9 else "reject",
                    (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
        ready.state = ms.load()
        return ready

    def test_execute_passes_with_the_track_record(self, cfg, in_propose):
        result = _run(cfg, in_propose, target="LIVE_EXECUTE", submode="execute")
        item = _item(result, "propose_track_record")
        assert item.status == pf.PASS, item.detail

    def test_execute_needs_enough_propose_days(self, cfg, in_propose, jdb):
        db.write(
            jdb, "UPDATE sleeve_runs SET started_utc=? WHERE run_id='live-a-1'",
            ((NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        item = _item(
            _run(cfg, in_propose, target="LIVE_EXECUTE", submode="execute"),
            "propose_track_record",
        )
        assert item.status == pf.FAIL and "min_propose_days" in item.detail

    def test_execute_needs_a_high_approval_rate(self, cfg, in_propose, jdb):
        db.write(jdb, "UPDATE proposal_approvals SET decision='reject'", ())
        item = _item(
            _run(cfg, in_propose, target="LIVE_EXECUTE", submode="execute"),
            "propose_track_record",
        )
        assert item.status == pf.FAIL and "approval rate" in item.detail

    def test_execute_needs_the_agent_user(self, cfg, in_propose):
        item = _item(
            _run(cfg, in_propose, target="LIVE_EXECUTE", submode="execute"),
            "automation_isolation",
        )
        assert item.status == pf.FAIL and "agent_user" in item.detail

    def test_execute_blocks_on_red_strategy_tests(self, cfg, in_propose):
        in_propose.strategy_tests = lambda: (False, "3 failed")
        item = _item(
            _run(cfg, in_propose, target="LIVE_EXECUTE", submode="execute"), "strategy_tests"
        )
        assert item.status == pf.FAIL and item.blocking is True


class TestRobustness:
    def test_a_probe_that_raises_is_a_failure_not_a_pass(self, cfg, ready):
        def boom(_sleeve):
            raise RuntimeError("binance timed out")

        ready.exchange_restrictions = boom
        item = _item(_run(cfg, ready), "exchange_keys")
        assert item.status == pf.FAIL and "binance timed out" in item.detail

    def test_missing_probes_fail_closed(self, cfg, state_root, jdb, kdb):
        result = pf.run_preflight(
            cfg, _request(), deps=pf.PreflightDeps(jdb=jdb, kdb=kdb, root=state_root, now=NOW)
        )
        assert not result.ok
        for check_id in ("bots_healthy", "exchange_keys", "telegram", "host_ready"):
            assert _item(result, check_id).status == pf.FAIL

    def test_result_round_trips_through_json(self, cfg, ready):
        result = _run(cfg, ready)
        payload = json.loads(pf.preflight_to_json(result))
        assert payload["preflight_id"] == result.preflight_id
        assert len(payload["items"]) == len(pf.CHECK_ORDER)
