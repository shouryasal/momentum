"""Comparative arithmetic for `docs`-cited Earn figures vs published retail-trader figures.

MEASURE 5 (workspace pa5). This script computes NO new backtest and adds NO selection
trials. Every Earn input is a figure cited from a design document or read out of the live
paper databases read-only; every human input is a figure read out of a published paper or
regulator notice. The script exists so the arithmetic in
`evals/research/profit-audit/vs-traders.md` can be re-run rather than trusted.

Run:  python3 evals/research/profit-audit/vs_traders_arith.py
"""

from __future__ import annotations

# --- Earn, cited (docs/design/*) ------------------------------------------------------
# exit-and-horizon-2026-09-29.md §2: the shipped 4h SleeveA book over 3,326 days.
FOURH_SELLS = 40
FOURH_ENTRIES = 42
FOURH_TOPUPS = 49
PANEL_DAYS = 3326
PANEL_YEARS = PANEL_DAYS / 365.0
FOURH_TURNOVER_X = 1.29          # x NAV / yr
FOURH_FEE_PCT_YR = 0.19          # % of NAV / yr
# trend-ensemble.md §1: the daily-rebalanced ensemble the docs justify.
ENSEMBLE_TURNOVER_X = 8.62
ENSEMBLE_FEE_PCT_YR = 1.29
# exit-and-horizon-2026-09-29.md §3: the fee gradient by holding period.
TURNOVER_1H_PCT_YR = 633.0       # % of NAV / yr at a one-hour hold
# exit-and-horizon-2026-09-29.md §2: cap drift.
DAYS_OVER_BTC_CAP = 422
DAYS_OVER_GROSS = 136
COST_ROUND_TRIP_PCT = 0.30       # the standing cost floor, always on

# --- Earn, measured live read-only 2026-09-30 (this run) -------------------------------
LIVE_TRADES_PER_SLEEVE = 12      # 2 (4h, day one) + 10 (1h fast profile)
LIVE_SPAN_HOURS = 168.07         # 2026-09-23T13:37:36Z -> 2026-09-30T13:41:42Z
LIVE_AWAKE_HOURS = 48            # distinct hours with a nav_points row
LIVE_NAV_SPAN_HOURS = 153.25     # 2026-09-23T21:45Z -> 2026-09-30T07:00Z
BINANCE_USDT_TRADING = 503       # /api/v3/exchangeInfo, permissions=SPOT
EARN_LIVE_PAIRS = 31
EARN_FEATHER_PAIRS = 108
EARN_PANEL_TICKERS = 747
EARN_PANEL_DEAD = 282

# --- Humans, cited (read, not summarised) ---------------------------------------------
# Barber & Odean (2000), J. Finance 55(2):773-806, 66,465 households, 1991-1996.
BO_AVG_TURNOVER_PCT = 75.0       # % of portfolio / yr, average household
BO_TOP_TURNOVER_PCT = 250.0      # "more than 250 percent" for those that trade most
BO_MARKET_RET = 17.9
BO_GROSS_AVG = 18.7
BO_NET_AVG = 16.4
BO_NET_HIGH_TURN = 11.4
BO_NET_LOW_TURN = 18.5


def line(label: str, value: str) -> None:
    print(f"  {label:<52} {value}")


def main() -> None:
    print("=" * 78)
    print("DECISIONS A YEAR")
    print("=" * 78)
    fourh_decisions = FOURH_SELLS + FOURH_ENTRIES + FOURH_TOPUPS
    line("4h shipped book, sells / yr", f"{FOURH_SELLS / PANEL_YEARS:.2f}")
    line("4h shipped book, all order decisions / yr",
         f"{fourh_decisions / PANEL_YEARS:.1f}  ({fourh_decisions} in {PANEL_YEARS:.2f}y)")
    line("daily ensemble, sizing decisions / yr", "~365 (it re-sizes every day)")
    per_wall = LIVE_TRADES_PER_SLEEVE / LIVE_SPAN_HOURS * 24 * 365
    per_awake = LIVE_TRADES_PER_SLEEVE / LIVE_AWAKE_HOURS * 24 * 365
    line("1h live profile, round trips / yr (wall clock)", f"{per_wall:.0f}")
    line("1h live profile, round trips / yr (at 100% uptime)", f"{per_awake:.0f}")
    line("ratio, live 1h pace to shipped 4h book",
         f"{per_awake / (FOURH_SELLS / PANEL_YEARS):.0f}x")

    print()
    print("=" * 78)
    print("TURNOVER AND FEE DRAG, ON ONE AXIS")
    print("=" * 78)
    line("Earn 4h shipped book", f"{FOURH_TURNOVER_X * 100:.0f}% of NAV/yr, "
                                f"{FOURH_FEE_PCT_YR}%/yr in fees")
    line("Earn daily ensemble (the book the docs justify)",
         f"{ENSEMBLE_TURNOVER_X * 100:.0f}% of NAV/yr, {ENSEMBLE_FEE_PCT_YR}%/yr")
    line("Earn at a one-hour hold", f"{TURNOVER_1H_PCT_YR:.0f}% of NAV/yr")
    line("Barber-Odean average household", f"{BO_AVG_TURNOVER_PCT:.0f}% / yr")
    line("Barber-Odean highest-turnover quintile",
         f">{BO_TOP_TURNOVER_PCT:.0f}% / yr")
    line("Earn 4h vs the average household",
         f"{FOURH_TURNOVER_X * 100 / BO_AVG_TURNOVER_PCT:.2f}x their turnover")
    line("Earn 4h vs the busiest quintile",
         f"{FOURH_TURNOVER_X * 100 / BO_TOP_TURNOVER_PCT:.2f}x their turnover")
    line("Earn 1h live vs the busiest quintile",
         f"{TURNOVER_1H_PCT_YR / BO_TOP_TURNOVER_PCT:.2f}x their turnover")

    print()
    print("  Barber-Odean, the shape of the damage (their table, not ours):")
    line("    gross return, average household", f"{BO_GROSS_AVG}%")
    line("    net return, average household", f"{BO_NET_AVG}%")
    line("    net, highest-turnover quintile", f"{BO_NET_HIGH_TURN}%")
    line("    net, lowest-turnover quintile", f"{BO_NET_LOW_TURN}%")
    line("    cost of turnover alone (pp/yr)",
         f"{BO_NET_LOW_TURN - BO_NET_HIGH_TURN:.1f}")
    line("    both quintiles vs the market index",
         f"market {BO_MARKET_RET}%")

    print()
    print("=" * 78)
    print("DISCIPLINE: WHERE EARN KEEPS IT AND WHERE IT DOES NOT")
    print("=" * 78)
    line("days over the BTC weight cap",
         f"{DAYS_OVER_BTC_CAP} of {PANEL_DAYS} = "
         f"{DAYS_OVER_BTC_CAP / PANEL_DAYS * 100:.1f}%")
    line("days over the 0.80 gross ceiling",
         f"{DAYS_OVER_GROSS} of {PANEL_DAYS} = "
         f"{DAYS_OVER_GROSS / PANEL_DAYS * 100:.1f}%")
    line("live uptime, this test window",
         f"{LIVE_AWAKE_HOURS} of {LIVE_NAV_SPAN_HOURS:.0f} h = "
         f"{LIVE_AWAKE_HOURS / LIVE_NAV_SPAN_HOURS * 100:.0f}%")

    print()
    print("=" * 78)
    print("UNIVERSE BREADTH")
    print("=" * 78)
    line("Binance USDT spot symbols TRADING today", f"{BINANCE_USDT_TRADING}")
    line("pairs the live bots may touch",
         f"{EARN_LIVE_PAIRS} = "
         f"{EARN_LIVE_PAIRS / BINANCE_USDT_TRADING * 100:.1f}% of them")
    line("pairs with local candles (backtestable)",
         f"{EARN_FEATHER_PAIRS} = "
         f"{EARN_FEATHER_PAIRS / BINANCE_USDT_TRADING * 100:.1f}%")
    line("tickers in the research panel (dead in)",
         f"{EARN_PANEL_TICKERS} ({EARN_PANEL_DEAD} dead)")
    line("pairs the book actually holds", "2 (BTC, ETH) + up to 2 satellites at 5%")

    print()
    print("=" * 78)
    print("THE COST FLOOR THAT DECIDES ALL OF IT")
    print("=" * 78)
    line("round trip, always on", f"{COST_ROUND_TRIP_PCT}%")
    line("round trips before 1% of NAV is gone in fees",
         f"{1.0 / COST_ROUND_TRIP_PCT:.1f}")
    line("gross edge the 1h rule produces per trade", "+0.022% (cited)")
    line("shortfall against the floor",
         f"{COST_ROUND_TRIP_PCT / 0.022:.1f}x")

    print()
    print("Selection trials added by this measure: 0 (no backtest was run).")


if __name__ == "__main__":
    main()
