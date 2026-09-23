import { Alert, Button, Code, Group, Stack, Text, Title } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  /** The screen's name, so the message says which one broke. */
  title: string;
  /** Changing this resets the boundary — the route path, so navigating away recovers. */
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
  resetKey: string;
}

/**
 * One screen failing must not blank the console.
 *
 * Without this, any exception thrown while rendering a page unmounts the whole React tree:
 * the header, the mode badges, the safety strip and the kill switch all disappear and the
 * operator sees a white rectangle.  That is how "I click on it and nothing happens" looks
 * from the outside, and it is also the worst possible failure mode for a trading console —
 * the kill switch has to survive a broken page.
 *
 * So each route renders inside a boundary: the chrome stays, the screen is replaced by
 * what went wrong and a Retry, and navigating to another screen clears it (the boundary is
 * keyed on the path, and `getDerivedStateFromProps` drops a stale error when it changes).
 */
export class RouteErrorBoundary extends Component<Props, State> {
  override state: State = { error: null, resetKey: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.resetKey) return { error: null, resetKey: props.resetKey };
    return null;
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Browser console only: never a network call, and never written anywhere it could be
    // persisted — a thrown API error can carry a request body with it.
    console.error(`[${this.props.title}] screen failed to render`, error, info.componentStack);
  }

  private retry = () => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <Stack gap="md" data-testid="route-error">
        <Title order={3}>{this.props.title}</Title>
        <Alert
          color="red"
          icon={<IconAlertTriangle size={18} />}
          title="This screen could not be drawn"
        >
          <Stack gap="xs">
            <Text size="sm">
              The rest of the console is still working — the kill switch, the mode badges and
              the safety strip at the top are live. Only this screen failed.
            </Text>
            <Code block data-testid="route-error-detail">
              {error.message || String(error)}
            </Code>
            <Group gap="xs">
              <Button size="xs" onClick={this.retry} data-testid="route-error-retry">
                Try again
              </Button>
            </Group>
          </Stack>
        </Alert>
      </Stack>
    );
  }
}
