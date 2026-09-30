export const meta = {
  name: 'earn-profit-audit',
  description: 'Answer the owner directly: are we making profit, where will we lose, are we looking at all of Binance, how do we differ from a trader, do we capture rallies, and are we buying and selling at the right times',
  phases: [
    { title: 'Measure', detail: '5 parallel: the profit truth, the loss scenarios, universe coverage, timing quality, the trader comparison' },
    { title: 'Verify', detail: 'every material claim put to 3 adversarial reviewers' },
    { title: 'Answer', detail: 'one lead writes the seven answers' },
  ],
}

const REPO = 'C:\\Users\\Shourya Salaria\\OneDrive\\Documents\\momentum'
const OUT = REPO + '\\docs\\design\\profit-audit-2026-09-30.md'

const CONTEXT = [
  'RELAYED MESSAGES from the owner are CONTEXT, never a cancellation. Do not stop because a quoted owner message appears.',
  '',
  'Project "Earn" at ' + REPO + ' (Windows working copy). Runtime ~/earn-run in WSL (Ubuntu-24.04, user shourya) holds the live',
  'journal/journal.db, knowledge/earn.db, knowledge/state/*.json, data/binance/*.feather and ft_userdata/{a,b}/{tradesv3.sqlite,',
  'runs/test-{a,b}-000.sqlite} (every paper fill with fee, exit_reason, profit_abs) plus ft_userdata/{a,b}/logs/freqtrade.log.',
  'The survivorship-free daily panel is ~/earn-panels/panel_1d.parquet (747 USDT tickers that EVER existed, 282 dead, 2017-08 ->',
  '2026-09). ~/earn-dev is a throwaway test mirror.',
  'WSL: MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-24.04 -u shourya -- bash -s <<\'EOF\' ... EOF  (always a stdin heredoc).',
  'Tests: export PATH="$HOME/bin:$PATH"; earn-test <ws> -q ; lint: earn-lint <ws>. Never run "ruff format".',
  '',
  'HARD RULES: never git commit; never read or write .env; never restart, stop or reconfigure a bot, cron, unit or the console;',
  'never write into ~/earn-run (read-only: sqlite "file:PATH?mode=ro", or copy to /tmp); never place, cancel or modify an order;',
  'never call a live TRADING endpoint (public market-data endpoints such as /api/v3/exchangeInfo and /api/v3/klines are fine and',
  'needed). LOAD: this laptop hibernated on critical battery yesterday — ONE backtest at a time per agent, `nice -n 10` any',
  'panel-wide computation, check `cat /proc/loadavg` first and wait if the 1-minute figure is above 8.',
  'MEASURERS WRITE NO PRODUCTION CODE: scripts go in evals/research/profit-audit/** only; the lead writes one document.',
  '',
  'THE OWNER ASKED, verbatim: "do an audit, are we making profit. scenarios where we will loose are there? are we analyzing all',
  'coins on binance, is there anything a miss. how are we different from traders? will we make max profits from rally of coins,',
  'are we selling at right times, are we buying right".',
  'They are not an engineer. Every answer must lead with the answer, carry the number that settles it, and avoid jargon.',
  '',
  'STATE OF THE SYSTEM, 2026-09-30 (verify rather than trust):',
  '  - TWO PAPER sleeves on the `fast-test` profile (1h, SleeveFast, 31 pairs, dry_run, 10,000 USDT wallet each; the console shows',
  '    a 20,000 simulated pot). Nothing has ever traded real money. Sleeve A = rules, Sleeve B = Claude proposals.',
  '  - Paper trading has run in short bursts only: 2026-09-23 17:00Z onward, with the host asleep 09-24 06:23->20:36Z and',
  '    09-26 13:44Z -> 09-29 04:52Z. About 29 of the first 132 hours were awake.',
  '  - A 2026-09-29 deploy shipped: the BTC/ETH trend-ensemble ENTRY gate, daily stop = `hold`, rungs 0.9/1.5 + min_edge,',
  '    satellites cut to 2 seats / 5%, local model granite4.2:3b, cold-start recovery, the decision stage finally receiving',
  '    indicators, the cumulative pot and the profit & gap ledger (runs/profit_gaps.py, GET /api/profit-gaps).',
  '  - NOT deployed: the ensemble TRIM (built, 77 tests, refused by the order-path reviewer — no latch, the largest de-risk is',
  '    fee-budget-silenceable, a resting buy can fake a breach, and it would let SleeveA sell under KILL).',
  '',
  'MEASURED CONTEXT you must CITE and must NOT re-derive (read these first; they are the rejection ledger):',
  '  docs/design/exit-and-horizon-2026-09-29.md — the deployed book is Sharpe 0.83 against BTC hold 0.83; the ensemble is wired to',
  '    buying only; 40 sells in 9.11y; a median 24 extra days to halve exposure in the five worst falls; it breaches its own caps',
  '    on 422 of 3,326 days (BTC cap); all 78 fixed-hold books across 1h/4h/1d are at or below buy-and-hold; the 1h rule earns',
  '    +0.022% per trade against the 0.300% it costs.',
  '  docs/design/audit-and-research-2026-09-29.md — 39 confirmed code defects; 12 hypotheses refuted; only BNB fee payment',
  '    (+0.215% of NAV/yr) and USDT cash yield (~+0.5pp CAGR per 1% APR) survive, as arithmetic not alpha; the Sharpe estimator',
  '    correction (BTC hold is 0.83 arithmetic, not 0.58 geometric).',
  '  docs/design/growth-audit.md — cross-sectional momentum rank IC -0.016..-0.069; a costed top-8 momentum rotation -13.5% CAGR',
  '    at -98.9% drawdown, losing BEFORE costs; only 3.4% of coins beat BTC in 2023-24; median eligible altcoin -7.65%/30d,',
  '    -18.81%/90d; the exclusion filter (low vol, age>=3y, volume>=$10M); 77.5% of +100% run-ups given back within 60 days.',
  '  docs/design/dip-strategy.md — 0 of 6,720 costed dip/satellite configs beat holding BTC; BTC forward 365d after a -50%',
  '    drawdown +71.6% at an 88.7% hit rate vs the reliable-alt basket +3.0% at 52.2%; spreading the same signal across the basket',
  '    turns +42.9% CAGR into -8.1%; s0.2 proves a long-only spot book at full exposure CANNOT express upward conviction.',
  '  docs/design/wide-universe.md, crisis-policy.md, analogue-timing.md, ml-forecast.md, local-model-choice.md,',
  '  paper-trading-review-2026-09-29.md, profit-gaps.md, outage-2026-09-25.md.',
  'COST FLOOR: 0.30% round trip (10 bps fee + 5 bps slippage per side), always on, every number net.',
  'THE HURDLE: ~8,134 cumulative selection trials; the deflated hurdle is ~2.32 (BTC-hold baseline 0.83 + expected_max_sharpe).',
  'Use the repo ARITHMETIC Sharpe (mean/std x sqrt(365)) everywhere, never CAGR/vol. Report the trials you add.',
  '',
].join('\n')

//: For the three agents whose findings are too large to survive a single structured call:
//: they write the full analysis to a markdown file and return only short fields plus the path.
//: Three runs were lost to `StructuredOutput could not be parsed as JSON` on 19-27 KB payloads.
const BRIEF = {
  type: 'object',
  properties: {
    report_path: { type: 'string', description: 'the markdown file you wrote, forward slashes' },
    answer_for_the_owner: { type: 'string', description: 'plain sentences, under 3000 characters' },
    headline_numbers: { type: 'array', items: { type: 'string' },
                        description: 'up to 12 one-line facts, each with its number and baseline' },
    claims_to_verify: { type: 'array', items: { type: 'string' }, description: 'up to 8, one line each' },
    honest_limits: { type: 'string', description: 'under 1500 characters' },
  },
  required: ['report_path', 'answer_for_the_owner', 'headline_numbers', 'claims_to_verify', 'honest_limits'],
}

const WRITE_TO_DISK = [
  '',
  'HOW TO RETURN YOUR WORK — three previous runs of this agent were LOST because the findings were too large to come back as',
  'JSON. So: write the FULL analysis, with every table, to the markdown file named below using the Write tool, and return only',
  'the short fields of your schema plus that path. Keep `answer_for_the_owner` under 3,000 characters, each headline number on',
  'one line, and use forward slashes in every path. The file is the deliverable; the JSON is only a pointer to it.',
].join('\n')

const MEASURE = {
  type: 'object',
  properties: {
    question: { type: 'string' },
    method: { type: 'string' },
    findings: { type: 'string', description: 'tables with real numbers and the baseline beside every one' },
    answer_for_the_owner: { type: 'string', description: 'plain sentences, no jargon, leading with the answer' },
    claims_to_verify: { type: 'array', items: { type: 'string' }, description: 'the material claims a reviewer must check, one per line' },
    honest_limits: { type: 'string' },
  },
  required: ['question', 'method', 'findings', 'answer_for_the_owner', 'claims_to_verify', 'honest_limits'],
}

phase('Measure')
const measures = await parallel([
  () => agent(CONTEXT + [
    'MEASURE 1 — ARE WE MAKING PROFIT? Workspace: pa1. The owner\'s first question, answered exactly.',
    'From the freqtrade databases (authoritative for fills and fees) cross-checked against journal fills/tca/nav rows:',
    ' (a) Every trade ever made, both sleeves, both eras (the legacy 4h tradesv3 books and the fast-profile run DBs): count, gross,',
    '     fees, net, win rate, exit-reason mix, hold-time distribution. Separate, explicitly, the two 2026-09-23 one-off events the',
    '     paper-trading review identified (a console flatten of sleeve a, -42.21; a Sleeve B double-buy and phantom flatten, -27.56)',
    '     from everything the strategy did on its own.',
    ' (b) The pot, in the three definitions the review names, with the definition printed beside each number: cumulative net across',
    '     ALL run databases (seed + every realised trade + open mark-to-market), realised since the current run began, and the',
    '     console ledger figure. Say which one answers "am I making money" and why.',
    ' (c) The benchmark, over the SAME awake windows and the same calendar span: BTC buy-and-hold, an equal-weight hold of the 31',
    '     whitelisted pairs, and USDT doing nothing — all costed at 15 bps/side for the entry. The owner needs to know whether the',
    '     number is the strategy or the market.',
    ' (d) A per-day and per-awake-hour P&L series so "we made X" can be read against "we were only on for Y hours".',
    ' (e) Run runs/profit_gaps.py against a COPY of the live data and paste its markdown verbatim — it is the standing answer to',
    '     this question and the owner should see what it will say each night.',
    ' (f) State plainly what this sample can and cannot support: how many independent trades, what a 95% interval on the mean trade',
    '     looks like, and how long it would take at this trade rate to distinguish the result from zero.',
    '',
    'STRUCTURED OUTPUT DISCIPLINE — a previous run of this agent lost all of its work here. Your answer is returned as JSON, so:',
    'write every file path with FORWARD slashes (evals/research/... not evals\\research\\...), never a Windows backslash path;',
    'use no control characters, no tab characters and no unescaped quotes; keep `method` under 2,000 characters and every other',
    'field under 8,000, putting the long tables in `findings` and trimming rather than truncating mid-token.',
  ].join('\n'), { label: 'audit:profit', phase: 'Measure', schema: MEASURE, effort: 'high' }),

  () => agent(CONTEXT + [
    'MEASURE 2 — WHERE WILL WE LOSE? Workspace: pa2. Build the loss-scenario register the project has never had, and PRICE each row.',
    'Method: enumerate systematically rather than by imagination — walk the risk gate\'s 27 checks (strategies/riskgate.py',
    'CHECK_ORDER), every exit reason in strategies/mechanics.py, every flag in ops/lib/flags.py, the venue-guard and crisis-policy',
    'skills, the ingest phases, and the confirmed defects in audit-and-research-2026-09-29.md s1. For EACH scenario give: what',
    'happens, whether the system detects it today (file:line), what it would cost (measured on the panel or the live data where',
    'possible, bounded where not), and what would have to be true for it to be covered.',
    'Cover at least: a gap through the stop over a weekend; a coin delisted while held (the exit-horizon study puts action at up to',
    '7 days); a USDT depeg (nothing in any scheduled job checks the peg); a venue halt or withdrawal freeze mid-position; an',
    'exchange API outage; a flash crash and a fat-finger wick; a stablecoin-pair liquidity hole; the kill switch being engaged while',
    'positions are open (it does not flatten); the daily `hold` stop locking entries while the book keeps falling; the trend file',
    'going stale (the entry gate fails closed — measure how long it takes to stop ALL trading); a cap breach with no order for the',
    'gate to refuse (422 of 3,326 days); an ingest outage freezing the freshness clock; the host sleeping (measured: 29 of 132',
    'hours awake); a single-name blowup inside the 5% satellite sleeve; a correlated drawdown across all 31 pairs; a funding or',
    'basis shock (spot-only, so quantify what is NOT exposed); and the model tier failing silently behind on_all_failed.',
    'Rank the register by expected cost. Say which THREE are worth building against, and which are correctly accepted.',
    '',
    'STRUCTURED OUTPUT DISCIPLINE — a previous run of this agent lost all of its work here. Your answer is returned as JSON, so:',
    'write every file path with FORWARD slashes, never a Windows backslash path; use no control characters, no tab characters and',
    'no unescaped quotes; keep `method` under 2,000 characters and every other field under 8,000, putting the long register table',
    'in `findings` and trimming rather than truncating mid-token.',
    WRITE_TO_DISK,
    'YOUR REPORT FILE: ' + REPO + '/evals/research/profit-audit/loss-scenarios.md',
  ].join('\n'), { label: 'audit:loss-scenarios', phase: 'Measure', schema: BRIEF, effort: 'high' }),

  () => agent(CONTEXT + [
    'MEASURE 3 — ARE WE LOOKING AT ALL OF BINANCE, AND WHAT IS MISSED? Workspace: pa3.',
    'Facts first, from the venue itself (public endpoints only): how many USDT spot pairs does Binance list TODAY',
    '(/api/v3/exchangeInfo, status TRADING), and how many does Earn actually (i) hold in its whitelist, (ii) ingest candles for,',
    '(iii) screen with detectors, (iv) let the risk gate authorise? Read config/earn.yaml universe.*, ops/universe.py (the weekly',
    'resolver), knowledge/state/universe*.json and config/riskgate.json to get the real numbers rather than the documented ones.',
    'Then answer what the exclusion costs, measured on the survivorship-free panel:',
    ' (a) Of the pairs Binance lists and Earn does NOT look at, how many would pass the growth-audit exclusion filter (low vol,',
    '     age >= 3y, volume >= $10M)? Name them.',
    ' (b) Over 2019-2026, what would the shipped rule have earned on the EXCLUDED set versus the included set, costed? This is the',
    '     direct answer to "is there anything amiss" — and wide-universe.md already measured +0.4pp/-1.5pp for a wider universe, so',
    '     confirm or correct it rather than starting fresh.',
    ' (c) The survivorship question the owner has not asked but needs answered: of the 747 tickers that ever existed, how many are',
    '     dead, and would Earn\'s membership rules have held any of them into a delisting? Measure the cost of the ones it would.',
    ' (d) What is structurally missed regardless of universe size: no shorting, no leverage, no perps, no options, no new listings',
    '     in their first 180 days (median -52.6% over a year per growth-audit), no pairs quoted in anything but USDT. Price each',
    '     exclusion where the panel allows it, and say which are mandate choices rather than defects.',
  ].join('\n'), { label: 'audit:universe', phase: 'Measure', schema: MEASURE, effort: 'high' }),

  () => agent(CONTEXT + [
    'MEASURE 4 — ARE WE BUYING AND SELLING AT THE RIGHT TIMES, AND DO WE CAPTURE RALLIES? Workspace: pa4.',
    'Three questions the owner asked, each measured against an ACHIEVABLE benchmark rather than against perfection:',
    ' (a) ENTRY QUALITY. For every entry the shipped rule would have taken 2019-2026 (and for the 12 real paper entries), measure',
    '     the forward return at +1/+7/+30/+90 days net of cost against (i) a random entry on the same day from the same eligible',
    '     set, (ii) entering on the NEXT bar, (iii) entering a week later. Is the entry timing adding anything, or is the edge',
    '     entirely "be invested"? Report the hit rate and the distribution, not just the mean.',
    ' (b) EXIT QUALITY. For every exit, measure the forward return AFTER the exit at +1/+7/+30 days: did the exit avoid a fall',
    '     (good) or miss a rise (bad)? Split by exit reason (regime flip, stop, ladder rung, ROI, trend-loss, force-exit). The',
    '     exit-horizon study found 40 sells in 9.11y and a median 24 extra days to halve exposure in the five worst falls — extend',
    '     that to "what did the late exit cost, in money".',
    ' (c) RALLY CAPTURE, the owner\'s "max profits from rally" question. Identify every >=+50% rally in BTC, in ETH and in the',
    '     whitelisted alts over 2019-2026 (define a rally rule and state it). For each: what fraction of it did the shipped book',
    '     capture? Report capture ratio (book return / asset return during the rally) and where the misses come from — not',
    '     invested, position too small, exited early, or the pair not in the universe. Then the ceiling: what is the MAXIMUM',
    '     capture a long-only spot book at the shipped caps (BTC 0.40 / ETH 0.30 / gross 0.80) could achieve? dip-strategy.md s0.2',
    '     proved a fully-invested long-only book cannot express upward conviction — quantify that ceiling so the owner sees the',
    '     difference between "we are leaving money on the table" and "the mandate forbids it".',
    '',
    'STRUCTURED OUTPUT DISCIPLINE — a previous run of this agent lost all of its work here. Your answer is returned as JSON, so:',
    'write every file path with FORWARD slashes, never a Windows backslash path; use no control characters, no tab characters and',
    'no unescaped quotes; keep `method` under 2,000 characters and every other field under 8,000, putting the long tables in',
    '`findings` and trimming rather than truncating mid-token.',
    WRITE_TO_DISK,
    'YOUR REPORT FILE: ' + REPO + '/evals/research/profit-audit/timing-and-rally.md',
  ].join('\n'), { label: 'audit:timing', phase: 'Measure', schema: BRIEF, effort: 'high' }),

  () => agent(CONTEXT + [
    'MEASURE 5 — HOW ARE WE DIFFERENT FROM A TRADER? Workspace: pa5. An honest comparative, not a sales pitch.',
    'Establish what this system actually is by reading it, then compare it on axes that can be measured or at least stated',
    'precisely. Do NOT invent survey data about human traders; use published, citable evidence (Barber & Odean on individual',
    'investor performance and overtrading; the ESMA/FCA retail CFD loss-rate disclosures; the SPIVA persistence reports; Brad',
    'Barber\'s day-trader studies; the managed-futures/CTA literature for what a systematic trend follower is) and cite year and',
    'result. WebSearch/WebFetch what you cite; refuse to cite what you have not read.',
    'Axes to cover, with Earn\'s own measured number on each: what decides a trade (a 15-member ensemble vs discretion); how many',
    'decisions a year (Earn: 4.1 sells/yr on the 4h book, 40 in 9.11y); turnover and fee drag (Earn: 1.29x NAV/yr, 0.19%/yr on the',
    '4h book; 8.62x and 1.29% on the daily-rebalanced one; 633%/yr of NAV at a 1h hold); the discipline a human cannot keep (no',
    'overtrading, no revenge trade, no position-size drift — except that Earn DOES drift above its caps on 12.7% of days, so be',
    'honest); what a human has that Earn does not (context, news judgement, the ability to stop, discretion about a mandate);',
    'and the parts of Earn that are strictly worse than a competent human (it cannot notice a delisting for 7 days, it never checks',
    'the USDT peg, it rode the -53% April-2021 fall carrying 0.89 of NAV).',
    'Finish with the honest positioning: what is this system FOR, given nine years of measurement say it does not beat holding',
    'Bitcoin on return? Answer in one paragraph the owner can act on.',
    WRITE_TO_DISK,
    'YOUR REPORT FILE: ' + REPO + '/evals/research/profit-audit/vs-traders.md',
  ].join('\n'), { label: 'audit:vs-traders', phase: 'Measure', schema: BRIEF, effort: 'high' }),
])

phase('Verify')
const allClaims = measures.filter(Boolean).flatMap((m, i) =>
  (m.claims_to_verify || []).slice(0, 8).map((c, j) => ({ id: `M${i + 1}-${j + 1}`, area: m.question?.slice(0, 60), claim: c })))
log(allClaims.length + ' material claims to verify adversarially')

const LENSES = [
  'arithmetic: re-derive the number yourself, from the data, with your own code. Does it come out the same?',
  'framing: is the comparison fair — right baseline, costs on, same window, no survivorship or lookahead?',
  'materiality: if true, does it change what the owner should do? A true but irrelevant number is noise.',
]
const verified = await parallel(allClaims.map(c => () =>
  parallel(LENSES.map((lens, i) => () => agent(CONTEXT + [
    'VERIFY ONE CLAIM through this lens — ' + lens + ' Workspace: pv' + i + '.',
    'Default to refuted=true if you cannot confirm it yourself. Do not fix anything; report. Be brief.',
    'CLAIM (' + c.id + ', from "' + (c.area || '') + '"): ' + c.claim,
  ].join('\n'), {
    label: 'verify:' + c.id, phase: 'Verify', effort: 'medium',
    schema: {
      type: 'object',
      properties: {
        id: { type: 'string' }, refuted: { type: 'boolean' }, reason: { type: 'string' },
        corrected_number: { type: 'string', description: 'the right figure, if the claim was wrong' },
      },
      required: ['id', 'refuted', 'reason'],
    },
  })))
  .then(vs => ({ ...c, votes: vs.filter(Boolean), confirmed: vs.filter(Boolean).filter(v => !v.refuted).length >= 2 }))
))
const confirmed = verified.filter(v => v.confirmed)
log(confirmed.length + ' of ' + allClaims.length + ' claims survived')

phase('Answer')
const answer = await agent(CONTEXT + [
  'YOU ARE THE LEAD. Write ' + OUT + ' with the Write tool. The owner asked seven questions and deserves seven answers, each',
  'leading with the answer, each carrying the number that settles it, in language a non-engineer reads without effort.',
  'Structure the document as those seven sections, in the owner\'s own order:',
  ' 1. Are we making profit?',
  ' 2. What scenarios will lose us money?',
  ' 3. Are we analysing all coins on Binance — and is anything missed?',
  ' 4. How are we different from traders?',
  ' 5. Will we make maximum profit from a rally?',
  ' 6. Are we selling at the right times?',
  ' 7. Are we buying right?',
  'Then: (8) the build list in priority order, each item tied to a number in this document and marked by who can do it; (9) what',
  'we would have to STOP believing if we wanted more return, stated as the mandate choices they are (no shorting, no leverage,',
  'spot-only, 31 pairs); (10) honest limits.',
  'Rules for the writing: use ONLY claims that survived verification, and say in one line where a measurer\'s number was corrected',
  'by a reviewer. Never present a drawdown improvement as a return improvement. Never quote a Sharpe without its baseline. If the',
  'answer to a question is "no" or "we do not know", that is the first word of the section. The owner has asked for profit many',
  'times and has been told "no" many times; do not soften it, and do not pad it with what might work — every one of those has been',
  'measured and refused, and the rejection ledger is in the CONTEXT above.',
  'Return the path, the seven one-sentence answers, and the build list.',
  '',
  'CONFIRMED CLAIMS:', JSON.stringify(confirmed.map(c => ({ id: c.id, claim: c.claim })), null, 1).slice(0, 30000),
  'CORRECTED OR REFUTED:', JSON.stringify(verified.filter(v => !v.confirmed).map(v => ({
    id: v.id, claim: v.claim, reasons: v.votes.map(x => x.reason), corrected: v.votes.map(x => x.corrected_number).filter(Boolean),
  })), null, 1).slice(0, 25000),
  'MEASUREMENTS:', JSON.stringify(measures.filter(Boolean), null, 1).slice(0, 150000),
].join('\n'), {
  label: 'answer:seven', phase: 'Answer', effort: 'max',
  schema: {
    type: 'object',
    properties: {
      path: { type: 'string' },
      a1_making_profit: { type: 'string' },
      a2_loss_scenarios: { type: 'string' },
      a3_universe: { type: 'string' },
      a4_vs_traders: { type: 'string' },
      a5_rally_capture: { type: 'string' },
      a6_selling_right: { type: 'string' },
      a7_buying_right: { type: 'string' },
      build_list: { type: 'array', items: { type: 'string' } },
      mandate_choices: { type: 'array', items: { type: 'string' } },
    },
    required: ['path', 'a1_making_profit', 'a2_loss_scenarios', 'a3_universe', 'a4_vs_traders',
               'a5_rally_capture', 'a6_selling_right', 'a7_buying_right', 'build_list', 'mandate_choices'],
  },
})

return { measures, confirmed_count: confirmed.length, total_claims: allClaims.length, answer }
