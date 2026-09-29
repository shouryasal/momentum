"""Pure trading-mechanics math. TIER 2 — human-only. STDLIB ONLY.

Everything freqtrade's callbacks need to decide *what* to do lives here as a pure
function of (config, numbers). `strategies/earn_base.py` is the only adapter: it
translates freqtrade objects into these arguments and turns the answers back into
callback return values. That split is what makes the mechanics unit-testable without
freqtrade, pandas or a broker.

Vocabulary (spec section 9):

* "from open"    — a ratio measured against ``trade.open_rate`` (what a profit is).
* "from current" — a ratio measured against the current rate; ``custom_stoploss``
                   returns this, hence :func:`stoploss_from_open`.
* a *rung*       — one entry of ``trading.take_profit.ladder``, fired at most once.
* an *add*       — a positive ``adjust_trade_position`` stake (DCA or pyramid); it is
                   always routed through the gate by the caller, never here.

Invariant: no function here reads a file, the clock, the network or any global. Every
limit arrives as an argument, so "every limit comes from config" is checkable by
reading the call sites in ``earn_base.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

_EPS = 1e-12

# One action per trade per candle, highest priority first (spec section 9).
ACTION_PRIORITY: tuple[str, ...] = (
    "flatten",       # a risk stop is in force: get out
    "risk_exit",     # custom_exit reason (target_zero, ...)
    "stoploss",      # fixed / trailing / ATR stop
    "take_profit",   # partial TP ladder rung
    "add",           # DCA or pyramid
    "rebalance",     # sleeve drift back toward target weight, including SleeveA's trim
)
# Priority is not classification. SleeveA's trim is offered in the ``rebalance`` slot — so a
# flatten, a stop or a take-profit rung on the same candle still beats it — while
# :func:`trim_reason` decides separately whether the risk GATE may refuse it. An exit no fee
# budget may silence can still be the last candidate offered in a candle.

# Exit reasons that are risk reductions: never blocked by a churn / turnover / fee check.
RISK_EXIT_REASONS: frozenset[str] = frozenset({
    "risk_stop_daily", "risk_stop_monthly", "target_zero", "stop_loss",
    "trailing_stop_loss", "stoploss_on_exchange", "emergency_exit", "force_exit",
    "liquidation", "kill", "flatten",
})

RISK_EXIT_PREFIXES: tuple[str, ...] = ("risk_stop", "stop_loss", "trailing_stop", "emergency")


def is_risk_exit(reason: str | None) -> bool:
    """True when ``reason`` is a risk reduction the gate must never block."""
    if not reason:
        return False
    r = str(reason).strip()
    return r in RISK_EXIT_REASONS or r.startswith(RISK_EXIT_PREFIXES)


# --------------------------------------------------------------------------- trim reason

#: A trim taken because the position DRIFTED outside the rebalance band while the book was
#: still inside every shipped limit. Routine housekeeping, so it stays a *discretionary*
#: exit: ``riskgate.check_discretionary_exit`` applies the orders-per-day, turnover and
#: monthly fee-budget checks and may refuse it, and it is priced on the maker side like any
#: other unhurried sell.
TRIM_DRIFT = "rebalance_trim"

#: A trim taken because the book is ALREADY outside one of its own exposure limits. This
#: one may not be silenceable: :func:`is_risk_exit` matches it on the ``risk_stop`` prefix
#: (``crisis-policy.md`` G7 item 8 asks for exactly that prefix on any de-risk), so the
#: churn and fee-budget checks wave it through, it crosses the spread, and it does not
#: consume the day's discretionary order count. A de-risk a fee budget can silence is not a
#: de-risk. It is NOT added to :data:`RISK_EXIT_REASONS`: the prefix is the mechanism, and
#: the two names in that set that share it (``risk_stop_daily`` / ``risk_stop_monthly``) are
#: sleeve-level flattens that ``riskgate.flatten_pending`` matches EXACTLY, so this reason
#: can never be mistaken for one.
TRIM_BREACH = "risk_stop_exposure"


def trim_reason(*, position_value: float, gross: float, free_usdt: float, nav: float,
                weight_cap: float, gross_cap: float, usdt_floor: float) -> str:
    """:data:`TRIM_BREACH` when the book is already outside a limit, else :data:`TRIM_DRIFT`.

    The three limits are the *sizing* checks ``riskgate`` applies to an incoming order, and
    all three have the shape ``(position + stake) / nav <= cap``. They refuse ORDERS, so a
    position that grows past its cap because the price moved raises no order and the gate
    never sees it: over 3,326 days the never-trimming sleeve sat above its BTC cap on 422 of
    them and above the gross ceiling on 136 (``docs/design/exit-and-horizon-2026-09-29.md``
    §2). This asks the same arithmetic of the position the book is actually carrying, which
    is what decides whether a trim is housekeeping or the only thing standing between the
    book and a breach it cannot otherwise clear.

    Every comparison is made against the book BEFORE the trim, because that is the state the
    classification is about. A trim is never classified by its size.
    """
    n = max(float(nav), _EPS)
    if float(position_value) / n > float(weight_cap) + _EPS:
        return TRIM_BREACH
    if float(gross) / n > float(gross_cap) + _EPS:
        return TRIM_BREACH
    if float(free_usdt) / n < float(usdt_floor) - _EPS:
        return TRIM_BREACH
    return TRIM_DRIFT


# --------------------------------------------------------------------------- merging


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Recursive dict merge; ``override`` wins. Neither argument is mutated."""
    out = dict(base)
    for key, value in (override or {}).items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = deep_merge(current, value)
        else:
            out[key] = value
    return out


def get_path(data: Any, path: str, default: Any = None) -> Any:
    """``get_path(cfg, 'stoploss.trailing.distance_pct')`` — missing keys give ``default``."""
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# --------------------------------------------------------------------------- order side

#: freqtrade uses TWO different "side" vocabularies and they are not interchangeable:
#:
#: * ``LongShort`` — the POSITION side, ``'long'`` | ``'short'``. This is what
#:   ``confirm_trade_entry``, ``custom_stake_amount`` and ``custom_entry_price`` are
#:   handed (``freqtradebot.execute_entry`` passes ``side=trade_side``), and what
#:   ``trade.trade_direction`` returns.
#: * ``BuySell`` — the ORDER side actually sent to the exchange, ``'buy'`` | ``'sell'``.
#:   This is ``order.ft_order_side``, ``trade.entry_side`` and ``trade.exit_side``.
#:
#: The journal stores the ORDER side everywhere (``gate_decisions.side``,
#: ``orders.side``, ``fills.side`` all CHECK ``IN ('buy','sell')``) so that a gate
#: decision, the order it produced and its fills can be compared column-for-column and
#: joined against the exchange. :func:`order_side` is the ONE place the position side is
#: converted; forwarding freqtrade's ``side`` argument straight into the column is what
#: made every entry row fail its CHECK and the audit trail silently disappear.
POSITION_SIDES = ("long", "short")
ORDER_SIDES = ("buy", "sell")
_ENTRY_ORDER_SIDE = {"long": "buy", "short": "sell"}
_EXIT_ORDER_SIDE = {"long": "sell", "short": "buy"}


def order_side(side: str | None, *, is_entry: bool) -> str | None:
    """Exchange order side (``buy``/``sell``) for a freqtrade ``side`` argument.

    Accepts either vocabulary: a position side (``long``/``short``) is converted for the
    given direction, an order side is passed through unchanged (spot long entries are
    already ``buy``). Anything else — including ``None`` — returns ``None`` rather than a
    guessed value, so the journal stores NULL and the caller raises an incident instead of
    writing a row that is wrong or a row that never lands at all.
    """
    s = str(side or "").strip().lower()
    if s in ORDER_SIDES:
        return s
    table = _ENTRY_ORDER_SIDE if is_entry else _EXIT_ORDER_SIDE
    return table.get(s)


# --------------------------------------------------------------------------- pricing


def resolve_price_side(side: str, *, is_entry: bool) -> str:
    """Map a configured price side onto the book side to read.

    ``bid``/``ask`` are literal. ``same`` is the maker side for this direction (entry
    joins the bid, exit joins the ask); ``other`` crosses the spread.
    """
    s = (side or "same").lower()
    if s in ("bid", "ask"):
        return s
    maker, taker = ("bid", "ask") if is_entry else ("ask", "bid")
    return maker if s == "same" else taker


def apply_offset_bps(price: float, offset_bps: float) -> float:
    """Signed basis-point offset. Never returns a non-positive price."""
    out = float(price) * (1.0 + float(offset_bps or 0.0) / 10_000.0)
    return out if out > 0 else float(price)


def limit_price(price_cfg: dict[str, Any] | None, bid: float, ask: float, *,
                is_entry: bool, fallback: float) -> float:
    """The limit price for an entry/exit from ``trading.*_price`` and the top of book."""
    if not price_cfg:
        return float(fallback)
    side = resolve_price_side(str(price_cfg.get("side", "same")), is_entry=is_entry)
    raw = bid if side == "bid" else ask
    if not raw or raw <= 0:
        return float(fallback)
    return apply_offset_bps(float(raw), float(price_cfg.get("offset_bps", 0.0)))


# --------------------------------------------------------------------------- stops


def stoploss_from_open(stop_from_open: float, current_profit: float) -> float:
    """Convert a stop expressed as profit-from-open into freqtrade's from-current ratio.

    Mirrors ``freqtrade.strategy.stoploss_from_open`` with stdlib arithmetic so the
    gate's stop math is testable without freqtrade installed.
    """
    denom = 1.0 + float(current_profit)
    if denom <= _EPS:
        return -1.0
    ratio = 1.0 - (1.0 + float(stop_from_open)) / denom
    return max(min(-ratio, 0.0), -1.0)


def trailing_stop_from_open(trailing: dict[str, Any] | None, *, max_profit: float) -> float | None:
    """Trailing stop as a profit-from-open ratio, or ``None`` when it is not armed."""
    if not trailing or not trailing.get("enabled"):
        return None
    activate = float(trailing.get("activate_profit_pct", 0.0) or 0.0)
    distance = float(trailing.get("distance_pct", 0.0) or 0.0)
    if distance <= 0:
        return None
    if trailing.get("only_offset_reached", True) and float(max_profit) < activate:
        return None
    if float(max_profit) < activate:
        return None
    return float(max_profit) - distance


def atr_stop_from_open(atr: dict[str, Any] | None, *, atr_value: float | None,
                       open_rate: float, current_rate: float) -> float | None:
    """ATR stop (``current - mult*ATR``) as a profit-from-open ratio, or ``None``."""
    if not atr or not atr.get("enabled"):
        return None
    if atr_value is None or not math.isfinite(float(atr_value)) or float(atr_value) <= 0:
        return None
    if open_rate <= 0 or current_rate <= 0:
        return None
    stop_price = float(current_rate) - float(atr.get("mult", 3.0) or 0.0) * float(atr_value)
    if stop_price <= 0:
        return None
    return stop_price / float(open_rate) - 1.0


def combined_stop_from_open(stoploss_cfg: dict[str, Any] | None, *, max_profit: float,
                            atr_value: float | None = None, open_rate: float = 0.0,
                            current_rate: float = 0.0,
                            fixed_ceiling: float | None = None) -> float:
    """The tightest of fixed / trailing / ATR, as a profit-from-open ratio.

    ``fixed_ceiling`` is ``risk.stoploss_per_trade``: the loosest stop the config may
    ask for. The result is never looser than the fixed stop, so enabling trailing or
    ATR can only tighten protection — never widen it.
    """
    cfg = stoploss_cfg or {}
    fixed = float(cfg.get("fixed_pct", 0.0) or 0.0)
    if fixed_ceiling is not None:
        fixed = min(fixed, float(fixed_ceiling))
    candidates = [-fixed]
    trail = trailing_stop_from_open(cfg.get("trailing"), max_profit=max_profit)
    if trail is not None:
        candidates.append(trail)
    atr = atr_stop_from_open(cfg.get("atr"), atr_value=atr_value, open_rate=open_rate,
                             current_rate=current_rate)
    if atr is not None:
        candidates.append(atr)
    return max(candidates)


def custom_stoploss_ratio(stoploss_cfg: dict[str, Any] | None, *, current_profit: float,
                          max_profit: float, atr_value: float | None = None,
                          open_rate: float = 0.0, current_rate: float = 0.0,
                          fixed_ceiling: float | None = None) -> float:
    """What ``EarnBaseStrategy.custom_stoploss`` returns: a negative from-current ratio."""
    from_open = combined_stop_from_open(
        stoploss_cfg, max_profit=max_profit, atr_value=atr_value, open_rate=open_rate,
        current_rate=current_rate, fixed_ceiling=fixed_ceiling,
    )
    return stoploss_from_open(from_open, current_profit)


def effective_fixed_stop(stoploss_cfg: dict[str, Any] | None, ceiling: float) -> float:
    """``self.stoploss`` for the strategy class: ``-min(fixed_pct, risk.stoploss_per_trade)``."""
    fixed = float((stoploss_cfg or {}).get("fixed_pct", ceiling) or ceiling)
    return -min(abs(fixed), abs(float(ceiling)))


def resolve_on_exchange(value: Any, *, live: bool) -> bool:
    """``stoploss.on_exchange``: ``auto`` means required in LIVE, off in TEST."""
    if isinstance(value, bool):
        return value
    return bool(live)


# --------------------------------------------------------------------------- ROI


def roi_table(take_profit: dict[str, Any] | None) -> dict[str, float]:
    """``minimal_roi`` for freqtrade: minutes-in-trade (as str) -> profit ratio."""
    raw = (take_profit or {}).get("roi_table") or {"0": 10.0}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(int(float(key)))] = float(value)
        except (TypeError, ValueError):
            continue
    return out or {"0": 10.0}


# --------------------------------------------------------------------------- exchange filters


@dataclass(frozen=True)
class ExchangeFilters:
    """The Binance spot filters that make an order legal (see the exchange-ops skill)."""

    step_size: float = 0.0      # LOT_SIZE.stepSize, in base units
    min_qty: float = 0.0        # LOT_SIZE.minQty
    tick_size: float = 0.0      # PRICE_FILTER.tickSize, in quote units
    min_notional: float = 0.0   # NOTIONAL.minNotional, in quote units

    @classmethod
    def from_limits(cls, limits: dict[str, Any] | None, *,
                    min_notional_floor: float = 0.0) -> ExchangeFilters:
        """Build from a ccxt-shaped ``market['limits']`` / ``market['precision']`` blob."""
        limits = limits or {}
        amount = limits.get("amount") or {}
        cost = limits.get("cost") or {}
        price = limits.get("price") or {}
        return cls(
            step_size=float(amount.get("step") or amount.get("min") or 0.0),
            min_qty=float(amount.get("min") or 0.0),
            tick_size=float(price.get("step") or 0.0),
            min_notional=max(float(cost.get("min") or 0.0), float(min_notional_floor)),
        )


def floor_to_step(value: float, step: float) -> float:
    """Largest multiple of ``step`` not above ``value``. ``step<=0`` passes through."""
    if step <= 0:
        return float(value)
    return math.floor(float(value) / step + 1e-9) * step


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.ceil(float(value) / step - 1e-9) * step


def clamp_price(price: float, tick: float, *, side: str) -> float:
    """Round a limit price onto the tick grid, away from crossing: bids down, asks up."""
    if tick <= 0 or price <= 0:
        return float(price)
    return floor_to_step(price, tick) if side == "bid" else ceil_to_step(price, tick)


def clamp_amount(amount: float, filters: ExchangeFilters) -> float:
    """Floor a base amount onto the LOT_SIZE grid; below ``min_qty`` becomes 0."""
    out = floor_to_step(max(float(amount), 0.0), filters.step_size)
    if filters.min_qty and out < filters.min_qty - 1e-12:
        return 0.0
    return out


def clamp_stake(stake: float, price: float, filters: ExchangeFilters) -> float:
    """Quote-currency stake the exchange will accept, or 0.

    Floors the implied base amount onto the LOT_SIZE step and rejects the result when
    it falls under MIN_NOTIONAL or minQty — so a clamped DCA add or TP rung is either
    a legal order or no order at all, never dust the exchange rejects.
    """
    if price <= 0 or stake <= 0:
        return 0.0
    amount = clamp_amount(float(stake) / float(price), filters)
    if amount <= 0:
        return 0.0
    notional = amount * float(price)
    if filters.min_notional and notional < filters.min_notional - 1e-9:
        return 0.0
    return notional


# --------------------------------------------------------------------------- take profit


@dataclass(frozen=True)
class LadderDecision:
    """What the TP ladder wants this candle. ``sell_stake`` is a positive USDT amount."""

    rung: int | None = None
    sell_stake: float = 0.0
    full_exit: bool = False
    reason: str = ""
    fired: tuple[int, ...] = ()

    @property
    def acts(self) -> bool:
        return self.rung is not None and (self.sell_stake > 0 or self.full_exit)


def ladder_step(ladder: list[dict[str, Any]] | None, fired: list[int] | tuple[int, ...] | None,
                *, current_profit: float, position_value: float,
                min_exit_stake: float = 0.0, dust_stake: float = 0.0,
                full_exit_below_min: bool = True,
                filters: ExchangeFilters | None = None,
                price: float = 0.0) -> LadderDecision:
    """The highest un-fired rung this profit has reached.

    Rules (spec section 9): a rung fires at most once; a rung whose slice is below
    ``min_exit_stake`` is skipped (and marked fired, so it cannot block the ladder
    forever); when the remainder after the slice would be dust, the whole position is
    exited instead. Exchange filters clamp the slice so a rung never emits dust.
    """
    rungs = list(ladder or [])
    if not rungs or position_value <= 0:
        return LadderDecision(fired=tuple(fired or ()))
    done = set(int(i) for i in (fired or ()))
    candidate: int | None = None
    for idx, rung in enumerate(rungs):
        if idx in done:
            continue
        try:
            at = float(rung.get("at_profit_pct"))
        except (TypeError, ValueError):
            continue
        if float(current_profit) >= at:
            candidate = idx  # keep walking: the highest reached rung wins
    if candidate is None:
        return LadderDecision(fired=tuple(sorted(done)))

    fraction = float(rungs[candidate].get("sell_fraction", 0.0) or 0.0)
    slice_value = max(min(fraction, 1.0), 0.0) * float(position_value)
    if filters is not None and price > 0:
        slice_value = clamp_stake(slice_value, price, filters)
    done.add(candidate)

    if slice_value <= 0 or slice_value < float(min_exit_stake) - 1e-9:
        # Too small to send: burn the rung rather than retry it every candle.
        return LadderDecision(rung=candidate, sell_stake=0.0, reason="below_min_exit",
                              fired=tuple(sorted(done)))

    remainder = float(position_value) - slice_value
    if full_exit_below_min and remainder < max(float(dust_stake), float(min_exit_stake)) - 1e-9:
        return LadderDecision(rung=candidate, sell_stake=float(position_value), full_exit=True,
                              reason="remainder_dust", fired=tuple(sorted(done)))
    return LadderDecision(rung=candidate, sell_stake=slice_value, reason=f"tp{candidate + 1}",
                          fired=tuple(sorted(done)))


# --------------------------------------------------------------------------- adds


@dataclass(frozen=True)
class AddDecision:
    """A positive ``adjust_trade_position`` stake proposal, before the gate sees it."""

    stake: float = 0.0
    kind: str = ""       # 'dca' | 'pyramid' | 'scheduled_dca'
    tag: str = ""        # 'avg_down_2', 'pyramid_1', ...
    reason: str = ""     # why not, when stake == 0

    @property
    def acts(self) -> bool:
        return self.stake > 0


def _cooldown_ok(now: datetime, last: datetime | None, hours: float) -> bool:
    if hours <= 0 or last is None:
        return True
    return now - last >= timedelta(hours=float(hours))


def dca_add(dca: dict[str, Any] | None, *, adds_used: int, current_profit: float,
            first_stake: float, now: datetime, last_add: datetime | None = None,
            regime_up: bool = True, max_entries: int | None = None,
            entries_used: int | None = None) -> AddDecision:
    """Averaging down: trigger at ``-step_pct*(n+1)``, size ``first * multiplier**n``."""
    cfg = dca or {}
    if not cfg.get("enabled"):
        return AddDecision(reason="disabled")
    max_adds = int(cfg.get("max_adds", 0) or 0)
    if adds_used >= max_adds:
        return AddDecision(reason="max_adds")
    if max_entries is not None and entries_used is not None and entries_used >= max_entries:
        return AddDecision(reason="entries_per_trade")
    if cfg.get("only_if_regime_up", True) and not regime_up:
        return AddDecision(reason="regime_down")
    step = float(cfg.get("step_pct", 0.0) or 0.0)
    if step <= 0 or float(current_profit) > -step * (adds_used + 1):
        return AddDecision(reason="trigger_not_reached")
    if not _cooldown_ok(now, last_add, float(cfg.get("cooldown_hours", 0) or 0)):
        return AddDecision(reason="cooldown")
    stake = float(first_stake) * float(cfg.get("size_multiplier", 1.0) or 1.0) ** adds_used
    if stake <= 0:
        return AddDecision(reason="zero_stake")
    return AddDecision(stake=stake, kind="dca", tag=f"avg_down_{adds_used + 1}")


def pyramid_add(pyramid: dict[str, Any] | None, *, adds_used: int, current_profit: float,
                first_stake: float, now: datetime, last_add: datetime | None = None,
                max_entries: int | None = None,
                entries_used: int | None = None) -> AddDecision:
    """Adding to a winner: trigger at ``trigger_profit_pct``, size ``first * multiplier**n``."""
    cfg = pyramid or {}
    if not cfg.get("enabled"):
        return AddDecision(reason="disabled")
    max_adds = int(cfg.get("max_adds", 0) or 0)
    if adds_used >= max_adds:
        return AddDecision(reason="max_adds")
    if max_entries is not None and entries_used is not None and entries_used >= max_entries:
        return AddDecision(reason="entries_per_trade")
    trigger = float(cfg.get("trigger_profit_pct", 0.0) or 0.0)
    if trigger <= 0 or float(current_profit) < trigger:
        return AddDecision(reason="trigger_not_reached")
    if not _cooldown_ok(now, last_add, float(cfg.get("cooldown_hours", 0) or 0)):
        return AddDecision(reason="cooldown")
    stake = float(first_stake) * float(cfg.get("size_multiplier", 1.0) or 1.0) ** adds_used
    if stake <= 0:
        return AddDecision(reason="zero_stake")
    return AddDecision(stake=stake, kind="pyramid", tag=f"pyramid_{adds_used + 1}")


def scheduled_dca_due(scheduled: dict[str, Any] | None, *, now: datetime,
                      last_fill: datetime | None) -> bool:
    """SleeveA's calendar DCA: due when ``interval_days`` have passed since the last FILL."""
    cfg = scheduled or {}
    if not cfg.get("enabled", True):
        return False
    if last_fill is None:
        return True
    return now - last_fill >= timedelta(days=float(cfg.get("interval_days", 7) or 7))


# --------------------------------------------------------------------------- rebalance / cooldown


def rebalance_allowed(*, now: datetime, last_rebalance: datetime | None,
                      min_interval_hours: float) -> bool:
    return _cooldown_ok(now, last_rebalance, float(min_interval_hours or 0))


def within_band(gap: float, nav: float, band: float) -> bool:
    """True when a target/position gap is inside the dead-band (so: do nothing)."""
    return abs(float(gap)) / max(float(nav), _EPS) <= float(band)


def reentry_blocked(*, now: datetime, stopped_at: datetime | None, cooldown_hours: float,
                    proposal_at: datetime | None = None) -> bool:
    """Re-entry cooldown after a stop/TP exit.

    Blocked until ``cooldown_hours`` have passed, unless a proposal newer than the
    stop exists — a fresh human/model decision overrides the cooldown by design.
    """
    if stopped_at is None or cooldown_hours <= 0:
        return False
    if now - stopped_at >= timedelta(hours=float(cooldown_hours)):
        return False
    if proposal_at is not None and proposal_at > stopped_at:
        return False
    return True


# --------------------------------------------------------------------------- backoff


def backoff_until(now: datetime, attempts: int, backoff_minutes: list[int] | None) -> datetime:
    """When to retry after ``attempts`` consecutive exchange rejections."""
    steps = [int(m) for m in (backoff_minutes or [15, 60, 240]) if int(m) >= 0]
    if not steps:
        return now
    idx = min(max(int(attempts), 1), len(steps)) - 1
    return now + timedelta(minutes=steps[idx])


def backoff_active(now: datetime, until: datetime | None) -> bool:
    return until is not None and now < until


# --------------------------------------------------------------------------- priority


@dataclass
class ActionSet:
    """Candidate actions for one trade this candle; :meth:`choose` applies the priority."""

    candidates: dict[str, Any] = field(default_factory=dict)

    def offer(self, kind: str, payload: Any) -> None:
        if kind not in ACTION_PRIORITY:
            raise ValueError(f"unknown action kind {kind!r}")
        if payload is not None:
            self.candidates[kind] = payload

    def choose(self) -> tuple[str, Any] | None:
        for kind in ACTION_PRIORITY:
            if kind in self.candidates:
                return kind, self.candidates[kind]
        return None


def choose_action(candidates: dict[str, Any]) -> tuple[str, Any] | None:
    """Standalone form of :meth:`ActionSet.choose` (one action per trade per candle)."""
    for kind in ACTION_PRIORITY:
        if kind in candidates and candidates[kind] is not None:
            return kind, candidates[kind]
    return None


# --------------------------------------------------------------------------- params clamping


def clamp_params(params: dict[str, Any], bounds: dict[str, dict[str, float]] | None,
                 ) -> tuple[dict[str, Any], list[str]]:
    """Clamp every bounded leaf of ``params`` into ``bounds``; report what was off.

    ``bounds`` is ``riskgate.json['bounds']``, keyed by dotted path with the sleeve
    prefix (``sleeve_a.trend.ma_days``). The returned list names every path that was
    out of range or non-numeric — the caller journals a ``params_out_of_bounds``
    breach and keeps the last good params.
    """
    violations: list[str] = []
    if not bounds:
        return dict(params), violations
    out = _copy_tree(params)
    for full_path, spec in bounds.items():
        parts = full_path.split(".")
        # Bounds are written with the sleeve prefix; params files are sleeve-local.
        for candidate in (parts, parts[1:]):
            if not candidate:
                continue
            node = out
            ok = True
            for part in candidate[:-1]:
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            leaf = candidate[-1]
            if not ok or not isinstance(node, dict) or leaf not in node:
                continue
            value = node[leaf]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append(full_path)
                break
            lo, hi = spec.get("min"), spec.get("max")
            clamped = float(value)
            if lo is not None:
                clamped = max(clamped, float(lo))
            if hi is not None:
                clamped = min(clamped, float(hi))
            if abs(clamped - float(value)) > 1e-12:
                violations.append(full_path)
                node[leaf] = clamped
            break
    return out, violations


def _copy_tree(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _copy_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_copy_tree(v) for v in node]
    return node


# --------------------------------------------------------------------------- misc


def derived_startup_candles(bounds: dict[str, dict[str, float]] | None, timeframe: str,
                            *, default: int = 1320) -> int:
    """``bounds['sleeve_a.trend.ma_days'].max + 20`` days expressed in ``timeframe`` candles."""
    spec = (bounds or {}).get("sleeve_a.trend.ma_days") or {}
    try:
        ma_days = float(spec["max"])
    except (KeyError, TypeError, ValueError):
        return int(default)
    per_day = candles_per_day(timeframe)
    return int(math.ceil((ma_days + 20.0) * per_day))


def candles_per_day(timeframe: str) -> float:
    tf = (timeframe or "4h").strip().lower()
    unit, number = tf[-1], tf[:-1]
    try:
        n = float(number)
    except ValueError:
        return 6.0
    minutes = {"m": 1.0, "h": 60.0, "d": 1440.0, "w": 10080.0}.get(unit)
    if not minutes or n <= 0:
        return 6.0
    return 1440.0 / (n * minutes)
