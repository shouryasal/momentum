/**
 * Home, as money.
 *
 * The owner's brief for this screen was one sentence: *"on main page i only want to see
 * seed money, ongoing holdings, current value of total money, maybe a table of each
 * holding showing value profit loss and a table of transactions done, if i double click i
 * should see reasoning"*.  So Home is four things, and this module is the arithmetic
 * behind them — pure functions of the payloads the screen already loaded, so the numbers
 * can be held to in a test without rendering anything.
 *
 * Three rules every function here keeps:
 *
 *   - **Money reads as money.** `$5,000.00`, `+$123.45 (+2.5%)`. Never a bare ratio, never
 *     a raw float.
 *   - **Unknown is not zero.** A holding the console could not price returns `null`, and
 *     the screen prints "not recorded" — printing `$0.00` would be a lie about the pot.
 *   - **No identifier leaks.** Run ids, model ids and callback paths belong in the
 *     reasoning pane, never in a row.
 */

import { formatUsd } from '@/lib/format';

import type { FillRow, PortfolioPayload, Position } from '../portfolio/api';
import type { OverviewPayload } from './api';

/* ------------------------------------------------------------------------- formatting */

function finite(value: number | null | undefined): number | null {
  return value === null || value === undefined || !Number.isFinite(value) ? null : value;
}

/** `5000` -> `$5,000.00`; anything unknown -> `—`. */
export function money(value: number | null | undefined): string {
  const n = finite(value);
  return n === null ? '—' : formatUsd(n);
}

/** `123.45` -> `+$123.45`; `-9` -> `-$9.00`. */
export function signedMoney(value: number | null | undefined): string {
  const n = finite(value);
  if (n === null) return '—';
  return n >= 0 ? `+${formatUsd(n)}` : formatUsd(n);
}

/** A *fraction* — `0.025` -> `+2.5%`. */
export function signedPct(fraction: number | null | undefined, digits = 2): string {
  const n = finite(fraction);
  if (n === null) return '—';
  const pct = n * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(digits)}%`;
}

/** `+$123.45 (+2.50%)`, the one shape every gain or loss on Home takes. */
export function gainPhrase(
  amount: number | null | undefined,
  fraction: number | null | undefined,
): string {
  const a = finite(amount);
  const f = finite(fraction);
  if (a === null && f === null) return 'not recorded';
  if (f === null) return signedMoney(a);
  if (a === null) return signedPct(f);
  return `${signedMoney(a)} (${signedPct(f)})`;
}

/** `BTC/USDT` -> `BTC`. */
export function assetOf(pair: string): string {
  return String(pair).split('/')[0] ?? String(pair);
}

/* --------------------------------------------------------------- 1 and 2: the two totals */

/**
 * Is a bot actually running on the demo account right now?
 *
 * This is the switch the whole screen turns on: when it is true the money cards and the
 * holdings table read the real demo balances, and when it is false they read the
 * simulation.  It is never "a bit of both" — see the rule in `seedMoney`.
 */
export function demoIsActive(data: OverviewPayload | null | undefined): boolean {
  return Boolean(data?.demo?.active && data.demo.state === 'ok');
}

/**
 * What was put in, across every bot.
 *
 * The server resolves this now, per mode, and says where the number came from — an active
 * run's record, the seed pinned at arming, the real account balance, or the configured
 * seed for the next run.  The old sum over `mode.sleeves` stays as the fallback for a
 * server that has not been restarted yet.
 *
 * `null` when the two bots are on *different kinds of money*: a simulated pot and a demo
 * pot are not addable, and printing their sum would be a lie about both.  Also `null` when
 * nothing at all is known — the screen then says the money has not been set yet rather
 * than showing `$0.00`, which would read as "you have nothing".
 */
export function seedMoney(data: OverviewPayload | null | undefined): number | null {
  const panel = data?.seed;
  if (panel) return panel.mixed ? null : finite(panel.total_usdt);
  const sleeves = Object.values(data?.mode?.sleeves ?? {});
  const seeds = sleeves.map((sleeve) => finite(sleeve.seed_usdt)).filter((v): v is number => v !== null);
  if (seeds.length === 0) return null;
  return seeds.reduce((sum, value) => sum + value, 0);
}

/**
 * The sentence under "Money put in" — what kind of pot it is, and where the figure is from.
 *
 * The owner's rule for this screen is that a number is never mysterious.  So the subtitle
 * always names its source in the same three words the server used: *recorded at run start*,
 * *configured for the next run*, or *live demo account balance*.
 */
export function seedHint(data: OverviewPayload | null | undefined): string {
  const panel = data?.seed;
  if (!panel) {
    return 'No starting pot has been recorded, so there is nothing to measure against.';
  }
  if (panel.note) return panel.note;
  if (panel.total_usdt === null) {
    return 'No starting pot has been recorded, so there is nothing to measure against.';
  }
  return `${panel.label} — ${panel.source_label}.`;
}

/**
 * What it is worth right now, across every bot. The yardstick row is not one of them.
 *
 * On a demo run the account itself is the answer: the ledger is a record of what our bots
 * did, the exchange is a record of what the money *is*.  Simulated NAV and demo balances
 * are never added — the whole screen is on one basis or the other.
 */
export function totalValue(data: OverviewPayload | null | undefined): number | null {
  if (demoIsActive(data)) return finite(data?.demo?.value_usdt);
  const cards = (data?.nav?.cards ?? []).filter((card) => card.sleeve !== 'benchmark');
  const values = cards.map((card) => finite(card.nav_usdt)).filter((v): v is number => v !== null);
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0);
}

/**
 * The one quiet line about a demo account that is configured but not yet in use.
 *
 * `null` when there is nothing to say — no demo key, or a bot is already running on it, in
 * which case the demo numbers are the money cards and repeating them here would be noise.
 * This is deliberately one sentence: the owner asked for a money screen, and a second
 * account that has never traded is a footnote, not a panel.
 */
export function demoLine(data: OverviewPayload | null | undefined): string | null {
  const demo = data?.demo;
  if (!demo?.configured || demo.active) return null;
  const host = demo.host ?? 'the demo account';
  if (demo.state !== 'ok') {
    const why = demo.error ? ` (${demo.error})` : '';
    return `Cannot reach ${host} right now${why}. Nothing above depends on it.`;
  }
  const held = Object.entries(demo.balances ?? {})
    .map(([asset, amount]) => `${formatAmount(amount)} ${asset}`)
    .join(', ');
  const worth = demo.value_usdt === null || demo.value_usdt === undefined
    ? 'an amount that could not be fully priced'
    : money(demo.value_usdt);
  const traded = demo.traded_here
    ? `${demo.open_order_count ?? 0} order(s) are resting there.`
    : 'Nothing has been traded there yet.';
  const trading = demo.can_trade ? 'trading is enabled' : 'trading is NOT enabled on the key';
  return `A demo account is set up at ${host}: ${worth}${held ? ` (${held})` : ''}, ${trading}. ${traded}`;
}

function formatAmount(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const digits = Math.abs(value) >= 1 ? 2 : 8;
  return Number(value.toFixed(digits)).toLocaleString('en-US');
}

export interface TotalSummary {
  seed: number | null;
  value: number | null;
  /** Money made or lost since the money went in. */
  gain: number | null;
  /** The same, as a fraction of what went in. */
  gainFraction: number | null;
}

export function totals(data: OverviewPayload | null | undefined): TotalSummary {
  const seed = seedMoney(data);
  const value = totalValue(data);
  const gain = seed === null || value === null ? null : value - seed;
  const gainFraction = seed === null || gain === null || seed === 0 ? null : gain / seed;
  return { seed, value, gain, gainFraction };
}

/**
 * The one line that compares the pot with simply having bought BTC and left it alone.
 *
 * One line is the whole allowance: the owner asked for the comparison, not for a chart of
 * it.  The chart, the attribution and the what-ifs are on the Performance screen.
 */
export function benchmarkLine(data: OverviewPayload | null | undefined): string {
  const bench = (data?.nav?.cards ?? []).find((card) => card.sleeve === 'benchmark');
  const benchFraction = bench ? finite(bench.run_pct) : null;
  if (benchFraction === null) {
    return 'There is nothing to compare against yet — the BTC yardstick starts once the first day is recorded.';
  }
  const mine = totals(data).gainFraction;
  const benchPhrase = `Simply holding BTC over the same time would be ${signedPct(benchFraction / 100)}`;
  if (mine === null) return `${benchPhrase}.`;
  const points = mine * 100 - benchFraction;
  if (Math.abs(points) < 0.005) return `${benchPhrase} — the same as you, near enough.`;
  return `${benchPhrase} — you are ${Math.abs(points).toFixed(2)} points ${
    points > 0 ? 'ahead' : 'behind'
  }.`;
}

/* ------------------------------------------------------------------- 3: what it holds */

export interface HoldingRow {
  /** Stable row key and the id the detail pane opens on. */
  key: string;
  sleeve: string;
  /** `BTC` — what a person calls it. */
  asset: string;
  /** `BTC/USDT` — what the exchange calls it, used to find the trades behind it. */
  pair: string;
  amount: number;
  /** What one unit cost on average, and what the lot cost in total. */
  costEach: number | null;
  costTotal: number | null;
  /** What it is worth now. */
  valueNow: number | null;
  /** Profit or loss, in money and as a fraction of what it cost. */
  gain: number | null;
  gainFraction: number | null;
  /** `sim` / `demo` / `live`, as the journal recorded it. */
  mode: string | null;
}

/**
 * One row per asset held, with what it cost beside what it is worth.
 *
 * The bot's own book (`/portfolio/<bot>`) is the only place an average entry price lives,
 * so it leads.  Anything the ledger says is held but the bot did not report — a bot that
 * is down, a coin bought before this run — still gets a row, with its cost marked as not
 * recorded rather than invented.
 */
export function holdingRows(
  portfolios: Array<PortfolioPayload | null | undefined>,
  data?: OverviewPayload | null,
): HoldingRow[] {
  if (demoIsActive(data)) return demoHoldingRows(portfolios, data);

  const rows: HoldingRow[] = [];
  const seen = new Set<string>();

  for (const payload of portfolios) {
    if (!payload) continue;
    for (const position of payload.positions ?? []) {
      const row = fromPosition(payload.sleeve, position);
      seen.add(`${payload.sleeve}:${row.asset}`);
      rows.push(row);
    }
  }

  for (const sleeve of data?.exposure?.sleeves ?? []) {
    if (sleeve.sleeve === 'benchmark') continue;
    for (const asset of sleeve.assets ?? []) {
      const amount = finite(asset.amount);
      if (amount === null || amount <= 0) continue;
      if (seen.has(`${sleeve.sleeve}:${asset.asset}`)) continue;
      seen.add(`${sleeve.sleeve}:${asset.asset}`);
      rows.push({
        key: `${sleeve.sleeve}:${asset.asset}`,
        sleeve: sleeve.sleeve,
        asset: asset.asset,
        pair: `${asset.asset}/USDT`,
        amount,
        costEach: null,
        costTotal: null,
        valueNow: finite(asset.value_usdt),
        gain: null,
        gainFraction: null,
        mode: null,
      });
    }
  }

  return rows.sort((a, b) => (b.valueNow ?? 0) - (a.valueNow ?? 0));
}

/**
 * What it holds, on a demo run: the exchange's answer, not the ledger's.
 *
 * The venue is the authority on *what is held* — it is the account the coins are actually
 * in.  The bot's own book is still the only place an average entry price lives, so a cost
 * is taken from there when a matching position exists and is marked "not recorded" when it
 * does not.  Stablecoins are the cash half of the account and are not holdings.
 */
export function demoHoldingRows(
  portfolios: Array<PortfolioPayload | null | undefined>,
  data?: OverviewPayload | null,
): HoldingRow[] {
  const byAsset = new Map<string, { sleeve: string; position: Position }>();
  for (const payload of portfolios) {
    if (!payload) continue;
    for (const position of payload.positions ?? []) {
      const asset = assetOf(position.pair);
      if (!byAsset.has(asset)) byAsset.set(asset, { sleeve: payload.sleeve, position });
    }
  }
  return (data?.demo?.holdings ?? [])
    .filter((holding) => !holding.stable && (finite(holding.amount) ?? 0) > 0)
    .map((holding) => {
      const match = byAsset.get(holding.asset);
      const costEach = match ? finite(match.position.avg_entry) : null;
      const amount = finite(holding.amount) ?? 0;
      const costTotal = costEach === null ? null : costEach * amount;
      const valueNow = finite(holding.value_usdt);
      const gain = costTotal === null || valueNow === null ? null : valueNow - costTotal;
      return {
        key: `demo:${holding.asset}`,
        sleeve: match?.sleeve ?? 'demo',
        asset: holding.asset,
        pair: `${holding.asset}/USDT`,
        amount,
        costEach,
        costTotal,
        valueNow,
        gain,
        gainFraction: gain === null || !costTotal ? null : gain / costTotal,
        mode: 'demo',
      };
    })
    .sort((a, b) => (b.valueNow ?? 0) - (a.valueNow ?? 0));
}

function fromPosition(sleeve: string, position: Position): HoldingRow {
  const amount = finite(position.amount) ?? 0;
  const costEach = finite(position.avg_entry);
  const costTotal = costEach === null ? null : costEach * amount;
  return {
    key: `${sleeve}:${assetOf(position.pair)}`,
    sleeve,
    asset: assetOf(position.pair),
    pair: position.pair,
    amount,
    costEach,
    costTotal,
    valueNow: finite(position.value_usdt),
    gain: finite(position.upnl_usdt),
    gainFraction: finite(position.upnl_pct),
    mode: position.mode ?? null,
  };
}

/* ------------------------------------------------------------- 4: what it bought and sold */

export interface TransactionRow {
  key: string;
  sleeve: string;
  /** UTC, as the journal recorded it. */
  when: string;
  asset: string;
  pair: string;
  side: 'buy' | 'sell';
  amount: number;
  price: number;
  /** `amount * price` — what the trade was worth. */
  value: number;
  /**
   * For a sell only: the money made or lost on the units sold, against the average price
   * paid for them. `null` on a buy, and on a sell whose buys are older than the trades on
   * record — an honest gap rather than a made-up number.
   */
  realised: number | null;
  realisedFraction: number | null;
  mode: string | null;
  /** The decision this came from, for the reasoning pane. Never shown on the row. */
  runId: string | null;
}

/**
 * Every buy and sell, newest first, with what each sell actually made.
 *
 * The journal records fills, not profits, so the realised figure is worked out here the
 * way an accountant would: buys raise the average price paid, a sell releases that many
 * units at that average, and the difference against the sale price is what it made.  When
 * a sell reaches further back than the trades on record the figure is `null`, because a
 * partial history would produce a confident wrong number.
 */
export function transactionRows(
  portfolios: Array<PortfolioPayload | null | undefined>,
): TransactionRow[] {
  const fills: Array<{ sleeve: string; fill: FillRow }> = [];
  for (const payload of portfolios) {
    if (!payload) continue;
    for (const fill of payload.fills ?? []) {
      fills.push({ sleeve: fill.sleeve || payload.sleeve, fill });
    }
  }

  // Oldest first, so the running average price paid is built in the order it happened.
  fills.sort((a, b) => String(a.fill.ts_utc).localeCompare(String(b.fill.ts_utc)));

  const book = new Map<string, { units: number; cost: number; complete: boolean }>();
  const rows: TransactionRow[] = [];

  for (const { sleeve, fill } of fills) {
    const key = `${sleeve}:${fill.pair}`;
    const lot = book.get(key) ?? { units: 0, cost: 0, complete: true };
    const amount = finite(fill.fill_amount) ?? 0;
    const price = finite(fill.fill_price) ?? 0;
    const side = String(fill.side).toLowerCase() === 'sell' ? 'sell' : 'buy';

    let realised: number | null = null;
    let realisedFraction: number | null = null;

    if (side === 'buy') {
      lot.units += amount;
      lot.cost += amount * price;
    } else if (lot.units > 0 && lot.complete) {
      const avg = lot.cost / lot.units;
      const sold = Math.min(amount, lot.units);
      realised = (price - avg) * sold;
      realisedFraction = avg === 0 ? null : (price - avg) / avg;
      lot.units -= sold;
      lot.cost -= avg * sold;
      // Sold more than the trades on record account for: every later sell of this coin
      // would be guesswork, so say so instead of guessing.
      if (amount > sold) lot.complete = false;
    } else {
      lot.complete = false;
    }
    book.set(key, lot);

    rows.push({
      key: `${sleeve}:${fill.id}`,
      sleeve,
      when: fill.ts_utc,
      asset: assetOf(fill.pair),
      pair: fill.pair,
      side,
      amount,
      price,
      value: amount * price,
      realised,
      realisedFraction,
      mode: fill.mode ?? null,
      runId: fill.run_id ?? null,
    });
  }

  return rows.reverse();
}

/** The decision behind the most recent trade in an asset — what the reasoning pane opens. */
export function lastRunFor(rows: TransactionRow[], holding: HoldingRow): string | null {
  for (const row of rows) {
    if (row.sleeve === holding.sleeve && row.asset === holding.asset && row.runId) return row.runId;
  }
  return null;
}
