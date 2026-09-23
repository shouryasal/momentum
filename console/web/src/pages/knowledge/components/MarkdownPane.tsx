/**
 * Markdown shown as text, deliberately.
 *
 * The console's CSP forbids inline scripts and the spec renders markdown through
 * rehype-sanitize; until that pipeline exists in the shell, a brief or a dossier is
 * displayed as preformatted text rather than parsed HTML. Showing a document unrendered is
 * a cosmetic loss; rendering untrusted markdown without sanitising is not.
 */
import { Code, Loader, ScrollArea } from '@mantine/core';

import { EmptyState } from '@/components';

export interface MarkdownPaneProps {
  text?: string;
  loading?: boolean;
  maxHeight?: number;
}

export function MarkdownPane({ text, loading, maxHeight = 540 }: MarkdownPaneProps) {
  if (loading) return <Loader />;
  if (!text) return <EmptyState compact title="Nothing to show" />;
  return (
    <ScrollArea.Autosize mah={maxHeight}>
      <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>
        {text}
      </Code>
    </ScrollArea.Autosize>
  );
}
