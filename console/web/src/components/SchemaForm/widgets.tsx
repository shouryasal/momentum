import {
  Badge,
  Group,
  Input,
  JsonInput,
  MultiSelect,
  NumberInput,
  PasswordInput,
  Select,
  Slider,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconFolder, IconKey, IconLock } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';

import { isValidCron, nextCronFires } from '../../lib/cron';
import { formatUtcStamp } from '../../lib/format';
import type { JsonSchema, SchemaFormOptions, SelectOption, WidgetName } from './types';
import { unitSuffix } from './util';

export interface LeafProps {
  widget: WidgetName;
  schema: JsonSchema;
  path: string;
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  error?: string | undefined;
  readOnly?: boolean;
  required?: boolean;
  options?: SchemaFormOptions;
}

/** Field label with the protected lock, the tier badge and the effect badges (spec 12). */
export function FieldLabel({ label, schema }: { label: string; schema: JsonSchema }) {
  const locked = schema['x-protected'] === true;
  const tier = schema['x-tier'];
  const effects = schema['x-effects'] ?? [];
  return (
    <Group gap={6} wrap="nowrap">
      <Text size="sm" fw={500}>
        {label}
      </Text>
      {locked ? (
        <Tooltip label="Protected field — saving it requires step-up re-authentication">
          <span data-testid="protected-lock" aria-label="protected field">
            <IconLock size={13} />
          </span>
        </Tooltip>
      ) : null}
      {tier !== undefined ? (
        <Badge size="xs" variant="light" color={String(tier) === '2' ? 'red' : 'gray'}>
          tier {String(tier)}
        </Badge>
      ) : null}
      {effects.map((effect) => (
        <Badge key={effect} size="xs" variant="outline" color="grape">
          {effect}
        </Badge>
      ))}
    </Group>
  );
}

function optionData(schema: JsonSchema, source?: SelectOption[]): SelectOption[] {
  if (source && source.length > 0) return source;
  return (schema.enum ?? []).map((item) => ({ value: String(item), label: String(item) }));
}

function lookupFor(widget: WidgetName, options?: SchemaFormOptions): SelectOption[] | undefined {
  if (!options) return undefined;
  if (widget === 'model-ref') return options.modelRefs;
  if (widget === 'skill-ref') return options.skillRefs;
  if (widget === 'pair') return options.pairs;
  if (widget === 'secret-ref') return options.secretRefs;
  return options.lookups?.[widget];
}

/* ------------------------------------------------------------------ duration */

/** Time units the `duration` widget converts between, smallest first. */
export const DURATION_UNITS = [
  { value: 'ms', label: 'ms', ms: 1 },
  { value: 's', label: 'seconds', ms: 1000 },
  { value: 'min', label: 'minutes', ms: 60_000 },
  { value: 'h', label: 'hours', ms: 3_600_000 },
  { value: 'd', label: 'days', ms: 86_400_000 },
] as const;

export type DurationUnit = (typeof DURATION_UNITS)[number]['value'];

const UNIT_MS: Record<DurationUnit, number> = {
  ms: 1, s: 1000, min: 60_000, h: 3_600_000, d: 86_400_000,
};

/**
 * The unit a `duration` value is *stored* in.
 *
 * `x-unit` names it when the schema has one (`minutes`, `hours`, `days`); otherwise the
 * suffix of the key does, which is the repo's own convention — `deadline_s` is seconds,
 * `poll_ms` milliseconds, `cooldown_hours` hours.  Everything the widget shows is a
 * conversion of this; the value written back is always in this unit.
 */
export function durationBaseUnit(path: string, schema: JsonSchema): DurationUnit {
  const unit = schema['x-unit'];
  if (unit === 'minutes') return 'min';
  if (unit === 'hours') return 'h';
  if (unit === 'days') return 'd';
  // Innermost segment first, then outwards: `console.poll_ms.db` carries its unit one level
  // up from the leaf, because the model is `poll_ms: {db, bots}`.
  const suffixes: Array<[RegExp, DurationUnit]> = [
    [/(^|_)(ms|millis|milliseconds)$/, 'ms'],
    [/(^|_)(s|sec|secs|seconds)$/, 's'],
    [/(^|_)(min|mins|minutes)$/, 'min'],
    [/(^|_)(h|hr|hrs|hours)$/, 'h'],
    [/(^|_)(d|days)$/, 'd'],
  ];
  for (const segment of path.split('.').reverse()) {
    for (const [pattern, unitName] of suffixes) {
      if (pattern.test(segment)) return unitName;
    }
  }
  return 's';
}

/** The largest unit that divides `value` exactly — what a human would have typed. */
export function naturalUnit(value: number | null, base: DurationUnit): DurationUnit {
  if (value === null || !Number.isFinite(value) || value === 0) return base;
  const ms = Math.abs(value) * UNIT_MS[base];
  let best: DurationUnit = base;
  for (const unit of DURATION_UNITS) {
    if (unit.ms >= UNIT_MS[base] && ms % unit.ms === 0) best = unit.value;
  }
  return best;
}

export function convertDuration(value: number, from: DurationUnit, to: DurationUnit): number {
  const raw = (value * UNIT_MS[from]) / UNIT_MS[to];
  // Sub-unit precision is noise for a duration; keep four decimals so `90 s` -> `1.5 min`
  // survives the round trip without dragging float dust into the saved config.
  return Number(raw.toFixed(4));
}

/** `LeafProps` plus the composed description node `LeafWidget` builds once. */
interface SubFieldProps extends LeafProps {
  description: ReactNode;
}

function DurationField(props: SubFieldProps) {
  const { schema, path, label, value, onChange, error, readOnly, required } = props;
  const base = durationBaseUnit(path, schema);
  const numeric = typeof value === 'number' ? value : null;
  const [unit, setUnit] = useState<DurationUnit>(() => naturalUnit(numeric, base));
  const shown = numeric === null ? '' : convertDuration(numeric, base, unit);
  const baseLabel = DURATION_UNITS.find((u) => u.value === base)?.label ?? base;

  return (
    <Input.Wrapper
      label={<FieldLabel label={label} schema={schema} />}
      description={props.description}
      error={error}
      withAsterisk={required}
      data-widget="duration"
    >
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <NumberInput
          flex={1}
          value={shown}
          disabled={readOnly || schema.readOnly === true}
          data-testid={`field-${path}`}
          data-widget="duration"
          onChange={(next) => {
            const asNumber = typeof next === 'number' ? next : Number(next);
            if (!Number.isFinite(asNumber)) {
              onChange(null);
              return;
            }
            const inBase = convertDuration(asNumber, unit, base);
            onChange(schema.type === 'integer' ? Math.round(inBase) : inBase);
          }}
          {...(schema.minimum !== undefined
            ? { min: convertDuration(schema.minimum, base, unit) }
            : {})}
          {...(schema.maximum !== undefined
            ? { max: convertDuration(schema.maximum, base, unit) }
            : {})}
        />
        <Select
          w={110}
          data={DURATION_UNITS.map((u) => ({ value: u.value, label: u.label }))}
          value={unit}
          allowDeselect={false}
          disabled={readOnly || schema.readOnly === true}
          data-testid={`duration-unit-${path}`}
          onChange={(next) => setUnit((next as DurationUnit | null) ?? base)}
        />
      </Group>
      <Text size="xs" c="dimmed" data-testid={`duration-base-${path}`}>
        stored as {numeric === null ? '—' : numeric} {baseLabel}
      </Text>
    </Input.Wrapper>
  );
}

function CronPreview({ expression }: { expression: string }) {
  if (!expression.trim()) return null;
  if (!isValidCron(expression)) {
    return (
      <Text size="xs" c="red" data-testid="cron-invalid">
        invalid cron expression
      </Text>
    );
  }
  const fires = nextCronFires(expression, 5);
  return (
    <Stack gap={0} data-testid="cron-preview">
      <Text size="xs" c="dimmed">
        next 5 fires (UTC)
      </Text>
      {fires.map((fire) => (
        <Text key={fire.toISOString()} size="xs" c="dimmed" ff="monospace">
          {formatUtcStamp(fire)}
        </Text>
      ))}
    </Stack>
  );
}

/** `HH:MM` (24-hour), the one clock format `research.slots` and the schedules use. */
const CLOCK_RE = /^([01]?\d|2[0-3]):[0-5]\d$/;

/** One leaf input.  Container widgets (object/array/map) live in `SchemaField`. */
export function LeafWidget(props: LeafProps) {
  const {
    widget,
    schema,
    path,
    label,
    value,
    onChange,
    error,
    readOnly,
    required,
    options,
  } = props;
  // `x-help-md` is the long help the backend emits (`ops.config.F(help_md=...)`); show it
  // under the short description rather than dropping it.
  const help = schema['x-help-md'] ?? schema['x-help'];
  const description: ReactNode =
    schema.description && help ? (
      <>
        {schema.description}{' '}
        <Text span size="xs" c="dimmed" data-testid={`help-${path}`}>
          {help}
        </Text>
      </>
    ) : (
      (schema.description ?? help)
    );
  const common = {
    label: <FieldLabel label={label} schema={schema} />,
    description,
    error,
    withAsterisk: required,
    disabled: readOnly || schema.readOnly === true,
    'data-testid': `field-${path}`,
    'data-widget': widget,
  } as const;

  switch (widget) {
    case 'switch':
      return (
        <Switch
          checked={value === true}
          onChange={(event) => onChange(event.currentTarget.checked)}
          label={<FieldLabel label={label} schema={schema} />}
          description={description}
          disabled={common.disabled}
          data-testid={common['data-testid']}
          data-widget={widget}
        />
      );

    case 'select':
    case 'model-ref':
    case 'skill-ref':
    case 'pair': {
      const data = optionData(schema, lookupFor(widget, options));
      if (data.length === 0) {
        return (
          <TextInput
            {...common}
            value={value === null || value === undefined ? '' : String(value)}
            onChange={(event) => onChange(event.currentTarget.value)}
            placeholder={schema['x-placeholder'] ?? `${widget} (no options loaded)`}
          />
        );
      }
      return (
        <Select
          {...common}
          data={data.map((option) => ({ value: option.value, label: option.label }))}
          value={value === null || value === undefined ? null : String(value)}
          onChange={(next) => onChange(next)}
          searchable
          clearable={!required}
          placeholder={schema['x-placeholder'] ?? 'Select…'}
        />
      );
    }

    case 'multiselect': {
      const data = optionData(schema.items ?? schema, lookupFor(widget, options));
      const current = Array.isArray(value) ? value.map(String) : [];
      return (
        <MultiSelect
          {...common}
          data={data.map((option) => ({ value: option.value, label: option.label }))}
          value={current}
          onChange={(next) => onChange(next)}
          searchable
        />
      );
    }

    case 'percent': {
      const numeric = typeof value === 'number' ? Number((value * 100).toFixed(6)) : '';
      return (
        <NumberInput
          {...common}
          value={numeric}
          onChange={(next) => {
            const asNumber = typeof next === 'number' ? next : Number(next);
            onChange(Number.isFinite(asNumber) ? asNumber / 100 : null);
          }}
          suffix="%"
          step={0.1}
          decimalScale={4}
          {...(schema.minimum !== undefined ? { min: schema.minimum * 100 } : {})}
          {...(schema.maximum !== undefined ? { max: schema.maximum * 100 } : {})}
        />
      );
    }

    case 'number': {
      const suffix = unitSuffix(schema);
      return (
        <NumberInput
          {...common}
          value={typeof value === 'number' ? value : ''}
          onChange={(next) => {
            const asNumber = typeof next === 'number' ? next : Number(next);
            onChange(Number.isFinite(asNumber) ? asNumber : null);
          }}
          allowDecimal={schema.type !== 'integer'}
          {...(suffix ? { suffix } : {})}
          {...(schema.minimum !== undefined ? { min: schema.minimum } : {})}
          {...(schema.maximum !== undefined ? { max: schema.maximum } : {})}
          {...(schema.multipleOf !== undefined ? { step: schema.multipleOf } : {})}
        />
      );
    }

    case 'textarea':
      return (
        <Textarea
          {...common}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) => onChange(event.currentTarget.value)}
          autosize
          minRows={3}
        />
      );

    case 'password':
      return (
        <PasswordInput
          {...common}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) => onChange(event.currentTarget.value)}
          autoComplete="off"
        />
      );

    case 'json':
      return (
        <JsonInput
          {...common}
          value={value === undefined ? '' : JSON.stringify(value, null, 2)}
          onChange={(next) => {
            try {
              onChange(JSON.parse(next));
            } catch {
              onChange(next);
            }
          }}
          autosize
          minRows={3}
          formatOnBlur
        />
      );

    case 'cron': {
      const expression = value === null || value === undefined ? '' : String(value);
      return (
        <Stack gap={4}>
          <TextInput
            {...common}
            value={expression}
            onChange={(event) => onChange(event.currentTarget.value)}
            placeholder="*/5 * * * *"
            spellCheck={false}
          />
          <CronPreview expression={expression} />
        </Stack>
      );
    }

    case 'datetime':
      return (
        <TextInput
          {...common}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) => onChange(event.currentTarget.value)}
          placeholder="2026-09-22T04:30:00Z"
          spellCheck={false}
        />
      );

    case 'slider': {
      // A slider needs a range.  Without both bounds there is nothing to slide along, so
      // the value falls back to the numeric input its unit implies rather than to a
      // control that silently clamps to an invented 0..1.
      const lo = schema.minimum ?? schema.exclusiveMinimum;
      const hi = schema.maximum ?? schema.exclusiveMaximum;
      const fraction = schema['x-unit'] === 'fraction';
      if (lo === undefined || hi === undefined || hi <= lo) {
        return <LeafWidget {...props} widget={fraction ? 'percent' : 'number'} />;
      }
      const scale = fraction ? 100 : 1;
      const current = typeof value === 'number' ? value : lo;
      const step = schema.multipleOf ?? (hi - lo) / 100;
      const commit = (next: number) => {
        const clamped = Math.min(hi, Math.max(lo, next));
        onChange(schema.type === 'integer' ? Math.round(clamped) : clamped);
      };
      return (
        <Input.Wrapper
          label={<FieldLabel label={label} schema={schema} />}
          description={description}
          error={error}
          withAsterisk={required}
        >
          <Group gap="md" wrap="nowrap" align="center">
            <Slider
              flex={1}
              min={lo * scale}
              max={hi * scale}
              step={step * scale}
              value={current * scale}
              disabled={common.disabled}
              label={(v) => `${v}${fraction ? '%' : (unitSuffix(schema) ?? '')}`}
              data-testid={`slider-${path}`}
              onChange={(next) => commit(next / scale)}
              marks={[
                { value: lo * scale, label: String(Number((lo * scale).toFixed(4))) },
                { value: hi * scale, label: String(Number((hi * scale).toFixed(4))) },
              ]}
            />
            <NumberInput
              w={120}
              value={Number((current * scale).toFixed(6))}
              disabled={common.disabled}
              data-testid={`field-${path}`}
              data-widget="slider"
              min={lo * scale}
              max={hi * scale}
              step={step * scale}
              {...(fraction ? { suffix: '%' } : {})}
              onChange={(next) => {
                const asNumber = typeof next === 'number' ? next : Number(next);
                if (Number.isFinite(asNumber)) commit(asNumber / scale);
              }}
            />
          </Group>
        </Input.Wrapper>
      );
    }

    case 'time': {
      // `<input type="time">` is the browser's own clock picker, and it keeps the stored
      // value in the `HH:MM` shape `research.slots` is validated against.  A stored value
      // that is not a clock time (a mis-annotated date, say) would be silently blanked by
      // the native control, so it stays editable as text and says why.
      const text = value === null || value === undefined ? '' : String(value);
      const clock = text === '' || CLOCK_RE.test(text);
      return (
        <Stack gap={4}>
          <TextInput
            {...common}
            type={clock ? 'time' : 'text'}
            value={text}
            onChange={(event) => onChange(event.currentTarget.value)}
            placeholder="08:30"
            spellCheck={false}
          />
          {clock ? null : (
            <Text size="xs" c="dimmed" data-testid={`time-freeform-${path}`}>
              not an HH:MM clock time — shown as text so the value is not lost
            </Text>
          )}
        </Stack>
      );
    }

    case 'duration':
      return <DurationField {...props} description={description} />;

    case 'path': {
      const text = value === null || value === undefined ? '' : String(value);
      const absolute = text.startsWith('/') || text.startsWith('~') || /^[A-Za-z]:[\\/]/.test(text);
      return (
        <Stack gap={4}>
          <TextInput
            {...common}
            value={text}
            onChange={(event) => onChange(event.currentTarget.value)}
            leftSection={<IconFolder size={14} />}
            placeholder={schema['x-placeholder'] ?? 'path/relative/to/the/repo'}
            spellCheck={false}
            styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
          />
          {text === '' ? null : (
            <Text size="xs" c="dimmed" data-testid={`path-kind-${path}`}>
              {absolute ? 'absolute path' : 'relative to the repository root'}
            </Text>
          )}
        </Stack>
      );
    }

    case 'secret-ref': {
      // A secret *name*, never a value: the chooser is filled from `GET /api/secrets`,
      // which reports `present`/`last4` only.  Nothing here can hold or reveal a value.
      const data = optionData(schema, lookupFor(widget, options));
      const note = (
        <Text size="xs" c="dimmed" data-testid={`secret-ref-note-${path}`}>
          Secret name only — the console never reads or shows the value.
        </Text>
      );
      return (
        <Stack gap={4}>
          {data.length === 0 ? (
            <TextInput
              {...common}
              value={value === null || value === undefined ? '' : String(value)}
              onChange={(event) => onChange(event.currentTarget.value)}
              leftSection={<IconKey size={14} />}
              placeholder={schema['x-placeholder'] ?? 'secret-ref (no options loaded)'}
              spellCheck={false}
            />
          ) : (
            <Select
              {...common}
              data={data.map((option) => ({ value: option.value, label: option.label }))}
              value={value === null || value === undefined ? null : String(value)}
              onChange={(next) => onChange(next)}
              leftSection={<IconKey size={14} />}
              searchable
              clearable={!required}
              placeholder={schema['x-placeholder'] ?? 'Choose a secret name…'}
            />
          )}
          {note}
        </Stack>
      );
    }

    default:
      return (
        <TextInput
          {...common}
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(event) => onChange(event.currentTarget.value)}
          {...(schema['x-placeholder'] ? { placeholder: schema['x-placeholder'] } : {})}
          spellCheck={false}
        />
      );
  }
}
