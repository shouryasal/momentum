import { createTheme, type MantineColorsTuple, type MantineThemeOverride } from '@mantine/core';

/** Muted slate-blue primary: the console is a cockpit, colour means state, not decoration. */
const earn: MantineColorsTuple = [
  '#eef3fb',
  '#dbe4f2',
  '#b5c7e6',
  '#8ca8da',
  '#6b8ecf',
  '#557ec9',
  '#4a76c7',
  '#3b64b0',
  '#32589e',
  '#264b8b',
];

export const theme: MantineThemeOverride = createTheme({
  primaryColor: 'earn',
  colors: { earn },
  defaultRadius: 'md',
  fontFamily:
    'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  fontFamilyMonospace:
    'ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace',
  headings: { fontWeight: '650' },
  components: {
    Table: { defaultProps: { striped: true, highlightOnHover: true, withTableBorder: true } },
    Paper: { defaultProps: { withBorder: true } },
    Card: { defaultProps: { withBorder: true } },
    Tooltip: { defaultProps: { withArrow: true, openDelay: 300 } },
  },
});

/** Mode -> Mantine colour, shared by badges and the header tint. */
export const MODE_COLORS: Record<string, string> = {
  TEST: 'blue',
  ARMING: 'grape',
  LIVE_PROPOSE: 'yellow',
  LIVE_EXECUTE: 'red',
  DISARMING: 'grape',
};

export const STATUS_COLORS: Record<string, string> = {
  ok: 'teal',
  warn: 'yellow',
  fail: 'red',
  unknown: 'gray',
};

export const SEVERITY_COLORS: Record<string, string> = {
  info: 'blue',
  warning: 'yellow',
  critical: 'red',
};
