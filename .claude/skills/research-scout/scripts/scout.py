"""The search surface and the standing rejection ledger — deterministic, no model.

TIER 2. A human writes this file. It is the *authority* for what Earn has already tried
and killed, and it lives in ``scripts/`` on purpose: a search process that can edit its own
list of things-not-to-search is not constrained by it. The prose explanation lives in
``../references/rejected-ledger.md`` (tier 1) and a test asserts the two agree.

    python3 scout.py ledger
    python3 scout.py check --idea "does exchange netflow predict 7d returns"
    python3 scout.py surfaces
    python3 scout.py unavailable
    python3 scout.py gate --name deribit-dvol --free yes --keyless yes \
        --history-days 2010 --lag-min 1440 --backtestable yes
    python3 scout.py --self-test

Every entry in :data:`LEDGER` carries the measurement that killed the idea, not an opinion
about it. Sources: ``docs/design/crypto-research.md`` §2 and §6, ``docs/design/wide-universe.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The four gates a data source must clear before it is worth a trial. Defaults are floors,
#: not targets — see ../references/source-test.md for the verified failure behind each.
MIN_HISTORY_DAYS = 730
MAX_LAG_MIN = 1440

REJECTED = "rejected"
OBSERVE_ONLY = "observe_only"
OPEN = "open"


@dataclass(frozen=True)
class Rejection:
    """One idea this repo tested and will not test again."""

    id: str
    title: str
    verdict: str
    reason: str
    evidence: str
    keywords: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "verdict": self.verdict,
                "reason": self.reason, "evidence": self.evidence,
                "keywords": list(self.keywords)}


def _r(**kw) -> tuple[str, Rejection]:
    rej = Rejection(**kw)
    return rej.id, rej


#: The standing ledger. An entry is added only by a human, only with the measurement that
#: killed it, and is never removed by an automated run.
LEDGER: dict[str, Rejection] = dict([
    _r(id="onchain-flows",
       title="On-chain exchange flows / whale deposits as a return or risk input",
       verdict=REJECTED,
       reason="Cannot be backtested from free data, so it cannot clear this project's own "
              "bar. Blockscout's coin-balance-history-by-day returns exactly 10 days and "
              "mempool.space gives cumulative sums that must be accumulated forward. The "
              "strongest published result is that BTC net inflows LACK return "
              "predictability except at 4h. Observe-only at best; never a decision input.",
       evidence="crypto-research.md §6 and the NOT_AVAILABLE_FREE entry "
                "exchange_balance_history",
       keywords=("exchange flow", "netflow", "net flow", "whale deposit", "whale wallet",
                 "exchange inflow", "exchange outflow", "on-chain flow", "onchain flow",
                 "coin days destroyed", "cdd", "lth supply", "sopr", "nupl", "mvrv",
                 "realized cap", "glassnode", "cryptoquant")),
    _r(id="hash-ribbons",
       title="Miner state / hash ribbons as a cycle or capitulation signal",
       verdict=REJECTED,
       reason="Graded folklore by the researcher who proposed it: a handful of "
              "non-independent events each rationalised after the fact, and post-2024 "
              "miners hedge production rather than force-sell it. Effective sample size is "
              "the number of cycles, which is two or three, not the number of days.",
       evidence="crypto-research.md §6 (miner-state)",
       keywords=("hash ribbon", "hashribbon", "miner capitulation", "hashrate", "hash rate",
                 "miner state", "difficulty ribbon", "miner selling")),
    _r(id="etf-flow-proxy",
       title="ETF-flow proxies (US-hours vs Asia-hours spread, session flow)",
       verdict=REJECTED,
       reason="A proxy for a number nobody here can see, with no measured relationship to "
              "the number it proxies. US spot ETF flows are not obtainable free from this "
              "host (farside 403, DefiLlama /etfs 404, CoinGlass and SoSoValue key-gated, "
              "Yahoo quoteSummary 'Invalid Crumb'), so the proxy can never be validated "
              "against its target. Free and backtestable is necessary, not sufficient.",
       evidence="crypto-research.md §6 (session-flow-proxy) and the NOT_AVAILABLE_FREE "
                "entry us_spot_etf_flows",
       keywords=("etf flow", "etf inflow", "etf outflow", "spot etf", "session flow",
                 "us hours", "asia hours", "session spread", "farside")),
    _r(id="unlock-calendar",
       title="Token unlock / emission calendars",
       verdict=REJECTED,
       reason="api.llama.fi/emissions returns HTTP 402, and it is irrelevant to this "
              "universe regardless: BTC has no unlock schedule and ETH's emission is not a "
              "cliff. Would be one of the more reliable effects if the universe ever "
              "widened to venture-backed tokens with vesting.",
       evidence="crypto-research.md §6 (unlock-calendar) and NOT_AVAILABLE_FREE "
                "token_unlocks",
       keywords=("unlock", "unlocks", "vesting", "emission", "emissions", "token supply "
                 "schedule", "cliff unlock")),
    _r(id="execution-timing",
       title="Execution-timing, entry-timing and cost-truth optimisation at our size",
       verdict=REJECTED,
       reason="Measured twice on the live 1000-level book: slippage versus mid is 0.001 bps "
              "on BTCUSDT and 0.019 bps on ETHUSDT at every size Earn will ever trade, "
              "against a 10 bps fee. Maker equals taker at Binance spot VIP0 and Earn will "
              "never leave VIP0 (VIP1 needs $1M/30d; the cap implies about $15k/month, 67x "
              "short). Optimising when to execute optimises under 1% of the cost line. The "
              "only real levers are the BNB discount and the number of orders, both already "
              "bounded and already measured by the tca skill.",
       evidence="crypto-research.md §1.10 and §6 (entry-timing / cost-truth)",
       keywords=("execution timing", "entry timing", "slippage optimisation",
                 "slippage optimization", "limit maker", "maker rebate", "passive fill",
                 "twap", "vwap execution", "order slicing", "reprice", "cost truth")),
    _r(id="oi-deleveraging-buy",
       title="Open-interest deleveraging as a BUY signal",
       verdict=REJECTED,
       reason="Worked and then died, which is worse than never working. OI 24h < -5%: "
              "H1 2023-09 to 2025-03 gave +1.83% with a 67% hit rate against a 55% base; "
              "H2 2025-03 to 2026-09 gave -0.18% with a 46.5% hit rate against a 51.4% "
              "base. The linear t went from -3.66 to -0.04. A full-sample backtest shows "
              "+1.16% and ships a corpse. Only the DRAWDOWN leg of the OI story survived "
              "both halves, and that is what leverage-state already uses.",
       evidence="crypto-research.md §2, first row",
       keywords=("oi deleveraging", "open interest drop", "deleveraging buy", "oi flush",
                 "long squeeze buy", "open interest as a buy")),
    _r(id="funding-as-direction",
       title="High funding means overheated, therefore sell",
       verdict=REJECTED,
       reason="Backwards on the measured table. Across 2,561 days the >=40% annualised "
              "funding bucket has the SECOND-HIGHEST median 7-day return (+1.41%) while the "
              "negative-funding bucket has the highest (+1.50%). Funding predicts the tail, "
              "not the sign: P(7d drawdown < -8%) rises monotonically 17.0% -> 41.4% across "
              "the buckets, and that ratio strengthened across halves (1.30 -> 1.46). Use "
              "funding to size down, never to choose a direction.",
       evidence="crypto-research.md §1.2 and §2",
       keywords=("funding direction", "high funding", "funding is high", "overheated",
                 "funding signal", "funding rate predicts return",
                 "short when funding", "sell when funding")),
    _r(id="taker-imbalance",
       title="Taker imbalance / CVD as alpha",
       verdict=REJECTED,
       reason="Two independent tests, both null. Newey-West(18) of forward 72h return on "
              "standardised taker ratio over 6,523 4h bars: t = +0.11, and +0.07 after "
              "controls. The 8h quintile sort looks monotone (+0.20% -> +0.58%) and that is "
              "exactly how people get fooled: contemporaneous correlation with the same "
              "bar's return is 0.414, so the sort re-sorts on past returns. CVD is a chart "
              "of where price has been.",
       evidence="crypto-research.md §2",
       keywords=("taker imbalance", "cvd", "cumulative volume delta", "buy sell pressure",
                 "aggressor ratio", "taker ratio")),
    _r(id="book-imbalance-slow",
       title="Order-book imbalance at 4h and slower",
       verdict=REJECTED,
       reason="t = -1.45 / -1.47 / -0.04 at 4h / 24h / 72h, with the sign backwards from "
              "folklore. Order-flow imbalance's documented horizon is tens of seconds; it "
              "is a market-making signal with no business in a system that decides at 4h. "
              "Book DEPTH as a volatility input is a different claim and it survived "
              "(t = -7.50 on forward realised vol).",
       evidence="crypto-research.md §2 and §1.4",
       keywords=("order book imbalance", "book imbalance", "bid ask imbalance",
                 "order flow imbalance", "ofi", "queue imbalance")),
    _r(id="cme-gap",
       title="CME gap fill",
       verdict=REJECTED,
       reason="The strongest folklore in crypto and it collapses against the right control. "
              "475 weekends: gaps >0.5% filled within 7 days 68.4% of the time — but random "
              "midweek 25-hour moves of the same size retraced 61.9% of the time. The whole "
              "phenomenon is 6.5pp of ordinary mean reversion in a costume. CME's own data "
              "is IP-blocked from this host anyway.",
       evidence="crypto-research.md §2 and NOT_AVAILABLE_FREE cme_futures_data",
       keywords=("cme gap", "weekend gap", "gap fill", "futures gap")),
    _r(id="funding-clock",
       title="The 8-hour funding settlement clock as a timing effect",
       verdict=REJECTED,
       reason="61,658 hourly bars: settlement hours returned +0.87 bps against +0.51 bps "
              "otherwise, t = 0.44. No clock effect at all.",
       evidence="crypto-research.md §2",
       keywords=("funding settlement", "settlement hour", "funding clock", "8h clock",
                 "funding time of day")),
    _r(id="weekend-effect",
       title="The weekend effect, and the claim that weekend books are thin",
       verdict=REJECTED,
       reason="Weekday +0.74 bps/h against weekend +0.50 bps/h over 79,658 bars — no "
              "tradable asymmetry. The volume ratio is genuinely lower (0.65x) and decaying "
              "fast (0.98 in 2017 to 0.52 in 2024-26), but the common claim that weekend "
              "books are thin is FALSE: the weekend/weekday resting depth ratio is 1.021. "
              "Quiet, not thin.",
       evidence="crypto-research.md §2",
       keywords=("weekend effect", "weekend return", "saturday", "sunday", "weekend "
                 "liquidity", "thin weekend")),
    _r(id="halving-cycle",
       title="The halving cycle as a timing or allocation input",
       verdict=REJECTED,
       reason="n = 2 observable halvings and they disagree at every horizon: +365d was "
              "+562% in 2020 and +31% in 2024; -90d was +19.4% against -36.0%. The "
              "days-since-halving bucket table looks compelling and is an artefact — the "
              "bins are two contiguous runs, so the effective n is 2, not 180.",
       evidence="crypto-research.md §2",
       keywords=("halving", "halvening", "four year cycle", "4 year cycle", "cycle top",
                 "days since halving", "stock to flow", "s2f")),
    _r(id="cross-sectional-momentum",
       title="Cross-sectional momentum across the core pairs",
       verdict=REJECTED,
       reason="Sharpe 1.04 / 0.85 / 0.55 for 20 / 60 / 120-day lookbacks, and every variant "
              "has a worse max drawdown (-77% to -89%) than simply holding BTC. Parameter "
              "instability of that size across a routine choice is the definition of an "
              "artefact, and on a two-name core it is a coin flip with two outcomes.",
       evidence="crypto-research.md §2 and §6",
       keywords=("cross sectional momentum", "cross-sectional", "relative momentum",
                 "rotate into the winner", "rank momentum")),
    _r(id="coinbase-premium",
       title="Coinbase premium / cross-exchange dispersion as a multi-hour signal",
       verdict=REJECTED,
       reason="Mean spread -5.3 bps, sd 6.0, p95 of the absolute value 15.5 bps — inside a "
              "single taker fee. And most of what is left is the USDT basis, not a "
              "dislocation: simultaneous reads showed a ~150 USD Binance/Coinbase gap that "
              "WAS the Tether basis, with the two USD venues agreeing to within $0.80. Any "
              "cross-venue number must be divided by a live USDT/USD rate first or it just "
              "measures Tether.",
       evidence="crypto-research.md §1.6 and §2",
       keywords=("coinbase premium", "kimchi premium", "cross exchange", "cross-venue "
                 "spread", "venue dispersion", "arbitrage spread")),
    _r(id="exploit-count",
       title="Exploit or hack COUNT as a risk input",
       verdict=REJECTED,
       reason="The free hacks dataset (1,283 events, verified) has recent entries of $4.9M, "
              "$4.4M, $35k and $16k. Counting events treats a $16k rug as a bridge failure. "
              "Only size relative to the contagion surface could matter, and that threshold "
              "must be calibrated before anything is wired — otherwise every exploit burns "
              "a validator slot under the existing fast-path rule.",
       evidence="crypto-research.md §2",
       keywords=("exploit count", "hack count", "number of hacks", "defi exploits",
                 "rekt count")),
    _r(id="meta-labelling",
       title="Meta-labelling a primary signal with an ML filter",
       verdict=REJECTED,
       reason="Run honestly it made things worse. Primary = MA100 x volTarget long days, "
              "six standard features, ridge, expanding purged walk-forward with a 5-day "
              "embargo, 851 OOS days: accuracy 0.496 against a 0.503 base rate — literally "
              "worse than always saying yes — and as a filter it cut Sharpe 1.16 to 0.80 "
              "and CAGR 53.5% to 23.1%. The binding constraint is effective sample size "
              "(about 3,020 independent observations), not algorithm choice, so no new "
              "model fixes it.",
       evidence="crypto-research.md §3",
       keywords=("meta label", "meta-labelling", "meta labeling", "secondary model",
                 "ml filter", "ensemble filter", "triple barrier classifier")),
    _r(id="hmm-regime",
       title="HMM or changepoint regime detection",
       verdict=REJECTED,
       reason="A 2-state Gaussian HMM fitted on the FULL sample — deliberate look-ahead, "
              "the most flattering possible test — scored Sharpe 0.78, below the "
              "no-look-ahead MA200 x volTarget at 0.87 and far below MA100 x volTarget at "
              "1.14. The trend/vol quadrant does the same job with two comparisons and no "
              "fitting.",
       evidence="crypto-research.md §3",
       keywords=("hmm", "hidden markov", "changepoint", "change point", "regime "
                 "classifier", "clustering regimes", "gaussian mixture")),
    _r(id="deep-learning-ohlcv",
       title="Deep learning on OHLCV",
       verdict=REJECTED,
       reason="About 3,020 effective independent observations and maybe three genuine "
              "regimes in nine years. Parameter count over information. The refusal is a "
              "sample-size fact, not a preference about architectures.",
       evidence="crypto-research.md §3",
       keywords=("deep learning", "neural network", "lstm", "transformer", "cnn on price",
                 "reinforcement learning", "rl agent", "autoencoder")),
    _r(id="onchain-valuation",
       title="On-chain valuation (MVRV / NUPL reconstruction) as a decision input",
       verdict=REJECTED,
       reason="Genuinely clever — realized cap IS available undelayed, which routes around "
              "the paywall — but it is a weeks-to-months tilt for a system that decides at "
              "4h/1d, every published threshold is known-overfit across cycles, and no "
              "forward-return evidence at our horizon was ever produced. It does not change "
              "a decision. Recorded as dossier context, not a signal.",
       evidence="crypto-research.md §6 (onchain-valuation)",
       keywords=("mvrv", "nupl", "realized price", "onchain valuation", "on-chain "
                 "valuation", "thermocap", "puell")),
    _r(id="eth-burn",
       title="ETH network state / EIP-1559 burn as a supply story",
       verdict=REJECTED,
       reason="eth_feeHistory reaches back about 1,024 blocks, so any history needs a "
              "per-block backfill, and the 'ultrasound money' framing decayed hard after "
              "EIP-4844 moved L2 data to blobs. It is a demand gauge, not a supply story, "
              "with no measured link to our decisions.",
       evidence="crypto-research.md §6 (eth-network-state)",
       keywords=("eip-1559", "eip1559", "eth burn", "ultrasound money", "gas fees as "
                 "signal", "blob fees", "fee burn")),
    _r(id="historical-liquidations",
       title="Backtesting on historical liquidation data",
       verdict=OBSERVE_ONLY,
       reason="No free historical source exists: allForceOrders 404, forceOrders 401 "
              "(account-scoped), the bulk liquidationSnapshot path 404, CoinGlass now paid. "
              "The public !forceOrder@arr websocket is free but FORWARD-ONLY, so a "
              "liquidation feature can be recorded from today and may not feed a decision "
              "for at least twelve months. Cascades must be INFERRED from open interest "
              "plus price until then.",
       evidence="crypto-research.md §2 and NOT_AVAILABLE_FREE historical_liquidations",
       keywords=("liquidation", "liquidations", "forced selling", "cascade data",
                 "liquidation heatmap", "forceorder")),
    _r(id="live-testing-proves-edge",
       title="Treating a good live quarter as validation of an edge",
       verdict=REJECTED,
       reason="Distinguishing Sharpe 1.14 from 0.83 at 80% power needs about 245 years; "
              "even 2.00 against 0.83 needs about 15. A 90-day test period proves the "
              "PLUMBING works and nothing whatever about edge. Say the number rather than "
              "the sentiment — edge-audit computes it with years_to_detect.",
       evidence="crypto-research.md §3; edge-audit references/method.md",
       keywords=("live results prove", "since we went live", "good quarter", "live "
                 "validation", "forward test proves", "paper trading proves")),
    _r(id="survivorship-universe",
       title="Backtesting a widened universe on today's listed pairs",
       verdict=REJECTED,
       reason="Any widening that picks today's liquid listings selects on coins that "
              "SURVIVED — the delisted ones are not in the klines API at all, so they "
              "cannot lose money in the backtest. A wide-universe study is admissible only "
              "if it assembles delisted pairs too and reports what it could not obtain.",
       evidence="crypto-research.md §3; wide-universe.md",
       keywords=("alt universe", "wide universe", "top 100 coins", "altcoin backtest",
                 "listed pairs", "survivorship")),
])

#: Where this repo can actually look. ``history`` and ``lag`` are the verified facts that
#: decide whether a surface can carry a backtest at all.
SURFACES: tuple[dict, ...] = (
    {"name": "local-candles",
     "what": "OHLCV feathers for the tradeable universe at 1h/4h/1d",
     "where": "data/binance/<PAIR>-<tf>.feather",
     "history": "2017-08 onward, about 1.06M candles",
     "lag": "one bar",
     "backtestable": True,
     "notes": "Deepest and cheapest surface. Point-in-time by construction. Start here."},
    {"name": "wired-features",
     "what": "Deribit DVOL, funding, open interest, basis, book depth, macro calendar",
     "where": "runs/features/*.py (registry in runs/features/__init__.py)",
     "history": "DVOL 2021-03 (BTC) and 2023-12 (ETH); funding 2019-09; bulk metrics 2020-09; "
                "bookDepth 2023-01",
     "lag": "per-key max_lag_min in the registry",
     "backtestable": True,
     "notes": "Already loaded and already trap-handled. Emitting a key that is not in "
              "REGISTRY means emitting a number nobody can trace."},
    {"name": "news-archive",
     "what": "Whitelisted, classified news with the two-source corroboration rule",
     "where": "knowledge/earn.db (news tables), cited by news_hash",
     "history": "from ingest start",
     "lag": "ingest cadence",
     "backtestable": False,
     "notes": "The only text source this system may cite. A claim with no news_hash is "
              "dropped by host verification. Not a price history."},
    {"name": "journal",
     "what": "Every past run, proposal, fill, grade and root cause",
     "where": "journal/journal.db, reports/*",
     "history": "from first run",
     "lag": "one run",
     "backtestable": False,
     "notes": "The right surface for questions about the SYSTEM rather than the market: "
              "what the loop keeps getting wrong is measurable here."},
    {"name": "binance-bulk-archive",
     "what": "data.binance.vision daily/monthly zips: klines, metrics, bookDepth",
     "where": "runs/features/binance_archive.py",
     "history": "metrics 2020-09-01; bookDepth 2023-01-01",
     "lag": "T+1 day",
     "backtestable": True,
     "notes": "The ONLY deep history for the derivative series. Four loader traps: "
              "out-of-order and irregular metrics rows, spot klines switching ms to us at "
              "2025-01, near-zero OI producing inf on pct_change, empty markPrice."},
    {"name": "verified-free-endpoints",
     "what": "Deribit volatility index, Binance fapi fundingRate and premiumIndex, "
             "exchangeInfo, api.bls.gov v1, federalreserve.gov FOMC calendar",
     "where": "docs/design/crypto-research.md §1 (each verified with a live request)",
     "history": "per endpoint; fapi futures/data/* retains ~30 DAYS",
     "lag": "minutes to a day",
     "backtestable": False,
     "notes": "Free and keyless, but check the history depth before building on one: the "
              "futures/data/* family silently gives a one-month sample."},
)


# --------------------------------------------------------------------------- ledger


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _keyword_re(keyword: str) -> re.Pattern[str]:
    """A phrase matcher that tolerates ordinary English inflection.

    ``whale deposit`` must match "whale deposits" and ``unlock`` must match "unlocks", or
    the ledger silently misses the way a run actually phrases things — which is the one
    failure mode that costs a trial. Only a suffix on the LAST word is allowed, so
    ``cme gap`` still does not match "gap" on its own.
    """
    parts = _norm(keyword).split()
    body = r"\s+".join(re.escape(p) for p in parts)
    return re.compile(rf"\b{body}\w*\b")


def check_idea(idea: str, ledger: dict[str, Rejection] | None = None) -> dict:
    """Match one idea against the ledger. Returns the verdict and every hit.

    Matching is deliberately generous: a false hit costs one sentence explaining why this
    idea is not that entry, while a miss costs a trial that raises the deflated hurdle for
    everything after it.
    """
    book = LEDGER if ledger is None else ledger
    hay = _norm(idea)
    hits: list[dict] = []
    for rej in book.values():
        matched = [kw for kw in rej.keywords if _keyword_re(kw).search(hay)]
        if matched:
            hits.append({"id": rej.id, "verdict": rej.verdict, "title": rej.title,
                         "matched": matched, "reason": rej.reason,
                         "evidence": rej.evidence})
    if any(h["verdict"] == REJECTED for h in hits):
        verdict = REJECTED
    elif hits:
        verdict = OBSERVE_ONLY
    else:
        verdict = OPEN
    return {"idea": idea, "verdict": verdict, "hits": hits}


# --------------------------------------------------------------------------- gate


@dataclass
class GateResult:
    name: str
    usable: bool
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "usable": self.usable, "checks": dict(self.checks),
                "reasons": list(self.reasons)}


def gate_source(name: str, *, free: bool, keyless: bool, history_days: int,
                lag_min: int, backtestable: bool,
                min_history_days: int = MIN_HISTORY_DAYS,
                max_lag_min: int = MAX_LAG_MIN) -> GateResult:
    """The four usability gates. All four must pass; any failure is a rejection."""
    checks = {
        "free": bool(free),
        "keyless": bool(keyless),
        "deep_enough": int(history_days) >= int(min_history_days),
        "low_lag": int(lag_min) <= int(max_lag_min),
        "backtestable": bool(backtestable),
    }
    reasons: list[str] = []
    if not checks["free"]:
        reasons.append("paywalled: an automated run cannot re-pull it and a replay cannot "
                       "reproduce it")
    if not checks["keyless"]:
        reasons.append("needs an account key: account-scoped series are out, not caveated")
    if not checks["deep_enough"]:
        reasons.append(f"history {history_days}d is under the {min_history_days}d floor; "
                       "a short sample looks magnificent and proves nothing")
    if not checks["low_lag"]:
        reasons.append(f"lag {lag_min} min exceeds {max_lag_min} min: a value older than "
                       "the decision cadence is a memory, not an input")
    if not checks["backtestable"]:
        reasons.append("not point-in-time: forward-only or vendor-restated series may "
                       "accrue an observe-only record and may not feed a decision")
    return GateResult(name=name, usable=all(checks.values()), checks=checks,
                      reasons=reasons)


# --------------------------------------------------------------------------- registry


def registry_view() -> dict:
    """What the wired feature registry says, when it can be imported."""
    try:
        from runs.features import NOT_AVAILABLE_FREE, REGISTRY
    except Exception as exc:  # pragma: no cover - environment-dependent
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    modules: dict[str, list[str]] = {}
    for spec in REGISTRY.values():
        modules.setdefault(spec.module, []).append(spec.key)
    return {"available": True,
            "keys": sorted(REGISTRY),
            "modules": {m: sorted(v) for m, v in sorted(modules.items())},
            "unavailable": dict(sorted(NOT_AVAILABLE_FREE.items()))}


# --------------------------------------------------------------------------- printing


def print_ledger() -> None:
    print(f"rejection_ledger entries={len(LEDGER)}")
    for rej in LEDGER.values():
        print(f"\n[{rej.verdict.upper()}] {rej.id} — {rej.title}")
        print(f"  reason: {rej.reason}")
        print(f"  evidence: {rej.evidence}")


def print_surfaces() -> None:
    print(f"search_surfaces count={len(SURFACES)}")
    for s in SURFACES:
        print(f"\n{s['name']}  backtestable={str(s['backtestable']).lower()}")
        print(f"  what: {s['what']}")
        print(f"  where: {s['where']}")
        print(f"  history: {s['history']}   lag: {s['lag']}")
        print(f"  notes: {s['notes']}")
    view = registry_view()
    if view.get("available"):
        print(f"\nwired_feature_keys={len(view['keys'])} "
              f"modules={len(view['modules'])}")
    else:
        print(f"\nwired_feature_registry unavailable ({view.get('error')})")


def print_unavailable() -> None:
    view = registry_view()
    if not view.get("available"):
        print(f"registry unavailable ({view.get('error')}); "
              "falling back to the ledger's observe_only entries")
        for rej in LEDGER.values():
            if rej.verdict == OBSERVE_ONLY:
                print(f"\n{rej.id}: {rej.reason}")
        return
    items = view["unavailable"]
    print(f"not_available_free count={len(items)}")
    for key, why in items.items():
        print(f"\n{key}: {why}")
    print("\nA model must never assert a number from any of the above. "
          "'Not obtainable' is the correct answer.")


# --------------------------------------------------------------------------- self-test


def self_test() -> int:
    """Pinned behaviour that needs no candle store, no network and no config."""
    ok = True

    print(f"ledger_entries={len(LEDGER)}")
    ok &= len(LEDGER) >= 20

    required = {"onchain-flows", "hash-ribbons", "etf-flow-proxy", "unlock-calendar",
                "execution-timing"}
    missing = sorted(required - set(LEDGER))
    print(f"standing_rejections_present={not missing} missing={missing}")
    ok &= not missing

    for rej in LEDGER.values():
        if not rej.keywords or len(rej.reason) < 80 or not rej.evidence:
            print(f"FAIL incomplete ledger entry: {rej.id}")
            ok = False
        if rej.verdict not in (REJECTED, OBSERVE_ONLY):
            print(f"FAIL bad verdict on {rej.id}: {rej.verdict}")
            ok = False

    dead = check_idea("does exchange netflow of whale deposits predict 7d returns")
    print(f"check('exchange netflow') verdict={dead['verdict']} "
          f"hits={[h['id'] for h in dead['hits']]}")
    ok &= dead["verdict"] == REJECTED

    timing = check_idea("can we save cost with a limit maker entry instead of market")
    print(f"check('limit maker') verdict={timing['verdict']}")
    ok &= timing["verdict"] == REJECTED

    liq = check_idea("record the liquidation stream and use it later")
    print(f"check('liquidation stream') verdict={liq['verdict']}")
    ok &= liq["verdict"] == OBSERVE_ONLY

    fresh = check_idea("does the dispersion of 1d realised vol across the core pairs "
                       "forecast the next week's drawdown")
    print(f"check('vol dispersion') verdict={fresh['verdict']}")
    ok &= fresh["verdict"] == OPEN

    good = gate_source("deribit-dvol", free=True, keyless=True, history_days=2010,
                       lag_min=1440, backtestable=True)
    print(f"gate(deribit-dvol) usable={str(good.usable).lower()}")
    ok &= good.usable

    shallow = gate_source("fapi-futures-data", free=True, keyless=True, history_days=31,
                          lag_min=5, backtestable=True)
    print(f"gate(fapi-futures-data) usable={str(shallow.usable).lower()} "
          f"deep_enough={str(shallow.checks['deep_enough']).lower()}")
    ok &= (not shallow.usable) and not shallow.checks["deep_enough"]

    paywalled = gate_source("etf-flows-vendor", free=False, keyless=False,
                            history_days=3000, lag_min=60, backtestable=True)
    print(f"gate(etf-flows-vendor) usable={str(paywalled.usable).lower()} "
          f"reasons={len(paywalled.reasons)}")
    ok &= (not paywalled.usable) and len(paywalled.reasons) == 2

    stale = gate_source("sopr-delayed", free=True, keyless=True, history_days=3000,
                        lag_min=7 * 24 * 60, backtestable=True)
    print(f"gate(sopr-delayed) low_lag={str(stale.checks['low_lag']).lower()}")
    ok &= not stale.checks["low_lag"]

    forward = gate_source("forceorder-stream", free=True, keyless=True, history_days=0,
                          lag_min=1, backtestable=False)
    print(f"gate(forceorder-stream) backtestable="
          f"{str(forward.checks['backtestable']).lower()}")
    ok &= not forward.checks["backtestable"]

    print("self_test: PASS" if ok else "self_test: FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- cli


def _yn(value: str) -> bool:
    return str(value).strip().lower() in ("y", "yes", "true", "1")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Earn research search surface and the "
                                             "standing rejection ledger.")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("ledger", help="print every standing rejection with its measurement")
    sub.add_parser("surfaces", help="where this repo can actually look")
    sub.add_parser("unavailable", help="what is not obtainable free from this host")

    chk = sub.add_parser("check", help="has this idea already been killed?")
    chk.add_argument("--idea", required=True)

    gt = sub.add_parser("gate", help="does a source clear the four usability gates?")
    gt.add_argument("--name", required=True)
    gt.add_argument("--free", default="yes")
    gt.add_argument("--keyless", default="yes")
    gt.add_argument("--history-days", type=int, default=0)
    gt.add_argument("--lag-min", type=int, default=0)
    gt.add_argument("--backtestable", default="yes")
    gt.add_argument("--min-history-days", type=int, default=MIN_HISTORY_DAYS)
    gt.add_argument("--max-lag-min", type=int, default=MAX_LAG_MIN)

    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if args.cmd == "ledger":
        if args.json:
            print(json.dumps({k: v.as_dict() for k, v in LEDGER.items()}, indent=2))
        else:
            print_ledger()
        return 0

    if args.cmd == "surfaces":
        if args.json:
            print(json.dumps({"surfaces": list(SURFACES),
                              "registry": registry_view()}, indent=2))
        else:
            print_surfaces()
        return 0

    if args.cmd == "unavailable":
        if args.json:
            print(json.dumps(registry_view(), indent=2))
        else:
            print_unavailable()
        return 0

    if args.cmd == "check":
        res = check_idea(args.idea)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        print(f"idea: {res['idea']}")
        print(f"verdict={res['verdict']}")
        for hit in res["hits"]:
            print(f"\n[{hit['verdict']}] {hit['id']} — {hit['title']}")
            print(f"  matched: {', '.join(hit['matched'])}")
            print(f"  reason: {hit['reason']}")
            print(f"  evidence: {hit['evidence']}")
        if res["verdict"] == OPEN:
            print("\nNot in the ledger. That is not a result — it is permission to state a "
                  "falsifiable hypothesis. Hand it to hypothesis-lab.")
        elif res["verdict"] == REJECTED:
            print("\nSTOP. Quote the reason above and drop the idea. Re-testing it spends a "
                  "trial and raises the deflated hurdle for every honest idea after it.")
        return 0

    if args.cmd == "gate":
        res = gate_source(args.name, free=_yn(args.free), keyless=_yn(args.keyless),
                          history_days=args.history_days, lag_min=args.lag_min,
                          backtestable=_yn(args.backtestable),
                          min_history_days=args.min_history_days,
                          max_lag_min=args.max_lag_min)
        if args.json:
            print(json.dumps(res.as_dict(), indent=2))
            return 0
        print(f"source: {res.name}")
        for key, val in res.checks.items():
            print(f"  {key}={str(val).lower()}")
        print(f"usable={str(res.usable).lower()}")
        for reason in res.reasons:
            print(f"  - {reason}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
