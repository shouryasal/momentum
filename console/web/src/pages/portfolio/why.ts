/**
 * Why a trade happened, and who did it — in the owner's words.
 *
 * The bot's own database tags every order: an enter tag on a buy, an exit reason on a
 * sell. The server joins those onto the journal's fills (`fills.ft_order_id` is the bot's
 * `orders.order_id`) and adds the actor from the audit log. This module turns the tags into
 * sentences, so the two day-one events read as what they were:
 *
 *   - `force_exit` by `human:console` — someone typed SELL EVERYTHING on this console;
 *   - `target_zero` after `targets:no_proposal_ever` — the bot sold everything because it
 *     found it had no valid mandate.
 *
 * A fill no bot database knows about says so; it is never given a guessed reason.
 */

import type { FillRow } from './api';

export interface Why {
  /** One short line for a table cell. */
  short: string;
  /** The fuller sentence for a tooltip or a detail pane. */
  long: string;
  /** `human` when a person did it, `bot` when the rules did, `unknown` otherwise. */
  who: 'human' | 'bot' | 'unknown';
  /** True for the two kinds of event that were not the strategy: a hand flatten or a
   *  mandate-loss flatten. The tables mark these so they cannot pass for trades. */
  event: boolean;
}

const EXIT_WORDS: Record<string, [string, string]> = {
  partial_exit: ['profit rung', 'A take-profit rung sold part of the position.'],
  trailing_stop_loss: ['trailing stop', 'The trailing stop closed the rest after the price turned.'],
  exit_signal: ['trend-loss signal', 'The rules saw the trend end and sold.'],
  roi: ['time-based profit', 'The position reached its time-based profit target.'],
  stop_loss: ['stop loss', 'The stop loss fired.'],
  stoploss_on_exchange: ['stop loss', 'The stop loss fired on the exchange.'],
  emergency_exit: ['emergency exit', 'An emergency exit closed the position.'],
  liquidation: ['liquidation', 'The position was liquidated.'],
};

const ENTRY_WORDS: Record<string, [string, string]> = {
  dca: ['rules entry', 'The rules bot opened this position.'],
  scheduled_dca: ['rules top-up', 'The rules bot added to the position on schedule.'],
  fast_breakout: ['breakout entry', 'The fast rules bought the breakout.'],
  proposal: ["Claude's plan", 'Bought to follow the approved plan.'],
  rebalance: ['rebalance top-up', 'A rebalance topped the position up towards its target.'],
};

/** Plain words for `fill.reason` / `fill.actor` / `fill.cause`. */
export function whyOf(fill: Pick<FillRow, 'side' | 'reason' | 'actor' | 'cause'>): Why {
  const side = String(fill.side ?? '').toLowerCase();
  const reason = fill.reason ?? null;
  if (reason === null) {
    return {
      short: 'not recorded',
      long: 'No bot database knows this order, so its reason is not recorded rather than guessed.',
      who: 'unknown',
      event: false,
    };
  }

  if (side === 'sell' && reason === 'force_exit') {
    if (fill.actor === 'human:console') {
      return {
        short: 'SOLD BY HAND — SELL EVERYTHING on this console',
        long: 'Someone typed SELL EVERYTHING on this console and the bot sold the position at market. The strategy did not decide this.',
        who: 'human',
        event: true,
      };
    }
    return {
      short: fill.actor === 'unknown' ? 'forced sale (no record of who)' : 'forced sale',
      long: 'The position was force-sold. No audit row names the hand behind it.',
      who: 'unknown',
      event: true,
    };
  }

  if (side === 'sell' && reason === 'target_zero') {
    const cause = fill.cause ? ` (${fill.cause})` : '';
    return {
      short: 'BOT SOLD EVERYTHING — no valid mandate',
      long: `The bot found it had no valid plan to hold anything${cause} and sold the whole position. A plumbing fault, not a strategy decision.`,
      who: 'bot',
      event: true,
    };
  }

  const table = side === 'sell' ? EXIT_WORDS : ENTRY_WORDS;
  const words = table[reason];
  if (words) {
    return { short: words[0], long: words[1], who: 'bot', event: false };
  }
  return {
    short: reason.replace(/_/g, ' '),
    long: `The bot recorded this ${side === 'sell' ? 'exit' : 'entry'} as "${reason}".`,
    who: fill.actor === 'human:console' ? 'human' : 'bot',
    event: false,
  };
}
