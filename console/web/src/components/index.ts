/** Shared components P1..P7 build their pages from. */
export { CodeEditor, languageForPath, loadLanguage, type EditorLanguage } from './CodeEditor';
export { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog';
export { DataTable, type DataTableColumn, type DataTableProps } from './DataTable';
export { DetailPane, type DetailPaneProps } from './DetailPane';
export {
  MasterDetail,
  clampDetailWidth,
  storedDetailWidth,
  DETAIL_WIDTH_DEFAULT,
  DETAIL_WIDTH_MAX,
  DETAIL_WIDTH_MIN,
  DETAIL_WIDTH_STEP,
  DETAIL_WIDTH_STORAGE_KEY,
  type MasterDetailProps,
} from './MasterDetail';
export { DiffView, type DiffViewProps } from './DiffView';
export { EmptyState, type EmptyStateProps } from './EmptyState';
export { ErrorAlert, type ErrorAlertProps } from './ErrorAlert';
export {
  ExecutionBadge,
  Explain,
  SleeveName,
  type ExecutionBadgeProps,
  type ExplainProps,
  type SleeveNameProps,
} from './Explain';
export { PageIntro, type PageIntroProps } from './PageIntro';
export { JsonViewer, type JsonViewerProps } from './JsonViewer';
export {
  KillButton,
  KILL_REASON_MAX,
  KILL_REASON_MIN,
  RESUME_TRADING_PHRASE,
  type KillButtonProps,
} from './KillButton';
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
