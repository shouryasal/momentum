import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

import { readStored, writeStored } from '../lib/storage';

/**
 * Which navigation the operator is looking at.
 *
 * `operator` is the four destinations plus Setup; `developer` adds the sixteen
 * builder-shaped screens, grouped as the system is built.  The words are the ones the
 * navigation uses, so the stored value and the label cannot drift apart.
 */
export type ViewMode = 'operator' | 'developer';

/** `earn.console.viewMode` in localStorage — one operator, one browser, one preference. */
export const VIEW_MODE_STORAGE_KEY = 'viewMode';

export interface ViewModeValue {
  mode: ViewMode;
  /** `true` when the console is showing the operator's five destinations. */
  simple: boolean;
  /** `true` when the developer area is open. */
  developer: boolean;
  setMode: (mode: ViewMode) => void;
  toggle: () => void;
}

const ViewModeContext = createContext<ViewModeValue | null>(null);

/**
 * Accepts the words this console used to store.
 *
 * An operator who already has `"simple"` or `"advanced"` in `localStorage` from the
 * previous shell keeps the view they chose instead of being silently reset.
 */
export function asViewMode(value: unknown): ViewMode | null {
  if (value === 'operator' || value === 'simple') return 'operator';
  if (value === 'developer' || value === 'advanced') return 'developer';
  return null;
}

/** What the last visit left behind, defaulting to the operator view on a fresh browser. */
export function storedViewMode(): ViewMode {
  return asViewMode(readStored<unknown>(VIEW_MODE_STORAGE_KEY, 'operator')) ?? 'operator';
}

/**
 * Operator view or Developer area, remembered per operator.
 *
 * Operator is the default because the console's job on day one is to show a person who has
 * never seen it what the system is doing.  The Developer area is one click away in the
 * corner and reveals the other sixteen screens; nothing is removed by being out of the
 * operator navigation, and every route stays reachable by URL and by Ctrl-K in both views.
 *
 * The preference is stored in `localStorage`, which can throw or come back empty in a
 * private window — `lib/storage` swallows that, and the fallback is the operator view.
 */
export function ViewModeProvider({
  children,
  initial,
}: {
  children: ReactNode;
  /** Tests pass this to start in a known view without touching localStorage. */
  initial?: ViewMode;
}) {
  const [mode, setModeState] = useState<ViewMode>(() => initial ?? storedViewMode());

  const setMode = useCallback((next: ViewMode) => {
    setModeState(next);
    writeStored(VIEW_MODE_STORAGE_KEY, next);
  }, []);

  const toggle = useCallback(() => {
    setModeState((current) => {
      const next: ViewMode = current === 'operator' ? 'developer' : 'operator';
      writeStored(VIEW_MODE_STORAGE_KEY, next);
      return next;
    });
  }, []);

  const value = useMemo<ViewModeValue>(
    () => ({
      mode,
      simple: mode === 'operator',
      developer: mode === 'developer',
      setMode,
      toggle,
    }),
    [mode, setMode, toggle],
  );

  return <ViewModeContext.Provider value={value}>{children}</ViewModeContext.Provider>;
}

/**
 * The current view.
 *
 * A page rendered outside the shell (a component test, a pane mounted on its own) has no
 * provider above it; rather than throwing, it reads as the developer area, because a page
 * shown on its own should show everything it has.
 */
export function useViewMode(): ViewModeValue {
  const value = useContext(ViewModeContext);
  if (value) return value;
  return {
    mode: 'developer',
    simple: false,
    developer: true,
    setMode: () => undefined,
    toggle: () => undefined,
  };
}
