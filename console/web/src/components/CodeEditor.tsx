import { Box, Loader, useComputedColorScheme } from '@mantine/core';
import { lazy, Suspense, useEffect, useState } from 'react';
import type { Extension } from '@codemirror/state';

const CodeMirror = lazy(() => import('@uiw/react-codemirror'));

export type EditorLanguage = 'json' | 'yaml' | 'markdown' | 'python' | 'text';

/** Guess the CodeMirror language from a file name (skills tree, prompts, raw config). */
export function languageForPath(path: string): EditorLanguage {
  const lower = path.toLowerCase();
  if (lower.endsWith('.json')) return 'json';
  if (lower.endsWith('.yaml') || lower.endsWith('.yml')) return 'yaml';
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown';
  if (lower.endsWith('.py')) return 'python';
  return 'text';
}

/** Language modes are code-split: the shell bundle must not carry all four grammars. */
export async function loadLanguage(language: EditorLanguage): Promise<Extension[]> {
  switch (language) {
    case 'json':
      return [(await import('@codemirror/lang-json')).json()];
    case 'yaml':
      return [(await import('@codemirror/lang-yaml')).yaml()];
    case 'markdown':
      return [(await import('@codemirror/lang-markdown')).markdown()];
    case 'python':
      return [(await import('@codemirror/lang-python')).python()];
    default:
      return [];
  }
}

export interface CodeEditorProps {
  value: string;
  onChange?: (value: string) => void;
  language?: EditorLanguage;
  readOnly?: boolean;
  height?: string;
  'data-testid'?: string;
}

/** CodeMirror wrapper used by Settings (raw YAML), Skills and Prompts. */
export function CodeEditor({
  value,
  onChange,
  language = 'text',
  readOnly = false,
  height = '340px',
  'data-testid': testId = 'code-editor',
}: CodeEditorProps) {
  const scheme = useComputedColorScheme('dark');
  const [extensions, setExtensions] = useState<Extension[]>([]);

  useEffect(() => {
    let cancelled = false;
    void loadLanguage(language).then((loaded) => {
      if (!cancelled) setExtensions(loaded);
    });
    return () => {
      cancelled = true;
    };
  }, [language]);

  return (
    <Box data-testid={testId} data-language={language}>
      <Suspense fallback={<Loader size="sm" />}>
        <CodeMirror
          value={value}
          height={height}
          theme={scheme === 'dark' ? 'dark' : 'light'}
          extensions={extensions}
          editable={!readOnly}
          onChange={onChange}
          basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: !readOnly }}
        />
      </Suspense>
    </Box>
  );
}
