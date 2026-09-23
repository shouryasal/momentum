"""Macro-event blackout state — code, not estimates.

Writes ``knowledge/state/macro.json``: whether we are inside a blackout window right now,
how far the next event is, how much future coverage the hand-maintained calendar still has,
and (with ``--audit``) how the calendar's FOMC rows compare with the dates published on
federalreserve.gov plus the latest CPI print from api.bls.gov.

The window is **asymmetric** and that is deliberate. ``risk.blackout.window_minutes`` is one
number applied on both sides; the measured elevation around a release is not symmetric, so
this script reads the shipped value as the default and accepts ``--pre``/``--post`` to
evaluate the width the study recommends. It never edits config and it never edits the
calendar — both are tier 2 and the calendar is human-maintained on purpose.

Modes
-----
``(no flags)``      current state from the calendar and the shipped window
``--at ISO``        evaluate as of an instant, for replay
``--audit``         also scrape the FOMC dates and the latest CPI print
``--write-flags``   move ``macro_blackout`` through ops.lib.flags
``--selftest``      offline fixture assertions (the deterministic eval case)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_DIR / "tests" / "fixtures"

from runs.features import macro_calendar as MC  # noqa: E402
from runs.features.venue import SourceDown  # noqa: E402

CALENDAR_REL = "config/macro_calendar.yaml"
STATE_REL = "knowledge/state/macro.json"


def calendar_path(root: Path) -> Path:
    return root / "config" / "macro_calendar.yaml"


def state_path(root: Path) -> Path:
    return root / "knowledge" / "state" / "macro.json"


def shipped_window(root: Path) -> tuple[float, float, str]:
    """The window in force. Falls back to a symmetric 60 minutes if config is unreadable.

    Returned as ``(pre, post, source)`` so the caller can say which it used rather than
    quietly presenting a fallback as the shipped value.
    """
    try:
        from ops.config import load_config

        w = float(load_config().risk.blackout.window_minutes)
        return w, w, "config"
    except Exception as exc:  # noqa: BLE001 - a config problem must not take the hazard check down
        return 60.0, 60.0, f"fallback (config unreadable: {type(exc).__name__})"


def build_state(root: Path, *, now: datetime, pre_min: float | None = None,
                post_min: float | None = None, audit: bool = False,
                fetcher=None) -> dict:
    pre_cfg, post_cfg, src = shipped_window(root)
    pre = pre_cfg if pre_min is None else float(pre_min)
    post = post_cfg if post_min is None else float(post_min)
    events = MC.load_calendar(calendar_path(root))
    active = MC.active_blackout(now, events, pre, post)
    stale_days = MC.calendar_stale_days(now, events)

    state = {
        "computed_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "calendar_path": CALENDAR_REL,
        "n_events": len(events),
        "n_future_events": len([e for e in events if e.at >= now]),
        "calendar_stale_days": stale_days,
        "calendar_stale": stale_days < MC.CALENDAR_STALE_DAYS,
        "calendar_stale_threshold_days": MC.CALENDAR_STALE_DAYS,
        "window": {"pre_minutes": pre, "post_minutes": post, "source": src,
                   "symmetric": pre == post},
        "in_blackout": active is not None,
        "active_event": active[0].as_dict() if active else None,
        "blackout_until": active[1].strftime("%Y-%m-%dT%H:%M:%SZ") if active else None,
        "hours_to_next_macro_event": MC.hours_to_next_macro_event(now, events),
        "next_events": [e.as_dict() for e in MC.next_events(now, events, 3)],
        "audit": None,
        "warnings": [],
    }
    if stale_days < MC.CALENDAR_STALE_DAYS:
        state["warnings"].append(
            f"calendar has only {stale_days}d of future coverage "
            f"(threshold {MC.CALENDAR_STALE_DAYS}d) - a hand-maintained calendar that runs "
            f"out blacks out nothing, silently")
    if not events:
        state["warnings"].append(
            "calendar is empty - no blackout can be applied, and that is not the same as "
            "there being no events")
    if pre == post:
        state["warnings"].append(
            "window is symmetric; the measured elevation around a release is not "
            "(see references/window.md)")

    if audit:
        state["audit"] = run_audit(events, now=now, fetcher=fetcher, root=root)
        if not state["audit"]["fomc"].get("scrape_ok"):
            state["warnings"].append(
                f"FOMC scrape failed ({state['audit']['fomc'].get('source')}) - last known "
                f"dates stand, flagged, and the blackout still applies")
        for m in state["audit"]["fomc"].get("missing") or []:
            state["warnings"].append(f"FOMC release {m} has no calendar row")
        for d in state["audit"]["fomc"].get("drifted") or []:
            state["warnings"].append(
                f"calendar row {d['name']} is {d['drift_min']} min from the 14:00 ET release")
    return state


def run_audit(events, *, now: datetime, fetcher=None, root: Path | None = None) -> dict:
    """Scrape FOMC dates and read the latest CPI print. Degrades, never raises.

    On a scrape failure the last cached schedule stands, flagged stale — an unreachable
    source is never evidence that there is no event.
    """
    out: dict = {}
    try:
        dates, status = MC.fetch_fomc_dates_cached(root or REPO_ROOT, fetcher=fetcher, now=now)
    except SourceDown as exc:
        dates, status = [], {"ok": False, "pages_ok": [], "pages_failed": {"current": str(exc)},
                             "n_dates": 0, "stale": True}
    times = MC.fomc_release_times(dates)
    audit = MC.audit_calendar(events, times, now=now) if times else {
        "ok": False, "matched": [], "drifted": [], "missing": [],
        "note": "no scraped dates - calendar not audited, last known state stands"}
    out["fomc"] = {**audit, **{k: status[k] for k in
                               ("ok", "pages_ok", "pages_failed", "n_dates", "n_scheduled",
                                "stale", "age_min", "source", "note") if k in status}}
    out["fomc"]["scrape_ok"] = bool(status.get("ok"))
    out["fomc"]["ok"] = bool(audit.get("ok")) and bool(status.get("ok"))
    try:
        out["cpi_latest"] = MC.bls_latest_cpi(fetcher=fetcher)
        out["cpi_latest"]["note"] = ("post-hoc release detection only - this says a release "
                                     "happened, never when the next one is")
    except SourceDown as exc:
        out["cpi_latest"] = {"error": str(exc)}
    return out


def write_state(root: Path, state: dict) -> Path:
    out = state_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(out)
    return out


def apply_flags(root: Path, state: dict, now: datetime) -> list[str]:
    """Move ``macro_blackout`` through the one sanctioned writer.

    ``runs/ingest.py`` owns the same flag on a symmetric window; this path exists so the
    skill's asymmetric evaluation can be exercised without touching ingest. A flag set by
    ingest is left alone — two writers fighting over one flag is worse than one writer
    being slightly wrong.
    """
    from ops.config import load_config
    from ops.lib import flags as flagslib

    fpath = root / load_config().paths.flags_file
    moved: list[str] = []
    try:
        current = flagslib.active_flags(fpath, now)
    except flagslib.FlagsError:
        current = {}
    existing = current.get("macro_blackout")
    if existing and existing.get("set_by") not in (None, "event-blackout"):
        return [f"skipped (owned by {existing.get('set_by')})"]
    if state["in_blackout"]:
        flagslib.set_flag(fpath, "macro_blackout", severity="block_entries",
                          reason=state["active_event"]["name"], set_by="event-blackout",
                          expires_at=state["blackout_until"], now=now)
        moved.append("set:macro_blackout")
    elif existing:
        flagslib.clear_flag(fpath, "macro_blackout", by="event-blackout", now=now)
        moved.append("clear:macro_blackout")
    else:
        flagslib.touch(fpath, now=now)
    return moved


def summarise(state: dict) -> str:
    w = state["window"]
    bits = [f"in_blackout={state['in_blackout']}",
            f"window=-{w['pre_minutes']:.0f}/+{w['post_minutes']:.0f}min",
            f"next_in_h={state['hours_to_next_macro_event']}",
            f"coverage_days={state['calendar_stale_days']}",
            f"calendar_stale={state['calendar_stale']}"]
    if state["active_event"]:
        bits.append(f"event={state['active_event']['name']}")
    return " ".join(bits)


# --------------------------------------------------------------------------- selftest

SELFTEST_EVENTS = [
    {"name": "FOMC_Test", "at": "2026-10-28T18:00:00Z"},
    {"name": "US_CPI_Test", "at": "2026-11-12T13:30:00Z"},
]


def run_selftest() -> int:
    """Offline assertions on the window arithmetic, the staleness alarm and the FOMC regex."""
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    events = MC.parse_events(SELFTEST_EVENTS)
    ev = events[0].at

    # Asymmetry: 360 before / 480 after must include T-5h and T+7h and exclude T-7h.
    check("asymmetric window includes T-5h",
          MC.in_macro_blackout(ev - timedelta(hours=5), ev, 360, 480))
    check("asymmetric window includes T+7h",
          MC.in_macro_blackout(ev + timedelta(hours=7), ev, 360, 480))
    check("asymmetric window excludes T-7h",
          not MC.in_macro_blackout(ev - timedelta(hours=7), ev, 360, 480))
    check("symmetric 60min covers only the peak hour",
          not MC.in_macro_blackout(ev - timedelta(hours=2), ev, 60, 60),
          "which is exactly the finding: 60 symmetric leaves the shoulder open")

    # Staleness alarm.
    now_far = datetime(2026, 9, 23, tzinfo=UTC)
    cov = MC.calendar_stale_days(now_far, events)
    check("coverage is measured to the FURTHEST future event", cov == 50, f"got {cov}d")
    check("empty calendar reports zero coverage, not infinite",
          MC.calendar_stale_days(now_far, []) == 0)
    check("a calendar that has run out is stale",
          MC.calendar_stale_days(datetime(2027, 1, 1, tzinfo=UTC), events) == 0)

    # DST: the same local 14:00 ET is a different UTC hour in January and June.
    winter, summer = MC.fomc_release_times(["20260128", "20260617"])
    check("DST is handled (Jan 19:00Z, Jun 18:00Z)",
          winter.hour == 19 and summer.hour == 18,
          f"{winter.isoformat()} / {summer.isoformat()}")

    # The FOMC link pattern, against a real slice of the real page.
    html = (FIXTURES / "fomccalendars_slice.html").read_text(encoding="utf-8")
    dates = MC.fomc_dates_from_html(html)
    check("FOMC regex still yields >= 40 dates from the live page slice",
          len(dates) >= 40, f"got {len(dates)}")
    check("scraped dates are plausible", all(d.isdigit() and len(d) == 8 for d in dates))

    # Forward verification. The statement links only exist AFTER a meeting, so auditing a
    # future calendar row needs the scheduled-meeting panels instead.
    sched = MC.fomc_scheduled_from_html(html)
    check("scheduled meetings parse beyond the last released statement",
          len(sched) >= 40 and max(sched) > max(dates),
          f"{len(sched)} scheduled, latest {max(sched)} vs released {max(dates)}")
    audit = MC.audit_calendar(
        MC.parse_events([{"name": "FOMC_Oct", "at": "2026-10-28T18:00:00Z"}]),
        MC.fomc_release_times(sched), now=datetime(2026, 9, 23, tzinfo=UTC))
    check("a calendar row is verified against the published schedule",
          any(m["name"] == "FOMC_Oct" and m["drift_min"] == 0.0 for m in audit["matched"]),
          str(audit["matched"]))

    # BLS: post-hoc only.
    bls = json.loads((FIXTURES / "bls_cpi.json").read_text())
    latest = MC.bls_latest_cpi(fetcher=lambda _u: (200, json.dumps(bls).encode()))
    check("BLS latest print parses", latest["latest"] is True and float(latest["value"]) > 0,
          f"{latest['year']}-{latest['period']} = {latest['value']}")

    # Degradation: an unreachable page must not fail open.
    def dead(_url):
        raise SourceDown("federalreserve.gov unreachable")

    got, status = MC.fetch_fomc_dates(fetcher=dead)
    check("unreachable FOMC page degrades rather than failing open",
          got == [] and status["ok"] is False and "current" in status["pages_failed"])

    print(f"selftest failures={len(failures)} verdict={'OK' if not failures else 'BROKEN'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Macro-event blackout state.")
    ap.add_argument("--at", metavar="ISO", help="evaluate as of this instant (replay)")
    ap.add_argument("--pre", type=float, help="minutes before the event (default: shipped)")
    ap.add_argument("--post", type=float, help="minutes after the event (default: shipped)")
    ap.add_argument("--audit", action="store_true",
                    help="also scrape FOMC dates and the latest CPI print")
    ap.add_argument("--write-flags", action="store_true",
                    help="move macro_blackout through ops.lib.flags")
    ap.add_argument("--selftest", action="store_true", help="offline assertions, then exit")
    ap.add_argument("--json", action="store_true", help="print the whole state document")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.selftest:
        return run_selftest()

    now = (datetime.fromisoformat(args.at.replace("Z", "+00:00")).astimezone(UTC)
           if args.at else datetime.now(UTC))
    state = build_state(REPO_ROOT, now=now, pre_min=args.pre, post_min=args.post,
                        audit=args.audit)
    write_state(REPO_ROOT, state)
    if args.write_flags:
        state["flags_moved"] = apply_flags(REPO_ROOT, state, now)
        write_state(REPO_ROOT, state)
    print(summarise(state))
    for w in state["warnings"]:
        print(f"  warn: {w}")
    print(f"  wrote {STATE_REL}")
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
