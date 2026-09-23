import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import type { Anchors, GateDecision, LimitRow, RiskOverview } from './api';
import { GateLog } from './components/GateLog';
import { LimitsTable } from './components/LimitsTable';
import { MeterBar, fmt } from './components/MeterBar';
import { ResumeMonthlyModal } from './components/ResumeMonthlyModal';
import RiskPage from './index';

const ANCHORS: Anchors = {
  sleeve: 'a',
  monthly_locked: true,
  monthly_locked_month: '2026-09',
  monthly_resumed_utc: '',
  month_anchor_nav: 10000,
  month_anchor_month: '2026-09',
  day_anchor_nav: 9800,
  day_anchor_date: '2026-09-22',
  locked_until: '',
  monthly_loss_stop: 0.1,
  daily_loss_stop: 0.03,
  confirm_phrase: 'RESUME SLEEVE A',
};

const MECHANICS = {
  sleeve: 'a',
  timeframe: '4h',
  startup_candles: 1920,
  mode: 'test',
  run_id: 'test-a-1',
  trading: {
    sizing_mode: 'target_weight',
    order_types: { entry: 'limit', exit: 'limit' },
    stoploss: { fixed_pct: 0.1, on_exchange: 'auto', reentry_cooldown_hours: 24,
      trailing: { enabled: false }, atr: { enabled: false } },
    take_profit: { ladder: [] },
    dca: { enabled: false },
    pyramid: { enabled: false },
    rebalance: { min_interval_hours: 4, band_source: 'execution' },
  },
  plan_bounds: {},
  config_path: 'trading.sleeves.a',
};

const LIMITS: LimitRow[] = [
  { name: 'max_order_notional_pct', path: 'risk.max_order_notional_pct', value: 0.2,
    unit: 'fraction' },
  { name: 'max_weight', path: 'risk.max_weight', value: { 'BTC/USDT': 0.4 },
    unit: 'fraction' },
];

const OVERVIEW: RiskOverview = {
  sleeves: {
    a: { limits: { sleeve: 'a', checks: ['nav_valid'], limits: LIMITS }, anchors: ANCHORS,
      mechanics: MECHANICS },
    b: { limits: { sleeve: 'b', checks: ['nav_valid'], limits: LIMITS },
      anchors: { ...ANCHORS, sleeve: 'b', monthly_locked: false,
        confirm_phrase: 'RESUME SLEEVE B' },
      mechanics: { ...MECHANICS, sleeve: 'b' } },
  },
  navs: { a: 9500, b: 10100 },
  flags: { path: 'knowledge/flags.json', ok: true,
    flags: { macro_blackout: { active: true, severity: 'block_entries' } } },
  kill: false,
};

const DECISION: GateDecision = {
  id: 7,
  ts_utc: '2026-09-22T08:00:00Z',
  sleeve: 'a',
  pair: 'BTC/USDT',
  side: 'buy',
  intent: 'entry',
  callback: 'confirm_trade_entry',
  allowed: false,
  reason: 'order_notional',
  severity: 'breach',
  checks: { kill: true, order_notional: false, nav_valid: true },
  proposed_stake: 2500,
  nav: 10000,
  gross_exposure: 0.2,
  run_id: 'test-a-1',
  action: 'reject',
  trade_id: null,
};

function client(overrides: Record<string, { status?: number; body?: unknown }> = {}) {
  return new ApiClient({
    fetchImpl: fakeFetch({
      '/api/risk': { body: OVERVIEW },
      '/api/risk/a/utilisation': {
        body: {
          sleeve: 'a',
          nav: 9500,
          meters: {
            orders_per_day: { used: 3, limit: 12, headroom: 9, pct: 0.25 },
            usdt_floor: { used: 0.35, limit: 0.2, headroom: 0.15, pct: 0.57 },
          },
        },
      },
      '/api/risk/gate-decisions': {
        body: { rows: [DECISION], counts: { allow: 4, reject: 1, breach: 1 },
          checks: ['nav_valid'] },
      },
      ...overrides,
    }),
  });
}

describe('MeterBar', () => {
  it('formats by unit and shows the headroom', () => {
    renderWithProviders(
      <MeterBar label="turnover_day"
        meter={{ used: 0.1, limit: 0.5, headroom: 0.4, pct: 0.2 }} />,
    );
    expect(screen.getByText('10.0% / 50.0%')).toBeInTheDocument();
    expect(screen.getByText('headroom 40.0%')).toBeInTheDocument();
  });

  it('reads a floor the other way round', () => {
    renderWithProviders(
      <MeterBar label="usdt_floor" floor
        meter={{ used: 0.35, limit: 0.2, headroom: 0.15, pct: 0.57 }} />,
    );
    expect(screen.getByText('above the floor by 15.0%')).toBeInTheDocument();
  });

  it('formats counts and USDT without inventing percentages', () => {
    expect(fmt(12, 'count')).toBe('12');
    expect(fmt(1234.5, 'usdt')).toBe('1234.50 USDT');
    expect(fmt(Number.NaN)).toBe('—');
  });
});

describe('LimitsTable', () => {
  it('renders a per-asset map as one row and names its config path', () => {
    renderWithProviders(<LimitsTable limits={LIMITS} />);
    expect(screen.getByText('BTC/USDT 40.0%')).toBeInTheDocument();
    expect(screen.getByText('risk.max_order_notional_pct')).toBeInTheDocument();
    expect(screen.getAllByText('human only')).toHaveLength(2);
  });
});

describe('GateLog', () => {
  it('shows the failing check filled and lets the severity filter change', async () => {
    const onChange = vi.fn();
    renderWithProviders(
      <GateLog rows={[DECISION]} counts={{ allow: 4, reject: 1, breach: 1 }} severity="all"
        onSeverityChange={onChange} />,
    );
    // once as the decision reason, once as the failed check in the matrix
    expect(screen.getAllByText('order_notional')).toHaveLength(2);
    expect(screen.getByText('nav_valid')).toBeInTheDocument();
    await userEvent.click(screen.getByText('breach (1)'));
    expect(onChange).toHaveBeenCalledWith('breach');
  });
});

describe('ResumeMonthlyModal', () => {
  it('explains the re-anchor and only resumes on the exact phrase', async () => {
    const onResume = vi.fn().mockResolvedValue({ resumed: true });
    renderWithProviders(
      <ResumeMonthlyModal opened onClose={vi.fn()} sleeve="a" anchors={ANCHORS}
        steppedUp onResume={onResume} />,
    );
    expect(screen.getByText(/re-anchor/)).toBeInTheDocument();
    const confirm = screen.getByRole('button', { name: /Resume sleeve A/ });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByRole('textbox'), 'RESUME SLEEVE A');
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);
    await waitFor(() => expect(onResume).toHaveBeenCalledWith('RESUME SLEEVE A'));
  });
});

describe('RiskPage', () => {
  it('renders the limits, meters and gate log for the selected sleeve', async () => {
    renderWithProviders(
      <ApiProvider client={client()}>
        <RiskPage />
      </ApiProvider>,
    );
    expect(await screen.findByText('Risk')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText('MONTHLY STOP ENGAGED')).toBeInTheDocument());
    expect(await screen.findByText('risk.max_order_notional_pct')).toBeInTheDocument();
    expect(await screen.findByTestId('meter-orders_per_day')).toBeInTheDocument();
    expect(await screen.findAllByText('order_notional')).toHaveLength(2);
    expect(screen.getByText('macro_blackout')).toBeInTheDocument();
  });

  it('surfaces a failed load instead of rendering an empty page', async () => {
    const failing = new ApiClient({
      fetchImpl: fakeFetch({ '/api/risk': { status: 500, body: {
        error: { code: 'boom', message: 'journal unavailable', detail: null } } } }),
    });
    renderWithProviders(
      <ApiProvider client={failing}>
        <RiskPage />
      </ApiProvider>,
    );
    expect(await screen.findByText('Could not load risk')).toBeInTheDocument();
  });
});
