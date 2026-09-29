"""The overnight silence of 2026-09-24, written as the scenario that happened.

Every component was healthy. Containers up, crontab installed, fifteen cron jobs firing on
time, both bots reporting themselves fine, ``liveness()`` answering ``alive``. And the system
had been switched off from the inside for fourteen hours.

The chain, in order, because each link is a separate defect:

1. PEPE/USDT has no PERPETUAL contract. ``runs/ingest.py`` asked
   ``fapi/v1/premiumIndex?symbol=PEPEUSDT`` and Binance answered **400**. One symbol's 400
   failed the **whole** funding phase, and the phase failure stopped the ingest job.
2. With ingest dead, ``knowledge/state/freshness.json`` froze at 06:20:03Z.
3. ``ops/healthcheck.py`` raised a ``data_stale`` flag at 06:00:03Z with
   **``expires_at: null``** — a flag that can never lapse.
4. ``strategies/riskgate.py`` then refused **every** entry: 655 ``blackout:data_stale``
   rejections since 03:00, while the strategy went on finding signals (SUI, XPL, NEAR,
   AAVE) every single cycle.
5. **Nothing alerted.** The console showed no problem. Its process had also died, with
   nothing to restart it.

The gate refusing to trade on stale data is correct and is not touched here. What is tested
here is the part that was missing: the measures that count **outcomes** rather than job
exits, the loud first-class alert, the words on Home, and the supervisor that brings the
console back.

The tests are written as the incident rather than as unit assertions about helpers, because
the defect was never inside one function — it was the absence of a question. Anything that
passes with a frozen sidecar, a non-expiring flag and a wall of refusals present is a
measure that would have caught the real thing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ops import autonomy
from ops.healthcheck import Healthcheck
from ops.lib import flags as flagslib
from ops.lib import freshness as freshlib

from .conftest import NOW

#: The exact line that repeated in ``~/earn-run/logs/ingest.log`` every cycle. It is quoted
#: verbatim because the alert has to carry the real cause, not a paraphrase of it: an
#: operator who reads "funding failed" still has to go digging, and one who reads this does
#: not.
PEPE_400 = ("ingest phase funding failed: Client error '400 Bad Request' for url"
            " 'https://fapi.binance.com/fapi/v1/premiumIndex?symbol=PEPEUSDT'")

#: What the gate actually wrote, 655 times.
BLOCKED_REASON = "blackout:data_stale"

#: The pairs the strategy kept finding while nothing could be bought.
SIGNAL_PAIRS = ("SUI/USDT", "XPL/USDT", "NEAR/USDT", "AAVE/USDT")


def _s(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeApi:
    """A bot that reports itself perfectly healthy, which is what both of them did."""

    def ping(self):
        return True

    def health(self):
        return {"last_process": _s(NOW)}

    def status(self):
        return []

    def show_config(self):
        return {"dry_run": True, "strategy": "EarnStrategy"}

    def stopentry(self):
        return {}

    def cancel_open_order(self, tid):
        return {}


@pytest.fixture
def hc(cfg, dbs):
    """A watchdog wired to an isolated state root, with every dependency captured."""
    root, jdb, kdb = dbs
    sent: list[tuple[str, str, str | None]] = []

    def sender(text, severity, key=None, ttl=60):
        sent.append((severity, text, key))
        return True

    h = Healthcheck(cfg, jdb, kdb, {"a": FakeApi(), "b": FakeApi()},
                    root=root, state_root=root, now=NOW,
                    runner=lambda cmd, timeout=600: 0, sender=sender,
                    spawner=lambda cmd, cwd: 1, deliver=lambda t, s, key=None: True)
    h._set_state("install_utc", _s(NOW - timedelta(days=7)))
    # A real host always has a flags file — the watchdog touches it every five minutes. Its
    # *absence* is its own, separate incident (the gate fails closed on it) and has its own
    # test below; leaving it missing here would make every other case read as blocked.
    flagslib.touch(h.flags_path, now=NOW)
    return h, sent, jdb, kdb, root


def overnight(h, jdb, kdb, *, refusals: int = 655, hours: float = 14.0,
              expires_at: str | None = None, reason: str = BLOCKED_REASON,
              flag: str = "data_stale", set_by: str = "healthcheck") -> datetime:
    """Reproduce the host exactly as it was found, and return when the block began.

    Assembled from the three real artefacts — a frozen freshness sidecar, a ``block_entries``
    flag with no expiry, and a wall of refused entry decisions — rather than from a
    convenience fixture, so nothing here can pass by agreeing with a mock.
    """
    at = NOW - timedelta(hours=hours)

    # 1 and 2: ingest died at the funding phase, and freshness froze with it.
    freshlib.record_many(
        {freshlib.SOURCE_BOOKS: at - timedelta(minutes=20),
         freshlib.candles_source("1h"): at - timedelta(minutes=40)},
        now=at, path=h.freshness_path)
    kdb.execute(
        "INSERT INTO ingest_runs(job, phase, window_start, started_at, finished_at, status,"
        " detail) VALUES ('ingest','candles',?,?,?,'ok',NULL)", (_s(at), _s(at), _s(at)))
    kdb.execute(
        "INSERT INTO ingest_runs(job, phase, window_start, started_at, finished_at, status,"
        " detail) VALUES ('ingest','funding',?,?,?,'error',?)",
        (_s(at), _s(at), _s(at), PEPE_400))
    kdb.commit()

    # 3: a flag nothing can ever lift on a clock.
    flagslib.set_flag(h.flags_path, flag, severity="block_entries",
                      reason="data age 30 min", set_by=set_by,
                      expires_at=expires_at, now=at)

    # 4: one entry allowed before the block, then the gate doing its job into the void.
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,1,'ok','allow')",
                (_s(at - timedelta(minutes=5)), "a", "BTC/USDT", "entry",
                 "confirm_trade_entry"))
    step = timedelta(minutes=hours * 60 / max(refusals, 1))
    for i in range(refusals):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,0,?,'reject')",
                    (_s(at + step * i), "a", SIGNAL_PAIRS[i % len(SIGNAL_PAIRS)], "entry",
                     "confirm_trade_entry", reason))
    jdb.commit()
    return at


def healthy(h, jdb, kdb, *, allowed: int = 4) -> None:
    """The same host, working: fresh data, no blocking flag, entries getting through."""
    kdb.execute("INSERT INTO book_snapshots(pair, captured_at, best_bid, best_ask, mid,"
                " spread_bps) VALUES ('BTC/USDT',?,1,1,1,0)", (_s(NOW),))
    kdb.execute("INSERT OR REPLACE INTO candles(pair, tf, open_time) VALUES"
                " ('BTC/USDT','1h',?)", (int(NOW.timestamp() * 1000),))
    kdb.commit()
    h.refresh_freshness()
    for i in range(allowed):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,1,'ok','allow')",
                    (_s(NOW - timedelta(minutes=i * 5)), "a", "BTC/USDT", "entry",
                     "confirm_trade_entry"))
    jdb.commit()


# ------------------------------------------------------------------ the outcome measures
#
# "Jobs ran on schedule" was true all night. Not one of these numbers would have been.


def test_entries_allowed_versus_refused_is_measured_at_all(hc, cfg, on):
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.entries_refused == 655
    # One, and it was before the block began. The count alone is not the measure — which is
    # exactly why `minutes_since_allowed_entry` and the refusal streak exist beside it.
    assert act.entries_allowed == 1
    assert act.verdict == "not_trading"


def test_time_since_the_last_allowed_entry_is_measured(hc, cfg, on):
    """The single number that would have answered "is it trading?" in one glance."""
    h, _sent, jdb, kdb, root = hc
    at = overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.last_allowed_entry == _s(at - timedelta(minutes=5))
    assert act.minutes_since_allowed_entry == pytest.approx(14 * 60 + 5, abs=1)


def test_the_refusal_streak_has_one_reason_and_a_start(hc, cfg, on):
    h, _sent, jdb, kdb, root = hc
    at = overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.streak == 655 and act.streak_reason == BLOCKED_REASON
    assert act.streak_since == _s(at)


def test_a_flag_that_cannot_expire_is_reported_as_such(hc, cfg, on):
    """``expires_at: null`` is the defect. The measure has to name it, not tolerate it."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    flag = next(f for f in act.blocking_flags if f["name"] == "data_stale")
    assert flag["can_expire"] is False
    assert flag["active_minutes"] == pytest.approx(14 * 60, abs=1)
    assert "no expiry" in flag["clears_when"]
    assert "healthcheck" in flag["clears_when"], "it must say who has to lift it"


def test_a_flag_with_an_expiry_says_when_it_lapses(hc, cfg, on):
    """An expiry is the difference between a pause and a wedge, so it is quoted."""
    h, _sent, jdb, kdb, root = hc
    lapses = _s(NOW + timedelta(minutes=30))
    overnight(h, jdb, kdb, expires_at=lapses)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    flag = next(f for f in act.blocking_flags if f["name"] == "data_stale")
    assert flag["can_expire"] is True
    assert flag["clears_when"] == f"it expires at {lapses}"


def test_a_flag_past_its_expiry_no_longer_blocks(hc, cfg, on):
    """The flag the incident needed. One with an expiry stops blocking on its own."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb, expires_at=_s(NOW - timedelta(hours=1)))
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.blocking_flags == []


def test_time_since_each_source_last_ingested_is_measured(hc, cfg, on):
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    by_name = {s["source"]: s for s in act.sources}
    assert by_name["book_snapshots"]["stale"] is True
    assert by_name["book_snapshots"]["age_minutes"] > 14 * 60
    # The candle allowance is applied here exactly as the gate applies it, so a source shown
    # fresh here is one the gate agrees is fresh.
    assert by_name["candles_1h"]["age_minutes_allowed"] < by_name["candles_1h"]["age_minutes"]


def test_the_failing_ingest_phase_is_named_with_its_own_error(hc, cfg, on):
    """`funding` hours stale while `candles` was minutes old. Nothing put those together."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    phases = {p["phase"]: p for p in act.phases}
    assert phases["funding"]["failing"] is True
    assert "PEPEUSDT" in phases["funding"]["last_error"]
    assert phases["candles"]["failing"] is False


def test_a_caller_holding_no_connections_still_sees_the_failing_phase(hc, cfg, on):
    """The console holds neither database, and it is the surface that has to say *why*.

    This was a real gap found by rendering Home against a real payload: ``acting()`` only
    read ``ingest_runs`` when a caller handed it a knowledge connection, so the watchdog's
    alert named PEPEUSDT and the screen did not. Both open their own now.
    """
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW)   # no jdb, no kdb, no paths
    assert act.entries_refused == 655, "it found the journal on its own"
    funding = next(p for p in act.phases if p["phase"] == "funding")
    assert funding["failing"] is True
    assert "PEPEUSDT" in funding["last_error"], "it found the knowledge DB on its own"


def test_the_headline_is_the_sentence_an_owner_asked_for(hc, cfg, on):
    """"Not trading: data has been stale since 06:00, 655 entries refused." """
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.headline.startswith("Not trading: data has been stale since ")
    assert "655 entries refused." in act.headline
    assert BLOCKED_REASON not in act.headline, "a slug is not plain language"


def test_a_working_host_says_it_is_trading(hc, cfg, on):
    h, _sent, jdb, kdb, root = hc
    healthy(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.verdict == "trading" and act.entries_allowed == 4
    assert act.blocking_flags == []


def test_a_short_refusal_run_is_the_gate_working_not_a_wedge(hc, cfg, on):
    """A ``weight_cap`` refusal on one pair is a limit doing its job. It is not news."""
    h, _sent, jdb, kdb, root = hc
    healthy(h, jdb, kdb, allowed=1)
    for i in range(40):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,0,"
                    "'weight_cap:BTC/USDT','reject')",
                    (_s(NOW - timedelta(seconds=i)), "a", "BTC/USDT", "entry",
                     "confirm_trade_entry"))
    jdb.commit()
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.streak >= 40, "the streak is still counted"
    assert act.verdict == "trading", "but a cap is not the system being switched off"


def test_hours_of_silence_count_even_with_one_old_entry_inside_the_window(hc, cfg, on):
    """A single entry allowed 23h ago must not hide the last three hours of paralysis.

    The measure is keyed on *time since the last allowed entry*, not on a count inside the
    window: an earlier draft used ``entries_allowed == 0`` and would have called this host
    healthy on the strength of one buy from the previous evening.
    """
    h, _sent, jdb, kdb, root = hc
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,1,'ok','allow')",
                (_s(NOW - timedelta(hours=23)), "a", "BTC/USDT", "entry",
                 "confirm_trade_entry"))
    # Too few refusals for the streak rule, and no flag at all for the flag rule.
    for i in range(5):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,0,'staleness','reject')",
                    (_s(NOW - timedelta(hours=3, minutes=i)), "a", "SUI/USDT", "entry",
                     "confirm_trade_entry"))
    jdb.commit()
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.entries_allowed == 1 and act.streak == 5
    assert act.verdict == "not_trading"
    assert "market data is older than the gate allows" in act.headline


def test_yesterdays_wedge_does_not_make_todays_quiet_hour_look_like_one(hc, cfg, on):
    """A REAL false positive, found by running this against the live host after it recovered.

    The morning after the incident the host was trading perfectly — 1524 entries allowed,
    nothing blocking, the newest refusal a plain ``beta_cap``. It still reported
    ``not_trading``, because the *window's* most common refusal was the previous night's
    1516 ``blackout:data_stale`` rows and the dry-spell test asked the window instead of
    asking what had happened since the last entry got through.
    """
    h, _sent, jdb, kdb, root = hc
    # Last night: the wedge. It refused from 20h ago until it was fixed 4h ago.
    flagslib.set_flag(h.flags_path, "data_stale", severity="block_entries",
                      reason="data age 30 min", set_by="healthcheck",
                      now=NOW - timedelta(hours=20))
    for i in range(600):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,0,?,'reject')",
                    (_s(NOW - timedelta(hours=20) + timedelta(seconds=i * 96)), "a",
                     SIGNAL_PAIRS[i % 4], "entry", "confirm_trade_entry", BLOCKED_REASON))
    jdb.commit()
    flagslib.clear_flag(h.flags_path, "data_stale", by="ingest", now=NOW - timedelta(hours=4))
    healthy(h, jdb, kdb, allowed=0)
    # Since then: entries getting through, and the newest refusal an ordinary limit.
    for i in range(40):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,1,'ok','allow')",
                    (_s(NOW - timedelta(hours=3, minutes=i)), "a", "BTC/USDT", "entry",
                     "confirm_trade_entry"))
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,0,'beta_cap','reject')",
                (_s(NOW - timedelta(minutes=1)), "a", "SUI/USDT", "entry",
                 "confirm_trade_entry"))
    jdb.commit()
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.refusals[0]["reason"] == "blackout:data_stale", "the window still remembers"
    assert act.current_reason == "beta_cap", "but the silence now has a different cause"
    assert act.refused_since_last_allowed == 1
    assert act.verdict == "trading", "a fixed problem must not keep raising the alarm"


def test_hours_of_silence_on_a_limit_rather_than_a_wedge_is_not_blocked(hc, cfg, on):
    """A bot that has spent its daily allowance is stopped on purpose, not broken."""
    h, _sent, jdb, kdb, root = hc
    jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                " allowed, reason, severity) VALUES (?,?,?,?,?,1,'ok','allow')",
                (_s(NOW - timedelta(hours=23)), "a", "BTC/USDT", "entry",
                 "confirm_trade_entry"))
    for i in range(30):
        jdb.execute("INSERT INTO gate_decisions(ts_utc, sleeve, pair, intent, callback,"
                    " allowed, reason, severity) VALUES (?,?,?,?,?,0,"
                    "'trades_per_day','reject')",
                    (_s(NOW - timedelta(hours=4, minutes=i)), "a", "SUI/USDT", "entry",
                     "confirm_trade_entry"))
    jdb.commit()
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.streak == 30
    assert act.verdict != "not_trading"


def test_a_flag_scoped_to_one_pair_does_not_claim_the_whole_book(hc, cfg, on):
    """Overstating its own case is how a surface loses the trust it needs when it is right."""
    h, _sent, jdb, kdb, root = hc
    flagslib.set_flag(h.flags_path, "pair_halt", severity="block_entries",
                      reason="the exchange halted the symbol", set_by="reg_watch",
                      scope="SUI/USDT", now=NOW - timedelta(hours=5))
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.verdict == "not_trading"
    assert act.blocked_what == "new entries in SUI/USDT"


def test_every_bot_off_is_idle_not_blocked(hc, cfg):
    """A deliberately switched-off system must never read as a fault."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.verdict == "idle", "no signed state on this root means every bot is off"


def test_an_unreadable_flags_file_is_blocked_not_fine(hc, cfg, on):
    """The gate fails closed on it, so the report has to as well."""
    h, _sent, jdb, kdb, root = hc
    healthy(h, jdb, kdb)
    h.flags_path.write_text("{ not json", encoding="utf-8")
    act = autonomy.acting(cfg, root=root, now=NOW, jdb=jdb, kdb=kdb,
                          flags_path=h.flags_path, freshness_path=h.freshness_path)
    assert act.verdict == "not_trading"
    assert "flags file" in act.headline


# ------------------------------------------------------------------ the loud alert


def test_the_overnight_state_fires_one_loud_critical(hc, cfg, on):
    """655 refusals, fourteen hours, nobody told. This is the alert that did not exist."""
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    h.check_trading_blocked()
    blocked = [t for s, t, _ in sent if s == "critical" and "TRADING IS BLOCKED" in t]
    assert len(blocked) == 1, "the one state that must never be silent was silent"
    text = blocked[0]
    # What is blocked, why, since when, what will clear it. All four, or an operator woken
    # at 3am cannot act on it.
    assert "blocked: every new entry, on both bots" in text
    assert BLOCKED_REASON in text and "data has been stale" in text
    assert "655 refused" in text
    assert "14h 00m ago" in text
    assert "clears when:" in text
    # And the defect itself, named.
    assert "IT HAS NO EXPIRY" in text
    assert "set by healthcheck" in text


def test_the_alert_carries_the_real_cause(hc, cfg, on):
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    h.check_trading_blocked()
    text = next(t for s, t, _ in sent if "TRADING IS BLOCKED" in t)
    assert "ingest phase funding failing" in text
    assert "PEPEUSDT" in text


def test_the_alert_says_the_loop_is_calling_itself_healthy(hc, cfg, on):
    """The sentence that makes it undismissable: every job ran, and nothing happened."""
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    view = h.liveness_view()
    view["verdict"] = "alive"
    h.check_trading_blocked()
    text = next(t for s, t, _ in sent if "TRADING IS BLOCKED" in t)
    assert "AND THE LOOP CALLS ITSELF 'ALIVE'" in text
    assert "achieving nothing" in text


def test_it_alerts_on_the_class_not_on_data_stale(hc, cfg, on):
    """A flag nobody has invented yet, wedged the same way, fires the same alarm.

    This is the property that matters. The check does not know what ``data_stale`` is; it
    knows that entries are being refused, for how long, by what, and what would lift it.
    """
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb, refusals=30, hours=6, flag="some_wedge_invented_next_year",
              reason="blackout:some_wedge_invented_next_year", set_by="some_new_job")
    h.check_trading_blocked()
    text = next(t for s, t, _ in sent if "TRADING IS BLOCKED" in t)
    assert "some_wedge_invented_next_year" in text
    assert "set by some_new_job" in text
    assert "IT HAS NO EXPIRY" in text


def test_a_working_host_is_not_alerted(hc, cfg, on):
    h, sent, jdb, kdb, _root = hc
    healthy(h, jdb, kdb)
    h.check_trading_blocked()
    assert not [t for _, t, _ in sent if "TRADING IS BLOCKED" in t]


def test_recovery_is_said_out_loud_and_the_incident_closes(hc, cfg, on):
    """A block that lifts has to be announced, or the last word on screen is the alarm."""
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    h.check_trading_blocked()
    assert h._state("trading_blocked_since")

    # Clearing the flag is the other agent's half; noticing that it lifted is this one's.
    flagslib.clear_flag(h.flags_path, "data_stale", by="healthcheck", now=NOW)
    healthy(h, jdb, kdb, allowed=1)
    h._liveness_view = None
    h.check_trading_blocked()
    assert any("Trading is possible again" in t for _, t, _ in sent)
    assert not h._state("trading_blocked_since")


def test_the_alert_repeats_hourly_rather_than_deduping_into_a_day_of_silence(hc, cfg, on):
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    h.check_trading_blocked()
    keys = [k for _, t, k in sent if "TRADING IS BLOCKED" in t]
    assert keys == ["trading_blocked"]


def test_an_unmeasurable_outcome_is_reported_not_assumed_fine(hc, monkeypatch):
    """The one state in which this check cannot see, it says so."""
    h, sent, *_ = hc

    def boom(*a, **k):
        raise RuntimeError("no journal")

    monkeypatch.setattr(autonomy, "acting", boom)
    h.check_trading_blocked()
    assert any("cannot measure whether the system is trading" in t for _, t, _ in sent)


def test_check_autonomy_does_not_also_shout_about_the_same_fact(hc, cfg, on):
    """One alarm per fact. Two would teach an operator to ignore both.

    ``check_trading_blocked`` says everything there is to say about a wedge — what, why,
    since when, what clears it. A second, vaguer "AUTONOMY BLOCKED" beside it is noise.
    """
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    h._liveness_view = {"verdict": "blocked", "headline": "TRADING IS BLOCKED. …",
                        "acting": None, "acting_error": None, "supervisor": None}
    h.check_autonomy()
    assert not [t for _, t, _ in sent if t.startswith("AUTONOMY")]


def test_the_whole_tick_still_returns_zero_in_the_overnight_state(hc, cfg, on):
    """A watchdog that crashes on the incident it exists to report is not a watchdog."""
    h, sent, jdb, kdb, _root = hc
    overnight(h, jdb, kdb)
    assert h.run() == 0
    assert any("TRADING IS BLOCKED" in t for _, t, _ in sent)


# ------------------------------------------------------------- the liveness verdict


def test_liveness_calls_a_blocked_system_blocked_not_alive(hc, cfg, on, installed_crontab):
    """"Jobs ran on schedule" was true all night. It must stop being the headline."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    for job in ("ingest", "scanner", "nav_tick", "healthcheck"):
        autonomy.record_finish(job, ok=True, exit_code=0, now=NOW - timedelta(minutes=2),
                               root=root)
    view = autonomy.liveness(cfg, root=root, now=NOW, runner=installed_crontab,
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] == "blocked"
    assert view["headline"].startswith("TRADING IS BLOCKED.")
    assert "655 entries refused" in view["headline"]
    assert view["acting"]["entries_refused"] == 655


def test_liveness_still_leads_with_not_scheduled_when_nothing_is_installed(hc, cfg, on):
    """A host with no crontab has a worse problem than a blocked gate, and says so first."""
    h, _sent, jdb, kdb, root = hc
    overnight(h, jdb, kdb)
    view = autonomy.liveness(cfg, root=root, now=NOW,
                             runner=lambda argv, stdin=None: (1, "", "no crontab"),
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] == "not_scheduled"
    # But the outcome numbers are still carried, so the screen can show both.
    assert view["acting"]["entries_refused"] == 655


def test_liveness_carries_the_outcome_block_even_when_healthy(hc, cfg, on, installed_crontab):
    h, _sent, jdb, kdb, root = hc
    healthy(h, jdb, kdb)
    for job in ("ingest", "scanner", "nav_tick", "healthcheck"):
        autonomy.record_finish(job, ok=True, exit_code=0, now=NOW - timedelta(minutes=2),
                               root=root)
    view = autonomy.liveness(cfg, root=root, now=NOW, runner=installed_crontab,
                             jdb=jdb, kdb=kdb, flags_path=h.flags_path,
                             freshness_path=h.freshness_path)
    assert view["verdict"] != "blocked"
    assert view["acting"]["verdict"] == "trading"
    assert view["acting_error"] is None


def test_a_failed_measurement_is_surfaced_rather_than_silently_green(hc, cfg, on,
                                                                    monkeypatch):
    h, _sent, jdb, kdb, root = hc

    def boom(*a, **k):
        raise RuntimeError("journal exploded")

    monkeypatch.setattr(autonomy, "acting", boom)
    view = autonomy.liveness(cfg, root=root, now=NOW,
                             runner=lambda argv, stdin=None: (1, "", ""))
    assert view["acting"] is None
    assert "journal exploded" in view["acting_error"]


# --------------------------------------------------------- the console that stayed dead


def _systemctl(verdict):
    """A fake ``systemctl``, so each of the probe's four answers can be tested."""
    def runner(argv):
        if verdict == "unknown":
            return 127, "", "systemctl: not found"
        if verdict == "unsupervised":
            return 4, "not-found", ""
        if "--user" not in argv:
            return 4, "not-found", ""
        if "is-enabled" in argv:
            return 0, "enabled", ""
        return (0, "active", "") if verdict == "supervised" else (3, "failed", "")
    return runner


@pytest.mark.parametrize("state,expected", [
    ("supervised", "supervised"),
    ("failing", "failing"),
    ("unsupervised", "unsupervised"),
    ("unknown", "unknown"),
])
def test_the_supervisor_probe_answers_honestly(state, expected):
    assert autonomy.supervisor_status(runner=_systemctl(state))["verdict"] == expected


def test_an_unsupervised_console_is_a_warning_with_the_fix_in_it(hc):
    """It died overnight with nothing to restart it. Worth saying before it happens again."""
    h, sent, *_ = hc
    h._liveness_view = {"verdict": "alive", "acting": None, "acting_error": "not under test",
                        "supervisor": autonomy.supervisor_status(
                            runner=_systemctl("unsupervised"))}
    h.check_console_supervised()
    warn = [t for s, t, _ in sent if s == "warn" and "Nothing supervises the console" in t]
    assert len(warn) == 1
    assert "install-user-units" in warn[0], "an alert without the fix is a complaint"
    assert "goes silent" in warn[0], "it must say why a dead console matters"


def test_a_console_unit_that_is_enabled_and_dead_is_critical(hc):
    h, sent, *_ = hc
    h._liveness_view = {"verdict": "alive", "acting": None, "acting_error": None,
                        "supervisor": autonomy.supervisor_status(
                            runner=_systemctl("failing"))}
    h.check_console_supervised()
    assert any(s == "critical" and "CONSOLE DOWN" in t for s, t, _ in sent)


@pytest.mark.parametrize("state", ["supervised", "unknown"])
def test_a_supervised_or_unprobeable_console_is_silent(hc, state):
    """Unknown is never an alarm: a watchdog that shouts when it cannot see gets muted."""
    h, sent, *_ = hc
    h._liveness_view = {"verdict": "alive", "acting": None, "acting_error": None,
                        "supervisor": autonomy.supervisor_status(runner=_systemctl(state))}
    h.check_console_supervised()
    assert not sent


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def on(cfg, dbs, monkeypatch):
    """Both bots switched on, signed into the test state root.

    Without this every verdict is ``idle``/``off``, which is correct and uninteresting: the
    incident happened with both bots at ``proposing``.
    """
    from ops.lib import autonomy_state as astate

    root = dbs[0]
    (root / "var" / "state").mkdir(parents=True, exist_ok=True)
    (root / "var" / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "locks").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EARN_STATE_ROOT", str(root))
    monkeypatch.setenv("EARN_CONSOLE_SECRET", "test-console-secret-0123456789")
    monkeypatch.delenv("EARN_AUTOMATED_RUN", raising=False)
    astate.write(astate.build(
        {b: astate.BotAutonomy(level="proposing") for b in ("a", "b")},
        set_by="human:cli"))
    return root


@pytest.fixture
def installed_crontab(cfg):
    """A ``crontab -l`` that reads back exactly what this config renders.

    The point of the overnight incident is that the schedule was fine. A runner that
    reported no crontab would let ``not_scheduled`` mask the verdict under test.
    """
    from ops import gen_ops_files

    rendered = gen_ops_files.render_crontab(cfg, gen_ops_files.host_ctx(None))

    def runner(argv, stdin=None):
        if argv[:2] == ["crontab", "-l"]:
            return 0, rendered, ""
        return 1, "", ""
    return runner
