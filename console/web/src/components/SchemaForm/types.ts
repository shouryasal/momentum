/** The slice of JSON Schema the console's pydantic models emit, plus the `x-*` annotations. */

export type JsonSchemaType = 'object' | 'array' | 'string' | 'number' | 'integer' | 'boolean' | 'null';

export interface JsonSchema {
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  type?: JsonSchemaType | JsonSchemaType[];
  title?: string;
  description?: string;
  default?: unknown;
  examples?: unknown[];
  enum?: unknown[];
  const?: unknown;
  format?: string;

  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  items?: JsonSchema;
  prefixItems?: JsonSchema[];

  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];

  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minItems?: number;
  maxItems?: number;
  readOnly?: boolean;

  /** Explicit widget override, e.g. `cron`, `model-ref`, `skill-ref`, `pair`, `textarea`. */
  'x-widget'?: string;
  /** `fraction` renders a percent input; `usdt`, `bps`, `seconds`, ... are shown as suffixes. */
  'x-unit'?: string;
  /** Protected fields show a lock and require step-up to save (spec 5.2 footnote 1). */
  'x-protected'?: boolean;
  'x-tier'?: number | string;
  'x-group'?: string;
  'x-order'?: number;
  'x-effects'?: string[];
  /** Long-form help (spec 3). `ops.config.F(help_md=...)` emits `x-help-md`. */
  'x-help-md'?: string;
  'x-help'?: string;
  'x-placeholder'?: string;
}

/** Server- or client-side validation message, addressed by the dotted field path. */
export interface ValidationIssue {
  path: string;
  message: string;
}

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
}

/**
 * Data-driven option sources.  `model-ref`/`skill-ref`/`pair`/`secret-ref` widgets are
 * filled by the page (P3/P6/P2); without them the widget degrades to a free-text input.
 */
export interface SchemaFormOptions {
  modelRefs?: SelectOption[];
  skillRefs?: SelectOption[];
  pairs?: SelectOption[];
  /**
   * Known secret **names** for `x-widget: secret-ref` — `GET /api/secrets` returns
   * `{name, present, last4}` and never a value, so this list carries names only.
   */
  secretRefs?: SelectOption[];
  /** Any other `x-widget: lookup:<name>` source. */
  lookups?: Record<string, SelectOption[]>;
}

export type WidgetName =
  | 'object'
  | 'map'
  | 'array'
  | 'select'
  | 'multiselect'
  | 'switch'
  | 'number'
  | 'percent'
  | 'text'
  | 'textarea'
  | 'password'
  | 'json'
  | 'cron'
  | 'model-ref'
  | 'skill-ref'
  | 'pair'
  | 'datetime'
  | 'slider'
  | 'time'
  | 'duration'
  | 'path'
  | 'secret-ref';
