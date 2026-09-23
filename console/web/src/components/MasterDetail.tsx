/**
 * The shape of every screen: a readable list, and the story behind the row you clicked.
 *
 * `MasterDetail` owns the geometry only — the list on the left, the {@link DetailPane} on
 * the right, and the grip between them.  It holds no selection state: the open row lives
 * in the URL (`app/detailParam.ts`), so the screen passes `detail={null}` when nothing is
 * open and a pane when something is.
 *
 * Below 1080px the pane is laid over the list by `shell.css` rather than squeezing it,
 * and the grip is hidden because there is nothing left to resize.  The markup does not
 * change with the window, so what a test sees is what a narrow window renders.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import { readStored, writeStored } from '../lib/storage';

/** `earn.console.detailWidth` — one operator, one browser, one preferred width. */
export const DETAIL_WIDTH_STORAGE_KEY = 'detailWidth';
export const DETAIL_WIDTH_MIN = 320;
export const DETAIL_WIDTH_MAX = 900;
export const DETAIL_WIDTH_DEFAULT = 460;
/** One arrow-key press, for operators who do not drag. */
export const DETAIL_WIDTH_STEP = 32;

export function clampDetailWidth(width: number): number {
  if (!Number.isFinite(width)) return DETAIL_WIDTH_DEFAULT;
  return Math.min(DETAIL_WIDTH_MAX, Math.max(DETAIL_WIDTH_MIN, Math.round(width)));
}

export function storedDetailWidth(): number {
  return clampDetailWidth(Number(readStored<number>(DETAIL_WIDTH_STORAGE_KEY, DETAIL_WIDTH_DEFAULT)));
}

export interface MasterDetailProps {
  /** The list. Always rendered, at every width. */
  children: ReactNode;
  /** A {@link DetailPane}, or `null` when no row is open. */
  detail: ReactNode;
}

export function MasterDetail({ children, detail }: MasterDetailProps) {
  const [width, setWidth] = useState<number>(storedDetailWidth);
  const dragFrom = useRef<{ x: number; width: number } | null>(null);
  const opened = detail !== null && detail !== undefined && detail !== false;

  const commit = useCallback((next: number) => {
    const clamped = clampDetailWidth(next);
    setWidth(clamped);
    writeStored(DETAIL_WIDTH_STORAGE_KEY, clamped);
  }, []);

  useEffect(() => {
    if (!opened) return;
    const onMove = (event: PointerEvent) => {
      const start = dragFrom.current;
      if (!start) return;
      // The pane is on the right, so dragging left makes it wider.
      setWidth(clampDetailWidth(start.width + (start.x - event.clientX)));
    };
    const onUp = () => {
      if (!dragFrom.current) return;
      dragFrom.current = null;
      setWidth((current) => {
        writeStored(DETAIL_WIDTH_STORAGE_KEY, current);
        return current;
      });
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
  }, [opened]);

  return (
    <div
      className="earn-md"
      data-testid="master-detail"
      data-detail-open={opened ? 'true' : 'false'}
      style={{ ['--earn-detail-width' as string]: `${width}px` }}
    >
      <div className="earn-md__master" data-testid="master-pane">
        {children}
      </div>
      {opened ? (
        <>
          <div
            className="earn-md__grip"
            data-testid="detail-pane-grip"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the details pane"
            aria-valuenow={width}
            aria-valuemin={DETAIL_WIDTH_MIN}
            aria-valuemax={DETAIL_WIDTH_MAX}
            tabIndex={0}
            onPointerDown={(event) => {
              dragFrom.current = { x: event.clientX, width };
              event.currentTarget.setPointerCapture?.(event.pointerId);
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowLeft') {
                event.preventDefault();
                commit(width + DETAIL_WIDTH_STEP);
              } else if (event.key === 'ArrowRight') {
                event.preventDefault();
                commit(width - DETAIL_WIDTH_STEP);
              }
            }}
          />
          <aside className="earn-md__detail">{detail}</aside>
        </>
      ) : null}
    </div>
  );
}
