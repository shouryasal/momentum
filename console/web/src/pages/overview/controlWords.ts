/**
 * The words the control says, and nothing that draws.
 *
 * Every sentence on the control is built here so it can be tested without a browser, and
 * so the two things the owner must never confuse cannot be phrased into one:
 *
 *   - **whose money** — simulated, the exchange practice account, or real;
 *   - **how much it does by itself** — off, watching, proposing, trading.
 *
 * The phrase "how much it does by itself" is the owner's own, and it is used in full
 * everywhere rather than shortened to a term that would need explaining.
 *
 * Nothing here restates a limit, a schedule or a price: those come from the server.
 */

import type {
  Acting,
  ActingFlag,
  BotSpend,
  BotView,
  ControlLevel,
  ControlPayload,
  JobLiveness,
  ModeView,
  SupervisorStatus,
  Verdict,
} from './control.api';

/* --------------------------------------------------------------------------- whose money */

export type MoneyKind = 'simulated' | 'practice' | 'real';

/**
 * Which kind of money a bot is playing with.
 *
 * An unverified mode file is simulated, always. That is the same fail-closed rule the
 * server keeps, and getting it wrong in the other direction — showing "real" when the file
 * could not be trusted — would be the more frightening bug, but showing "simulated" for
 * real money would be the more expensive one, so the two must agree exactly.
 */
export function moneyKind(mode: ModeView | undefined): MoneyKind {
  if (!mode || !mode.verified) return 'simulated';
  if (mode.is_live) return 'real';
  if (mode.is_demo) return 'practice';
  return 'simulated';
}

export const MONEY_LABEL: Record<MoneyKind, string> = {
  simulated: 'Play money',
  practice: 'Practice account',
  real: 'Real money',
};

export const MONEY_MEANING: Record<MoneyKind, string> = {
  simulated: 'Nothing reaches an exchange. Every buy and sell is pretend.',
  practice: 'Real orders on the exchange practice network, with money that is not real.',
  real: 'Real orders with your own money on the real exchange.',
};

export const MONEY_COLOR: Record<MoneyKind, string> = {
  simulated: 'blue',
  practice: 'violet',
  real: 'red',
};

/* ------------------------------------------------------ how much it does by itself */

/** The question this half of the control answers, in the owner's words. */
export const BY_ITSELF_QUESTION = 'How much it does by itself';

export const LEVEL_LABEL: Record<ControlLevel, string> = {
  off: 'Off',
  watching: 'Watching only',
  proposing: 'Planning, asks me first',
  trading: 'Trading on its own',
};

/**
 * One sentence per level, in plain words.
 *
 * The server sends its own `level_meaning`, which is written for the contract; these are
 * written for the person, and {@link levelMeaning} prefers these. Both say the same thing,
 * and `control.test.tsx` fails if a level ever loses either one.
 */
export const LEVEL_MEANING: Record<ControlLevel, string> = {
  off: 'Nothing runs for this bot. It will not look at prices, make a plan or buy anything.',
  watching:
    'It keeps prices and news up to date and keeps an eye on what it already holds. It makes no plans and buys nothing.',
  proposing:
    'It works out what it thinks you should hold, every scheduled run, and then waits for you to say yes before anything is bought or sold.',
  trading:
    'It works out what to hold and acts on it without asking. The safety checks still decide whether each order is allowed.',
};

export function levelMeaning(level: ControlLevel, fallback?: Record<string, string>): string {
  return LEVEL_MEANING[level] ?? fallback?.[level] ?? '';
}

export const LEVEL_COLOR: Record<ControlLevel, string> = {
  off: 'gray',
  watching: 'blue',
  proposing: 'yellow',
  trading: 'teal',
};

/** Why the level on screen is lower than the one that was asked for. */
export const CLAMP_REASON: Record<string, string> = {
  config_disabled: 'a setting has switched all of this off',
  config_ceiling: 'a setting caps how far this may go',
  kill_engaged: 'the emergency stop is on',
  untrusted_state: 'the saved setting could not be trusted, so it reads as off',
};

export function clampSentence(bot: BotView): string | null {
  if (!bot.clamped_by.length || bot.level === bot.requested) return null;
  const why = bot.clamped_by.map((key) => CLAMP_REASON[key] ?? key).join(', and ');
  return `You asked for ${LEVEL_LABEL[bot.requested] ?? bot.requested}, but ${why}.`;
}

/**
 * One line that says what this bot is doing, joining both halves.
 *
 * This is the sentence that makes it impossible to read "the bots are up" as "the system is
 * deciding": it names the money and the behaviour together, and it never says "running".
 */
export function botSentence(bot: BotView, mode: ModeView | undefined): string {
  const money = moneyKind(mode);
  if (bot.level === 'off') return 'Off. It does nothing until you start it.';
  if (bot.level === 'watching') {
    return 'Watching only. It keeps up with prices and what it holds, and buys nothing.';
  }
  if (bot.level === 'proposing') {
    return `It makes a plan on every scheduled run and waits for you. Nothing is bought or sold with ${MONEY_LABEL[money].toLowerCase()} until you approve it.`;
  }
  return `It makes a plan and acts on it without asking, with ${MONEY_LABEL[money].toLowerCase()}. Every order still has to pass the safety checks.`;
}

/* --------------------------------------------------------------------------- liveness */

/** Never a tick. `not_scheduled` and `never_ran` are failures of the loop, not of a job. */
export const VERDICT_TONE: Record<Verdict, 'good' | 'warn' | 'bad' | 'idle'> = {
  alive: 'good',
  late: 'warn',
  failing: 'bad',
  schedule_drifted: 'warn',
  not_scheduled: 'bad',
  never_ran: 'bad',
  blocked: 'bad',
  off: 'idle',
};

export const VERDICT_COLOR: Record<Verdict, string> = {
  alive: 'teal',
  late: 'yellow',
  failing: 'red',
  schedule_drifted: 'yellow',
  not_scheduled: 'red',
  never_ran: 'red',
  blocked: 'red',
  off: 'gray',
};

/** What a person calls each scheduled job. The raw name stays available for the logs. */
export const JOB_LABEL: Record<string, string> = {
  ingest: 'fetch new prices and news',
  scanner: 'look for something worth a closer look',
  watch: 'keep an eye on what it holds',
  nav_tick: 'record what each bot is worth',
  nav_job: 'the daily value record',
  reconcile: 'check its books against the exchange',
  tca_job: 'measure what the trading cost',
  healthcheck: 'check nothing has fallen over',
  research_run: 'work out what to hold',
  daily_review: "grade yesterday's decisions",
  review_run: 'the weekly review',
  backtest_data: 'refresh the price history',
  backup: 'back everything up',
  maintenance: 'weekly housekeeping',
};

export function jobLabel(job: string): string {
  return JOB_LABEL[job] ?? job.replace(/_/g, ' ');
}

/** `2026-09-24T04:30:00Z` → `Thu 08:30`, in the timezone the server names. */
export function whenText(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return '';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  try {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone,
    }).format(at);
  } catch {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(at);
  }
}

/** The soonest job that is allowed to run, or `null` when nothing may. */
export function nextRun(jobs: JobLiveness[]): JobLiveness | null {
  const due = jobs
    .filter((job) => job.permitted && job.next_fire)
    .sort((left, right) => String(left.next_fire).localeCompare(String(right.next_fire)));
  return due[0] ?? null;
}

/** True when at least one bot is allowed to work out what to hold. */
export function anythingDeciding(data: ControlPayload): boolean {
  return Object.values(data.bots ?? {}).some((bot) => bot.decides);
}

/**
 * The second line under the headline: what runs next and when, or why nothing will.
 *
 * Three rules, each of them a mistake this line used to make possible:
 *
 * 1. When nothing is scheduled it must not soften into "nothing due right now", which reads
 *    like a quiet Sunday. It says the loop is not running.
 * 2. When the loop *is* running but every bot is only watching, it says so first. This is
 *    the exact distinction the owner asked about — the bots being up is not the system
 *    deciding — and it is also what makes Pause visible here: the jobs still fire, the
 *    headline stays "alive", and this line is what changes.
 * 3. Otherwise it names the next job in words and the time it is due.
 */
export function nextRunSentence(data: ControlPayload): string {
  if (data.verdict === 'off') {
    return 'Both bots are off. Nothing is scheduled, and nothing will happen until you start one.';
  }
  if (data.verdict === 'not_scheduled') {
    return 'Nothing is scheduled on this machine — this system is not running by itself. Press Start to install the schedule.';
  }
  const next = nextRun(data.jobs);
  const when = next ? whenText(next.next_fire, data.timezone) : '';
  const nextText = next
    ? `Next: ${jobLabel(next.job)}${when ? `, ${when}` : ''}.`
    : 'Nothing is due to happen next, because no job is allowed to run yet.';
  if (!anythingDeciding(data)) {
    return `Nothing is deciding — every bot is watching only, so no plan will be made. ${nextText}`;
  }
  return nextText;
}

/** The headline the screen prints. The server's own words, kept verbatim. */
export function livenessHeadline(data: ControlPayload): string {
  return data.headline;
}

/* ------------------------------------------------------- trading is blocked (the banner) */

/**
 * The words for the state this console could not show on 2026-09-24.
 *
 * For fourteen hours the risk gate refused every entry — 655 of them, all
 * `blackout:data_stale` — behind a flag written with `expires_at: null`, and this screen
 * said "The loop is alive and on schedule", which was true and useless. So this is a
 * separate, red, first-class banner rather than a tone change on the liveness line: the
 * question "is it running" and the question "is it allowed to do anything" have different
 * answers and must have different places to be answered.
 *
 * It says four things, because an operator woken at 3am needs all four and nothing else:
 * what is blocked, why, since when, and what will ever clear it. The last one is the line
 * the original failure lacked entirely — a flag that cannot expire and nobody saying so.
 *
 * Returns `null` when nothing is blocked, so the banner simply does not exist on a healthy
 * host. It never softens: there is no arrangement of this function that turns
 * `not_trading` into a tick.
 */
export interface BlockedBanner {
  /** One sentence, the one a person reads first. */
  title: string;
  /** `label: value` rows under it, in the order they are needed. */
  facts: Array<{ label: string; value: string }>;
  /** Present only when the loop is simultaneously calling itself healthy. */
  contradiction: string | null;
}

export function blockedBanner(data: ControlPayload): BlockedBanner | null {
  const acting = data.acting;
  if (!acting || acting.verdict !== 'not_trading') return null;

  const facts: Array<{ label: string; value: string }> = [
    { label: 'What is stopped', value: acting.blocked_what ?? 'Every new buy, on both bots' },
  ];
  // The reason for the silence happening *now*, with the window's dominant reason as a
  // fallback. They differ on a host that was wedged yesterday and is fine today, and naming
  // the older one there would point the operator at a problem that is already fixed.
  const now = acting.current_words
    ? { words: acting.current_words, count: acting.refused_since_last_allowed }
    : acting.refusals[0]
      ? { words: acting.refusals[0].words, count: acting.refusals[0].count }
      : null;
  if (now) {
    facts.push({
      label: 'Why',
      value: `${now.words} — ${now.count} ${now.count === 1 ? 'buy' : 'buys'} refused since the last one got through (${acting.entries_refused} in the last ${acting.window_hours} hours)`,
    });
  }
  facts.push({
    label: 'Since',
    value: acting.blocked_since
      ? `${whenText(acting.blocked_since, data.timezone)} — ${humaniseMinutes(acting.blocked_minutes)} ago`
      : 'not recorded',
  });
  facts.push({
    label: 'Last buy actually allowed',
    value: acting.last_allowed_entry
      ? `${whenText(acting.last_allowed_entry, data.timezone)} (${humaniseMinutes(acting.minutes_since_allowed_entry)} ago)`
      : 'never on this machine',
  });
  facts.push({
    label: 'What will clear it',
    value: acting.clears_when ?? 'nobody has recorded what would lift this',
  });
  for (const flag of acting.blocking_flags) {
    facts.push({ label: `Flag "${flag.name}"`, value: flagSentence(flag, data.timezone) });
  }
  const stale = acting.sources.filter((source) => source.stale).map((source) => source.source);
  if (stale.length) {
    facts.push({ label: 'Stale data', value: stale.join(', ') });
  }
  for (const phase of acting.phases.filter((item) => item.failing)) {
    facts.push({
      label: `Fetching "${phase.phase}" is failing`,
      value: `last worked ${phase.last_ok ? whenText(phase.last_ok, data.timezone) : 'never'} — ${phase.last_error ?? 'no error recorded'}`,
    });
  }

  // The sentence that makes the banner impossible to dismiss as noise. `blocked` is now the
  // server's own verdict, so a payload still saying `alive` means two things are wrong.
  const contradiction = ['alive', 'late', 'failing'].includes(data.verdict)
    ? `Every scheduled job is running normally, and has been the whole time. That is why nothing looked wrong: "${data.headline}" was true and told you nothing.`
    : null;

  return { title: `TRADING IS BLOCKED — ${plainHeadline(acting)}`, facts, contradiction };
}

/** The server's own `Not trading: …` sentence, which is already written for a person. */
export function plainHeadline(acting: Acting): string {
  return acting.headline;
}

/**
 * One flag, said the way a person would ask about it — including whether it can ever lapse.
 *
 * The exit is said exactly once. A flag with no expiry gets the short, blunt sentence
 * (`It has no expiry, so it cannot lift itself.`) and not the server's longer `clears_when`,
 * which says the same thing at more length; a flag that *can* lapse gets the server's
 * wording, because the time it lapses at is the useful part.
 */
export function flagSentence(flag: ActingFlag, timeZone: string): string {
  const set = flag.set_at ? whenText(flag.set_at, timeZone) : 'an unrecorded time';
  const age = flag.active_minutes === null ? '' : ` (${humaniseMinutes(flag.active_minutes)})`;
  const exit = flag.can_expire
    ? ` ${flag.clears_when}`
    : ' It has no expiry, so it cannot lift itself — only whatever set it can.';
  return `set by ${flag.set_by ?? 'something unrecorded'} at ${set}${age}: ${flag.reason ?? 'no reason recorded'}.${exit}`;
}

/** `95` → `1h 35m`. Minutes are what the server sends; hours are what a person thinks in. */
export function humaniseMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return 'an unknown time';
  if (minutes < 60) return `${Math.floor(minutes)}m`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.floor(minutes % 60);
  if (hours < 24) return `${hours}h ${String(rest).padStart(2, '0')}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/**
 * The one-line answer to "did it actually do anything", for a host that is not blocked.
 *
 * It sits under the liveness line because "on schedule" and "achieving something" are
 * different claims, and until this existed only the first one was ever on screen.
 */
export function actingSentence(data: ControlPayload): string | null {
  const acting = data.acting;
  if (!acting) {
    return data.acting_error
      ? `Cannot tell whether anything has actually been bought or refused: ${data.acting_error}`
      : null;
  }
  const window = `the last ${acting.window_hours} hours`;
  if (acting.verdict === 'idle') return null;
  if (acting.verdict === 'unknown') return acting.headline;
  if (acting.verdict === 'not_trading') return null; // the banner says it, louder.
  if (acting.verdict === 'quiet') {
    return `Nothing is blocked, and nothing has come up: no buy was even proposed in ${window}.`;
  }
  const last = acting.last_allowed_entry
    ? ` Last one ${humaniseMinutes(acting.minutes_since_allowed_entry)} ago.`
    : '';
  return `${acting.entries_allowed} ${acting.entries_allowed === 1 ? 'buy' : 'buys'} allowed and ${acting.entries_refused} refused in ${window}.${last}`;
}

/* --------------------------------------------------------------- will it come back up? */

/**
 * Whether anything will restart the console when it dies, said plainly.
 *
 * On the night this was written the console process had died and nothing brought it back,
 * so the one surface that could have shown a red state was not there to show it. `unknown`
 * deliberately says nothing: a warning that fires because a probe could not answer is a
 * warning people learn to ignore, and this one has to keep working.
 */
export function supervisorWarning(
  supervisor: SupervisorStatus | null | undefined,
): { tone: 'bad' | 'warn'; text: string; canInstall: boolean } | null {
  if (!supervisor) return null;
  if (supervisor.verdict === 'unsupervised') {
    return {
      tone: 'warn',
      canInstall: true,
      text: 'Nothing will restart this console if it stops. It has already happened once, and while it was down nothing could tell you the system had stopped trading.',
    };
  }
  if (supervisor.verdict === 'failing') {
    return {
      tone: 'bad',
      canInstall: true,
      text: `The console's restarter is installed but not running (${supervisor.active ?? 'unknown'}). If this console stops, it stays stopped.`,
    };
  }
  return null;
}

/** One line for the good case, so "it will come back" is visible rather than assumed. */
export function supervisorGoodNews(
  supervisor: SupervisorStatus | null | undefined,
): string | null {
  if (!supervisor || supervisor.verdict !== 'supervised') return null;
  return `If this console stops, ${supervisor.unit} starts it again within seconds.`;
}

/** Jobs an operator should see first: the ones that are allowed and are not fine. */
export function troubleJobs(jobs: JobLiveness[]): JobLiveness[] {
  return jobs.filter((job) => job.permitted && ['late', 'failing', 'never'].includes(job.verdict));
}

/** How many jobs to name before summarising. A wall of rows is not a warning. */
export const TROUBLE_SHOWN = 3;

/**
 * The trouble list, said once rather than nine times.
 *
 * On a host that has just been started every job is "never run", and listing all of them
 * filled the card with the same sentence repeated — which buries the one case that is
 * genuinely interesting, a single job failing while the rest are fine. So when everything
 * is in the same state it is one line; otherwise it names the first few and counts the rest.
 */
export function troubleSummary(jobs: JobLiveness[]): { lines: string[]; more: number } | null {
  const trouble = troubleJobs(jobs);
  if (!trouble.length) return null;
  const permitted = jobs.filter((job) => job.permitted);
  if (trouble.length === permitted.length && trouble.every((job) => job.verdict === 'never')) {
    return {
      lines: [
        `None of the ${trouble.length} scheduled jobs has run yet, so nothing has happened by itself so far.`,
      ],
      more: 0,
    };
  }
  const shown = trouble.slice(0, TROUBLE_SHOWN);
  return {
    lines: shown.map((job) => `${jobLabel(job.job)} — ${job.note ?? job.verdict}`),
    more: trouble.length - shown.length,
  };
}

/* --------------------------------------------------------------------------- spend */

export function money(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

/**
 * Spend against the ceiling, in one line, beside the switch that causes it.
 *
 * It sits on the control rather than on a builder's page because turning a bot up to
 * "planning" is the act that starts the spending, and a ceiling you only meet on another
 * screen is a ceiling you meet by surprise.
 */
/** At either ceiling. The server sends the two separately; `over` is not on the wire. */
export function atCeiling(spend: BotSpend | undefined): boolean {
  return Boolean(spend && (spend.over_day || spend.over_month));
}

export function spendSentence(spend: BotSpend | undefined): string {
  if (!spend) return 'Not measured yet.';
  const month = spend.month_cap
    ? `${money(spend.month_usd)} of ${money(spend.month_cap)} this month`
    : `${money(spend.month_usd)} this month`;
  const day = spend.day_cap ? `, ${money(spend.day_usd)} of ${money(spend.day_cap)} today` : '';
  if (atCeiling(spend)) {
    return `${month}${day} — at the ceiling, so ${
      spend.action === 'hold' ? 'model work stops until it resets' : 'it drops to cheaper models'
    }.`;
  }
  return `${month}${day}.`;
}

export function spendTone(spend: BotSpend | undefined): 'ok' | 'warn' | 'over' {
  if (!spend) return 'ok';
  if (atCeiling(spend)) return 'over';
  const pct = Math.max(spend.month_pct ?? 0, spend.day_pct ?? 0);
  return pct >= 80 ? 'warn' : 'ok';
}

/* --------------------------------------------------------------------------- actions */

/** What the primary button does next for this bot, given where it is. */
export function primaryAction(bot: BotView): { label: string; level: ControlLevel } | null {
  if (bot.level === 'off') {
    return { label: 'Start', level: bot.resume_level ?? 'watching' };
  }
  return null;
}

/** Whether the typed live phrase is needed to move this bot to this level. */
export function needsLivePhrase(level: ControlLevel, mode: ModeView | undefined): boolean {
  return level === 'trading' && moneyKind(mode) === 'real';
}

/**
 * What a move is about to do, said before it is made.
 *
 * Every dialog on the control opens with one of these, because "are you sure?" is not a
 * question anybody can answer.
 */
export function moveWarning(level: ControlLevel, mode: ModeView | undefined): string {
  const money = moneyKind(mode);
  if (level === 'off') return 'This bot will stop doing anything on its own. Nothing is sold.';
  if (level === 'watching') {
    return 'It will keep up with prices and what it holds. It will make no plans and buy nothing.';
  }
  if (level === 'proposing') {
    return 'It will start working out what to hold on every scheduled run. Nothing is bought or sold until you approve it. This starts spending on model calls.';
  }
  return money === 'real'
    ? 'It will buy and sell with your real money without asking you first. The safety checks still apply to every order, and the emergency stop still overrides everything.'
    : `It will buy and sell on its own with ${MONEY_LABEL[money].toLowerCase()}. The safety checks still apply to every order.`;
}
