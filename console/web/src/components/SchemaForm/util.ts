import type { JsonSchema, JsonSchemaType, ValidationIssue, WidgetName } from './types';

/** Resolve a local `$ref` (`#/$defs/Name`) against the root schema. */
export function resolveRef(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (!schema.$ref) return schema;
  const parts = schema.$ref.replace(/^#\//, '').split('/');
  let cursor: unknown = root;
  for (const part of parts) {
    if (!cursor || typeof cursor !== 'object') return schema;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  const resolved = (cursor as JsonSchema | undefined) ?? schema;
  // Keep the `x-*` annotations of the referencing site: pydantic puts Field(...) metadata there.
  const { $ref: _ref, ...rest } = schema;
  return { ...resolved, ...rest };
}

export interface NormalisedSchema {
  schema: JsonSchema;
  nullable: boolean;
}

/**
 * Collapse the shapes pydantic v2 emits: `$ref`, and `anyOf: [T, {type:null}]` for
 * `Optional[T]`.  Returns the effective schema plus whether `null` is allowed.
 */
export function normaliseSchema(schema: JsonSchema, root: JsonSchema): NormalisedSchema {
  let current = resolveRef(schema, root);
  let nullable = false;

  const variants = current.anyOf ?? current.oneOf;
  if (variants && variants.length > 0) {
    const nonNull = variants.filter((variant) => variant.type !== 'null');
    nullable = nonNull.length !== variants.length;
    if (nonNull.length === 1) {
      const only = resolveRef(nonNull[0] as JsonSchema, root);
      const { anyOf: _a, oneOf: _o, ...rest } = current;
      current = { ...only, ...stripUndefined(rest) };
    }
  }
  if (Array.isArray(current.type)) {
    nullable = nullable || current.type.includes('null');
    const first = current.type.find((t) => t !== 'null');
    current = { ...current, ...(first ? { type: first } : {}) };
  }
  return { schema: current, nullable };
}

function stripUndefined(input: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(input)) {
    if (value !== undefined) out[key] = value;
  }
  return out;
}

export function schemaType(schema: JsonSchema): JsonSchemaType | undefined {
  if (Array.isArray(schema.type)) return schema.type.find((t) => t !== 'null');
  if (schema.type) return schema.type;
  if (schema.properties || schema.additionalProperties) return 'object';
  if (schema.items) return 'array';
  if (schema.enum) return 'string';
  return undefined;
}

/**
 * Widget names `LeafWidget`/`SchemaField` render specially — the full `x-widget` vocabulary
 * of spec 3 plus the type-derived names.  An `x-widget` outside this set still falls
 * through to the type-derived widget rather than resolving to a widget nothing handles.
 */
const IMPLEMENTED_WIDGETS: ReadonlySet<string> = new Set<WidgetName>([
  'object', 'map', 'array', 'select', 'multiselect', 'switch', 'number', 'percent',
  'text', 'textarea', 'password', 'json', 'cron', 'model-ref', 'skill-ref', 'pair',
  'datetime', 'slider', 'time', 'duration', 'path', 'secret-ref',
]);

/**
 * The JSON-Schema types an `x-widget` can actually render.
 *
 * `research.slots` is a `list[str]` annotated `x-widget: time`: the annotation describes the
 * *entries*, so the array itself must stay an array widget and each item gets the clock
 * picker (see {@link withInheritedPresentation}).  Without this table the container would
 * resolve to `time` and the list would lose its add/remove controls.
 */
const WIDGET_TYPES: Readonly<Record<string, ReadonlySet<JsonSchemaType>>> = {
  slider: new Set<JsonSchemaType>(['number', 'integer']),
  duration: new Set<JsonSchemaType>(['number', 'integer']),
  percent: new Set<JsonSchemaType>(['number', 'integer']),
  cron: new Set<JsonSchemaType>(['string']),
  time: new Set<JsonSchemaType>(['string']),
  path: new Set<JsonSchemaType>(['string']),
  datetime: new Set<JsonSchemaType>(['string']),
  'secret-ref': new Set<JsonSchemaType>(['string']),
  'model-ref': new Set<JsonSchemaType>(['string']),
  'skill-ref': new Set<JsonSchemaType>(['string']),
  pair: new Set<JsonSchemaType>(['string']),
};

/**
 * Presentation annotations an array item or a map value inherits from its container.
 *
 * Pydantic never annotates `list[str]` items or `dict[str, float]` values — the `Field(...)`
 * sits on the container — so the server-side index (`console/schema_meta.INHERITED_KEYS`)
 * pushes `x-widget`/`x-unit` down to them.  The form has to do the same, or
 * `research.slots[0]` is a bare text box while Ctrl-K calls it a time.
 */
export function withInheritedPresentation(container: JsonSchema, child: JsonSchema): JsonSchema {
  const widget = child['x-widget'] ?? container['x-widget'];
  const unit = child['x-unit'] ?? container['x-unit'];
  return {
    ...child,
    ...(widget !== undefined ? { 'x-widget': widget } : {}),
    ...(unit !== undefined ? { 'x-unit': unit } : {}),
  };
}

/**
 * Widget resolution (spec 12): an explicit `x-widget` this form implements — and that suits
 * the node's JSON-Schema type — wins, otherwise the type plus `x-unit`/`format` decide.
 */
export function widgetFor(schema: JsonSchema): WidgetName {
  const explicit = schema['x-widget'];
  if (explicit && IMPLEMENTED_WIDGETS.has(explicit)) {
    const allowed = WIDGET_TYPES[explicit];
    const type = schemaType(schema);
    if (!allowed || type === undefined || allowed.has(type)) return explicit as WidgetName;
  }
  if (schema.enum && schema.enum.length > 0) return 'select';
  const type = schemaType(schema);
  switch (type) {
    case 'boolean':
      return 'switch';
    case 'object':
      return schema.properties && Object.keys(schema.properties).length > 0 ? 'object' : 'map';
    case 'array':
      return 'array';
    case 'integer':
    case 'number':
      return schema['x-unit'] === 'fraction' ? 'percent' : 'number';
    case 'string':
      if (schema.format === 'date-time') return 'datetime';
      return 'text';
    default:
      return 'json';
  }
}

export function titleFor(key: string, schema: JsonSchema): string {
  if (schema.title) return schema.title;
  return key
    .split(/[._-]/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

export function isProtected(schema: JsonSchema): boolean {
  return schema['x-protected'] === true;
}

export function unitSuffix(schema: JsonSchema): string | undefined {
  const unit = schema['x-unit'];
  if (!unit) return undefined;
  if (unit === 'fraction') return '%';
  return ` ${unit}`;
}

/** Default value for a newly added array item / map entry. */
export function defaultForSchema(schema: JsonSchema, root: JsonSchema): unknown {
  const { schema: resolved } = normaliseSchema(schema, root);
  if (resolved.default !== undefined) return resolved.default;
  switch (schemaType(resolved)) {
    case 'object': {
      if (!resolved.properties) return {};
      const out: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(resolved.properties)) {
        const value = defaultForSchema(child, root);
        if (value !== undefined) out[key] = value;
      }
      return out;
    }
    case 'array':
      return [];
    case 'boolean':
      return false;
    case 'integer':
    case 'number':
      return resolved.minimum ?? 0;
    case 'string':
      return resolved.enum && resolved.enum.length > 0 ? String(resolved.enum[0]) : '';
    default:
      return null;
  }
}

/** Issues addressed exactly at `path` (leaf errors) . */
export function issuesAt(issues: ValidationIssue[], path: string): ValidationIssue[] {
  return issues.filter((issue) => issue.path === path);
}

/** Issues anywhere below `path`, used to flag a collapsed section. */
export function issuesUnder(issues: ValidationIssue[], path: string): ValidationIssue[] {
  if (path === '') return issues;
  return issues.filter((issue) => issue.path === path || issue.path.startsWith(`${path}.`) || issue.path.startsWith(`${path}[`));
}

export function sortedProperties(schema: JsonSchema): Array<[string, JsonSchema]> {
  const entries = Object.entries(schema.properties ?? {});
  return entries.sort((a, b) => {
    const oa = a[1]['x-order'] ?? 0;
    const ob = b[1]['x-order'] ?? 0;
    if (oa !== ob) return oa - ob;
    return 0;
  });
}

/** Group properties by `x-group` for the Settings section tree; ungrouped keeps `''`. */
export function groupProperties(schema: JsonSchema): Map<string, Array<[string, JsonSchema]>> {
  const groups = new Map<string, Array<[string, JsonSchema]>>();
  for (const entry of sortedProperties(schema)) {
    const group = entry[1]['x-group'] ?? '';
    const bucket = groups.get(group);
    if (bucket) bucket.push(entry);
    else groups.set(group, [entry]);
  }
  return groups;
}
