"""The feature registry — one audited place that says where every number came from.

TIER 2 (``runs/**``): a human writes this, a Claude run only reads it.

Why a registry exists
---------------------
Earn's standing rule is *numbers in, numbers out*: Python computes a value and the model
reasons over it. The failure mode that rule does not cover is a value that is **real but
wrong about itself** — a metric that is three days stale, or one whose free tier silently
became a paywall, quietly re-entering a prompt as if it were current. So every feature key
a skill may emit is declared here with its source URL, whether it needs a key, its rate
limit, its refresh cadence and the maximum lag past which it must be flagged stale.

:data:`NOT_AVAILABLE_FREE` is the other half: things that were *looked for and are not
free from this host*, recorded with the verified failure so a future run does not spend a
day rediscovering each paywall — and, more importantly, so nobody writes a skill that asks
a model to estimate one.

Dependencies
------------
Standard library plus pandas/numpy. Nothing here imports ``ops``, ``console`` or
freqtrade, so a skill script, a backtest and a bare REPL can all import it. Path
resolution mirrors :mod:`ops.lib.paths` (``$EARN_STATE_ROOT``) rather than importing it,
which keeps this package importable from a container that ships no ``ops``.

No look-ahead
-------------
Every loader in this package returns a frame with a UTC timestamp column, and every
consumer slices it with :func:`as_of` before computing. A feature is reproducible as of a
timestamp or it does not ship — the backtest and the decision replay both run these
functions over history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

__all__ = [
    "NOT_AVAILABLE_FREE",
    "REGISTRY",
    "FeatureSpec",
    "Sourced",
    "as_of",
    "cache_dir",
    "candle_path",
    "data_dir",
    "iso",
    "knowledge_dir",
    "load_candles",
    "parse_iso",
    "read_json",
    "repo_root",
    "spec_for",
    "staleness_minutes",
    "state_root",
    "utcnow",
    "write_json_atomic",
]

#: ``runs/features/__init__.py`` -> ``runs/features`` -> ``runs`` -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

STATE_ROOT_ENV = "EARN_STATE_ROOT"
DATA_DIR_ENV = "EARN_DATA_DIR"


# --------------------------------------------------------------------------- paths


def repo_root() -> Path:
    """The checkout this module was imported from."""
    return REPO_ROOT


def state_root(env: dict[str, str] | None = None) -> Path:
    """The live data root: ``$EARN_STATE_ROOT`` when set, else the checkout root."""
    raw = (env if env is not None else os.environ).get(STATE_ROOT_ENV)
    return Path(raw).expanduser().resolve() if raw else REPO_ROOT


def data_dir(env: dict[str, str] | None = None) -> Path:
    """Where the feather candle store lives.

    ``$EARN_DATA_DIR`` wins, then ``<state root>/data``. The override exists because the
    Windows checkout carries no ``data/`` — the populated copy is the WSL one, and a
    validation run points at it without moving 1.5 GB of candles.
    """
    raw = (env if env is not None else os.environ).get(DATA_DIR_ENV)
    return Path(raw).expanduser().resolve() if raw else state_root(env) / "data"


def knowledge_dir(env: dict[str, str] | None = None) -> Path:
    return state_root(env) / "knowledge"


def cache_dir(name: str, env: dict[str, str] | None = None) -> Path:
    """``knowledge/market/<name>/`` — the on-disk cache for a fetched series.

    Tier 0, so it is readable by every run and committed with the repo: a cached series is
    the only reason a vendor-hosted feature can be replayed a year from now.
    """
    d = knowledge_dir(env) / "market" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def candle_path(pair: str, timeframe: str, root: Path | None = None) -> Path:
    """``data/binance/BTC_USDT-4h.feather`` — the same layout the containers read."""
    base = root if root is not None else data_dir()
    return base / "binance" / f"{pair.replace('/', '_')}-{timeframe}.feather"


# --------------------------------------------------------------------------- time


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with ``Z``, the one timestamp format in this repo."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def staleness_minutes(as_of_utc: str | datetime | None,
                      now: datetime | None = None) -> float | None:
    """Minutes between ``as_of`` and ``now``. ``None`` in, ``None`` out — never a zero."""
    if as_of_utc is None:
        return None
    stamp = parse_iso(as_of_utc) if isinstance(as_of_utc, str) else as_of_utc
    ref = now or utcnow()
    return round((ref - stamp.astimezone(UTC)).total_seconds() / 60.0, 1)


# --------------------------------------------------------------------------- values


@dataclass(frozen=True)
class Sourced:
    """A number that knows where it came from and how old it is.

    A source being down produces ``stale=True`` with the last good value, never a guess and
    never a silent zero: every consumer in this repo treats ``stale`` as a reason to
    abstain or to tighten, never as a reason to act.
    """

    key: str
    value: float | int | str | None
    as_of: str | None = None
    stale: bool = False
    source: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "as_of": self.as_of,
                "stale": self.stale, "source": self.source, "note": self.note}


@dataclass(frozen=True)
class FeatureSpec:
    """One declared feature key.

    ``max_lag_min`` is the age past which the value must be flagged stale; it is the
    number a consumer checks, not a suggestion. ``needs_key=True`` means the endpoint is
    paywalled or account-scoped and therefore **out** — such a key is never registered as
    usable, only recorded.
    """

    key: str
    module: str
    source: str
    url: str
    cadence: str
    max_lag_min: int
    needs_key: bool = False
    rate_limit: str = ""
    history_from: str = ""
    verified_utc: str = ""
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key, "module": self.module, "source": self.source, "url": self.url,
            "cadence": self.cadence, "max_lag_min": self.max_lag_min,
            "needs_key": self.needs_key, "rate_limit": self.rate_limit,
            "history_from": self.history_from, "verified_utc": self.verified_utc,
            "notes": self.notes,
        }


def _spec(**kw) -> tuple[str, FeatureSpec]:
    s = FeatureSpec(**kw)
    return s.key, s


#: Verification date carried on every entry below: each URL was requested from this host
#: and returned the status recorded in ``notes``.
VERIFIED = "2026-09-23"

#: Every feature key any Earn skill may emit, keyed by name.
#:
#: The rule this table enforces: a key that is not in here has no audited provenance, so a
#: skill that emits it is emitting a number nobody can trace. ``evals``/tests assert that
#: the skills' JSON payloads only carry registered keys.
REGISTRY: dict[str, FeatureSpec] = dict([
    # ---- Deribit DVOL and the option surface (runs/features/deribit.py, B1) -----------
    _spec(key="dvol_last", module="deribit", source="Deribit DVOL index",
          url="https://www.deribit.com/api/v2/public/get_volatility_index_data"
              "?currency=BTC&start_timestamp=<ms>&end_timestamp=<ms>&resolution=86400",
          cadence="hourly (daily bar closes 00:00 UTC)", max_lag_min=1500,
          rate_limit="public tier, <=1000 points/request; page backwards on `continuation`",
          history_from="2021-03-24 (BTC and ETH both, verified by paging)",
          verified_utc=VERIFIED,
          notes="HTTP 200. Keyless. A browser User-Agent is required — Deribit resets the "
                "connection on urllib's default UA."),
    _spec(key="dvol_pctile_2y", module="deribit", source="derived from dvol_last",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Rank of today's DVOL in the trailing 730 calendar days. Percentile, never "
                "a fixed threshold — the level regime-shifts."),
    _spec(key="dvol_chg_5d", module="deribit", source="derived from dvol_last",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="DVOL today minus DVOL 5 daily bars ago, in vol points."),
    _spec(key="rr25", module="deribit", source="Deribit option book summary",
          url="https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
              "?currency=BTC&kind=option",
          cadence="on demand", max_lag_min=180, verified_utc=VERIFIED,
          rate_limit="public tier; 970 instruments in one ~430 KB response",
          notes="25-delta risk reversal from mark_iv + underlying_price by a local "
                "Black-Scholes delta. OBSERVE ONLY until six months of self-collected "
                "history exist — there is no free skew history to backtest against."),
    _spec(key="butterfly", module="deribit", source="Deribit option book summary",
          url="https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
              "?currency=BTC&kind=option",
          cadence="on demand", max_lag_min=180, verified_utc=VERIFIED,
          notes="25-delta butterfly. OBSERVE ONLY, same reason as rr25."),
    _spec(key="deribit_option_oi_total", module="deribit",
          source="Deribit option book summary",
          url="https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
              "?currency=BTC&kind=option",
          cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Venue-migration canary: DVOL goes STALE rather than WRONG if Deribit's "
                "BTC option liquidity migrates, which is the dangerous failure. Alert on a "
                "50% fall from the 90d median."),

    # ---- volatility (runs/features/volatility.py, B1) ---------------------------------
    _spec(key="rv_d", module="volatility", source="local 4h feather candles",
          url="data/binance/<PAIR>-4h.feather", cadence="4h", max_lag_min=300,
          history_from="2017-08-17", verified_utc=VERIFIED,
          notes="Realised variance for one UTC day = sum of squared 4h log returns."),
    _spec(key="sigma_hat", module="volatility", source="fitted a + b*DVOL, else HAR",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Forward 7d annualised vol forecast, in vol POINTS (percent). The fitted "
                "mapping is mandatory: raw DVOL overstates realised vol by 13-29%."),
    _spec(key="source", module="volatility", source="blend | dvol | har | none",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Which estimator produced sigma_hat. Always emitted; a forecast that will "
                "not say where it came from is not usable. `blend` is the normal case: "
                "the HAR walk-forward and the fitted DVOL map averaged 50/50, which beat "
                "both components out of sample on both assets."),
    _spec(key="trailing_vol_30d", module="volatility", source="local 4h feather candles",
          url="data/binance/<PAIR>-4h.feather", cadence="daily", max_lag_min=1500,
          verified_utc=VERIFIED,
          notes="Trailing 30d annualised realised vol, in points. Reported for contrast — "
                "it is the estimator sigma_hat replaces, not an input to sizing."),
    _spec(key="oos_r2_250d", module="volatility", source="local walk-forward",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Rolling 250d OOS R^2 of whatever produced sigma_hat, scored against the "
                "expanding mean of past actuals. Below 0.05 the forecast is refused."),
    _spec(key="dvol_oos_r2_250d", module="volatility", source="local walk-forward",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="The DVOL map's own rolling health, reported beside the HAR one so a "
                "decayed component is visible before it drags the blend under the floor."),
    _spec(key="vrp", module="volatility", source="dvol_last - trailing 30d realised",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Variance risk premium in vol points. Sample mean +8.09 — DVOL sits "
                "systematically ABOVE realised, which is why the raw index may not be "
                "substituted into a target_vol/sigma denominator."),
    _spec(key="har_oos_r2_250d", module="volatility", source="local walk-forward",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Rolling 250d out-of-sample R^2 of the HAR forecast. Self-health number: "
                "below 0.05 the model is broken and no forecast is emitted."),
    _spec(key="vol_target_scalar", module="volatility", source="derived",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="clip(target_annual / sigma_hat, 0, 1). Clamped at 1.0 by construction: a "
                "stuck or absent input can shrink a position, never grow one."),
    _spec(key="scalar_source", module="volatility",
          source="sigma_hat | trailing_30d_fallback | none",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="Which estimator the scalar was computed from. A withheld forecast still "
                "yields a cautious scalar: the health floor bites on 11.7% of BTC days and "
                "those cluster in March 2020 and Jan-Aug 2022, so emitting nothing would "
                "remove the volatility cap in exactly the regimes it exists for."),
    _spec(key="degraded", module="volatility", source="derived",
          url="local", cadence="daily", max_lag_min=1500, verified_utc=VERIFIED,
          notes="True when sigma_hat was withheld and the scalar came from the fallback. "
                "A degraded reading may tighten a position and may never loosen one."),

    # ---- statistical honesty (runs/features/sampling.py, B1) --------------------------
    _spec(key="effective_n", module="sampling", source="label uniqueness",
          url="local", cadence="per study", max_lag_min=10**7, verified_utc=VERIFIED,
          notes="sum of per-label average uniqueness. Measured on BTC 4h triple-barrier "
                "labels: 19,929 rows, average uniqueness 0.154, EFFECTIVE_N ~ 3,067. A "
                "proposal that quotes a row count instead of this is refused."),
    _spec(key="expected_max_sr", module="sampling", source="deflated-Sharpe hurdle",
          url="local", cadence="per study", max_lag_min=10**7, verified_utc=VERIFIED,
          notes="Expected best in-sample Sharpe from N zero-skill trials over T years. "
                "N=200, T=9.1 gives ~1.19, which is the hurdle a candidate must clear."),
    _spec(key="years_to_detect", module="sampling", source="power calculation",
          url="local", cadence="per study", max_lag_min=10**7, verified_utc=VERIFIED,
          notes="Years of live data needed to separate two Sharpes at a stated power. "
                "1.14 vs 0.83 needs 219 years — attached to every post-mortem so a good "
                "quarter is never read as validation."),

    # ---- the leverage record (runs/features/derivatives.py, B2) -----------------------
    _spec(key="fr_8h", module="derivatives", source="Binance USD-M funding",
          url="https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT"
              "&startTime=<ms>&limit=1000",
          cadence="8h", max_lag_min=600, rate_limit="weight 1; 1000 rows/request",
          history_from="2019-09-10 (7,711 prints, paged)", verified_utc=VERIFIED,
          notes="HTTP 200. Full history, unlike fapi/*/futures/data/*. `markPrice` is an "
                "EMPTY STRING on early rows — the loader must tolerate it."),
    _spec(key="funding_live", module="derivatives", source="Binance premium index",
          url="https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
          cadence="realtime", max_lag_min=30, verified_utc=VERIFIED,
          notes="HTTP 200. lastFundingRate, markPrice, indexPrice, nextFundingTime."),
    _spec(key="oi_now", module="derivatives", source="Binance open interest",
          url="https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
          cadence="realtime", max_lag_min=30, verified_utc=VERIFIED, notes="HTTP 200."),
    _spec(key="oi_hist", module="binance_archive", source="data.binance.vision metrics",
          url="https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/"
              "BTCUSDT-metrics-<YYYY-MM-DD>.zip",
          cadence="daily file", max_lag_min=2880,
          history_from="2020-09-01 (verified 200, 12,191 B)", verified_utc=VERIFIED,
          notes="The ONLY deep OI history that is free: the fapi futures/data/* endpoints "
                "retain ~30 days and reject an older startTime with -1130. Rows are "
                "IRREGULARLY SPACED AND UNSORTED — sort, dedupe, resample; never index by "
                "position. Near-zero OI values produce inf on pct_change; clip them."),

    # ---- venue and calendar hazards (runs/features/venue.py, macro_calendar.py, B3) ---
    _spec(key="symbol_status", module="venue", source="Binance spot exchangeInfo",
          url='https://api.binance.com/api/v3/exchangeInfo?symbols=["BTCUSDT","ETHUSDT"]',
          cadence="per scanner cycle", max_lag_min=30,
          rate_limit="REQUEST_WEIGHT 6000/min (the response's own rateLimits block)",
          verified_utc=VERIFIED,
          notes="HTTP 200, both TRADING. The only venue source documented and stable "
                "enough to BLOCK an order."),
    _spec(key="usdt_usd_mid", module="venue", source="Coinbase Exchange ticker",
          url="https://api.exchange.coinbase.com/products/USDT-USD/ticker",
          cadence="per scanner cycle", max_lag_min=60, verified_utc=VERIFIED,
          notes="HTTP 200. Every cross-venue price must be divided by this first or it "
                "just measures the Tether basis."),
    _spec(key="fomc_dates", module="macro_calendar", source="Federal Reserve calendar",
          url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
          cadence="weekly", max_lag_min=20160, verified_utc=VERIFIED,
          notes="HTTP 200, 165 KB. 47 statement dates parse from monetary(\\d{8})a.htm. "
                "The /json/ne-fomccalendar.json path is a 404 that returns an HTML body."),
    _spec(key="cpi_release_audit", module="macro_calendar", source="BLS public API v1",
          url="https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0",
          cadence="monthly", max_lag_min=44640, verified_utc=VERIFIED,
          notes="HTTP 200, KEYLESS. Post-hoc release detection only (`latest:true` flips "
                "at release) — enough to AUDIT a hand-maintained calendar, not to build "
                "one. Every www.bls.gov path returns 403 from this host."),
])


#: Looked for, verified unavailable free from this host, and therefore barred.
#:
#: The point of writing a paywall down is that a model asked for an unavailable number will
#: otherwise supply a plausible one. Each entry is the *verified failure*, not a guess.
NOT_AVAILABLE_FREE: dict[str, str] = {
    "us_spot_etf_flows":
        "farside.co.uk 403; DefiLlama /etfs 404; CoinGlass and SoSoValue key-gated; Yahoo "
        "quoteSummary 'Invalid Crumb'. No free source. A model must never assert a flow "
        "number.",
    "historical_liquidations":
        "fapi/v1/allForceOrders 404; forceOrders 401 (account-scoped); "
        "data.binance.vision liquidationSnapshot 404; CoinGlass paid. The public "
        "!forceOrder@arr websocket is free but FORWARD-ONLY and unbacktestable, so "
        "cascades must be inferred from OI + price.",
    "token_unlocks":
        "api.llama.fi/emissions 402. Irrelevant here regardless: BTC has no unlock "
        "schedule and ETH's is not a cliff.",
    "coin_days_destroyed_lth_supply":
        "Glassnode / CryptoQuant only. Self-indexing a UTXO set is out of proportion to a "
        "weak weeks-horizon input. Explicitly declined.",
    "realtime_sopr_nupl":
        "bitcoin-data.com withholds the last 7 days behind a subscription. A 7-day-old "
        "profitability reading cannot inform a 4h or 1d decision — barred from the decide "
        "feature set rather than allowed to look current.",
    "exchange_balance_history":
        "Blockscout coin-balance-history-by-day returns exactly 10 days. On-chain flow "
        "features cannot be backtested and may only ship observe-only.",
    "cme_futures_data":
        "CME's own data is IP-blocked from this host. Irrelevant anyway: the CME gap-fill "
        "effect collapses against the right control — gaps >0.5% fill within 7 days 68.4% "
        "of the time against 61.9% for random midweek moves of the same size.",
    "bridge_volumes":
        "HTTP 402 from the vendor endpoint. No free alternative found, and no measured "
        "link to a BTC/ETH decision at a 4h-1d horizon.",
    "beaconchain_staking_queue":
        "HTTP 401 — beaconcha.in requires signup for the queue endpoints. Etherscan V1 is "
        "deprecated on the same data.",
}


def spec_for(key: str) -> FeatureSpec:
    """The declared provenance of one feature key.

    Raises ``KeyError`` on an unregistered key on purpose: an unregistered feature has no
    audited source, and the whole point of this module is that such a value never reaches
    a prompt.
    """
    return REGISTRY[key]


def is_stale(key: str, as_of_utc: str | datetime | None,
             now: datetime | None = None) -> bool:
    """True when this key's value is older than its declared ``max_lag_min``."""
    if as_of_utc is None:
        return True
    lag = staleness_minutes(as_of_utc, now)
    return lag is None or lag > spec_for(key).max_lag_min


# --------------------------------------------------------------------------- loaders


def load_candles(pair: str, timeframe: str, *, root: Path | None = None,
                 as_of_utc: datetime | str | None = None) -> pd.DataFrame:
    """Read the feather candle store into a UTC-indexed frame.

    Columns: ``date, open, high, low, close, volume`` with ``date`` tz-aware UTC, sorted
    and deduplicated. ``as_of_utc`` truncates the frame to bars whose **close** is at or
    before that instant, which is the no-look-ahead contract every caller relies on.
    """
    path = candle_path(pair, timeframe, root)
    if not path.exists():
        raise FileNotFoundError(f"no candles at {path}")
    df = pd.read_feather(path)
    if "date" not in df.columns:
        raise ValueError(f"{path}: expected a 'date' column, got {list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    if as_of_utc is not None:
        df = as_of(df, as_of_utc, column="date", closed_offset=_tf_delta(timeframe))
    return df


def _tf_delta(timeframe: str) -> timedelta:
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    unit = timeframe[-1].lower()
    if unit not in units:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    return timedelta(**{units[unit]: int(timeframe[:-1])})


def as_of(df: pd.DataFrame, when: datetime | str, *, column: str = "date",
          closed_offset: timedelta | None = None) -> pd.DataFrame:
    """Truncate ``df`` to rows knowable at ``when``.

    ``closed_offset`` is the bar length: a candle stamped at its OPEN is only knowable one
    bar later, so the cut is ``open_time + offset <= when``. Without an offset the stamp
    is taken as the observation time. This one function is why every feature in this
    package can be recomputed over history without leaking the future into it.
    """
    stamp = parse_iso(when) if isinstance(when, str) else when.astimezone(UTC)
    ts = pd.to_datetime(df[column], utc=True)
    cutoff = pd.Timestamp(stamp)
    if closed_offset is not None:
        return df.loc[ts + closed_offset <= cutoff].reset_index(drop=True)
    return df.loc[ts <= cutoff].reset_index(drop=True)


# --------------------------------------------------------------------------- json io


def write_json_atomic(path: Path, payload: dict) -> Path:
    """Write ``payload`` to ``path`` via a temp file + rename.

    Centralised here so a skill script never has to name a write destination itself — the
    skill lint reads those literals, and one helper is easier to audit than five scripts.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path: Path, default: dict | None = None) -> dict:
    """Read JSON, returning ``default`` on a missing or unparseable file."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


@dataclass
class RegistryReport:
    """What :func:`describe_registry` returns — handy for a skill that prints provenance."""

    keys: list[str] = field(default_factory=list)
    modules: dict[str, int] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)


def describe_registry() -> RegistryReport:
    modules: dict[str, int] = {}
    for spec in REGISTRY.values():
        modules[spec.module] = modules.get(spec.module, 0) + 1
    return RegistryReport(keys=sorted(REGISTRY), modules=dict(sorted(modules.items())),
                          unavailable=sorted(NOT_AVAILABLE_FREE))
