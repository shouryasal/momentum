import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import {
  SchemaForm,
  convertDuration,
  durationBaseUnit,
  naturalUnit,
  widgetFor,
  withInheritedPresentation,
  type JsonSchema,
  type ValidationIssue,
} from '../components/SchemaForm';
import { renderWithProviders } from './utils';

/** Exercises every branch of spec 12's widget table in one schema. */
const SCHEMA: JsonSchema = {
  type: 'object',
  title: 'Earn',
  $defs: {
    Sleeve: {
      type: 'object',
      title: 'Sleeve',
      properties: {
        enabled: { type: 'boolean', title: 'Enabled' },
        seed_usdt: { type: 'number', title: 'Seed', 'x-unit': 'usdt', minimum: 0 },
      },
    },
  },
  properties: {
    name: { type: 'string', title: 'Name', description: 'Free text' },
    notes: { type: 'string', title: 'Notes', 'x-widget': 'textarea' },
    seed_ms: {
      type: 'integer',
      title: 'Poll interval',
      'x-widget': 'duration',
      description: 'How often to poll.',
      'x-help-md': 'Milliseconds; the console restarts to pick this up.',
      minimum: 250,
    },
    deadline_s: { type: 'integer', title: 'Deadline', 'x-widget': 'duration', minimum: 60 },
    screen_score: {
      type: 'number',
      title: 'Minimum score',
      'x-widget': 'slider',
      'x-unit': 'fraction',
      minimum: 0,
      maximum: 1,
      multipleOf: 0.01,
    },
    max_turns: { type: 'integer', title: 'Max turns', 'x-widget': 'slider' },
    slots: {
      type: 'array',
      title: 'Research slots',
      'x-widget': 'time',
      items: { type: 'string' },
    },
    summary_time: { type: 'string', title: 'Digest time', 'x-widget': 'time' },
    started: { type: 'string', title: 'Shadow started', 'x-widget': 'time' },
    schema_path: { type: 'string', title: 'Proposal schema', 'x-widget': 'path' },
    password_env: { type: 'string', title: 'Password env', 'x-widget': 'secret-ref' },
    max_dd: {
      type: 'number',
      title: 'Max drawdown',
      'x-unit': 'fraction',
      'x-protected': true,
      'x-tier': 2,
      minimum: 0,
      maximum: 1,
    },
    max_entries: { type: 'integer', title: 'Max entries', minimum: 1, maximum: 5 },
    live: { type: 'boolean', title: 'Live' },
    submode: { type: 'string', title: 'Submode', enum: ['propose', 'execute'] },
    schedule: { type: 'string', title: 'Schedule', 'x-widget': 'cron' },
    model: { type: 'string', title: 'Model', 'x-widget': 'model-ref' },
    skill: { type: 'string', title: 'Skill', 'x-widget': 'skill-ref' },
    pair: { type: 'string', title: 'Pair', 'x-widget': 'pair' },
    pairs: { type: 'array', title: 'Pairs', items: { type: 'string', title: 'Pair' } },
    weights: {
      type: 'object',
      title: 'Weights',
      additionalProperties: { type: 'number', title: 'Weight' },
    },
    sleeve: { $ref: '#/$defs/Sleeve' },
    optional_note: { anyOf: [{ type: 'string' }, { type: 'null' }], title: 'Optional note' },
    raw: { type: 'object', title: 'Raw', 'x-widget': 'json' },
  },
  required: ['name'],
};

function Harness({
  initial = {},
  issues = [],
  options,
  onValue,
}: {
  initial?: Record<string, unknown>;
  issues?: ValidationIssue[];
  options?: Parameters<typeof SchemaForm>[0]['options'];
  onValue?: (value: Record<string, unknown>) => void;
}) {
  const [value, setValue] = useState<Record<string, unknown>>(initial);
  return (
    <SchemaForm
      schema={SCHEMA}
      value={value}
      issues={issues}
      {...(options ? { options } : {})}
      onChange={(next) => {
        setValue(next as Record<string, unknown>);
        onValue?.(next as Record<string, unknown>);
      }}
    />
  );
}

describe('widgetFor', () => {
  it('maps JSON-Schema types and x-* annotations to widgets', () => {
    expect(widgetFor({ type: 'string' })).toBe('text');
    expect(widgetFor({ type: 'string', enum: ['a', 'b'] })).toBe('select');
    expect(widgetFor({ type: 'boolean' })).toBe('switch');
    expect(widgetFor({ type: 'integer' })).toBe('number');
    expect(widgetFor({ type: 'number', 'x-unit': 'fraction' })).toBe('percent');
    expect(widgetFor({ type: 'array', items: { type: 'string' } })).toBe('array');
    expect(widgetFor({ type: 'object', properties: { a: {} } })).toBe('object');
    expect(widgetFor({ type: 'object', additionalProperties: { type: 'number' } })).toBe('map');
    expect(widgetFor({ type: 'string', format: 'date-time' })).toBe('datetime');
    expect(widgetFor({ type: 'string', 'x-widget': 'cron' })).toBe('cron');
  });

  it('resolves the five widgets spec 3 adds to the vocabulary', () => {
    expect(widgetFor({ type: 'integer', 'x-widget': 'duration' })).toBe('duration');
    expect(widgetFor({ type: 'number', 'x-widget': 'slider', 'x-unit': 'fraction' })).toBe('slider');
    expect(widgetFor({ type: 'string', 'x-widget': 'path' })).toBe('path');
    expect(widgetFor({ type: 'string', 'x-widget': 'time' })).toBe('time');
    expect(widgetFor({ type: 'string', 'x-widget': 'secret-ref' })).toBe('secret-ref');
  });

  it('ignores an x-widget the node type cannot render', () => {
    // `research.slots` is a `list[str]` annotated `time`: the annotation is about the
    // entries, so the container must stay an array and keep its add/remove controls.
    expect(widgetFor({ type: 'array', items: { type: 'string' }, 'x-widget': 'time' })).toBe('array');
    expect(widgetFor({ type: 'string', 'x-widget': 'slider' })).toBe('text');
    expect(widgetFor({ type: 'boolean', 'x-widget': 'duration' })).toBe('switch');
  });

  it('pushes x-widget and x-unit down to array items and map values', () => {
    expect(
      withInheritedPresentation({ 'x-widget': 'time' }, { type: 'string' })['x-widget'],
    ).toBe('time');
    expect(withInheritedPresentation({ 'x-unit': 'fraction' }, { type: 'number' })['x-unit']).toBe(
      'fraction',
    );
    // A child that says otherwise keeps its own annotation.
    expect(
      withInheritedPresentation({ 'x-widget': 'time' }, { 'x-widget': 'cron' })['x-widget'],
    ).toBe('cron');
  });
});

describe('duration conversion', () => {
  it('reads the stored unit from x-unit, then from the key suffix', () => {
    expect(durationBaseUnit('console.poll_ms.db', {})).toBe('ms');
    expect(durationBaseUnit('research.deadline_s', {})).toBe('s');
    expect(durationBaseUnit('dca.cooldown_hours', {})).toBe('h');
    expect(durationBaseUnit('anything', { 'x-unit': 'minutes' })).toBe('min');
    expect(durationBaseUnit('anything', { 'x-unit': 'days' })).toBe('d');
    // Nothing to go on: seconds, the repo's default for a bare deadline.
    expect(durationBaseUnit('deadline', {})).toBe('s');
  });

  it('shows a value in the largest unit that divides it exactly', () => {
    expect(naturalUnit(2400, 's')).toBe('min');
    expect(naturalUnit(3600, 's')).toBe('h');
    expect(naturalUnit(90, 's')).toBe('s');
    expect(naturalUnit(null, 'ms')).toBe('ms');
    // Never below the stored unit: hours cannot be displayed as milliseconds.
    expect(naturalUnit(1, 'h')).toBe('h');
  });

  it('round-trips between units', () => {
    expect(convertDuration(2400, 's', 'min')).toBe(40);
    expect(convertDuration(40, 'min', 's')).toBe(2400);
    expect(convertDuration(2000, 'ms', 's')).toBe(2);
  });
});

describe('SchemaForm', () => {
  it('renders a field for every property, including nested $ref objects', () => {
    renderWithProviders(<Harness />);
    expect(screen.getByTestId('field-name')).toBeInTheDocument();
    expect(screen.getByTestId('field-notes')).toHaveAttribute('data-widget', 'textarea');
    expect(screen.getByTestId('field-submode')).toHaveAttribute('data-widget', 'select');
    expect(screen.getByTestId('field-schedule')).toHaveAttribute('data-widget', 'cron');
    expect(screen.getByTestId('array-pairs')).toBeInTheDocument();
    expect(screen.getByTestId('map-weights')).toBeInTheDocument();
    expect(screen.getByTestId('object-sleeve')).toBeInTheDocument();
    expect(screen.getByTestId('field-sleeve.seed_usdt')).toBeInTheDocument();
    expect(screen.getByTestId('field-optional_note')).toHaveAttribute('data-widget', 'text');
    expect(screen.getByTestId('field-raw')).toHaveAttribute('data-widget', 'json');
    expect(screen.getByTestId('field-seed_ms')).toHaveAttribute('data-widget', 'duration');
  });

  it('renders x-help-md alongside the short description', () => {
    renderWithProviders(<Harness />);
    expect(screen.getByTestId('help-seed_ms')).toHaveTextContent(
      'Milliseconds; the console restarts to pick this up.',
    );
  });

  it('shows a lock on protected fields', () => {
    renderWithProviders(<Harness />);
    expect(screen.getAllByTestId('protected-lock').length).toBe(1);
  });

  it('edits a string field', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(<Harness onValue={onValue} />);
    await user.type(screen.getByTestId('field-name'), 'earn');
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ name: 'earn' }));
  });

  it('stores a percent input as a fraction', () => {
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ max_dd: 0.08 }} onValue={onValue} />);
    const input = screen.getByTestId('field-max_dd') as HTMLInputElement;
    expect(input.value).toBe('8%');
    fireEvent.change(input, { target: { value: '12' } });
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ max_dd: 0.12 }));
  });

  it('toggles a boolean switch', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(<Harness onValue={onValue} />);
    await user.click(screen.getByTestId('field-live'));
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ live: true }));
  });

  it('adds and removes array items', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ pairs: [] }} onValue={onValue} />);
    await user.click(screen.getByTestId('array-add-pairs'));
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ pairs: [''] }));
    expect(screen.getByTestId('field-pairs[0]')).toBeInTheDocument();
    await user.click(screen.getByTestId('array-remove-pairs-0'));
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ pairs: [] }));
  });

  it('adds and removes additionalProperties entries', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ weights: { 'BTC/USDT': 0.5 } }} onValue={onValue} />);
    expect(screen.getByTestId('field-weights.BTC/USDT')).toBeInTheDocument();

    await user.type(screen.getByTestId('map-newkey-weights'), 'ETH/USDT');
    await user.click(screen.getByTestId('map-add-weights'));
    expect(onValue).toHaveBeenLastCalledWith(
      expect.objectContaining({ weights: { 'BTC/USDT': 0.5, 'ETH/USDT': 0 } }),
    );

    await user.click(screen.getByTestId('map-remove-weights-BTC/USDT'));
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ weights: expect.not.objectContaining({ 'BTC/USDT': 0.5 }) }));
  });

  it('previews the next cron fires', () => {
    renderWithProviders(<Harness initial={{ schedule: '30 4 * * *' }} />);
    const preview = screen.getByTestId('cron-preview');
    expect(within(preview).getAllByText(/Z$/)).toHaveLength(5);
  });

  it('flags an invalid cron expression', () => {
    renderWithProviders(<Harness initial={{ schedule: 'not a cron' }} />);
    expect(screen.getByTestId('cron-invalid')).toBeInTheDocument();
    expect(screen.queryByTestId('cron-preview')).toBeNull();
  });

  it('falls back to a text input when a data-driven widget has no options', () => {
    renderWithProviders(<Harness />);
    expect(screen.getByTestId('field-model')).toHaveAttribute('data-widget', 'model-ref');
    expect((screen.getByTestId('field-model') as HTMLInputElement).placeholder).toContain('no options');
  });

  it('renders model-ref / skill-ref / pair as selects when options are supplied', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(
      <Harness
        options={{
          modelRefs: [{ value: 'claude:sonnet', label: 'Claude Sonnet' }],
          skillRefs: [{ value: 'decide', label: 'decide' }],
          pairs: [{ value: 'BTC/USDT', label: 'BTC/USDT' }],
        }}
        onValue={onValue}
      />,
    );
    await user.click(screen.getByTestId('field-model'));
    await user.click(await screen.findByText('Claude Sonnet'));
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ model: 'claude:sonnet' }));
    expect(screen.getByTestId('field-skill')).toBeInTheDocument();
    expect(screen.getByTestId('field-pair')).toBeInTheDocument();
  });

  it('shows validation issues next to the field they address', () => {
    renderWithProviders(
      <Harness issues={[{ path: 'max_dd', message: 'must be <= risk ceiling' }]} initial={{ max_dd: 0.9 }} />,
    );
    expect(screen.getByText('must be <= risk ceiling')).toBeInTheDocument();
  });

  it('shows root-level issues in a banner', () => {
    renderWithProviders(<Harness issues={[{ path: '', message: 'weights must sum to 1' }]} />);
    expect(screen.getByTestId('schema-form-errors')).toHaveTextContent('weights must sum to 1');
  });

  it('disables every input in read-only mode', () => {
    renderWithProviders(
      <SchemaForm schema={SCHEMA} value={{ name: 'x' }} onChange={() => undefined} readOnly />,
    );
    expect(screen.getByTestId('field-name')).toBeDisabled();
    expect(screen.getByTestId('array-add-pairs')).toBeDisabled();
    expect(screen.getByTestId('field-seed_ms')).toBeDisabled();
    expect(screen.getByTestId('duration-unit-seed_ms')).toBeDisabled();
  });
});

/** Spec 3/12's five widgets that used to degrade to a plain input. */
describe('SchemaForm — slider, time, duration, path, secret-ref', () => {
  it('renders a slider with a bounded range and keeps the stored fraction', () => {
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ screen_score: 0.6 }} onValue={onValue} />);
    const input = screen.getByTestId('field-screen_score') as HTMLInputElement;
    expect(input).toHaveAttribute('data-widget', 'slider');
    expect(screen.getByTestId('slider-screen_score')).toBeInTheDocument();
    expect(input.value).toBe('60%');

    fireEvent.change(input, { target: { value: '75' } });
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ screen_score: 0.75 }));
  });

  it('clamps a slider edit to the schema range', () => {
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ screen_score: 0.6 }} onValue={onValue} />);
    fireEvent.change(screen.getByTestId('field-screen_score'), { target: { value: '140' } });
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ screen_score: 1 }));
  });

  it('falls back to a number input when a slider has no range to slide along', () => {
    renderWithProviders(<Harness initial={{ max_turns: 4 }} />);
    expect(screen.getByTestId('field-max_turns')).toHaveAttribute('data-widget', 'number');
    expect(screen.queryByTestId('slider-max_turns')).toBeNull();
  });

  it('gives research slots a real time picker, one per entry', () => {
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ slots: ['08:30', '16:00'] }} onValue={onValue} />);
    const first = screen.getByTestId('field-slots[0]') as HTMLInputElement;
    expect(first).toHaveAttribute('type', 'time');
    expect(first).toHaveAttribute('data-widget', 'time');
    expect(first.value).toBe('08:30');

    fireEvent.change(first, { target: { value: '09:15' } });
    expect(onValue).toHaveBeenLastCalledWith(
      expect.objectContaining({ slots: ['09:15', '16:00'] }),
    );
  });

  it('renders a scalar time field as a clock picker', () => {
    renderWithProviders(<Harness initial={{ summary_time: '21:30' }} />);
    expect(screen.getByTestId('field-summary_time')).toHaveAttribute('type', 'time');
    expect(screen.queryByTestId('time-freeform-summary_time')).toBeNull();
  });

  it('keeps a non-clock value editable instead of blanking it', () => {
    renderWithProviders(<Harness initial={{ started: '2026-10-27' }} />);
    const input = screen.getByTestId('field-started') as HTMLInputElement;
    expect(input).toHaveAttribute('type', 'text');
    expect(input.value).toBe('2026-10-27');
    expect(screen.getByTestId('time-freeform-started')).toBeInTheDocument();
  });

  it('shows a duration in its natural unit and stores it in the schema unit', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ deadline_s: 2400 }} onValue={onValue} />);
    const input = screen.getByTestId('field-deadline_s') as HTMLInputElement;
    expect(input.value).toBe('40');
    expect(screen.getByTestId('duration-base-deadline_s')).toHaveTextContent('2400 seconds');

    fireEvent.change(input, { target: { value: '45' } });
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ deadline_s: 2700 }));

    // Every Mantine dropdown stays mounted, so scope the option to this field's listbox.
    const unit = screen.getByTestId('duration-unit-deadline_s');
    await user.click(unit);
    const listbox = document.getElementById(unit.getAttribute('aria-controls') ?? '');
    expect(listbox).not.toBeNull();
    await user.click(within(listbox as HTMLElement).getByText('hours'));
    fireEvent.change(screen.getByTestId('field-deadline_s'), { target: { value: '2' } });
    expect(onValue).toHaveBeenLastCalledWith(expect.objectContaining({ deadline_s: 7200 }));
  });

  it('reads milliseconds from the key suffix', () => {
    renderWithProviders(<Harness initial={{ seed_ms: 2000 }} />);
    expect((screen.getByTestId('field-seed_ms') as HTMLInputElement).value).toBe('2');
    expect(screen.getByTestId('duration-base-seed_ms')).toHaveTextContent('2000 ms');
  });

  it('labels a path as repo-relative or absolute', () => {
    const onValue = vi.fn();
    renderWithProviders(<Harness initial={{ schema_path: 'schemas/proposal.json' }} onValue={onValue} />);
    expect(screen.getByTestId('field-schema_path')).toHaveAttribute('data-widget', 'path');
    expect(screen.getByTestId('path-kind-schema_path')).toHaveTextContent('relative to the repository root');

    fireEvent.change(screen.getByTestId('field-schema_path'), { target: { value: '/etc/earn.json' } });
    expect(onValue).toHaveBeenLastCalledWith(
      expect.objectContaining({ schema_path: '/etc/earn.json' }),
    );
    expect(screen.getByTestId('path-kind-schema_path')).toHaveTextContent('absolute path');
  });

  it('offers a chooser over known secret names and never a value', async () => {
    const user = userEvent.setup();
    const onValue = vi.fn();
    renderWithProviders(
      <Harness
        options={{
          secretRefs: [
            { value: 'FREQTRADE_API_PASSWORD_A', label: 'FREQTRADE_API_PASSWORD_A' },
            { value: 'TELEGRAM_BOT_TOKEN', label: 'TELEGRAM_BOT_TOKEN' },
          ],
        }}
        onValue={onValue}
      />,
    );
    await user.click(screen.getByTestId('field-password_env'));
    await user.click(await screen.findByText('TELEGRAM_BOT_TOKEN'));
    expect(onValue).toHaveBeenLastCalledWith(
      expect.objectContaining({ password_env: 'TELEGRAM_BOT_TOKEN' }),
    );
    expect(screen.getByTestId('secret-ref-note-password_env')).toHaveTextContent(
      'never reads or shows the value',
    );
  });

  it('degrades secret-ref to a name input when no secrets are loaded', () => {
    renderWithProviders(<Harness />);
    const input = screen.getByTestId('field-password_env') as HTMLInputElement;
    expect(input).toHaveAttribute('data-widget', 'secret-ref');
    expect(input.placeholder).toContain('no options loaded');
    expect(screen.getByTestId('secret-ref-note-password_env')).toBeInTheDocument();
  });
});
