"""The profit & gap ledger: what the system is likely earning, and what it is losing for
reasons that are not the strategy.

The owner asked, verbatim: *"keep checking what is likely profit we are getting and what
are gaps making us miss profit"*. The honest shape of that answer, from the studies in
``docs/design/`` (``paper-trading-review-2026-09-29.md`` §0-2, 4.3, 7;
``dip-strategy.md`` §8.2, 8.3, 10), is:

* **Likely profit is a DECLARED expectation, not a measurement.** The active profile has
  a costed backtest of its own (``config/profiles/fast-test.yaml``: −1.29% per 30 days,
  fees 1.35% of the balance); the shipped 4h sleeves with the trend ensemble have a
  rolling-365d distribution (median +24.8%, p25 +2.2%, p10 −15.0%, worst −38.9%, planned
  max drawdown −45%). Days of realised P&L are NOISE against that: 30 days cannot measure
  return (``dip-strategy.md`` §10), so section A says so in words.
* **What CAN be measured every day is the GAP**: money the mechanism forwent or wasted for
  reasons that are not the strategy — a host asleep, a flag that blocked entries, a
  validator that errored, model spend that produced nothing, a watcher looking at the
  wrong database, a ledger that reset, a hand-typed flatten. The review measured each of
  these once, by hand. This module is that measurement as a job.

Every line carries a number, a unit and the query (or file rule) that produced it, so a
reader can re-run it. Two windows are always reported: the last 24 hours and everything
since the test began (the first row in any run database).

Read-only everywhere: the bots' databases are opened ``mode=ro``, the journal and
knowledge connections are the caller's. Nothing here writes into a database. The only
outputs are ``reports/profit-gaps/<YYYY-MM-DD>.md`` and
``knowledge/state/profit_gaps.json`` under the state root.

Home shows the ledger (``console/web/src/pages/overview/ProfitGapsCard.tsx``); the
daily review writes it once a night (``runs/daily_review.py``); ``GET /api/profit-gaps``
computes it on demand.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.lib import paths as ops_paths

__all__ = [
    "BENCHMARK_COST_PER_SIDE",
    "EXPECTATIONS",
    "Gap",
    "Ledger",
    "Line",
    "Report",
    "appendix_line",
    "compute",
    "compute_report",
    "report_json_path",
    "report_md_path",
    "write",
]

# --------------------------------------------------------------------------- constants
#
# Config is owned elsewhere, so these are code constants with a reason each. Every limit
# that IS in ``config/earn.yaml`` (the fee budget, the seeds) is read from the config.

#: Where the bots' databases and logs live under the state root — the same literal
#: ``console.services.pot_service`` and ``ops/backup.py`` use.
FT_USERDATA_DIR = "ft_userdata"
#: The watcher's JSON-lines report, one line per cycle (``ops/crontab``: ``>> logs/watch.log``).
WATCH_LOG_REL = "logs/watch.log"
#: Gate rows with the same (sleeve, pair, reason) closer than this are Freqtrade's 5-second
#: retries of ONE refusal, not new decisions — the review collapsed 1,998 rows to 40 this way.
RETRY_COLLAPSE_S = 300
#: Freqtrade heartbeats once a minute; a gap longer than this means the bot was not running.
HEARTBEAT_GAP_MIN = 10.0
#: ``nav_points`` ticks every 15 minutes; two missed ticks is a hole, one is jitter.
NAV_GAP_MIN = 45.0
#: The planning cost floor: 15 bps per side (10 fee + 5 slippage, ``config/backtest.yaml``).
BENCHMARK_COST_PER_SIDE = 0.0015
#: Hours in the "per 30 days" the expectations are quoted in.
MONTH_HOURS = 30 * 24
#: The default window when nothing says otherwise.
DEFAULT_WINDOW_HOURS = 24
#: A ledger reset is a tick whose realised P&L is exactly zero right after one that was not.
RESET_EPS = 0.01
#: What the screen calls the sleeves. "sleeve" itself is a builder word Home never prints.
SLEEVE_NAMES: dict[str, str] = {"a": "the rules bot", "b": "the AI bot"}
#: Exit reasons that are an operator's or the code's hand, never the strategy's.
EVENT_EXIT_REASONS = ("force_exit", "target_zero")
#: Audit actions that are the same hand, seen from the console.
EVENT_AUDIT_ACTIONS = ("autonomy.flatten", "kill.engage", "bot.forceexit")

#: Declared expectations per profile, from the profile file's own evidence block when it
#: has one (parsed by :func:`_profile_evidence`) and otherwise from this table. ``None`` is
#: the shipped configuration.
EXPECTATIONS: dict[str | None, dict[str, Any]] = {
    "fast-test": {
        "source": "config/profiles/fast-test.yaml evidence block (20260824-20260923, 1h, "
                  "31 pairs, fee 15 bps/side) and docs/design/risk-and-ladder-2026-09-29.md §2",
        "expected_per_30d_pct": -1.29,
        "fees_pct_of_balance_per_30d": 1.35,
        "measured_max_drawdown_30d_pct": -1.46,
        "planned_max_drawdown_pct": -26.8,
        "median_annual_pct": None, "p25_annual_pct": None, "p10_annual_pct": None,
        "worst_annual_pct": None,
    },
    None: {
        "source": "docs/design/dip-strategy.md §8.2 (rolling 365-day, 2019-2026, costs on), "
                  "§8.3 (drawdown) and §10 (median 30-day −0.3%)",
        "expected_per_30d_pct": -0.3,
        "fees_pct_of_balance_per_30d": 0.11,
        "measured_max_drawdown_30d_pct": -35.5,
        "planned_max_drawdown_pct": -45.0,
        "median_annual_pct": 24.8, "p25_annual_pct": 2.2, "p10_annual_pct": -15.0,
        "worst_annual_pct": -38.9,
    },
}

#: The sentence the owner asked for on the card, once, so the server and the page agree.
CANNOT_MEASURE_NOTE = (
    "Days of realised profit or loss cannot confirm or refute this expectation: 30 days "
    "can only tell whether the mechanism behaves, not what it earns (dip-strategy.md §10)."
)

#: The words beside the three realised numbers on Home.
DEFINITIONS: dict[str, str] = {
    "realised_net_usdt": (
        "Closed trades in this window, net of fees, across every database a bot has run "
        "on. Not a verdict on the strategy: a day of results is noise against the "
        "declared expectation."
    ),
    "fees_usdt": (
        "What the venue took on those trades, and the share of gross profit it took. "
        "Fees are the one cost the mechanism controls: fewer, longer trades pay less."
    ),
    "btc_hold_usdt": (
        "What the same money would have done simply held as BTC over the same window, "
        "costed at 15 bps per side. The yardstick the whole system is judged against."
    ),
}

#: How much of the system each gap touches, 0-1. ``severity`` measures how much of the
#: window or the money a gap took *within its own scope*; the weight says how wide that
#: scope is, so the top three rank by ``severity × weight``. A host asleep stops both bots
#: and every trade (1.0); an empty decision input stops the AI bot's proposals only (0.7);
#: a watcher that sees nothing loses monitoring, not money (0.4); a ledger reset misreports
#: money without losing it (0.5).
GAP_WEIGHTS: dict[str, float] = {
    "uptime": 1.0,
    "data_staleness": 1.0,
    "operator_events": 1.0,
    "fee_drag": 0.9,
    "refused_entries": 0.8,
    "funnel": 0.7,
    "decision_inputs": 0.7,
    "model_spend": 0.6,
    "ledger_integrity": 0.5,
    "holdings_watcher": 0.4,
}

#: Plain causes for the gate's refusal slugs.
REFUSAL_CAUSES: dict[str, str] = {
    "blackout:data_stale": "the data_stale flag was up: candles or the order book were too "
                           "old to trust",
    "staleness": "the gate's own freshness probe found the data too old",
    "beta_cap": "the book would have moved more than the BTC-beta cap allows",
    "targets:no_proposal_ever": "no proposal had ever given the bot a mandate",
}

_HEARTBEAT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*Bot heartbeat")
_CYCLE_RE = re.compile(r"watch-(\d{8}T\d{6})Z")
_EMPTY_INPUTS_RE = re.compile(r"assets\s*=?\s*\{\}|asof_candle_utc\s*=?\s*null|zero computed"
                              r" indicators|no computed", re.I)


# --------------------------------------------------------------------------- data classes


@dataclass
class Line:
    """One ledger line: a number, its unit, and the query that produced it."""

    key: str
    label: str
    value: float | int | None
    unit: str
    query: str
    note: str | None = None


@dataclass
class Gap:
    """One way the mechanism forwent or wasted money for a reason that is not the strategy."""

    key: str
    title: str
    #: USDT where realised, hours or a count where not (``unit`` says which).
    size: float
    unit: str
    #: 0-100, comparable across gaps: how much of the window or the money this took.
    severity: float
    cause: str
    #: The sentence a non-engineer reads on Home. Empty when the gap did not occur.
    sentence: str
    lines: list[Line] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: :data:`GAP_WEIGHTS` — how much of the system this gap touches.
    weight: float = 1.0

    @property
    def score(self) -> float:
        """What the top three rank by: severity within scope × width of scope."""
        return round(self.severity * self.weight, 2)


@dataclass
class Ledger:
    """One window of the ledger: A expected, B realised, C gaps, D the top three."""

    window: dict[str, Any]
    expected: dict[str, Any]
    realised: dict[str, Any]
    gaps: list[Gap]
    top_three: list[dict[str, Any]]
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        data = _jsonable(asdict(self))
        for raw, gap in zip(data["gaps"], self.gaps, strict=True):
            raw["score"] = gap.score
        return data


@dataclass
class Report:
    """Both windows, the profile they were judged against, and when."""

    generated_utc: str
    profile: str | None
    windows: dict[str, Ledger]
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "generated_utc": self.generated_utc,
            "profile": self.profile,
            "windows": {k: v.to_json() for k, v in self.windows.items()},
            "error": self.error,
        }


@dataclass
class _Trade:
    sleeve: str
    run: str
    trade_id: int
    pair: str
    opened: datetime | None
    closed: datetime | None
    is_open: bool
    profit: float
    fees: float
    stake: float
    exit_reason: str | None
    enter_tag: str | None


@dataclass
class _Window:
    key: str
    since: datetime
    until: datetime

    @property
    def hours(self) -> float:
        return max(0.0, (self.until - self.since).total_seconds() / 3600.0)

    @property
    def since_iso(self) -> str:
        return _iso(self.since)

    @property
    def until_iso(self) -> str:
        return _iso(self.until)

    def contains(self, t: datetime | None) -> bool:
        return t is not None and self.since <= t < self.until

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "since_utc": self.since_iso, "until_utc": self.until_iso,
                "hours": round(self.hours, 3)}


# --------------------------------------------------------------------------- small helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    """ISO-8601 with ``Z`` or an offset, or Freqtrade's naive ``YYYY-MM-DD HH:MM:SS.ffffff``
    (UTC). ``None`` for anything else — never a guess."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN -> default


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _jsonable(obj: Any) -> Any:
    """Floats that JSON cannot carry become ``None``; datetimes become ISO strings."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


def _rows(conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = (),
          errors: list[str] | None = None, where: str = "") -> list[dict[str, Any]]:
    """Read by column name; a missing connection or table is an empty list and a note,
    never an exception — the ledger must render on a fresh checkout."""
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, tuple(params))
        cols = [c[0] for c in cur.description or ()]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        if errors is not None:
            errors.append(f"{where or sql.split(' FROM ')[-1].split()[0]}: {exc}")
        return []


def _sleeves(cfg: Any) -> tuple[str, ...]:
    return tuple(ops_paths.SLEEVES)


def _state_root(root: Path | None) -> Path:
    return Path(root) if root is not None else ops_paths.state_root()


def _userdata(root: Path, sleeve: str) -> Path:
    return root / FT_USERDATA_DIR / sleeve.lower()


def _sleeve_name(sleeve: str) -> str:
    return SLEEVE_NAMES.get(sleeve.lower(), f"bot {sleeve}")


def _usd(value: float) -> str:
    return f"{value:,.2f} USDT"


def _clock(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%H:%MZ")


def _clip(text: str, width: int) -> str:
    """Cut a long error at a word boundary and say so, so a sentence never ends mid-token."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    cut = text[:width].rsplit(" ", 1)[0] or text[:width]
    return cut.rstrip(",;:") + " …"


def _span_text(a: datetime, b: datetime) -> str:
    if a.date() == b.date():
        return f"{a.strftime('%m-%d')} {_clock(a)}-{_clock(b)}"
    return f"{a.strftime('%m-%d')} {_clock(a)} to {b.strftime('%m-%d')} {_clock(b)}"


# --------------------------------------------------------------------------- the run DBs


def _run_databases(root: Path, sleeve: str) -> list[Path]:
    """Every Freqtrade database this sleeve has written to — the pot service's rule."""
    try:
        from console.services import pot_service

        return pot_service.run_databases(sleeve, root=root)
    except Exception:  # noqa: BLE001 - the console package may be absent in a slim checkout
        base = _userdata(root, sleeve)
        out = [p for p in (base / "tradesv3.sqlite",) if p.exists()]
        runs = base / "runs"
        if runs.is_dir():
            out.extend(sorted(p for p in runs.glob("*.sqlite") if p.is_file()))
        return out


def _read_trades(path: Path, sleeve: str, errors: list[str]) -> list[_Trade]:
    """Every trade in one bot database, read-only, fees from the filled orders (the trade
    row's ``fee_close_cost`` covers the LAST exit leg only). Never raises."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        errors.append(f"{path.name}: cannot open ({type(exc).__name__})")
        return []
    conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute(
            "SELECT id, pair, is_open, open_date, close_date, close_profit_abs, exit_reason,"
            " enter_tag, stake_amount, fee_open, fee_close, fee_open_cost, fee_close_cost"
            " FROM trades ORDER BY id").fetchall()
        try:
            orders = conn.execute(
                "SELECT ft_trade_id, status, filled, average, cost, side, ft_order_side"
                " FROM orders").fetchall()
        except sqlite3.Error:
            orders = []
    except sqlite3.Error as exc:
        errors.append(f"{path.name}: cannot read trades ({type(exc).__name__})")
        return []
    finally:
        conn.close()
    by_trade: dict[int, list[sqlite3.Row]] = {}
    for o in orders:
        by_trade.setdefault(int(o["ft_trade_id"]), []).append(o)
    out: list[_Trade] = []
    for t in trades:
        fee_open, fee_close = _f(t["fee_open"]), _f(t["fee_close"])
        fees, filled = 0.0, 0
        for o in by_trade.get(int(t["id"]), []):
            if str(o["status"] or "") != "closed" or not _f(o["filled"]):
                continue
            filled += 1
            cost = _f(o["cost"]) or _f(o["filled"]) * _f(o["average"])
            side = str(o["side"] or o["ft_order_side"] or "").lower()
            fees += cost * (fee_open if side == "buy" else fee_close)
        is_open = bool(int(t["is_open"] or 0))
        if not filled:
            fees = _f(t["fee_open_cost"]) + (0.0 if is_open else _f(t["fee_close_cost"]))
        out.append(_Trade(
            sleeve=sleeve, run=path.stem, trade_id=int(t["id"]), pair=str(t["pair"] or ""),
            opened=_parse(t["open_date"]), closed=_parse(t["close_date"]), is_open=is_open,
            profit=_f(t["close_profit_abs"]), fees=fees, stake=_f(t["stake_amount"]),
            exit_reason=t["exit_reason"], enter_tag=t["enter_tag"]))
    return out


def _all_trades(cfg: Any, root: Path, errors: list[str]) -> list[_Trade]:
    out: list[_Trade] = []
    for sleeve in _sleeves(cfg):
        for path in _run_databases(root, sleeve):
            out.extend(_read_trades(path, sleeve, errors))
    return out


def _test_start(trades: Iterable[_Trade]) -> datetime | None:
    """When the test began: the first row in any run database."""
    starts = [t.opened for t in trades if t.opened is not None]
    return min(starts) if starts else None


# --------------------------------------------------------------------------- A. expected


def _profile_evidence(cfg: Any, root: Path) -> dict[str, Any] | None:
    """The active profile's own evidence block, if the file carries one.

    The block is prose in comments, so this is a regex over the text: the first
    ``net X%`` / ``max drawdown X%`` / ``fees paid X% of starting balance`` triple is
    the 30-day measurement; the last ``MaxDD X%`` is the long-window replay.
    """
    name = getattr(getattr(cfg, "profiles", None), "active", None)
    if not name:
        return None
    directory = str(getattr(cfg.profiles, "dir", "config/profiles") or "config/profiles")
    candidates = [Path(directory) / f"{name}.yaml"]
    candidates = [c if c.is_absolute() else ops_paths.REPO_ROOT / c for c in candidates]
    candidates.append(root / directory / f"{name}.yaml")
    text = None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
            break
        except OSError:
            continue
    if text is None:
        return None
    net = re.search(r"\bnet (-?\d+(?:\.\d+)?)%", text)
    dd = re.search(r"\bmax drawdown (\d+(?:\.\d+)?)%", text, re.I)
    fees = re.search(r"fees paid (\d+(?:\.\d+)?)% of starting balance", text)
    long_dd = re.findall(r"\bMaxDD (\d+(?:\.\d+)?)%", text)
    if not (net and dd and fees):
        return None
    return {
        "source": f"{directory}/{name}.yaml evidence block (parsed)",
        "expected_per_30d_pct": float(net.group(1)),
        "fees_pct_of_balance_per_30d": float(fees.group(1)),
        "measured_max_drawdown_30d_pct": -float(dd.group(1)),
        "planned_max_drawdown_pct": -float(long_dd[-1]) if long_dd else -float(dd.group(1)),
        "median_annual_pct": None, "p25_annual_pct": None, "p10_annual_pct": None,
        "worst_annual_pct": None,
    }


def _expected(cfg: Any, root: Path, window: _Window) -> dict[str, Any]:
    name = getattr(getattr(cfg, "profiles", None), "active", None) or None
    table = _profile_evidence(cfg, root) or EXPECTATIONS.get(name) or EXPECTATIONS[None]
    src = str(table["source"])
    per30 = table.get("expected_per_30d_pct")
    lines = [
        Line("expected_per_30d_pct", "Expected return per 30 days", _r(per30, 2), "%",
             src, "declared from the profile's own costed evidence; not a forecast"),
        Line("planned_max_drawdown_pct", "Planned max drawdown", _r(table.get(
            "planned_max_drawdown_pct"), 2), "%", src,
            "the envelope to plan for; the true forward worst case is unknown and worse"),
        Line("fees_pct_of_balance_per_30d", "Fees expected per 30 days",
             _r(table.get("fees_pct_of_balance_per_30d"), 2), "% of balance", src),
        Line("measured_max_drawdown_30d_pct", "Measured worst 30-day drawdown",
             _r(table.get("measured_max_drawdown_30d_pct"), 2), "%", src),
    ]
    for key, label in (("median_annual_pct", "Rolling 365-day median"),
                       ("p25_annual_pct", "Rolling 365-day p25"),
                       ("p10_annual_pct", "Rolling 365-day p10"),
                       ("worst_annual_pct", "Rolling 365-day worst")):
        if table.get(key) is not None:
            lines.append(Line(key, label, _r(table[key], 2), "%", src))
    scaled = None if per30 is None else float(per30) * window.hours / MONTH_HOURS
    return {
        "profile": name or "shipped",
        "source": src,
        "expected_per_30d_pct": _r(per30, 2),
        "expected_this_window_pct": _r(scaled, 4),
        "planned_max_drawdown_pct": _r(table.get("planned_max_drawdown_pct"), 2),
        "note": CANNOT_MEASURE_NOTE,
        "lines": lines,
    }


# --------------------------------------------------------------------------- B. realised


def _price_at(kdb: sqlite3.Connection | None, pair: str, at: datetime,
              errors: list[str]) -> tuple[float | None, str]:
    """The last closed 1h candle at or before ``at``; the first one after it when the data
    starts later (``source`` says which). ``None`` when there is no candle at all."""
    ms = int(at.timestamp() * 1000)
    rows = _rows(kdb, "SELECT close, open_time FROM candles WHERE pair=? AND tf='1h'"
                 " AND is_closed=1 AND open_time<=? ORDER BY open_time DESC LIMIT 1",
                 (pair, ms), errors, "candles")
    if rows and rows[0].get("close") is not None:
        return _f(rows[0]["close"]), "closed candle at or before"
    rows = _rows(kdb, "SELECT close, open_time FROM candles WHERE pair=? AND tf='1h'"
                 " AND is_closed=1 AND open_time>? ORDER BY open_time ASC LIMIT 1",
                 (pair, ms), errors, "candles")
    if rows and rows[0].get("close") is not None:
        return _f(rows[0]["close"]), "first closed candle after (data starts later)"
    return None, "no candle"


def _costed_hold(seed: float, p0: float | None, p1: float | None) -> float | None:
    if not p0 or not p1 or seed <= 0:
        return None
    return seed * ((p1 / p0) * (1.0 - BENCHMARK_COST_PER_SIDE) ** 2 - 1.0)


def _benchmarks(cfg: Any, kdb: sqlite3.Connection | None, window: _Window, seed: float,
                errors: list[str]) -> dict[str, Any]:
    q = ("SELECT close FROM candles WHERE pair=? AND tf='1h' AND is_closed=1 AND open_time<=?"
         " ORDER BY open_time DESC LIMIT 1  -- at since and at until; return = p1/p0 *"
         f" (1 - {BENCHMARK_COST_PER_SIDE})^2 - 1")
    p0, s0 = _price_at(kdb, "BTC/USDT", window.since, errors)
    p1, s1 = _price_at(kdb, "BTC/USDT", window.until, errors)
    btc = _costed_hold(seed, p0, p1)
    data_only = {str(p) for p in (getattr(getattr(cfg, "universe", None),
                                          "data_only_symbols", None) or [])}
    pairs = [str(r["pair"]) for r in _rows(
        kdb, "SELECT DISTINCT pair FROM candles WHERE tf='1h' ORDER BY pair", (), errors,
        "candles") if str(r["pair"]) not in data_only]
    ratios: list[float] = []
    for pair in pairs:
        a, _ = _price_at(kdb, pair, window.since, errors)
        b, _ = _price_at(kdb, pair, window.until, errors)
        if a and b:
            ratios.append(b / a)
    basket = None
    if ratios and seed > 0:
        mean_ratio = sum(ratios) / len(ratios)
        basket = seed * (mean_ratio * (1.0 - BENCHMARK_COST_PER_SIDE) ** 2 - 1.0)
    return {
        "btc_price_since": p0, "btc_price_until": p1, "btc_price_source": f"{s0}; {s1}",
        "btc_hold_usdt": _r(btc, 2),
        "btc_hold_pct": None if btc is None or seed <= 0 else _r(100.0 * btc / seed, 4),
        "basket_pairs": len(ratios),
        "basket_hold_usdt": _r(basket, 2),
        "basket_hold_pct": None if basket is None or seed <= 0 else _r(100.0 * basket / seed, 4),
        "cost_per_side": BENCHMARK_COST_PER_SIDE,
        "query": q,
    }


def _pot(cfg: Any, jdb: sqlite3.Connection | None, root: Path, sleeve: str,
         errors: list[str]) -> dict[str, Any]:
    """The sleeve's cumulative pot — the console's own function, by import, never a copy."""
    try:
        from console.services import pot_service

        return pot_service.sleeve_pot(cfg, sleeve, conn=jdb, root=root)
    except Exception as exc:  # noqa: BLE001 - the pot is one line; its absence is a note
        errors.append(f"pot_service.sleeve_pot({sleeve}): {type(exc).__name__}: {exc}")
        seed = None
        try:
            seed = float(cfg.modes.test.seed_usdt.get(sleeve, 0.0))
        except Exception:  # noqa: BLE001
            pass
        return {"sleeve": sleeve, "seed_usdt": seed, "cumulative_net_usdt": None,
                "open_mark_usdt": 0.0, "ledger_gap_usdt": None, "fees_usdt": 0.0,
                "gross_usdt": 0.0, "realised_all_runs_usdt": 0.0, "restarts": 0}


def _realised(cfg: Any, jdb: sqlite3.Connection | None, kdb: sqlite3.Connection | None,
              root: Path, window: _Window, trades: Sequence[_Trade],
              errors: list[str]) -> dict[str, Any]:
    q_trades = ("SELECT close_profit_abs, exit_reason, close_date FROM trades WHERE is_open=0"
                " AND close_date>=:since AND close_date<:until  -- every ft_userdata/<s>/"
                "{tradesv3,runs/*}.sqlite, mode=ro; fees = Σ orders.cost × fee per side")
    closed = [t for t in trades if not t.is_open and window.contains(t.closed)]
    net = sum(t.profit for t in closed)
    fees = sum(t.fees for t in closed)
    gross = net + fees
    wins = sum(1 for t in closed if t.profit > 0)
    exits: dict[str, dict[str, Any]] = {}
    for t in closed:
        e = exits.setdefault(str(t.exit_reason or "unknown"), {"trades": 0, "net_usdt": 0.0,
                                                                "wins": 0})
        e["trades"] += 1
        e["net_usdt"] += t.profit
        e["wins"] += 1 if t.profit > 0 else 0
    for e in exits.values():
        e["net_usdt"] = round(e["net_usdt"], 4)
    per_sleeve: dict[str, Any] = {}
    seed_total = 0.0
    open_mark = 0.0
    cumulative_total: float | None = 0.0
    for sleeve in _sleeves(cfg):
        pot = _pot(cfg, jdb, root, sleeve, errors)
        mine = [t for t in closed if t.sleeve == sleeve]
        seed = _f(pot.get("seed_usdt"), 0.0)
        seed_total += seed
        open_mark += _f(pot.get("open_mark_usdt"), 0.0)
        cum = pot.get("cumulative_net_usdt")
        cumulative_total = None if (cum is None or cumulative_total is None) \
            else cumulative_total + float(cum)
        per_sleeve[sleeve] = {
            "name": _sleeve_name(sleeve),
            "seed_usdt": seed,
            "cumulative_net_usdt": cum,
            "gain_usdt": pot.get("gain_usdt"),
            "realised_all_runs_usdt": pot.get("realised_all_runs_usdt"),
            "open_mark_usdt": pot.get("open_mark_usdt"),
            "ledger_gap_usdt": pot.get("ledger_gap_usdt"),
            "restarts": pot.get("restarts"),
            "window_net_usdt": _r(sum(t.profit for t in mine), 4),
            "window_fees_usdt": _r(sum(t.fees for t in mine), 4),
            "window_trades": len(mine),
        }
    bench = _benchmarks(cfg, kdb, window, seed_total, errors)
    fee_ratio = None if gross <= 0 else fees / gross
    lines = [
        Line("cumulative_net_usdt", "Cumulative pot, all run databases", _r(cumulative_total, 2),
             "USDT", "console.services.pot_service.sleeve_pot: seed + Σ close_profit_abs over"
             " every run database + open positions marked to market"),
        Line("realised_net_usdt", "Realised in the window, net of fees", _r(net, 2), "USDT",
             q_trades),
        Line("gross_usdt", "Gross before fees", _r(gross, 2), "USDT", q_trades),
        Line("fees_usdt", "Fees paid", _r(fees, 2), "USDT", q_trades),
        Line("fee_gross_ratio", "Fees as a share of gross", None if fee_ratio is None
             else _r(100.0 * fee_ratio, 2), "% of gross", q_trades,
             None if fee_ratio is not None else "gross was not positive, so no share"),
        Line("trades", "Trades closed", len(closed), "count", q_trades),
        Line("win_rate_pct", "Win rate", None if not closed else _r(100.0 * wins / len(closed),
                                                                    2), "%", q_trades),
        Line("open_mark_usdt", "Open positions, marked to market", _r(open_mark, 2), "USDT",
             "pot_service.sleeve_pot: open trades in the current database at the newest"
             " closed candle"),
        Line("btc_hold_usdt", "Holding BTC over the same window, costed", bench["btc_hold_usdt"],
             "USDT", bench["query"], bench["btc_price_source"]),
        Line("basket_hold_usdt", f"Holding the equal-weight basket ({bench['basket_pairs']}"
             " pairs), costed", bench["basket_hold_usdt"], "USDT", bench["query"]),
    ]
    return {
        "seed_total_usdt": _r(seed_total, 2),
        "cumulative_net_usdt": _r(cumulative_total, 2),
        "realised_net_usdt": _r(net, 4),
        "gross_usdt": _r(gross, 4),
        "fees_usdt": _r(fees, 4),
        "fee_gross_ratio": _r(fee_ratio, 4),
        "trades": len(closed),
        "wins": wins,
        "win_rate": None if not closed else _r(wins / len(closed), 4),
        "exit_reasons": exits,
        "open_mark_usdt": _r(open_mark, 4),
        "benchmark": bench,
        "per_sleeve": per_sleeve,
        "definitions": dict(DEFINITIONS),
        "lines": lines,
    }


# --------------------------------------------------------------------------- C. the gaps


def _spans_from_ticks(ticks: Sequence[datetime], window: _Window,
                      gap_min: float) -> list[tuple[datetime, datetime]]:
    """Dark spans inside the window: any stretch longer than ``gap_min`` between two ticks,
    plus the stretch before the first tick and after the last one, clipped to the window."""
    gap = timedelta(minutes=gap_min)
    points = sorted(t for t in ticks if t is not None)
    spans: list[tuple[datetime, datetime]] = []
    if not points:
        return [(window.since, window.until)] if window.hours > 0 else []
    prev: datetime | None = None
    for t in points:
        if prev is not None and t - prev > gap:
            spans.append((prev, t))
        prev = t
    before = [t for t in points if t <= window.since]
    if not before and points[0] - window.since > gap:
        spans.append((window.since, points[0]))
    if window.until - points[-1] > gap:
        spans.append((points[-1], window.until))
    out: list[tuple[datetime, datetime]] = []
    for a, b in spans:
        a2, b2 = max(a, window.since), min(b, window.until)
        if b2 > a2:
            out.append((a2, b2))
    out.sort()
    return out


def _merge(spans: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _hours(spans: Sequence[tuple[datetime, datetime]]) -> float:
    return sum((b - a).total_seconds() for a, b in spans) / 3600.0


def _heartbeats(root: Path, sleeve: str, since: datetime, errors: list[str]) -> list[datetime]:
    """Every ``Bot heartbeat`` stamp in the sleeve's log from an hour before ``since``."""
    path = _userdata(root, sleeve) / "logs" / "freqtrade.log"
    floor = since - timedelta(hours=1)
    out: list[datetime] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "Bot heartbeat" not in line:
                    continue
                m = _HEARTBEAT_RE.match(line)
                if not m:
                    continue
                t = _parse(m.group(1))
                if t is not None and t >= floor:
                    out.append(t)
    except OSError:
        errors.append(f"{path.relative_to(root).as_posix()}: no log")
    return out


def _suspend_incidents(kdb: sqlite3.Connection | None, window: _Window,
                       errors: list[str]) -> tuple[int, float, list[dict[str, Any]]]:
    """``host_suspended`` incidents in the window and the hours they say the host slept."""
    rows = _rows(kdb, "SELECT opened_at, detail FROM ops_incidents WHERE kind='host_suspended'"
                 " AND opened_at>=? AND opened_at<? ORDER BY opened_at",
                 (window.since_iso, window.until_iso), errors, "ops_incidents")
    total = 0.0
    out: list[dict[str, Any]] = []
    for r in rows:
        hours = None
        data: Any = None
        try:
            data = json.loads(r.get("detail") or "")
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            for k, mult in (("hours", 1.0), ("slept_hours", 1.0), ("gap_hours", 1.0),
                            ("minutes", 1 / 60), ("gap_min", 1 / 60), ("seconds", 1 / 3600)):
                if isinstance(data.get(k), int | float):
                    hours = float(data[k]) * mult
                    break
            if hours is None:
                a = _parse(data.get("from") or data.get("start") or data.get("since"))
                b = _parse(data.get("to") or data.get("end") or data.get("until"))
                if a and b and b > a:
                    hours = (b - a).total_seconds() / 3600.0
        elif r.get("detail"):
            m = re.search(r"(\d+(?:\.\d+)?)\s*h\b", str(r["detail"]))
            if m:
                hours = float(m.group(1))
        total += hours or 0.0
        out.append({"opened_at": r.get("opened_at"), "hours": _r(hours, 3)})
    return len(rows), total, out


def _gap_uptime(cfg: Any, jdb: sqlite3.Connection | None, kdb: sqlite3.Connection | None,
                root: Path, window: _Window, errors: list[str]) -> Gap:
    q_log = ("grep 'Bot heartbeat' ft_userdata/<s>/logs/freqtrade.log (UTC stamps); a gap >"
             f" {HEARTBEAT_GAP_MIN:.0f} min in the merged series of both bots is dark time")
    q_nav = ("SELECT ts_utc FROM nav_points WHERE sleeve IN ('a','b') AND ts_utc>=? AND"
             f" ts_utc<? ORDER BY ts_utc; a gap > {NAV_GAP_MIN:.0f} min is a missed tick")
    beats: list[datetime] = []
    for sleeve in _sleeves(cfg):
        beats.extend(_heartbeats(root, sleeve, window.since, errors))
    dark = _spans_from_ticks(beats, window, HEARTBEAT_GAP_MIN)
    nav_ticks = [t for t in (_parse(r["ts_utc"]) for r in _rows(
        jdb, "SELECT ts_utc FROM nav_points WHERE sleeve IN ('a','b') AND ts_utc>=? AND"
        " ts_utc<? ORDER BY ts_utc", (_iso(window.since - timedelta(hours=1)),
                                      window.until_iso), errors, "nav_points")) if t]
    nav_dark = _spans_from_ticks(nav_ticks, window, NAV_GAP_MIN)
    either = _merge(list(dark) + list(nav_dark))
    dark_h, nav_h, either_h = _hours(dark), _hours(nav_dark), _hours(either)
    n_inc, inc_hours, inc_rows = _suspend_incidents(kdb, window, errors)
    spans = [{"from": _iso(a), "to": _iso(b), "hours": _r((b - a).total_seconds() / 3600, 3)}
             for a, b in dark]
    severity = 0.0 if window.hours <= 0 else min(100.0, 100.0 * dark_h / window.hours)
    if dark_h >= 0.05:
        shown = ", ".join(_span_text(a, b) for a, b in dark[:3])
        more = f" and {len(dark) - 3} more" if len(dark) > 3 else ""
        why = (f"the host was suspended ({n_inc} incident{'s' if n_inc != 1 else ''})"
               if n_inc else "no bot heartbeat was logged")
        sentence = (f"You were not trading {dark_h:.1f} of the last {window.hours:.0f} hours:"
                    f" {why}, {shown}{more}.")
    else:
        sentence = ""
    return Gap(
        key="uptime", title="Time the bots were not running", size=round(dark_h, 3),
        unit="hours", severity=round(severity, 2),
        cause="the host slept or the bots were down, so no candle was seen and no order"
              " could be placed",
        sentence=sentence,
        lines=[
            Line("dark_hours", "Hours with no bot heartbeat", _r(dark_h, 3), "hours", q_log),
            Line("nav_dark_hours", "Hours with no ledger tick", _r(nav_h, 3), "hours", q_nav),
            Line("dark_hours_either", "Hours with neither", _r(either_h, 3), "hours",
                 "union of the two"),
            Line("uptime_pct", "Uptime", None if window.hours <= 0 else
                 _r(100.0 * (1 - dark_h / window.hours), 2), "%", q_log),
            Line("host_suspended_incidents", "host_suspended incidents", n_inc, "count",
                 "SELECT opened_at, detail FROM ops_incidents WHERE kind='host_suspended' AND"
                 " opened_at>=? AND opened_at<?"),
            Line("host_suspended_hours", "Hours those incidents say the host slept",
                 _r(inc_hours, 3), "hours", "ops_incidents.detail (hours / from-to)"),
        ],
        detail={"spans": spans, "heartbeats": len(beats), "incidents": inc_rows},
    )


def _gap_refusals(jdb: sqlite3.Connection | None, window: _Window,
                  errors: list[str]) -> Gap:
    q = ("SELECT ts_utc, sleeve, pair, reason FROM gate_decisions WHERE allowed=0 AND"
         " intent='entry' AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc; rows with the same"
         f" (sleeve, pair, reason) within {RETRY_COLLAPSE_S}s collapse to one refusal")
    rows = _rows(jdb, "SELECT ts_utc, sleeve, pair, reason FROM gate_decisions WHERE allowed=0"
                 " AND intent='entry' AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc",
                 (window.since_iso, window.until_iso), errors, "gate_decisions")
    allowed = _rows(jdb, "SELECT COUNT(*) AS n FROM gate_decisions WHERE allowed=1 AND"
                    " callback='confirm_trade_entry' AND ts_utc>=? AND ts_utc<?",
                    (window.since_iso, window.until_iso), errors, "gate_decisions")
    n_allowed = int(allowed[0]["n"]) if allowed else 0
    last: dict[tuple[str, str, str], datetime] = {}
    by_reason: dict[str, dict[str, Any]] = {}
    distinct = 0
    for r in rows:
        t = _parse(r["ts_utc"])
        if t is None:
            continue
        key = (str(r["sleeve"]), str(r["pair"]), str(r["reason"]))
        bucket = by_reason.setdefault(key[2], {"rows": 0, "distinct": 0, "pairs": set()})
        bucket["rows"] += 1
        prev = last.get(key)
        if prev is None or (t - prev).total_seconds() > RETRY_COLLAPSE_S:
            distinct += 1
            bucket["distinct"] += 1
            bucket["pairs"].add(key[1])
        last[key] = t
    reasons = {k: {"rows": v["rows"], "distinct": v["distinct"],
                   "pairs": sorted(v["pairs"]),
                   "cause": REFUSAL_CAUSES.get(k, REFUSAL_CAUSES.get(k.split(":")[0], k))}
               for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["distinct"])}
    attempts = distinct + n_allowed
    severity = 0.0 if attempts == 0 else 100.0 * distinct / attempts
    top = next(iter(reasons.items()), None)
    sentence = ""
    if distinct:
        sentence = (f"{distinct} distinct entries were refused by the safety check"
                    f" ({len(rows)} rows counting retries) against {n_allowed} allowed;"
                    f" the biggest reason was {top[0]}: {top[1]['cause']}.") if top else ""
    return Gap(
        key="refused_entries", title="Entries the safety check refused", size=float(distinct),
        unit="count", severity=round(severity, 2),
        cause="each refusal is a rule doing its job or a data fault; only the reason says which",
        sentence=sentence,
        lines=[
            Line("refused_rows", "Refusal rows in the journal", len(rows), "count", q),
            Line("refused_distinct", "Distinct refusals after collapsing retries", distinct,
                 "count", q),
            Line("allowed_entries", "Entries allowed", n_allowed, "count",
                 "SELECT COUNT(*) FROM gate_decisions WHERE allowed=1 AND"
                 " callback='confirm_trade_entry' AND ts_utc>=? AND ts_utc<?"),
        ] + [Line(f"reason:{k}", f"Refused for {k}", v["distinct"], "distinct", q, v["cause"])
             for k, v in reasons.items()],
        detail={"by_reason": reasons},
    )


def _gap_funnel(jdb: sqlite3.Connection | None, window: _Window, trades: Sequence[_Trade],
                errors: list[str]) -> Gap:
    q_sig = ("SELECT status, status_reason FROM signals WHERE ts_utc>=? AND ts_utc<?")
    q_val = ("SELECT s.signal_id, v.verdict, v.error FROM signals s JOIN signal_validations v"
             " ON v.signal_id=s.signal_id WHERE s.ts_utc>=? AND s.ts_utc<?")
    q_prop = "SELECT abstain FROM proposals WHERE shadow=0 AND ts_utc>=? AND ts_utc<?"
    q_sw = "SELECT reason, COUNT(*) AS n FROM provider_switches WHERE ts_utc>=? AND ts_utc<?"
    q_llm = ("SELECT task, status, COUNT(*) AS n FROM llm_calls WHERE status!='ok' AND"
             " ts_utc>=? AND ts_utc<? GROUP BY task, status")
    q_gate = ("SELECT COUNT(*) FROM gate_decisions WHERE allowed=1 AND"
              " callback='confirm_trade_entry' AND ts_utc>=? AND ts_utc<?")
    params = (window.since_iso, window.until_iso)
    sigs = _rows(jdb, "SELECT signal_id, status, status_reason, proposal_run_id FROM signals"
                 " WHERE ts_utc>=? AND ts_utc<?", params, errors, "signals")
    vals = _rows(jdb, q_val, params, errors, "signal_validations")
    props = _rows(jdb, q_prop, params, errors, "proposals")
    switches = _rows(jdb, q_sw + " GROUP BY reason", params, errors, "provider_switches")
    llm = _rows(jdb, q_llm, params, errors, "llm_calls")
    gate = _rows(jdb, "SELECT COUNT(*) AS n FROM gate_decisions WHERE allowed=1 AND"
                 " callback='confirm_trade_entry' AND ts_utc>=? AND ts_utc<?", params, errors,
                 "gate_decisions")
    validated_ids = {str(v["signal_id"]) for v in vals}
    by_status: dict[str, int] = {}
    for s in sigs:
        by_status[str(s["status"])] = by_status.get(str(s["status"]), 0) + 1
    candidates = len(sigs)
    screened_out = by_status.get("screened_out", 0)
    unscreened = by_status.get("candidate", 0)
    screened = candidates - screened_out - unscreened
    with_verdict = sum(1 for v in vals if v["verdict"] != "uncertain" or not v.get("error"))
    errored = sum(1 for v in vals if v.get("error"))
    expired_unvalidated = sum(1 for s in sigs if s["status"] == "expired"
                              and str(s["signal_id"]) not in validated_ids)
    error_status = by_status.get("error", 0)
    blocked = by_status.get("blocked", 0)
    proposed = len(props)
    abstained = sum(int(p["abstain"] or 0) for p in props)
    gate_allowed = int(gate[0]["n"]) if gate else 0
    filled = sum(1 for t in trades if window.contains(t.opened))
    skipped_capability = sum(int(r["n"]) for r in switches
                             if str(r["reason"]) == "skipped_capability")
    on_all_failed = sum(1 for s in sigs
                        if str(s.get("status_reason") or "").startswith("screen_unavailable"))
    model_errors = {f"{r['task']}:{r['status']}": int(r["n"]) for r in llm}
    stages = [
        {"stage": "candidates", "count": candidates, "drop": "unscreened", "dropped": unscreened},
        {"stage": "screened", "count": screened, "drop": "screened_out", "dropped": screened_out},
        {"stage": "validated", "count": len(validated_ids), "drop": "expired_unvalidated",
         "dropped": expired_unvalidated},
        {"stage": "verdict", "count": with_verdict, "drop": "validator_error",
         "dropped": errored},
        {"stage": "proposed", "count": proposed, "drop": "abstain", "dropped": abstained},
        {"stage": "gate_allowed", "count": gate_allowed, "drop": "blackout",
         "dropped": blocked},
        {"stage": "filled", "count": filled, "drop": None, "dropped": 0},
    ]
    lost = max(0, screened - with_verdict)
    severity = 0.0 if screened == 0 else 100.0 * lost / screened
    sentence = ""
    if lost:
        top_err = next((_clip(str(v.get("error")), 100) for v in vals if v.get("error")), None)
        why = (f"the validator errored on {top_err}" if top_err
               else f"{expired_unvalidated} expired before any check ran")
        sentence = (f"{lost} of {screened} signals that passed the screen never got a"
                    f" verdict: {why}.")
    return Gap(
        key="funnel", title="Signals lost between the detector and a fill", size=float(lost),
        unit="count", severity=round(severity, 2),
        cause="each stage that drops a signal for a reason that is not the market is a"
              " decision never taken",
        sentence=sentence,
        lines=[
            Line("candidates", "Detector candidates", candidates, "count", q_sig),
            Line("screened", "Passed the screen", screened, "count", q_sig),
            Line("screened_out", "Dropped by the screen", screened_out, "count", q_sig),
            Line("validated", "Reached the validator", len(validated_ids), "count", q_val),
            Line("with_verdict", "Got a usable verdict", with_verdict, "count", q_val),
            Line("validator_errors", "Validator errors", errored, "count", q_val),
            Line("expired_unvalidated", "Expired unvalidated", expired_unvalidated, "count",
                 q_sig + " AND status='expired' AND no signal_validations row"),
            Line("error_status", "Signals in error status", error_status, "count", q_sig),
            Line("blocked", "Blocked (blackout, cooldown, stale data)", blocked, "count",
                 q_sig),
            Line("proposed", "Proposals written", proposed, "count", q_prop),
            Line("abstained", "Proposals that abstained", abstained, "count", q_prop),
            Line("gate_allowed", "Entries the gate allowed", gate_allowed, "count", q_gate),
            Line("filled", "Entries filled", filled, "count",
                 "trades.open_date in the window, every run database"),
            Line("skipped_capability", "Local model skipped (prompt too long)",
                 skipped_capability, "count", q_sw + " AND reason='skipped_capability'"),
            Line("on_all_failed", "Screen skipped because every provider failed", on_all_failed,
                 "count", q_sig + " AND status_reason LIKE 'screen_unavailable%'"),
            Line("model_errors", "Model call failures", sum(model_errors.values()), "count",
                 q_llm),
        ],
        detail={"stages": stages, "by_status": by_status, "model_errors": model_errors,
                "switches": {str(r["reason"]): int(r["n"]) for r in switches}},
    )


def _gap_model_spend(jdb: sqlite3.Connection | None, window: _Window,
                     errors: list[str]) -> Gap:
    q_dead = ("SELECT task, run_ref, SUM(COALESCE(cost_usd,0)) AS cost, SUM(status='ok') AS"
              " ok_n FROM llm_calls WHERE ts_utc>=? AND ts_utc<? GROUP BY task, run_ref;"
              " dead = ok_n = 0")
    q_runs = ("SELECT stage, SUM(COALESCE(cost_usd,0)) AS cost, COUNT(*) AS n FROM runs WHERE"
              " status!='success' AND COALESCE(cost_usd,0)>0 AND started_utc>=? AND"
              " started_utc<? GROUP BY stage")
    q_unc = ("SELECT SUM(COALESCE(cost_usd,0)) AS cost, COUNT(*) AS n FROM signal_validations"
             " WHERE verdict='uncertain' AND error IS NOT NULL AND ts_utc>=? AND ts_utc<?")
    q_total = ("SELECT SUM(COALESCE(cost_usd,0)) AS cost, COUNT(*) AS n FROM llm_calls WHERE"
               " ts_utc>=? AND ts_utc<?")
    params = (window.since_iso, window.until_iso)
    groups = _rows(jdb, "SELECT task, run_ref, SUM(COALESCE(cost_usd,0)) AS cost,"
                   " SUM(status='ok') AS ok_n, COUNT(*) AS n FROM llm_calls WHERE ts_utc>=?"
                   " AND ts_utc<? GROUP BY task, run_ref", params, errors, "llm_calls")
    failed_runs = _rows(jdb, q_runs, params, errors, "runs")
    uncertain = _rows(jdb, q_unc, params, errors, "signal_validations")
    total = _rows(jdb, q_total, params, errors, "llm_calls")
    dead_by_task: dict[str, dict[str, Any]] = {}
    dead_usd = 0.0
    for g in groups:
        if int(g["ok_n"] or 0) == 0:
            d = dead_by_task.setdefault(str(g["task"]), {"runs": 0, "usd": 0.0})
            d["runs"] += 1
            d["usd"] += _f(g["cost"])
            dead_usd += _f(g["cost"])
    for d in dead_by_task.values():
        d["usd"] = round(d["usd"], 4)
    runs_usd = sum(_f(r["cost"]) for r in failed_runs)
    unc_usd = _f(uncertain[0]["cost"]) if uncertain else 0.0
    unc_n = int(uncertain[0]["n"] or 0) if uncertain else 0
    total_usd = _f(total[0]["cost"]) if total else 0.0
    wasted = dead_usd + unc_usd
    severity = 0.0 if total_usd <= 0 else min(100.0, 100.0 * wasted / total_usd)
    sentence = ""
    if wasted >= 0.005:
        parts = []
        if dead_usd >= 0.005:
            worst = max(dead_by_task.items(), key=lambda kv: kv[1]["usd"])
            parts.append(f"{dead_usd:.2f} USD on calls that never returned a usable answer"
                         f" (most of it {worst[0]})")
        if unc_usd >= 0.005:
            parts.append(f"{unc_usd:.2f} USD on {unc_n} validations that came back"
                         " uncertain with an error")
        parts_text = ", and ".join(parts)
        sentence = f"{wasted:.2f} USD of model spend bought nothing: {parts_text}."
    return Gap(
        key="model_spend", title="Model spend that produced nothing", size=round(wasted, 4),
        unit="USD", severity=round(severity, 2),
        cause="a run that errors, times out or answers with a schema it was not asked for"
              " costs the same as one that decides",
        sentence=sentence,
        lines=[
            Line("dead_run_usd", "Spend on runs with no ok call", _r(dead_usd, 4), "USD", q_dead),
            Line("uncertain_error_usd", "Spend on validations uncertain-with-error",
                 _r(unc_usd, 4), "USD", q_unc),
            Line("uncertain_error_n", "Validations uncertain-with-error", unc_n, "count", q_unc),
            Line("failed_runs_usd", "Spend the runs table books on failed stages",
                 _r(runs_usd, 4), "USD", q_runs, "overlaps the llm_calls figure; an upper bound"),
            Line("total_usd", "All model spend in the window", _r(total_usd, 4), "USD", q_total),
        ] + [Line(f"dead:{task}", f"Dead spend on {task}", d["usd"], "USD", q_dead,
                  f"{d['runs']} run(s)") for task, d in sorted(dead_by_task.items())],
        detail={"dead_by_task": dead_by_task,
                "failed_runs": {str(r["stage"]): {"usd": _r(_f(r["cost"]), 4),
                                                  "runs": int(r["n"])} for r in failed_runs}},
    )


def _gap_decision_inputs(cfg: Any, jdb: sqlite3.Connection | None, root: Path,
                         window: _Window, errors: list[str]) -> Gap:
    rel = str(getattr(getattr(cfg, "paths", None), "state_latest", None)
              or "knowledge/state/latest.json")
    path = Path(rel) if Path(rel).is_absolute() else root / rel
    state: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        state = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError) as exc:
        errors.append(f"{rel}: {type(exc).__name__}")
    assets = state.get("assets") if isinstance(state.get("assets"), dict) else {}
    empty = not assets
    q = ("SELECT run_id, abstain, rationale_json FROM proposals WHERE shadow=0 AND ts_utc>=?"
         " AND ts_utc<?; empty inputs = rationale mentions assets={} / asof_candle_utc null")
    props = _rows(jdb, "SELECT run_id, abstain, rationale_json FROM proposals WHERE shadow=0"
                  " AND ts_utc>=? AND ts_utc<?", (window.since_iso, window.until_iso), errors,
                  "proposals")
    abstained = [p for p in props if int(p["abstain"] or 0)]
    on_empty = [p for p in abstained if _EMPTY_INPUTS_RE.search(str(p.get("rationale_json")
                                                                    or ""))]
    n = len(props)
    if n:
        severity = 100.0 * len(on_empty) / n
    else:
        severity = 50.0 if empty else 0.0
    sentence = ""
    if on_empty:
        name = _sleeve_name("b")
        sentence = (f"{name[0].upper()}{name[1:]}'s decision stage abstained"
                    f" {len(on_empty)} of {n} times because it was handed no indicators"
                    " (the market state has an empty assets block).")
    elif empty and n == 0:
        sentence = ("The market state still carries no per-asset indicators, so the next"
                    " decision will have nothing to decide on.")
    status = "empty" if empty else f"{len(assets)} assets"
    return Gap(
        key="decision_inputs", title="Decisions made on empty inputs", size=float(len(on_empty)),
        unit="count", severity=round(severity, 2),
        cause="the decision stage receives numbers computed by code; when the state file has"
              " none it must abstain, and every such run is a decision never taken",
        sentence=sentence,
        lines=[
            Line("latest_assets", "Assets in knowledge/state/latest.json", len(assets), "count",
                 f"json.load({rel})['assets']", status),
            Line("latest_data_fresh", "latest.json data_fresh", 1 if state.get("data_fresh")
                 else 0, "bool", f"json.load({rel})['data_fresh']"),
            Line("latest_age_min", "latest.json newest_data_age_min",
                 _r(_f(state.get("newest_data_age_min"), 0.0), 2) if state else None, "minutes",
                 f"json.load({rel})['newest_data_age_min']"),
            Line("proposals", "Proposals in the window", n, "count", q),
            Line("abstained", "Abstained", len(abstained), "count", q),
            Line("abstained_on_empty_inputs", "Abstained on empty inputs", len(on_empty),
                 "count", q),
        ],
        detail={"asof_candle_utc": state.get("asof_candle_utc"),
                "computed_utc": state.get("computed_utc"),
                "regime": (state.get("portfolio") or {}).get("regime") if isinstance(
                    state.get("portfolio"), dict) else None,
                "run_ids_on_empty": [str(p["run_id"]) for p in on_empty]},
    )


def _gap_staleness(cfg: Any, kdb: sqlite3.Connection | None, root: Path, window: _Window,
                   errors: list[str]) -> Gap:
    ages: dict[str, float] = {}
    fresh_path = root / "knowledge" / "state" / "freshness.json"
    try:
        from ops.lib import freshness

        fresh_path = Path(freshness.freshness_path(root))
        ages = freshness.blocking_ages(path=fresh_path, now=window.until)
    except Exception as exc:  # noqa: BLE001 - a missing sidecar is a line, not a crash
        errors.append(f"freshness: {type(exc).__name__}: {exc}")
    # The flags table is an audit log (ops.lib.flags._audit): a set row is active=1, a
    # clear row is active=0 with cleared_utc. Rows before 2026-09-25 stamped the clear row's
    # set_utc with the CLEAR time, newer ones carry the original set time — so pairing by
    # set_utc is unsafe. Walk the rows in order instead: a set opens the outage, the next
    # clear closes it, and an outage still open ends at its expires_utc or now.
    q_flags = ("SELECT active, set_utc, cleared_utc, expires_utc FROM flags WHERE"
               " name='data_stale' AND set_utc<? ORDER BY id; a set row (active=1) opens an"
               " outage, the next clear row (active=0) closes it at cleared_utc, an outage"
               " still open ends at expires_utc or now; clipped to the window")
    rows = _rows(kdb, "SELECT active, set_utc, cleared_utc, expires_utc FROM flags WHERE"
                 " name='data_stale' AND set_utc<? ORDER BY id", (window.until_iso,),
                 errors, "flags")
    spans: list[tuple[datetime, datetime]] = []
    open_since: datetime | None = None
    open_expires: datetime | None = None
    for r in rows:
        if int(r.get("active") or 0):
            a = _parse(r["set_utc"])
            if a is None:
                continue
            if open_since is None:
                open_since = a
            open_expires = _parse(r["expires_utc"])
            continue
        b = _parse(r["cleared_utc"])
        if b is None:
            continue
        if open_since is not None:
            spans.append((open_since, b))
            open_since = open_expires = None
        else:
            a = _parse(r["set_utc"])
            if a is not None and a < b:
                spans.append((a, b))
    if open_since is not None:
        spans.append((open_since, open_expires or window.until))
    clipped = [(max(a, window.since), min(b, window.until)) for a, b in spans]
    merged = _merge([(a, b) for a, b in clipped if b > a])
    stale_min = _hours(merged) * 60.0
    incidents = _rows(kdb, "SELECT COUNT(*) AS n FROM ops_incidents WHERE kind='stale_data'"
                      " AND opened_at>=? AND opened_at<?", (window.since_iso, window.until_iso),
                      errors, "ops_incidents")
    n_inc = int(incidents[0]["n"]) if incidents else 0
    # A candles_<tf> stamp is legitimately one timeframe old (the gate's own allowance), so
    # the staleness that matters is the age beyond that allowance.
    over: dict[str, float] = {}
    for source, age in ages.items():
        over[source] = max(0.0, age - _tf_allowance_min(source)) if age != float("inf") \
            else float("inf")
    worst = max(over.items(), key=lambda kv: kv[1]) if over else None
    severity = 0.0 if window.hours <= 0 else min(100.0, 100.0 * stale_min / (window.hours * 60))
    sentence = ""
    if stale_min >= 1:
        worst_text = ""
        if worst and worst[1] == float("inf"):
            worst_text = f"; {worst[0]} has never been stamped"
        elif worst and worst[1] >= 5:
            allowance = _tf_allowance_min(worst[0])
            past = f" ({worst[1]:.0f} past its allowance)" if allowance else ""
            worst_text = f"; {worst[0]} is {ages[worst[0]]:.0f} minutes old now{past}"
        sentence = (f"Entries were blocked for {stale_min / 60:.1f} hours by the data-stale"
                    f" flag{worst_text}.")
    lines = [
        Line("data_stale_minutes", "Minutes the data_stale flag was active", _r(stale_min, 1),
             "minutes", q_flags),
        Line("stale_data_incidents", "stale_data incidents", n_inc, "count",
             "SELECT COUNT(*) FROM ops_incidents WHERE kind='stale_data' AND opened_at in window"),
    ]
    for source, age in sorted(ages.items()):
        allowance = _tf_allowance_min(source)
        lines.append(Line(f"age:{source}", f"{source} age now",
                          None if age == float("inf") else _r(age, 1), "minutes",
                          f"ops.lib.freshness.blocking_ages({fresh_path.name}, now=until)",
                          "missing" if age == float("inf") else
                          (f"allowed {allowance:.0f}" if allowance else None)))
    return Gap(
        key="data_staleness", title="Time entries were blocked by stale data",
        size=round(stale_min, 1), unit="minutes", severity=round(severity, 2),
        cause="a stale price feed stops entries by design; the loss is every entry the rule"
              " would have made meanwhile",
        sentence=sentence, lines=lines,
        detail={"spans": [{"from": _iso(a), "to": _iso(b)} for a, b in merged],
                "ages_min": {k: (None if v == float("inf") else _r(v, 1))
                             for k, v in ages.items()}},
    )


def _tf_allowance_min(source: str) -> float:
    """``candles_1h`` → 60: a candle's stamp is its open, so it may be one bar old."""
    m = re.fullmatch(r"candles_(\d+)([mhdw])", source.strip().lower())
    if not m:
        return 0.0
    n = int(m.group(1))
    return n * {"m": 1, "h": 60, "d": 1440, "w": 10080}[m.group(2)]


def _gap_watcher(root: Path, window: _Window, trades: Sequence[_Trade],
                 errors: list[str]) -> Gap:
    path = root / WATCH_LOG_REL
    rule = (f"{WATCH_LOG_REL}: JSON lines with cycle_id watch-<stamp>Z and holdings; a cycle"
            " is wrong when holdings=0 while any run database has a trade open at that stamp")
    cycles: list[tuple[datetime, int]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                m = _CYCLE_RE.search(str(data.get("cycle_id") or ""))
                if not m:
                    continue
                t = _parse(datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").isoformat() + "Z")
                if t is not None and window.contains(t):
                    cycles.append((t, int(_f(data.get("holdings"), 0))))
    except OSError:
        errors.append(f"{WATCH_LOG_REL}: no log")
    opens = [(t.opened, t.closed) for t in trades if t.opened is not None]

    def open_at(when: datetime) -> bool:
        return any(a <= when and (b is None or when < b) for a, b in opens)

    with_open = [c for c in cycles if open_at(c[0])]
    wrong = [c for c in with_open if c[1] == 0]
    severity = 0.0 if not with_open else 100.0 * len(wrong) / len(with_open)
    sentence = ""
    if wrong:
        sentence = (f"The position watcher reported no holdings in {len(wrong)} of"
                    f" {len(with_open)} cycles while a position was open, so nothing was"
                    " watching it.")
    return Gap(
        key="holdings_watcher", title="Watcher cycles that saw no holdings while one was open",
        size=float(len(wrong)), unit="count", severity=round(severity, 2),
        cause="the watcher reads a database; when it is the wrong one, every open position"
              " goes unwatched",
        sentence=sentence,
        lines=[
            Line("cycles", "Watcher cycles in the window", len(cycles), "count", rule),
            Line("cycles_with_open_trade", "Cycles while a trade was open", len(with_open),
                 "count", rule),
            Line("cycles_zero_while_open", "Of those, reporting 0 holdings", len(wrong), "count",
                 rule),
        ],
        detail={"first_wrong": _iso(wrong[0][0]) if wrong else None,
                "last_wrong": _iso(wrong[-1][0]) if wrong else None},
    )


def _gap_fee_drag(cfg: Any, window: _Window, realised: Mapping[str, Any]) -> Gap:
    budget_month = _f(getattr(getattr(cfg, "risk", None), "max_fee_pct_per_month", 0.01), 0.01)
    n_trades = int(realised.get("trades") or 0)
    seed = _f(realised.get("seed_total_usdt"), 0.0)
    fees = _f(realised.get("fees_usdt"), 0.0)
    gross = _f(realised.get("gross_usdt"), 0.0)
    prorated = budget_month * window.hours / MONTH_HOURS
    fee_pct = None if seed <= 0 else fees / seed
    util = None if fee_pct is None or prorated <= 0 else fee_pct / prorated
    ratio = None if gross <= 0 else fees / gross
    severity = 0.0
    if ratio is not None:
        severity = max(severity, min(100.0, 100.0 * ratio))
    if util is not None:
        severity = max(severity, min(100.0, 100.0 * util))
    sentence = ""
    if fees >= 0.005:
        if ratio is not None:
            sentence = (f"Fees took {100 * ratio:.0f}% of gross profit ({_usd(fees)} on"
                        f" {_usd(gross)}) over {n_trades} closed trade"
                        f"{'' if n_trades == 1 else 's'}; the cost floor the strategy is"
                        f" planned against is {200 * BENCHMARK_COST_PER_SIDE:.2f}% a round"
                        " trip.")
        else:
            sentence = (f"Fees were {_usd(fees)} on trades that lost {_usd(-gross)} before"
                        " fees, so the venue was paid twice: once by the trade and once by"
                        " the loss.")
    q = ("fees from the run databases (Σ orders.cost × fee) ÷ Σ seed; budget ="
         f" risk.max_fee_pct_per_month × window_hours / {MONTH_HOURS}")
    return Gap(
        key="fee_drag", title="Fee drag against the monthly budget", size=round(fees, 4),
        unit="USDT", severity=round(severity, 2),
        cause="every sub-day trade pays the full round trip; the budget is 1% of NAV a month",
        sentence=sentence,
        lines=[
            Line("fees_usdt", "Fees paid", _r(fees, 4), "USDT", q),
            Line("fees_pct_of_seed", "Fees as % of the money in", None if fee_pct is None
                 else _r(100 * fee_pct, 6), "%", q),
            Line("budget_pct_prorated", "Fee budget pro-rated to the window",
                 _r(100 * prorated, 6), "%", q),
            Line("budget_utilisation", "Share of the pro-rated budget used", None if util is None
                 else _r(100 * util, 2), "%", q),
            Line("fee_gross_ratio", "Fees as a share of gross", None if ratio is None
                 else _r(100 * ratio, 2), "% of gross", q),
        ],
        detail={"max_fee_pct_per_month": budget_month},
    )


def _gap_ledger_integrity(cfg: Any, jdb: sqlite3.Connection | None, window: _Window,
                          realised: Mapping[str, Any], errors: list[str]) -> Gap:
    q = ("SELECT ts_utc, sleeve, nav_usdt, realized_pnl FROM nav_points WHERE sleeve IN"
         " ('a','b') AND ts_utc>=? AND ts_utc<? ORDER BY sleeve, ts_utc; a reset is a tick"
         " whose realized_pnl is 0 and nav is the seed right after a tick whose realized_pnl"
         " was not 0")
    rows = _rows(jdb, "SELECT ts_utc, sleeve, nav_usdt, realized_pnl FROM nav_points WHERE"
                 " sleeve IN ('a','b') AND ts_utc>=? AND ts_utc<? ORDER BY sleeve, ts_utc",
                 (_iso(window.since - timedelta(hours=1)), window.until_iso), errors,
                 "nav_points")
    per_sleeve = realised.get("per_sleeve") or {}
    resets: list[dict[str, Any]] = []
    prev: dict[str, dict[str, Any]] = {}
    for r in rows:
        sleeve = str(r["sleeve"])
        seed = _f((per_sleeve.get(sleeve) or {}).get("seed_usdt"), 0.0)
        p = prev.get(sleeve)
        t = _parse(r["ts_utc"])
        if p is not None and t is not None and window.contains(t):
            was = _f(p.get("realized_pnl"))
            now = _f(r.get("realized_pnl"))
            nav = _f(r.get("nav_usdt"))
            if abs(now) < RESET_EPS and abs(was) >= RESET_EPS and (
                    seed <= 0 or abs(nav - seed) < RESET_EPS):
                resets.append({"ts_utc": r["ts_utc"], "sleeve": sleeve,
                               "jump_usdt": _r(nav - _f(p.get("nav_usdt")), 4),
                               "hidden_usdt": _r(was, 4)})
        prev[sleeve] = r
    hidden = sum(_f(x["hidden_usdt"]) for x in resets)
    gap_total = 0.0
    gap_known = False
    for s in per_sleeve.values():
        g = s.get("ledger_gap_usdt")
        if g is not None:
            gap_total += float(g)
            gap_known = True
    strategy_net = _f(realised.get("realised_net_usdt"), 0.0)
    denom = abs(hidden) + abs(strategy_net)
    severity = 0.0 if not resets else (100.0 if denom <= 0
                                       else min(100.0, 100.0 * abs(hidden) / denom))
    sentence = ""
    if resets:
        who = ", ".join(sorted({_sleeve_name(x["sleeve"]) for x in resets}))
        sentence = (f"The 15-minute ledger reset to the seed {len(resets)} time(s) in this"
                    f" window ({who}), hiding {_usd(hidden)} of earlier results; the"
                    " cumulative pot on Home counts them.")
    return Gap(
        key="ledger_integrity", title="Ledger resets", size=float(len(resets)), unit="count",
        severity=round(severity, 2),
        cause="the 15-minute ledger only knows the database the bot is running on; a fresh"
              " database restarts it from the seed",
        sentence=sentence,
        lines=[
            Line("resets", "Ledger resets in the window", len(resets), "count", q),
            Line("hidden_usdt", "Results a reset hid", _r(hidden, 4), "USDT", q),
            Line("ledger_gap_usdt", "Cumulative pot minus the ledger, now",
                 _r(gap_total, 4) if gap_known else None, "USDT",
                 "pot_service.sleeve_pot: cumulative_net_usdt - nav_points latest nav_usdt"),
        ],
        detail={"resets": resets},
    )


def _gap_events(jdb: sqlite3.Connection | None, window: _Window, trades: Sequence[_Trade],
                realised: Mapping[str, Any], errors: list[str]) -> Gap:
    q_trades = ("SELECT pair, exit_reason, close_profit_abs FROM trades WHERE is_open=0 AND"
                f" exit_reason IN {EVENT_EXIT_REASONS} AND close_date in the window, every"
                " run database")
    q_audit = ("SELECT ts_utc, actor, action, target FROM audit_log WHERE result='ok' AND"
               f" action IN {EVENT_AUDIT_ACTIONS} AND ts_utc>=? AND ts_utc<?")
    events = [t for t in trades if not t.is_open and window.contains(t.closed)
              and str(t.exit_reason or "") in EVENT_EXIT_REASONS]
    audit = _rows(jdb, "SELECT ts_utc, actor, action, target FROM audit_log WHERE result='ok'"
                  " AND action IN (?,?,?) AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc",
                  (*EVENT_AUDIT_ACTIONS, window.since_iso, window.until_iso), errors,
                  "audit_log")

    def actor_for(t: _Trade) -> str:
        if t.exit_reason != "force_exit" or t.closed is None:
            return "code" if t.exit_reason == "target_zero" else "unknown"
        best: tuple[float, str] | None = None
        for a in audit:
            at = _parse(a["ts_utc"])
            if at is None:
                continue
            target = str(a.get("target") or "").lower()
            if target not in ("", t.sleeve, "both", "all"):
                continue
            gap = abs((at - t.closed).total_seconds())
            if gap <= 300 and (best is None or gap < best[0]):
                best = (gap, ":".join(str(a["actor"]).split(":")[:2]))
        return best[1] if best else "unknown"

    rows = [{"sleeve": t.sleeve, "pair": t.pair, "exit_reason": t.exit_reason,
             "closed_utc": _iso(t.closed) if t.closed else None, "net_usdt": _r(t.profit, 4),
             "fees_usdt": _r(t.fees, 4), "actor": actor_for(t)} for t in events]
    pnl = sum(t.profit for t in events)
    strategy_net = _f(realised.get("realised_net_usdt"), 0.0) - pnl
    denom = abs(pnl) + abs(strategy_net)
    severity = 0.0 if denom <= 0 else min(100.0, 100.0 * abs(pnl) / denom)
    sentence = ""
    if events:
        by_kind: dict[str, float] = {}
        for r in rows:
            label = ("a hand-typed flatten" if r["exit_reason"] == "force_exit"
                     and str(r["actor"]).startswith("human") else
                     "a sell on no mandate" if r["exit_reason"] == "target_zero"
                     else f"a {r['exit_reason']}")
            key = f"{label} on {_sleeve_name(str(r['sleeve']))}"
            by_kind[key] = by_kind.get(key, 0.0) + _f(r["net_usdt"])
        listed = ", ".join(f"{k} ({v:+.2f})" for k, v in by_kind.items())
        sentence = (f"Operator and code events cost {_usd(pnl)} that the strategy never"
                    f" chose: {listed}.")
    return Gap(
        key="operator_events", title="Operator and code events", size=round(pnl, 4), unit="USDT",
        severity=round(severity, 2),
        cause="a flatten typed at the console or a sell the code made on a missing mandate is"
              " not a strategy result in either direction",
        sentence=sentence,
        lines=[
            Line("events", "Force exits and target-zero sells", len(events), "count", q_trades),
            Line("events_net_usdt", "Their net P&L", _r(pnl, 4), "USDT", q_trades),
            Line("audit_rows", "Flatten / kill rows in the audit log", len(audit), "count",
                 q_audit),
        ],
        detail={"events": rows, "audit": [{"ts_utc": a["ts_utc"], "action": a["action"],
                                           "target": a.get("target"),
                                           "actor": ":".join(str(a["actor"]).split(":")[:2])}
                                          for a in audit]},
    )


def _guarded(name: str, fn: Any, notes: list[str], **kwargs: Any) -> Gap:
    """A gap that cannot be computed is a gap row that says so — never a missing ledger."""
    try:
        gap = fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the ledger must render whatever broke
        notes.append(f"{name}: {type(exc).__name__}: {exc}")
        gap = Gap(key=name, title=name.replace("_", " "), size=0.0, unit="n/a", severity=0.0,
                  cause="could not be computed", sentence="",
                  error=f"{type(exc).__name__}: {exc}")
    gap.weight = GAP_WEIGHTS.get(gap.key, 1.0)
    return gap


# --------------------------------------------------------------------------- D. top three


def _top_three(gaps: Sequence[Gap]) -> list[dict[str, Any]]:
    ranked = sorted((g for g in gaps if g.sentence and g.error is None),
                    key=lambda g: (-g.score, -abs(g.size)))
    return [{"key": g.key, "title": g.title, "sentence": g.sentence, "size": g.size,
             "unit": g.unit, "severity": g.severity, "weight": g.weight, "score": g.score}
            for g in ranked[:3]]


# --------------------------------------------------------------------------- compute


def compute(cfg: Any, jdb: sqlite3.Connection | None, kdb: sqlite3.Connection | None,
            root: Path | str | None, *, since: datetime | str, until: datetime | str,
            key: str = "custom") -> Ledger:
    """The ledger for one window ``[since, until)``. Never raises for a data fault: every
    section that cannot be computed is a line or a gap that says so, listed in ``errors``."""
    a, b = _parse(since), _parse(until)
    if a is None or b is None:
        raise ValueError("since and until must be ISO-8601 timestamps or datetimes")
    base = _state_root(Path(root) if root is not None else None)
    window = _Window(key, a, b)
    errors: list[str] = []
    trades = _all_trades(cfg, base, errors)
    expected = _expected(cfg, base, window)
    realised = _realised(cfg, jdb, kdb, base, window, trades, errors)
    gaps = [
        _guarded("uptime", _gap_uptime, errors, cfg=cfg, jdb=jdb, kdb=kdb, root=base,
                 window=window, errors=errors),
        _guarded("refused_entries", _gap_refusals, errors, jdb=jdb, window=window,
                 errors=errors),
        _guarded("funnel", _gap_funnel, errors, jdb=jdb, window=window, trades=trades,
                 errors=errors),
        _guarded("model_spend", _gap_model_spend, errors, jdb=jdb, window=window,
                 errors=errors),
        _guarded("decision_inputs", _gap_decision_inputs, errors, cfg=cfg, jdb=jdb, root=base,
                 window=window, errors=errors),
        _guarded("data_staleness", _gap_staleness, errors, cfg=cfg, kdb=kdb, root=base,
                 window=window, errors=errors),
        _guarded("holdings_watcher", _gap_watcher, errors, root=base, window=window,
                 trades=trades, errors=errors),
        _guarded("fee_drag", _gap_fee_drag, errors, cfg=cfg, window=window, realised=realised),
        _guarded("ledger_integrity", _gap_ledger_integrity, errors, cfg=cfg, jdb=jdb,
                 window=window, realised=realised, errors=errors),
        _guarded("operator_events", _gap_events, errors, jdb=jdb, window=window,
                 trades=trades, realised=realised, errors=errors),
    ]
    return Ledger(window=window.to_json(), expected=expected, realised=realised, gaps=gaps,
                  top_three=_top_three(gaps), errors=errors)


def compute_report(cfg: Any, jdb: sqlite3.Connection | None, kdb: sqlite3.Connection | None,
                   root: Path | str | None, *, now: datetime | None = None) -> Report:
    """Both windows: the last 24 hours, and since the test began (the first row in any run
    database; the last 24 hours when there is none)."""
    until = (now or datetime.now(UTC)).astimezone(UTC)
    base = _state_root(Path(root) if root is not None else None)
    start = _test_start(_all_trades(cfg, base, []))
    day = until - timedelta(hours=DEFAULT_WINDOW_HOURS)
    profile = getattr(getattr(cfg, "profiles", None), "active", None) or None
    windows = {
        "last_24h": compute(cfg, jdb, kdb, base, since=day, until=until, key="last_24h"),
        "since_start": compute(cfg, jdb, kdb, base, since=min(start, day) if start else day,
                               until=until, key="since_start"),
    }
    return Report(generated_utc=_iso(until), profile=profile, windows=windows)


# --------------------------------------------------------------------------- write


def report_md_path(root: Path | str | None, day: str) -> Path:
    return _state_root(Path(root) if root is not None else None) / "reports" / "profit-gaps" \
        / f"{day}.md"


def report_json_path(root: Path | str | None) -> Path:
    """``knowledge/state/profit_gaps.json`` under the state root — the same root
    ``ops.lib.paths.data_path`` resolves ``paths.*`` against."""
    base = _state_root(Path(root) if root is not None else None)
    return base / "knowledge" / "state" / "profit_gaps.json"


def _fmt(value: Any, unit: str) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}" if abs(value) >= 100 else f"{value:,.4g}"
    return str(value)


def _md_lines(lines: Iterable[Line]) -> list[str]:
    out = ["| line | value | unit | query |", "|---|---|---|---|"]
    for ln in lines:
        note = f" _{ln.note}_" if ln.note else ""
        q = ln.query.replace("|", "\\|").replace("\n", " ")
        out.append(f"| {ln.label}{note} | {_fmt(ln.value, ln.unit)} | {ln.unit} | `{q}` |")
    return out


def render_markdown(report: Report) -> str:
    out = [f"# Profit & gap ledger — {report.generated_utc}", "",
           f"Active profile: **{report.profile or 'shipped'}**. Two windows: the last 24"
           " hours and since the test began. Every line carries the query that produced"
           " it; re-run it before arguing with it.", ""]
    for key, led in report.windows.items():
        w = led.window
        out += [f"## Window `{key}`: {w['since_utc']} → {w['until_utc']}"
                f" ({w['hours']:.1f} h)", ""]
        e = led.expected
        out += ["### A. Expected", "",
                f"Profile **{e['profile']}** — expected **{_fmt(e['expected_per_30d_pct'], '%')}"
                f"% per 30 days** ({_fmt(e['expected_this_window_pct'], '%')}% scaled to this"
                f" window), planned max drawdown **{_fmt(e['planned_max_drawdown_pct'], '%')}%**."
                f" Source: {e['source']}.", "", f"> {e['note']}", "",
                *_md_lines(e["lines"]), ""]
        r = led.realised
        out += ["### B. Realised", "", *_md_lines(r["lines"]), ""]
        if r.get("exit_reasons"):
            out += ["Exit-reason mix: " + ", ".join(
                f"{k} ×{v['trades']} ({v['net_usdt']:+.2f}, {v['wins']} won)"
                for k, v in r["exit_reasons"].items()), ""]
        out += ["### C. Gaps", ""]
        for g in led.gaps:
            head = (f"#### C.{led.gaps.index(g) + 1} {g.title} — {_fmt(g.size, g.unit)} {g.unit}"
                    f" (severity {g.severity:.0f} × weight {g.weight:.1f} = {g.score:.0f})")
            out += [head, "", f"Cause: {g.cause}.", ""]
            if g.error:
                out += [f"Could not be computed: {g.error}", ""]
            if g.sentence:
                out += [f"> {g.sentence}", ""]
            out += [*_md_lines(g.lines), ""]
        out += ["### D. The top three", ""]
        if led.top_three:
            out += [f"{i + 1}. {t['sentence']}" for i, t in enumerate(led.top_three)]
        else:
            out += ["Nothing in this window was lost to a reason that is not the strategy."]
        out += [""]
        if led.errors:
            out += ["Notes: " + "; ".join(led.errors), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def write(report: Report, root: Path | str | None = None) -> tuple[Path, Path]:
    """``reports/profit-gaps/<YYYY-MM-DD>.md`` and ``knowledge/state/profit_gaps.json`` under
    the state root. Both are rewritten whole, atomically, so a rerun is idempotent."""
    day = report.generated_utc[:10]
    md = report_md_path(root, day)
    js = report_json_path(root)
    for path, text in ((md, render_markdown(report)),
                       (js, json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return md, js


def appendix_line(cfg: Any, jdb: sqlite3.Connection | None, kdb: sqlite3.Connection | None,
                  root: Path | str | None, *, now: datetime | None = None) -> str:
    """Compute and write the ledger, and return the one line the daily review appends.
    Never raises: a ledger failure is a line that says so, never a failed review."""
    try:
        report = compute_report(cfg, jdb, kdb, root, now=now)
        md, _ = write(report, root)
        top = report.windows["last_24h"].top_three
        first = top[0]["sentence"] if top else "no gap found in the last 24h"
        base = _state_root(Path(root) if root is not None else None)
        try:
            rel = md.relative_to(base).as_posix()
        except ValueError:
            rel = md.as_posix()
        return f"- profit & gap ledger: `{rel}` — {first}"
    except Exception as exc:  # noqa: BLE001 - guarded by contract
        return f"- profit & gap ledger: failed ({type(exc).__name__}: {exc})"
