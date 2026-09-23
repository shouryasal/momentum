/**
 * Plain language for the console.
 *
 * The owner had to ask what "sleeve A" and "sleeve B" mean.  That is a naming bug, not a
 * documentation bug, so the fix lives here: one table of human names for the sleeves, and
 * one glossary of the builder words the UI used to print bare.  Every screen imports from
 * this module rather than spelling a sleeve or a piece of jargon out again, so the words
 * stay the same everywhere and changing them is one edit.
 *
 * Rules this file keeps:
 *   - The human name leads; the raw identifier (`sleeve a`) stays visible as the secondary
 *     label, because logs, run ids and the API still speak it.
 *   - A glossary entry is one sentence, in words an operator who is not a programmer can
 *     read, and it never restates a limit — limits come from `config/earn.yaml`.
 */

export type SleeveKey = 'a' | 'b' | 'benchmark' | string;

export interface SleeveNaming {
  /** Short human name for a badge or a tab: "Rules bot". */
  name: string;
  /** Full human name for a heading: "Rules bot (no AI)". */
  full: string;
  /** The raw identifier, shown as the secondary label. */
  raw: string;
  /** One sentence explaining what this sleeve is, for a tooltip. */
  hint: string;
}

const SLEEVES: Record<string, SleeveNaming> = {
  a: {
    name: 'Rules bot',
    full: 'Rules bot (no AI)',
    raw: 'sleeve a',
    hint: 'Sleeve A. Trades from fixed rules written in code. Claude never touches it, so it is the control group.',
  },
  b: {
    name: 'AI bot',
    full: 'AI bot (Claude)',
    raw: 'sleeve b',
    hint: 'Sleeve B. Claude reads the numbers and suggests how much to hold; the risk checks still decide whether the order is allowed.',
  },
  benchmark: {
    name: 'Buy & hold BTC',
    full: 'Buy and hold BTC',
    raw: 'benchmark',
    hint: 'The yardstick: what the same money would be worth if you had simply bought BTC on day one and left it alone.',
  },
};

export function sleeveNaming(sleeve: SleeveKey | null | undefined): SleeveNaming {
  const key = String(sleeve ?? '').toLowerCase();
  return (
    SLEEVES[key] ?? {
      name: key ? key.toUpperCase() : 'unknown',
      full: key ? key.toUpperCase() : 'unknown',
      raw: key ? `sleeve ${key}` : 'unknown',
      hint: 'One of the parallel trading sleeves.',
    }
  );
}

/** "Rules bot" — for a badge or a tab. */
export function sleeveName(sleeve: SleeveKey | null | undefined): string {
  return sleeveNaming(sleeve).name;
}

/** "Rules bot (no AI)" — for a heading. */
export function sleeveFullName(sleeve: SleeveKey | null | undefined): string {
  return sleeveNaming(sleeve).full;
}

/** "Rules bot (no AI) · sleeve a" — name first, raw id second. */
export function sleeveWithRaw(sleeve: SleeveKey | null | undefined): string {
  const naming = sleeveNaming(sleeve);
  return `${naming.full} · ${naming.raw}`;
}

export function sleeveHint(sleeve: SleeveKey | null | undefined): string {
  return sleeveNaming(sleeve).hint;
}

/**
 * Builder words the UI cannot avoid printing, each with the one sentence that explains it.
 *
 * Keys are lowercase; look one up with {@link explain}, which also accepts the plural and
 * a `tier-1`/`tier 1` spelling.
 */
export const GLOSSARY: Record<string, string> = {
  sleeve:
    'One of the two bots running side by side: the Rules bot (sleeve a, no AI) and the AI bot (sleeve b, Claude).',
  gate:
    'The risk gate: deterministic checks in code that every order must pass before it is sent. Claude cannot change it or skip it.',
  'risk gate':
    'Deterministic checks in code that every order must pass before it is sent. Claude cannot change it or skip it.',
  proposal:
    "Claude's suggestion for how much of each coin to hold. It is only a suggestion — the risk gate and, in live mode, you decide whether it happens.",
  abstain:
    'Claude declining to suggest anything because the inputs were stale or contradicted each other. Doing nothing is the safe default.',
  abstained:
    'Claude declined to suggest anything, because the inputs were stale or contradicted each other. Doing nothing is the safe default.',
  'min_tier':
    'The weakest kind of model a job is allowed to use. A job that matters is never answered by a small local model.',
  sleeves:
    'The two bots running side by side: the Rules bot (sleeve a, no AI) and the AI bot (sleeve b, Claude).',
  proposals:
    "Claude's suggestions for how much of each coin to hold. They are only suggestions — the safety checks and, in live mode, you decide whether anything happens.",
  overlay:
    'A machine-local file that changes settings for this computer only, without touching the settings everyone shares.',
  bless:
    'Signing the protected settings so the system can prove they have not been edited behind its back. A change to them has to be re-signed before going live.',
  blessed:
    'The protected settings still match the signature taken when you last approved them.',
  'tier-1':
    'A file Claude may change only by proposing the change and having it reviewed and merged.',
  'tier-2':
    'A file only you may change. An automated Claude run is blocked from touching it at all.',
  seed:
    'The starting pot of money for a run, in USDT. Performance is measured from it.',
  nav: 'Net asset value: what the bot is worth right now, its cash plus everything it holds.',
  'step-up':
    'Re-entering your console token before something dangerous. It stays valid for a few minutes, then asks again.',
  'kill switch':
    'Stops all trading immediately and cancels what it can. Only a human can switch it back off.',
  drawdown: 'How far down you are from the best value the run has reached.',
  slippage:
    'The gap between the price the decision assumed and the price the order actually got.',
  bps: 'Basis points: one hundredth of a percent. 10 bps is 0.10%.',
  'dry run':
    'The bot places no real orders; fills are simulated from live prices. Nothing can be lost.',
  sim: 'Simulated. No order reached an exchange and no real money moved.',
  demo: 'Placed against the exchange test network with play money. Real order flow, fake funds.',
  live: 'Real orders with real money on the real exchange.',
  funnel:
    'How many candidate ideas survived each stage: spotted, screened, validated, acted on.',
  effort: 'How hard the model was told to think before answering.',
  escalation: 'Retrying a question with a stronger model after a weak answer.',
  invariant:
    'A safety rule enforced by code, not by settings. Nothing in the console can turn one off.',
  autonomy: 'How much the system is allowed to change about itself without asking you.',
  'walk-forward':
    'Testing a strategy on one stretch of history, then checking it on the stretch that came next, repeatedly.',
  tca: 'Trade cost analysis: what the trading actually cost in fees and slippage.',
  reconcile: "Comparing the system's own books against what the exchange says you hold.",
  'ops lock': 'A door lock that stops two jobs writing the same files at the same time.',
  flag: 'A switch the risk gate reads, such as a trading blackout for one coin.',
  incident: 'Something that went wrong and has not been closed off yet.',
};

/**
 * The builder words the owner should never meet bare, and what to say instead.
 *
 * The owner had to ask what "sleeve A" meant, so the rule is now mechanical: a word on
 * this list is either renamed to its plain form or wrapped in `<Explain>`, which attaches
 * the {@link GLOSSARY} sentence to it.  `src/test/vocabulary.test.tsx` sweeps every label
 * and blurb the console ships against this table, so a new screen cannot reintroduce one
 * by accident.
 *
 * Keys are lowercase and matched on word boundaries.  The value is the phrase that
 * replaces them, which is also what the failure message suggests.
 */
export const PLAIN_REPLACEMENTS: Record<string, string> = {
  sleeve: 'bot (Rules bot / AI bot)',
  sleeves: 'bots',
  'sleeve a': 'Rules bot (no AI)',
  'sleeve b': 'AI bot (Claude)',
  gate: 'safety check',
  'risk gate': 'safety check',
  proposal: "the AI's plan",
  proposals: "the AI's plans",
  abstain: 'chose not to trade',
  abstained: 'chose not to trade',
  'tier-1': 'Claude may propose a change here',
  'tier-2': 'only you may change this',
  'min_tier': 'the weakest model allowed',
  overlay: 'a setting for this computer only',
  bless: 'sign the protected settings',
  blessed: 'signed',
  funnel: 'how many ideas survived each step',
  tca: 'what the trading cost',
  nav: 'what it is worth',
};

/**
 * The words the sweep refuses to find in a label, a blurb or a navigation entry.
 *
 * "Print it with an explanation" is allowed anywhere in a table cell or a tooltip, via
 * `<Explain>`.  What is not allowed is a *name* — a menu item, a page subtitle, a stat
 * card's caption — that only makes sense if you built the thing.
 */
export const BUILDER_TERMS: string[] = Object.keys(PLAIN_REPLACEMENTS);

/** The builder words in `text`, lowercased, or `[]` when it reads plainly. */
export function builderTermsIn(text: string): string[] {
  const haystack = ` ${text.toLowerCase().replace(/[^a-z0-9_-]+/g, ' ').replace(/\s+/g, ' ').trim()} `;
  return BUILDER_TERMS.filter((term) => haystack.includes(` ${term} `));
}

/** The one-sentence explanation for a jargon word, or `null` when there is none. */
export function explain(term: string): string | null {
  const key = term.trim().toLowerCase();
  const direct = GLOSSARY[key];
  if (direct) return direct;
  const dashed = GLOSSARY[key.replace(/\s+/g, '-')];
  if (dashed) return dashed;
  const spaced = GLOSSARY[key.replace(/-/g, ' ')];
  if (spaced) return spaced;
  if (key.endsWith('s')) return GLOSSARY[key.slice(0, -1)] ?? null;
  return null;
}

/**
 * How an order or a fill actually reached the market.
 *
 * `SIM` and `LIVE` are the two the journal records today (`mode` on every order, fill and
 * position row).  `DEMO` exists because the operator is on exchange test-network keys and
 * a row that came from them must not read the same as one that spent real money — but it
 * is only ever shown when the row says so.  Guessing would be the dangerous direction:
 * labelling a real-money row "DEMO" is exactly the mistake this badge exists to prevent,
 * so anything unrecognised falls back to `SIM` only when it is also not live.
 */
export type ExecutionKind = 'sim' | 'demo' | 'live';

export function executionKind(mode: string | null | undefined): ExecutionKind {
  const value = String(mode ?? '').toLowerCase();
  if (value === 'live' || value === 'live_execute' || value === 'real') return 'live';
  if (value === 'demo' || value === 'testnet' || value === 'sandbox' || value === 'paper_exchange') {
    return 'demo';
  }
  return 'sim';
}

export const EXECUTION_LABEL: Record<ExecutionKind, string> = {
  sim: 'SIM',
  demo: 'DEMO',
  live: 'LIVE',
};

export const EXECUTION_COLOR: Record<ExecutionKind, string> = {
  sim: 'blue',
  demo: 'violet',
  live: 'red',
};

export const EXECUTION_HINT: Record<ExecutionKind, string> = {
  sim: 'Simulated: no order reached an exchange and no real money moved.',
  demo: 'Exchange test network: a real order was placed, with play money.',
  live: 'Real money on the real exchange.',
};
