/**
 * The detail pane: one component, used everywhere, that behaves the same everywhere.
 *
 * Depth in this console is not a separate part of the app — it is the same story zoomed
 * in, opened beside the list the row was clicked in. That only works if going deeper is
 * cheap and reversible, which is what these tests hold: Escape and the close button both
 * dismiss it, the open row lives in the URL so a refresh and a pasted link land on the
 * same thing, the width is draggable and keyboard-adjustable and remembered, and a row
 * opened from one screen never opens the wrong thing on another.
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MantineProvider } from '@mantine/core';
import { useState } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  DETAIL_PARAM,
  detailHref,
  parseDetailValue,
  useDetailRoute,
  useDetailSelection,
} from '../app/detailParam';
import { DetailPane } from '../components/DetailPane';
import {
  DETAIL_WIDTH_DEFAULT,
  DETAIL_WIDTH_MAX,
  DETAIL_WIDTH_MIN,
  DETAIL_WIDTH_STEP,
  DETAIL_WIDTH_STORAGE_KEY,
  MasterDetail,
  clampDetailWidth,
  storedDetailWidth,
} from '../components/MasterDetail';
import { theme } from '../theme';

const ROWS = ['btc-1', 'eth-2'];

/** A screen in the shape every screen has: a list, and the pane the list opens. */
function Screen({ kind = 'row' }: { kind?: string }) {
  const selection = useDetailSelection(kind);
  const location = useLocation();
  return (
    <>
      <div data-testid="search">{location.search}</div>
      <MasterDetail
        detail={
          selection.id ? (
            <DetailPane
              title={`Row ${selection.id}`}
              subtitle="Everything behind this one row."
              rawId={selection.id}
              onClose={selection.close}
            >
              <div data-testid="detail-body">loaded {selection.id}</div>
            </DetailPane>
          ) : null
        }
      >
        {ROWS.map((row) => (
          <button key={row} type="button" onClick={() => selection.open(row)}>
            {row}
          </button>
        ))}
      </MasterDetail>
    </>
  );
}

function renderScreen(route = '/trading', kind = 'row') {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark" env="test">
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/trading" element={<Screen kind={kind} />} />
        </Routes>
      </MemoryRouter>
    </MantineProvider>,
  );
}

beforeEach(() => {
  try {
    globalThis.localStorage?.clear();
  } catch {
    /* a private window has no localStorage; the component copes and so does this */
  }
});

describe('the detail parameter', () => {
  it('reads and writes `kind:id`, keeping ids that contain a colon or a plus', () => {
    expect(parseDetailValue('run:2026-09-22T08:30+04:00')).toEqual({
      kind: 'run',
      id: '2026-09-22T08:30+04:00',
    });
    expect(parseDetailValue(null)).toBeNull();
    expect(parseDetailValue('nokind')).toBeNull();
    expect(parseDetailValue(':leading')).toBeNull();
    expect(parseDetailValue('trailing:')).toBeNull();
  });

  it('builds a link another screen can hand out', () => {
    const href = detailHref('/decisions', 'run', '2026-09-22T08:30+04:00');
    expect(href.startsWith('/decisions?')).toBe(true);
    const value = new URLSearchParams(href.split('?')[1]).get(DETAIL_PARAM);
    expect(value).toBe('run:2026-09-22T08:30+04:00');
  });
});

describe('opening and closing', () => {
  it('opens the clicked row, with that row loaded', async () => {
    const user = userEvent.setup();
    renderScreen();
    expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument();
    expect(screen.getByTestId('master-detail')).toHaveAttribute('data-detail-open', 'false');

    await user.click(screen.getByText('btc-1'));

    expect(await screen.findByTestId('detail-pane')).toBeInTheDocument();
    expect(screen.getByTestId('detail-pane-title')).toHaveTextContent('Row btc-1');
    expect(screen.getByTestId('detail-body')).toHaveTextContent('loaded btc-1');
    expect(screen.getByTestId('detail-pane-subtitle')).toHaveTextContent(
      'Everything behind this one row.',
    );
    expect(screen.getByTestId('detail-pane-raw-id')).toHaveTextContent('btc-1');
    expect(screen.getByTestId('master-detail')).toHaveAttribute('data-detail-open', 'true');
    expect(screen.getByTestId('search')).toHaveTextContent('detail=row%3Abtc-1');
  });

  it('switches to another row without closing', async () => {
    const user = userEvent.setup();
    renderScreen();
    await user.click(screen.getByText('btc-1'));
    await screen.findByTestId('detail-pane');
    await user.click(screen.getByText('eth-2'));
    await waitFor(() =>
      expect(screen.getByTestId('detail-body')).toHaveTextContent('loaded eth-2'),
    );
  });

  it('closes on the close button, and clears the URL', async () => {
    const user = userEvent.setup();
    renderScreen('/trading?detail=row%3Abtc-1');
    await screen.findByTestId('detail-pane');
    await user.click(screen.getByTestId('detail-pane-close'));
    await waitFor(() => expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument());
    expect(screen.getByTestId('search')).not.toHaveTextContent('detail');
  });

  it('closes on Escape', async () => {
    const user = userEvent.setup();
    renderScreen('/trading?detail=row%3Abtc-1');
    await screen.findByTestId('detail-pane');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument());
  });

  it('leaves the rest of the query string alone when it closes', async () => {
    const user = userEvent.setup();
    renderScreen('/trading?file=earn&detail=row%3Abtc-1');
    await screen.findByTestId('detail-pane');
    await user.click(screen.getByTestId('detail-pane-close'));
    await waitFor(() => expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument());
    expect(screen.getByTestId('search')).toHaveTextContent('file=earn');
  });
});

describe('deep links', () => {
  it('restores the open row straight from the URL', async () => {
    renderScreen('/trading?detail=row%3Aeth-2');
    expect(await screen.findByTestId('detail-pane')).toBeInTheDocument();
    expect(screen.getByTestId('detail-body')).toHaveTextContent('loaded eth-2');
  });

  it('ignores a row belonging to another screen rather than opening the wrong thing', async () => {
    renderScreen('/trading?detail=order%3A41', 'row');
    await screen.findByTestId('master-detail');
    expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument();
  });
});

/** A screen that hosts more than one kind of row, like Trading does. */
function MultiScreen() {
  const detail = useDetailRoute(['position', 'order']);
  return (
    <MasterDetail
      detail={
        detail.opened ? (
          <DetailPane
            title={`${detail.kind} ${detail.id}`}
            subtitle="One row, whichever kind it is."
            onClose={detail.close}
          >
            <div data-testid="detail-body">{`${detail.kind}/${detail.id}`}</div>
          </DetailPane>
        ) : null
      }
    >
      <button type="button" onClick={() => detail.open('position', 'BTC/USDT')}>
        position
      </button>
      <button type="button" onClick={() => detail.open('order', '41')}>
        order
      </button>
    </MasterDetail>
  );
}

describe('a screen with several kinds of row', () => {
  it('opens each kind in the same pane', async () => {
    const user = userEvent.setup();
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark" env="test">
        <MemoryRouter initialEntries={['/trading']}>
          <MultiScreen />
        </MemoryRouter>
      </MantineProvider>,
    );
    await user.click(screen.getByText('position'));
    expect(await screen.findByTestId('detail-body')).toHaveTextContent('position/BTC/USDT');
    await user.click(screen.getByText('order'));
    await waitFor(() => expect(screen.getByTestId('detail-body')).toHaveTextContent('order/41'));
  });
});

describe('the pane width', () => {
  it('clamps to something usable and survives a broken stored value', () => {
    expect(clampDetailWidth(10)).toBe(DETAIL_WIDTH_MIN);
    expect(clampDetailWidth(5000)).toBe(DETAIL_WIDTH_MAX);
    expect(clampDetailWidth(Number.NaN)).toBe(DETAIL_WIDTH_DEFAULT);
    expect(storedDetailWidth()).toBe(DETAIL_WIDTH_DEFAULT);
  });

  it('is adjustable from the keyboard and remembered', async () => {
    const user = userEvent.setup();
    renderScreen('/trading?detail=row%3Abtc-1');
    await screen.findByTestId('detail-pane');
    const grip = screen.getByTestId('detail-pane-grip');
    expect(grip).toHaveAttribute('aria-valuenow', String(DETAIL_WIDTH_DEFAULT));

    grip.focus();
    await user.keyboard('{ArrowLeft}');
    expect(screen.getByTestId('detail-pane-grip')).toHaveAttribute(
      'aria-valuenow',
      String(DETAIL_WIDTH_DEFAULT + DETAIL_WIDTH_STEP),
    );
    expect(storedDetailWidth()).toBe(DETAIL_WIDTH_DEFAULT + DETAIL_WIDTH_STEP);

    await user.keyboard('{ArrowRight}{ArrowRight}');
    expect(screen.getByTestId('detail-pane-grip')).toHaveAttribute(
      'aria-valuenow',
      String(DETAIL_WIDTH_DEFAULT - DETAIL_WIDTH_STEP),
    );
  });

  it('starts from the width this operator last chose', async () => {
    globalThis.localStorage.setItem(`earn.console.${DETAIL_WIDTH_STORAGE_KEY}`, '640');
    renderScreen('/trading?detail=row%3Abtc-1');
    await screen.findByTestId('detail-pane');
    expect(screen.getByTestId('detail-pane-grip')).toHaveAttribute('aria-valuenow', '640');
    expect(screen.getByTestId('master-detail')).toHaveAttribute(
      'style',
      expect.stringContaining('640px') as unknown as string,
    );
  });
});

describe('a confirmation opened from inside the pane', () => {
  /**
   * Escape has to reach the dialog, not the pane underneath it. A step-up confirmation is
   * a safety affordance; dismissing the pane out from under it would look like the
   * dangerous action was cancelled when the dialog is what closed.
   */
  function WithDialog() {
    const [open, setOpen] = useState(true);
    return (
      <>
        <DetailPane title="Row" subtitle="A row." onClose={() => setOpen(false)}>
          <div data-testid="detail-body">body</div>
        </DetailPane>
        {open ? (
          <div className="mantine-Modal-content" data-testid="fake-modal">
            confirm
          </div>
        ) : null}
      </>
    );
  }

  it('does not close the pane while a dialog is on screen', async () => {
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark" env="test">
        <WithDialog />
      </MantineProvider>,
    );
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(screen.getByTestId('detail-pane')).toBeInTheDocument();
    expect(screen.getByTestId('fake-modal')).toBeInTheDocument();
  });
});
