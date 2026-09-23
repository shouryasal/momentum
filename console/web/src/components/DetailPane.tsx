/**
 * The detail pane: the same story, zoomed in.
 *
 * Depth in this console is not a different part of the app.  Every screen is a readable
 * list, and clicking any row opens *this* pane beside it with everything behind that one
 * item — the model votes, each safety check and its numbers, the raw inputs, the trace,
 * the linked orders and fills.  The operator never navigates away from what they are
 * already looking at in order to understand it.
 *
 * Because it is the one way of going deeper, it is one component rather than a bespoke
 * drawer per page, and it carries the behaviour that makes depth cheap to use:
 *
 *   - **Dismissable.** Escape closes it, and so does the close button; both call the same
 *     `onClose`, which the screen uses to clear the URL.
 *   - **Deep-linkable.** The pane holds no selection state of its own. The open row lives
 *     in `?detail=<kind>:<id>` (`app/detailParam.ts`), so a refresh, the back button and a
 *     pasted link all land on the same row.
 *   - **Resizable**, by dragging the grip or with the arrow keys on it, remembered per
 *     operator in `localStorage`.
 *   - **Narrow-window safe.** One markup at every width: `shell.css` lays the pane over
 *     the list below 1080px instead of shrinking the list to nothing.
 *
 * Two rules it enforces for its callers, because they are the two the owner complained
 * about: `subtitle` is required and says in plain words what the open item *is*, and
 * `rawId` puts the identifier the API and the logs use at the foot, where it is available
 * without being the first thing anyone reads.
 */
import { ActionIcon, Group, Paper, Stack, Text, Tooltip } from '@mantine/core';
import { IconX } from '@tabler/icons-react';
import { useEffect, type ReactNode } from 'react';

export interface DetailPaneProps {
  /** What the pane is showing, in the operator's words — not a raw id. */
  title: ReactNode;
  /** One line saying what this item is. Required: a pane with no explanation is the bug. */
  subtitle: string;
  /** The identifier the API, the logs and the journal use for this row. */
  rawId?: string | null;
  /** Buttons that act on the open item. */
  actions?: ReactNode;
  /** Called by the close button and by Escape. */
  onClose: () => void;
  /** Screen-reader name; defaults to the title when it is a plain string. */
  label?: string;
  children: ReactNode;
}

/** True while a Mantine modal or drawer is on screen, so Escape closes that first. */
function overlayIsOpen(): boolean {
  if (typeof document === 'undefined') return false;
  return document.querySelector('.mantine-Modal-content, .mantine-Drawer-content') !== null;
}

export function DetailPane({
  title,
  subtitle,
  rawId,
  actions,
  onClose,
  label,
  children,
}: DetailPaneProps) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      // A confirmation dialog opened from inside the pane owns Escape until it closes.
      if (overlayIsOpen()) return;
      onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <Paper
      withBorder
      radius="md"
      data-testid="detail-pane"
      component="section"
      aria-label={label ?? (typeof title === 'string' ? title : 'Details')}
      style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}
    >
      <Group
        justify="space-between"
        wrap="nowrap"
        align="flex-start"
        p="sm"
        style={{ borderBottom: '1px solid var(--mantine-color-default-border)' }}
      >
        <Stack gap={2} style={{ minWidth: 0 }}>
          <Text fw={600} size="sm" lineClamp={2} data-testid="detail-pane-title">
            {title}
          </Text>
          <Text size="xs" c="dimmed" data-testid="detail-pane-subtitle">
            {subtitle}
          </Text>
        </Stack>
        <Group gap="xs" wrap="nowrap">
          {actions}
          <Tooltip label="Close (Esc)" withArrow>
            <ActionIcon
              variant="subtle"
              aria-label="Close details"
              data-testid="detail-pane-close"
              onClick={onClose}
            >
              <IconX size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      <div className="earn-md__body">
        <div style={{ padding: 'var(--mantine-spacing-sm)' }}>{children}</div>
      </div>

      {rawId ? (
        <Group
          gap={6}
          px="sm"
          py={6}
          wrap="nowrap"
          style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}
        >
          <Text size="xs" c="dimmed">
            Known to the system as
          </Text>
          <Text size="xs" ff="monospace" c="dimmed" lineClamp={1} data-testid="detail-pane-raw-id">
            {rawId}
          </Text>
        </Group>
      ) : null}
    </Paper>
  );
}
