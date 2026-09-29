"""The strategies' reader for ``knowledge/state/trend.json``. STDLIB ONLY (in-container).

``runs/features/trend.py: write_state`` computes the fifteen-member trend ensemble on the
last CLOSED daily bar and writes one exposure weight per ``universe.core`` asset. This
module is the other half: it reads that file inside the freqtrade container, where neither
pandas-with-history nor ``runs/`` exists, and hands ``EarnBaseStrategy`` a weight it can
size against — the same shape as ``proposal_loader.py`` for proposals.

It never raises. Every failure mode is a *named refusal*, because the strategy fails
CLOSED on this input (``docs/design/dip-strategy.md`` §10.2 item 6: *"an empty signal is
not a flat signal"*, and CLAUDE.md: abstain is the default when inputs are stale):

``trend_state_missing``   no file, or a file that will not parse
``trend_state_stale``     the bar the weight was computed on closed more than
                          ``max_age_hours`` before ``now`` — the daily job stopped running
``trend_state_warmup``    the writer refused to read flat (fewer than 250 bars, or a gap)
``trend_state_no_asset``  the file carries no entry for this asset
``weight_zero``           a real, fresh weight of exactly zero: every member is off

The distinction matters to whoever reads the journal: the first four are plumbing, the
last one is the market. Only ``ok`` carries a weight the strategy may trade on.

This file is an INPUT to sizing, never an authorisation. ``strategies/riskgate.py`` still
validates every order the scaled stake produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: Path of the state file relative to the directory that holds ``knowledge/earn.db`` —
#: the same directory the freshness sidecar lives in, so no new mount is needed.
STATE_RELATIVE = ("state", "trend.json")

#: The daily bar closes at 00:00 UTC and the writer runs every ingest cycle, so a weight
#: whose bar closed more than two days ago means a whole daily bar was missed — the
#: writer is down, not the market. ``trading.trend_ensemble.max_age_hours`` overrides it.
DEFAULT_MAX_AGE_HOURS = 48.0

REASON_MISSING = "trend_state_missing"
REASON_STALE = "trend_state_stale"
REASON_WARMUP = "trend_state_warmup"
REASON_NO_ASSET = "trend_state_no_asset"
REASON_ZERO = "weight_zero"
REASON_OK = "ok"


@dataclass(frozen=True)
class TrendState:
    """The parsed file. ``ok`` is False when nothing in it may be traded on."""

    path: str
    ok: bool
    reason: str
    computed_utc: datetime | None = None
    #: asset -> (weight or None, status, bar-close instant)
    assets: dict[str, tuple[float | None, str, datetime | None]] = field(default_factory=dict)
    mtime: float = 0.0


@dataclass(frozen=True)
class TrendWeight:
    """What the strategy sizes with: a weight in [0, 1] and the reason it is what it is."""

    weight: float
    reason: str
    asof_close_utc: datetime | None = None

    @property
    def tradeable(self) -> bool:
        return self.weight > 0.0


def state_path(knowledge_db: str) -> Path:
    """``<dir of knowledge_db>/state/trend.json``."""
    return Path(knowledge_db).parent.joinpath(*STATE_RELATIVE)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _weight(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    w = float(raw)
    if w != w or w < 0.0 or w > 1.0:
        return None
    return w


def load(path: Path | str) -> TrendState:
    """Parse the file. Never raises; a bad file is a :class:`TrendState` with ``ok=False``."""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TrendState(str(p), False, REASON_MISSING)
    if not isinstance(raw, dict) or not isinstance(raw.get("assets"), dict):
        return TrendState(str(p), False, REASON_MISSING, mtime=mtime)
    assets: dict[str, tuple[float | None, str, datetime | None]] = {}
    for asset, entry in raw["assets"].items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        w = _weight(entry.get("weight")) if status == "ok" else None
        assets[str(asset)] = (w, status or "unknown", _parse_ts(entry.get("asof_close_utc")))
    return TrendState(str(p), True, REASON_OK, computed_utc=_parse_ts(raw.get("computed_utc")),
                      assets=assets, mtime=mtime)


def weight_for(state: TrendState, asset: str, now: datetime,
               max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> TrendWeight:
    """The weight the strategy may size ``asset`` with at ``now``, and why.

    Freshness is judged on the BAR, not on the file's clock: a file rewritten every fifteen
    minutes from a candle store that stopped updating is stale, whatever ``computed_utc``
    says.
    """
    if not state.ok:
        return TrendWeight(0.0, state.reason or REASON_MISSING)
    entry = state.assets.get(asset)
    if entry is None:
        return TrendWeight(0.0, REASON_NO_ASSET)
    w, status, asof_close = entry
    if status != "ok" or w is None:
        return TrendWeight(0.0, REASON_WARMUP, asof_close)
    ref = now if now.tzinfo else now.replace(tzinfo=UTC)
    if asof_close is None or (ref - asof_close) > timedelta(hours=float(max_age_hours)):
        return TrendWeight(0.0, REASON_STALE, asof_close)
    if w <= 0.0:
        return TrendWeight(0.0, REASON_ZERO, asof_close)
    return TrendWeight(w, REASON_OK, asof_close)
