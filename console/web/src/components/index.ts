/** Shared components P1..P7 build their pages from. */
export { CodeEditor, languageForPath, loadLanguage, type EditorLanguage } from './CodeEditor';
export { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog';
export { DataTable, type DataTableColumn, type DataTableProps } from './DataTable';
export { DiffView, type DiffViewProps } from './DiffView';
export { EmptyState, type EmptyStateProps } from './EmptyState';
export { ErrorAlert, type ErrorAlertProps } from './ErrorAlert';
export { JsonViewer, type JsonViewerProps } from './JsonViewer';
export { KillButton, RESUME_TRADING_PHRASE, type KillButtonProps } from './KillButton';
export { SSEIndicator, type SSEIndicatorProps } from './SSEIndicator';
export { StatCard, type StatCardProps } from './StatCard';
export {
  SchemaForm,
  SchemaField,
  FieldLabel,
  LeafWidget,
  widgetFor,
  normaliseSchema,
  defaultForSchema,
  groupProperties,
  issuesAt,
  issuesUnder,
  titleFor,
  type JsonSchema,
  type SchemaFormOptions,
  type SchemaFormProps,
  type SelectOption,
  type ValidationIssue,
  type WidgetName,
} from './SchemaForm';
