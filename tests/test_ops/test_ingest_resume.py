"""Ingest after a host resume: network grace and the resume-transient label.

At 04:55:08Z on 2026-09-29, 2m13s after the lid opened, the first ingest asked Binance for
candles and got ``[Errno -3] Temporary failure in name resolution`` (WSL's resolver was
still down), then ``The read operation timed out`` on the books. Both were recorded as
``error``, quoted in TRADING IS BLOCKED, and gone by the next cron slot. The gate was right
to stay shut on stale data; the *label* and the lack of a retry were the defects.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from ops.lib import freshness as freshlib
from ops.lib import suspend as suspendlib
from runs import ingest as ingestmod
from runs.ingest import NET_BACKOFF_S, NET_RETRIES, Ingest

from .conftest import NOW
from .test_ingest import Binance


class FlakyVenue:
    """Binance behind a network that is coming back: the first ``fail`` transport calls to
    ``paths`` raise; everything else answers normally."""

    def __init__(self, fail: int, paths=("/api/v3/klines",), exc=None):
        self.inner = Binance()
        self.fail = fail
        self.paths = set(paths)
        self.exc = exc or httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        self.transport_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path in self.paths:
            self.transport_calls += 1
            if self.transport_calls <= self.fail:
                raise self.exc
        return self.inner.handler(request)


@pytest.fixture
def make(cfg, dbs):
    root, jdb, kdb = dbs
    (root / "config").mkdir(exist_ok=True)

    def _make(venue: FlakyVenue):
        slept: list[float] = []
        client = httpx.Client(transport=httpx.MockTransport(venue.handler))
        ing = Ingest(cfg, kdb, client, now=NOW, root=root, sleep=slept.append)
        return ing, slept, kdb, root
    return _make


def test_name_resolution_failures_are_retried_three_times_over_thirty_seconds(make):
    venue = FlakyVenue(fail=2)
    ing, slept, kdb, root = make(venue)
    assert ing._phase("candles", ing.refresh_candles)
    assert slept == list(NET_BACKOFF_S) and sum(slept) == 30.0
    assert ing.net_retries == NET_RETRIES - 1
    row = kdb.execute("SELECT status FROM ingest_runs WHERE phase='candles'").fetchone()
    assert row["status"] == "ok"


def test_a_read_timeout_is_retried_too(make):
    venue = FlakyVenue(fail=1, paths=("/api/v3/depth",),
                       exc=httpx.ReadTimeout("The read operation timed out"))
    ing, slept, kdb, root = make(venue)
    assert ing._phase("books", ing.snapshot_books)
    assert slept == [NET_BACKOFF_S[0]]


def test_a_network_that_stays_down_fails_once_slowly_then_fast(make):
    """Thirty seconds of patience per RUN, not per request: 31 pairs × 30 s would blow the
    600 s deadline and still fail."""
    venue = FlakyVenue(fail=10_000, paths=("/api/v3/klines", "/api/v3/depth"))
    ing, slept, kdb, root = make(venue)
    assert not ing._phase("candles", ing.refresh_candles)
    assert slept == list(NET_BACKOFF_S), "the first request used the whole budget"
    assert not ing._phase("books", ing.snapshot_books)
    assert slept == list(NET_BACKOFF_S), "later requests fail fast — no more sleeping"
    statuses = {r["phase"]: r["status"] for r in kdb.execute(
        "SELECT phase, status FROM ingest_runs").fetchall()}
    assert statuses == {"candles": "error", "books": "error"}


def test_an_http_status_error_is_an_answer_not_a_retry(make):
    class Down(FlakyVenue):
        def handler(self, request):
            if request.url.path == "/api/v3/klines":
                self.transport_calls += 1
                return httpx.Response(500, json={"msg": "down"})
            return self.inner.handler(request)

    venue = Down(fail=0)
    ing, slept, kdb, root = make(venue)
    assert not ing._phase("candles", ing.refresh_candles)
    assert slept == [] and venue.transport_calls == 1


# --------------------------------------------------------------------------- the label


def _record_resume(kdb, *, resumed_ago_min: float):
    """The watchdog's record of a sleep that ended ``resumed_ago_min`` ago."""
    to = NOW - timedelta(minutes=resumed_ago_min)
    suspendlib.record_tick(kdb, to - timedelta(days=3), interval_min=5, mult=4, floor_min=20)
    assert suspendlib.record_tick(kdb, to, interval_min=5, mult=4, floor_min=20) is not None


def test_a_failure_in_the_resume_grace_is_transient_not_an_error(make, cfg):
    """Degraded, labelled, exit 0 — and the gate is exactly as shut as before."""
    venue = FlakyVenue(fail=10_000, paths=("/api/v3/klines", "/api/v3/depth"))
    ing, slept, kdb, root = make(venue)
    _record_resume(kdb, resumed_ago_min=2)
    assert ing.run() == 0
    row = kdb.execute("SELECT status, detail FROM ingest_runs WHERE phase='candles'"
                      ).fetchone()
    assert row["status"] == "degraded"
    assert row["detail"].startswith(f"{suspendlib.RESUME_TRANSIENT_PREFIX}: [Errno -3]")
    # fail-closed, untouched: neither candles nor books were written, so the sidecar has
    # no blocking source at all and the age is infinite.
    assert freshlib.data_age_minutes(root / freshlib.FRESHNESS_REL, NOW) == float("inf")


def test_the_same_failure_outside_the_grace_is_an_error(make):
    venue = FlakyVenue(fail=10_000)
    ing, slept, kdb, root = make(venue)
    _record_resume(kdb, resumed_ago_min=ingestmod.RESUME_TRANSIENT_MIN + 5)
    assert ing.run() == 1
    row = kdb.execute("SELECT status FROM ingest_runs WHERE phase='candles'").fetchone()
    assert row["status"] == "error"


def test_the_first_run_after_a_long_gap_of_its_own_gets_the_grace_once(make):
    """No watchdog record, but this job's last row is two hours old: the network is coming
    back from *something*. The next run, with a row minutes old, gets no such grace."""
    venue = FlakyVenue(fail=10_000)
    ing, slept, kdb, root = make(venue)
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status)"
                " VALUES ('ingest','candles','old',?,'ok')",
                ((NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    kdb.commit()
    assert ing.resume_transient() is True
    fresh = Ingest(ing.cfg, kdb, ing.http, now=NOW + timedelta(minutes=15), root=root,
                   sleep=lambda s: None)
    kdb.execute("INSERT INTO ingest_runs(job, phase, window_start, started_at, status)"
                " VALUES ('ingest','candles','recent',?,'error')",
                (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    kdb.commit()
    assert fresh.resume_transient() is False


def test_a_fresh_host_with_no_history_gets_no_grace(make):
    ing, slept, kdb, root = make(FlakyVenue(fail=0))
    assert ing.resume_transient() is False


def test_the_grace_constants_agree_across_modules():
    from ops import healthcheck

    assert ingestmod.RESUME_TRANSIENT_MIN == healthcheck.RESUME_TRANSIENT_MIN
