import { Badge, Tooltip } from '@mantine/core';
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  /** What this widget is, for the replacement chip and the console line. */
  label: string;
  children: ReactNode;
}

interface State {
  failed: boolean;
}

/**
 * One header widget failing must never take the kill switch off the screen.
 *
 * `AppLayout` renders above every route's error boundary, so anything that throws while
 * drawing the header unmounts the entire React tree: no header, no mode badges, no safety
 * strip, and no kill switch. From the outside that is a blank page on which no click does
 * anything — the symptom the owner reported. It has already happened once for real, when a
 * health payload arrived without `freshness_minutes` and `Object.entries(undefined)` threw
 * inside the header's render.
 *
 * So each informational widget is wrapped here. A widget that throws is replaced by a chip
 * saying so, and everything beside it — above all the kill switch — keeps working. The kill
 * switch itself is deliberately **not** wrapped: a kill switch that fails must fail loudly,
 * not shrink into a grey chip.
 */
export class ChromeGuard extends Component<Props, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Browser console only — a thrown API error can carry a request body with it.
    console.error(`[chrome:${this.props.label}] failed to render`, error, info.componentStack);
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <Tooltip
        label={`The ${this.props.label} could not be drawn. Everything else on this bar, including the kill switch, is still live.`}
        multiline
        w={280}
      >
        <Badge
          color="gray"
          variant="light"
          data-testid={`chrome-failed-${this.props.label.replace(/\s+/g, '-')}`}
        >
          {this.props.label} unavailable
        </Badge>
      </Tooltip>
    );
  }
}
