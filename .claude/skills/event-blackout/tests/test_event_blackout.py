"""event-blackout: window arithmetic, the staleness alarm, the scrape and the event study.

The fixtures are real: ``fomccalendars_slice.html`` is the statement-link lines of the live
federalreserve.gov calendar page, ``bls_cpi.json`` is a live keyless BLS response, and
``btc_1h_2024-08_2026-09.json`` is 18,805 contiguous Binance BTC/USDT hourly closes. The
golden numbers below were measured on them, not chosen.

Run from the repo root: ``pytest .claude/skills/event-blackout/tests``
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runs.features import macro_calendar as MC  # noqa: E402
from runs.features.venue import SourceDown  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_DIR / "tests" / "fixtures"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL_DIR / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


macro_state = _load_script("macro_state")
fomc_study = _load_script("fomc_study")

EV = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)


# --------------------------------------------------------------------- window arithmetic


class TestAsymmetricWindow:
    @pytest.mark.parametrize("offset_h,inside", [
        (-8, False), (-7, True), (-6, True), (-1, True), (0, True),
        (4, True), (8, True), (8.5, False), (12, False),
    ])
    def test_boundaries(self, offset_h, inside):
        now = EV + timedelta(hours=offset_h)
        assert MC.in_macro_blackout(now, EV, 420, 480) is inside

    def test_symmetric_60_covers_only_the_peak_hour(self):
        """The shipped config, measured against the profile it is supposed to cover."""
        assert MC.in_macro_blackout(EV, EV, 60, 60)
        assert not MC.in_macro_blackout(EV - timedelta(hours=2), EV, 60, 60)
        assert not MC.in_macro_blackout(EV + timedelta(hours=3), EV, 60, 60)
        # the same instants ARE covered by the measured width
        assert MC.in_macro_blackout(EV - timedelta(hours=2), EV, 420, 480)
        assert MC.in_macro_blackout(EV + timedelta(hours=3), EV, 420, 480)

    def test_active_blackout_returns_the_end_of_the_window(self):
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"}])
        got = MC.active_blackout(EV + timedelta(hours=2), events, 420, 480)
        assert got is not None
        ev, until = got
        assert ev.name == "FOMC_Oct" and ev.kind == "FOMC"
        assert until == EV + timedelta(minutes=480)

    def test_no_event_no_blackout(self):
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"}])
        assert MC.active_blackout(EV + timedelta(days=5), events, 420, 480) is None


class TestCalendarStaleness:
    def test_coverage_is_to_the_furthest_future_event(self):
        events = MC.parse_events([{"name": "A", "at": "2026-10-01T00:00:00Z"},
                                  {"name": "B", "at": "2026-12-01T00:00:00Z"}])
        assert MC.calendar_stale_days(datetime(2026, 9, 23, tzinfo=UTC), events) == 69

    def test_exhausted_calendar_is_zero_not_infinite(self):
        events = MC.parse_events([{"name": "A", "at": "2020-01-01T00:00:00Z"}])
        assert MC.calendar_stale_days(datetime(2026, 9, 23, tzinfo=UTC), events) == 0

    def test_empty_calendar_is_zero(self):
        assert MC.calendar_stale_days(datetime(2026, 9, 23, tzinfo=UTC), []) == 0

    def test_state_raises_the_alarm_and_says_it_first(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "macro_calendar.yaml").write_text(
            "events:\n  - { name: FOMC_Soon, at: \"2026-10-01T18:00:00Z\" }\n")
        state = macro_state.build_state(tmp_path, now=datetime(2026, 9, 23, tzinfo=UTC))
        assert state["calendar_stale"] is True
        assert state["calendar_stale_days"] == 8
        assert "future coverage" in state["warnings"][0]

    def test_malformed_row_is_skipped_not_fatal(self):
        events = MC.parse_events([{"name": "ok", "at": "2026-10-01T00:00:00Z"},
                                  {"name": "bad", "at": "not-a-date"},
                                  {"nope": 1}])
        assert [e.name for e in events] == ["ok"]


class TestDST:
    def test_release_time_shifts_with_dst(self):
        winter, summer = MC.fomc_release_times(["20260128", "20260617"])
        assert winter.hour == 19 and summer.hour == 18
        assert winter.tzinfo is not None and summer.tzinfo is not None

    def test_ordering_and_bad_input(self):
        assert MC.fomc_release_times(["20260617", "20260128"])[0].month == 1
        assert MC.fomc_release_times(["nonsense", "2026"]) == []


# --------------------------------------------------------------------- the sources


class TestScrape:
    def test_regex_still_yields_at_least_40_dates(self):
        html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
        dates = MC.fomc_dates_from_html(html)
        assert len(dates) >= 40
        assert all(len(d) == 8 and d.isdigit() for d in dates)
        assert dates == sorted(set(dates))

    def test_scheduled_meetings_parse_forward(self):
        """The statement-link scrape only knows the PAST. Forward audit needs the panels."""
        html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
        released = MC.fomc_dates_from_html(html)
        scheduled = MC.fomc_scheduled_from_html(html)
        assert len(scheduled) >= 40
        # the released set stops at the last meeting that has happened; the schedule does not
        assert max(scheduled) > max(released)
        assert "20261028" in scheduled and "20261209" in scheduled

    def test_two_day_meeting_resolves_to_its_last_day(self):
        html = ('<h4>2026 FOMC Meetings</h4>'
                '<div class="fomc-meeting__month"><strong>October</strong></div>'
                '<div class="fomc-meeting__date">27-28</div>')
        assert MC.fomc_scheduled_from_html(html) == ["20261028"]

    def test_meeting_across_a_month_boundary_rolls_over(self):
        html = ('<h4>2026 FOMC Meetings</h4>'
                '<div class="fomc-meeting__month"><strong>April</strong></div>'
                '<div class="fomc-meeting__date">29-1</div>')
        assert MC.fomc_scheduled_from_html(html) == ["20260501"]

    def test_scheduled_scrape_is_included_in_the_fetch(self):
        html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
        dates, status = MC.fetch_fomc_dates(
            fetcher=lambda _u: (200, html.encode()))
        assert status["n_scheduled"] >= 40
        assert status["ok"] is True
        assert len(dates) > len(MC.fomc_dates_from_html(html))

    def test_unreachable_page_degrades_and_never_fails_open(self):
        def dead(_url):
            raise SourceDown("federalreserve.gov unreachable")

        dates, status = MC.fetch_fomc_dates(fetcher=dead)
        assert dates == []
        assert status["ok"] is False
        assert "current" in status["pages_failed"]

    def test_page_that_returns_html_with_no_dates_is_a_failure_not_a_success(self):
        """The /json/ne-fomccalendar.json trap: a 404 that returns an HTML body."""
        def poisoned(_url):
            return 200, b"<html><body>Page not found</body></html>"

        dates, status = MC.fetch_fomc_dates(fetcher=poisoned)
        assert dates == []
        assert status["ok"] is False
        assert "zero dates" in status["pages_failed"]["current"]

    def test_cached_schedule_survives_an_unreachable_source(self, tmp_path):
        """Never fail open: an outage yields the last known list, flagged, not an empty one."""
        html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
        now = datetime(2026, 9, 23, tzinfo=UTC)
        good, st = MC.fetch_fomc_dates_cached(
            tmp_path, fetcher=lambda _u: (200, html.encode()), now=now)
        assert good and st["stale"] is False and st["source"] == "scrape"

        def dead(_url):
            raise SourceDown("federalreserve.gov unreachable")

        after, st2 = MC.fetch_fomc_dates_cached(
            tmp_path, fetcher=dead, now=now + timedelta(hours=2))
        assert after == good, "the last known schedule must stand"
        assert st2["source"] == "cache" and st2["stale"] is False
        assert "blackout still applies" in st2["note"]

        old, st3 = MC.fetch_fomc_dates_cached(
            tmp_path, fetcher=dead, now=now + timedelta(days=30))
        assert old == good and st3["stale"] is True

    def test_no_scrape_and_no_cache_says_unaudited_not_clear(self, tmp_path):
        def dead(_url):
            raise SourceDown("down")

        dates, st = MC.fetch_fomc_dates_cached(tmp_path, fetcher=dead)
        assert dates == []
        assert st["stale"] is True and st["source"] == "none"
        assert "not the same as the calendar being right" in st["note"]

    def test_bls_is_post_hoc_only(self):
        payload = json.loads((FIXTURES / "bls_cpi.json").read_text())
        got = MC.bls_latest_cpi(fetcher=lambda _u: (200, json.dumps(payload).encode()))
        assert got["latest"] is True
        assert float(got["value"]) > 0
        # nothing in the response can say when the NEXT release is
        assert "next" not in json.dumps(got).lower()

    def test_bls_failure_raises_rather_than_guessing(self):
        def bad(_url):
            return 200, b'{"status": "REQUEST_FAILED"}'

        with pytest.raises(SourceDown):
            MC.bls_latest_cpi(fetcher=bad)


class TestAudit:
    def test_missing_calendar_row_is_reported(self):
        now = datetime(2026, 9, 23, tzinfo=UTC)
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"}])
        scraped = MC.fomc_release_times(["20261028", "20261209"])
        a = MC.audit_calendar(events, scraped, now=now)
        assert len(a["matched"]) == 1
        assert a["missing"] == ["2026-12-09T19:00:00Z"]
        assert a["ok"] is False

    def test_horizon_keeps_missing_actionable(self):
        """A quarterly-maintained calendar must not be told about 2027 every day."""
        now = datetime(2026, 9, 23, tzinfo=UTC)
        scraped = MC.fomc_release_times(["20261028", "20261209", "20270127", "20271027"])
        a = MC.audit_calendar([], scraped, now=now)
        assert "2027-10-27T18:00:00Z" not in a["missing"]
        assert "2026-10-28T18:00:00Z" in a["missing"]

    def test_the_shipped_calendar_matches_the_published_schedule(self):
        """Regression against the real thing: 0.0 minutes of drift when this was written."""
        now = datetime(2026, 9, 23, tzinfo=UTC)
        html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
        scraped = MC.fomc_release_times(MC.fomc_scheduled_from_html(html))
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"},
                                  {"name": "FOMC_Dec", "at": "2026-12-09T19:00:00Z"}])
        a = MC.audit_calendar(events, scraped, now=now)
        assert {m["name"] for m in a["matched"]} == {"FOMC_Oct", "FOMC_Dec"}
        assert all(m["drift_min"] == 0.0 for m in a["matched"])
        assert a["drifted"] == []

    def test_time_drift_is_reported_not_corrected(self):
        now = datetime(2026, 9, 23, tzinfo=UTC)
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T12:00:00Z"}])
        a = MC.audit_calendar(events, MC.fomc_release_times(["20261028"]), now=now)
        assert a["drifted"] and a["drifted"][0]["drift_min"] == -360.0
        assert a["ok"] is False

    def test_clean_calendar_passes(self):
        now = datetime(2026, 9, 23, tzinfo=UTC)
        events = MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"}])
        a = MC.audit_calendar(events, MC.fomc_release_times(["20261028"]), now=now)
        assert a["ok"] is True and not a["missing"] and not a["drifted"]


# --------------------------------------------------------------------- the event study


class TestEventStudy:
    @pytest.fixture(scope="class")
    @classmethod
    def real(cls):
        bars, src = fomc_study.load_fixture_bars(FIXTURES / "btc_1h_2024-08_2026-09.json")
        events, _ = fomc_study.event_times(REPO_ROOT, offline=True)
        return MC.event_study(bars, events), src

    def test_golden_values_on_the_real_slice(self, real):
        s, _ = real
        assert s["n_bars"] == 18805
        assert s["n_events"] == 18
        assert s["unconditional_abs_ret_pct"] == pytest.approx(0.2058, abs=1e-4)
        assert s["window_abs_ratio"] == pytest.approx(1.618, abs=1e-3)

    def test_the_release_bar_is_the_peak(self, real):
        s, _ = real
        peak = max(s["profile"], key=lambda p: p["multiple"] or 0)
        assert peak["h"] == 0
        assert peak["multiple"] > 2.0

    def test_direction_is_reported_and_is_not_significant(self, real):
        s, _ = real
        assert abs(s["direction_t24h"]["t"]) < 2.0
        assert abs(s["direction_t1h"]["t"]) < 2.0
        assert "never to be traded on" in s["direction_note"]

    def test_smoothing_survives_dropping_any_single_event(self):
        """The window must not be a function of one noisy bucket."""
        bars, _ = fomc_study.load_fixture_bars(FIXTURES / "btc_1h_2024-08_2026-09.json")
        events, _ = fomc_study.event_times(REPO_ROOT, offline=True)
        inside = [e for e in events
                  if "2024-08" <= e.strftime("%Y-%m") <= "2026-09"]
        base = MC.suggested_window(MC.event_study(bars, inside))
        seen = set()
        for i in range(len(inside)):
            w = MC.suggested_window(MC.event_study(bars, inside[:i] + inside[i + 1:]))
            seen.add((w["pre_minutes"], w["post_minutes"]))
        assert len(seen) <= 3, f"window is unstable across leave-one-out: {seen}"
        assert (base["pre_minutes"], base["post_minutes"]) in seen

    def test_flat_series_recommends_no_extra_width(self):
        bars = [(datetime.fromtimestamp(1_700_000_000 + i * 3600, UTC)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
                 100.0 * math.exp(0.001 * (i % 2 * 2 - 1))) for i in range(4000)]
        ev = [datetime.fromtimestamp(1_700_000_000 + i * 3600, UTC)
              for i in range(200, 4000, 400)]
        w = MC.suggested_window(MC.event_study(bars, ev, pre_h=6, post_h=6))
        assert w["pre_minutes"] == 0 and w["post_minutes"] == 60

    def test_planted_bump_is_found_and_bounded(self):
        """A series with an elevated T-2h..T+3h run must recommend exactly that run."""
        n, step = 6000, 3600
        rets = [0.0005 * (1 if i % 2 else -1) for i in range(n)]
        ev_idx = list(range(300, n - 100, 300))
        for i in ev_idx:
            for h in range(-2, 4):
                rets[i + h] *= 6.0
        closes, px = [], 100.0
        for r in rets:
            px *= (1 + r)
            closes.append(px)
        bars = [(datetime.fromtimestamp(1_700_000_000 + i * step, UTC)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"), c) for i, c in enumerate(closes)]
        ev = [datetime.fromtimestamp(1_700_000_000 + i * step, UTC) for i in ev_idx]
        w = MC.suggested_window(MC.event_study(bars, ev, pre_h=8, post_h=8))
        assert w["pre_minutes"] == 120, w
        assert w["post_minutes"] == 240, w

    def test_no_events_returns_an_empty_study_not_a_crash(self):
        bars = [("2026-01-01T00:00:00Z", 100.0), ("2026-01-01T01:00:00Z", 101.0)]
        s = MC.event_study(bars, [])
        assert s["n_events"] == 0

    def test_no_bars_returns_nothing_rather_than_a_number(self):
        s = MC.event_study([], [datetime(2026, 1, 1, tzinfo=UTC)])
        assert s["n_bars"] == 0 and s["unconditional_abs_ret_pct"] is None


# --------------------------------------------------------------------- the scripts


class TestScripts:
    def test_macro_state_selftest_passes(self, capsys):
        assert macro_state.run_selftest() == 0
        assert "verdict=OK" in capsys.readouterr().out

    def test_fomc_study_selftest_passes(self, capsys):
        assert fomc_study.run_selftest() == 0
        assert "verdict=OK" in capsys.readouterr().out

    def test_state_uses_the_shipped_window_by_default(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "macro_calendar.yaml").write_text(
            "events:\n  - { name: FOMC_X, at: \"2026-10-28T18:00:00Z\" }\n")
        s = macro_state.build_state(tmp_path, now=datetime(2026, 10, 28, 18, 0, tzinfo=UTC))
        assert s["in_blackout"] is True
        assert s["window"]["symmetric"] is True
        assert any("symmetric" in w for w in s["warnings"])

    def test_state_accepts_the_measured_asymmetric_width(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "macro_calendar.yaml").write_text(
            "events:\n  - { name: FOMC_X, at: \"2026-10-28T18:00:00Z\" }\n")
        at = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)   # T-6h
        narrow = macro_state.build_state(tmp_path, now=at, pre_min=60, post_min=60)
        wide = macro_state.build_state(tmp_path, now=at, pre_min=420, post_min=480)
        assert narrow["in_blackout"] is False
        assert wide["in_blackout"] is True
        assert wide["window"]["symmetric"] is False

    def test_missing_calendar_is_a_warning_not_an_exception(self, tmp_path):
        s = macro_state.build_state(tmp_path, now=datetime(2026, 9, 23, tzinfo=UTC))
        assert s["n_events"] == 0
        assert s["in_blackout"] is False
        assert any("calendar is empty" in w for w in s["warnings"])

    def test_no_direction_field_anywhere_in_the_state(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "macro_calendar.yaml").write_text(
            "events:\n  - { name: FOMC_X, at: \"2026-10-28T18:00:00Z\" }\n")
        s = macro_state.build_state(tmp_path, now=datetime(2026, 10, 28, 18, 0, tzinfo=UTC))
        blob = json.dumps(s).lower()
        for word in ("bullish", "bearish", "target_weight", "expected_move", "surprise"):
            assert word not in blob
