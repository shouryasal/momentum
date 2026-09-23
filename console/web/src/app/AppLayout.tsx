import {
  ActionIcon,
  AppShell,
  Badge,
  Box,
  Burger,
  Group,
  Indicator,
  Menu,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
  useMantineColorScheme,
} from '@mantine/core';
import { useDisclosure, useHotkeys } from '@mantine/hooks';
import { notifications } from '@mantine/notifications';
import {
  IconBell,
  IconLayoutGrid,
  IconLogout,
  IconMoon,
  IconSearch,
  IconSun,
  IconTool,
  IconUser,
} from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { NavLink as RouterNavLink, useLocation, useNavigate } from 'react-router-dom';

import type {
  HealthSnapshot,
  MetaResponse,
  ProvidersResponse,
  SafetyStripResponse,
} from '../api/contracts';
import { errorMessage } from '../api/errors';
import { shellKeys } from '../api/queryClient';
import { KillButton } from '../components/KillButton';
import { SSEIndicator } from '../components/SSEIndicator';
import { DEVELOPER_ROUTE_IDS, PRIMARY_NAV, developerNavGroups, routeById } from '../routes';
import { AlertsDrawer } from './AlertsDrawer';
import { useEndpoints } from './ApiContext';
import { pendingApprovalCount } from './approvals';
import { ChromeGuard } from './ChromeGuard';
import { Clock } from './Clock';
import { CommandPalette, useShellCommands } from './CommandPalette';
import { useEvents } from './EventStreamContext';
import { healthSummaryFrom } from './health';
import { HealthDot } from './HealthDot';
import { ModeBadges } from './ModeBadges';
import { ProviderChip } from './ProviderChip';
import { SafetyStrip } from './SafetyStrip';
import { useSession } from './SessionContext';
import { StepUpLock } from './StepUpLock';
import { useViewMode } from './ViewModeContext';

/** Global chrome from spec 12, mounted on every route. */
export function AppLayout({ children }: { children: ReactNode }) {
  const calls = useEndpoints();
  const [navOpen, navHandlers] = useDisclosure(false);
  const [alertsOpen, alertsHandlers] = useDisclosure(false);
  const [paletteOpen, paletteHandlers] = useDisclosure(false);
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  const { session, stepUpActive, stepUp, logout } = useSession();
  const events = useEvents();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const view = useViewMode();

  useShellCommands();
  useHotkeys([
    ['mod+K', () => paletteHandlers.open()],
    ['mod+J', () => toggleColorScheme()],
  ]);

  const meta = useQuery<MetaResponse>({
    queryKey: shellKeys.meta,
    queryFn: () => calls.meta(),
    refetchInterval: 60_000,
  });
  const health = useQuery<HealthSnapshot>({
    queryKey: shellKeys.health,
    queryFn: () => calls.opsHealth(),
    refetchInterval: 60_000,
    retry: false,
  });
  const approvals = useQuery({
    queryKey: shellKeys.approvals,
    queryFn: () => calls.approvals(),
    refetchInterval: 60_000,
    retry: false,
  });
  const providers = useQuery<ProvidersResponse>({
    queryKey: shellKeys.providers,
    queryFn: () => calls.providers(),
    refetchInterval: 120_000,
    retry: false,
  });
  const safetyStrip = useQuery<SafetyStripResponse>({
    queryKey: shellKeys.safetyStrip,
    queryFn: () => calls.safetyStrip(),
    refetchInterval: 120_000,
    retry: false,
  });

  const killed = meta.data?.kill.engaged === true;
  const killReason = meta.data?.kill.reason ?? null;
  // `PendingResponse` carries no count, and its items include decided/expired rows.
  const pending = pendingApprovalCount(approvals.data);

  const onKill = async ({ reason, flatten }: { reason: string; flatten: boolean }) => {
    try {
      await calls.kill({ reason, flatten });
      notifications.show({ color: 'red', title: 'KILL engaged', message: reason });
    } catch (error) {
      notifications.show({ color: 'red', title: 'KILL failed', message: errorMessage(error) });
      throw error;
    } finally {
      void queryClient.invalidateQueries({ queryKey: shellKeys.meta });
    }
  };

  const onResume = async ({
    confirmPhrase,
    stepUpToken,
  }: {
    confirmPhrase: string;
    stepUpToken?: string;
  }) => {
    if (stepUpToken) await stepUp(stepUpToken);
    await calls.resume(confirmPhrase);
    notifications.show({ color: 'teal', title: 'Trading resumed', message: 'KILL removed' });
    void queryClient.invalidateQueries({ queryKey: shellKeys.meta });
  };

  return (
    <AppShell
      header={{ height: killed ? 88 : 76 }}
      navbar={{ width: 232, breakpoint: 'sm', collapsed: { mobile: !navOpen } }}
      padding="md"
    >
      <AppShell.Header data-testid="app-header" data-killed={killed ? 'true' : 'false'}>
        <Box bg={killed ? 'var(--mantine-color-red-light)' : undefined}>
          <Group h={48} px="md" justify="space-between" wrap="nowrap">
            <Group gap="sm" wrap="nowrap">
              <Burger opened={navOpen} onClick={navHandlers.toggle} hiddenFrom="sm" size="sm" />
              <Text fw={800} size="sm" ff="monospace">
                EARN
              </Text>
              <ChromeGuard label="mode badges">
                <ModeBadges modes={meta.data?.modes ?? []} />
              </ChromeGuard>
              {killed ? (
                <Badge color="red" variant="filled" data-testid="kill-banner">
                  KILL{killReason ? `: ${killReason}` : ''}
                </Badge>
              ) : null}
            </Group>

            <Group gap="xs" wrap="nowrap">
              <Clock />
              <ChromeGuard label="health dot">
                <HealthDot summary={healthSummaryFrom(health.data)} />
              </ChromeGuard>
              <ChromeGuard label="model status">
                <ProviderChip data={providers.data ?? null} />
              </ChromeGuard>
              <Tooltip
                label={
                  pending === 0
                    ? 'Nothing is waiting on you.'
                    : `${pending} thing(s) are waiting for your yes or no.`
                }
              >
                <Indicator label={pending > 0 ? String(pending) : undefined} disabled={pending === 0} size={16}>
                  <ActionIcon
                    variant="subtle"
                    aria-label="things waiting on you"
                    data-testid="approvals-counter"
                    data-pending={pending}
                    onClick={() => navigate('/decisions')}
                  >
                    <IconUser size={18} />
                  </ActionIcon>
                </Indicator>
              </Tooltip>
              <SSEIndicator status={events.status} attempt={events.attempt} onRetry={events.retryNow} />
              <StepUpLock />
              <Tooltip label="Alerts">
                <Indicator
                  label={events.unread > 0 ? String(events.unread) : undefined}
                  disabled={events.unread === 0}
                  size={16}
                >
                  <ActionIcon variant="subtle" aria-label="alerts" data-testid="alerts-button" onClick={alertsHandlers.open}>
                    <IconBell size={18} />
                  </ActionIcon>
                </Indicator>
              </Tooltip>
              <Tooltip label="Search (Ctrl-K)">
                <ActionIcon variant="subtle" aria-label="search" data-testid="palette-button" onClick={paletteHandlers.open}>
                  <IconSearch size={18} />
                </ActionIcon>
              </Tooltip>
              <ActionIcon
                variant="subtle"
                aria-label="toggle colour scheme"
                data-testid="theme-toggle"
                onClick={() => toggleColorScheme()}
              >
                {colorScheme === 'dark' ? <IconSun size={18} /> : <IconMoon size={18} />}
              </ActionIcon>
              <KillButton
                engaged={killed}
                reason={killReason}
                stepUpSatisfied={stepUpActive}
                onKill={onKill}
                onResume={onResume}
                compact
              />
              <Menu position="bottom-end">
                <Menu.Target>
                  <ActionIcon variant="subtle" aria-label="account" data-testid="account-menu">
                    <IconUser size={18} />
                  </ActionIcon>
                </Menu.Target>
                <Menu.Dropdown>
                  <Menu.Label>{session?.actor ?? 'human'}</Menu.Label>
                  <Menu.Item leftSection={<IconLogout size={14} />} onClick={() => void logout()}>
                    Sign out
                  </Menu.Item>
                </Menu.Dropdown>
              </Menu>
            </Group>
          </Group>
          <ChromeGuard label="safety strip">
            <SafetyStrip strip={safetyStrip.data ?? null} invariants={meta.data?.invariants ?? []} />
          </ChromeGuard>
        </Box>
      </AppShell.Header>

      <AppShell.Navbar p="xs">
        <ScrollArea>
          <Stack gap="xs">
            {/* The four destinations plus Setup, in both views: the operator's places do
                not move when the developer area is open. */}
            <Stack gap={2} data-testid="nav-primary">
              {PRIMARY_NAV.map((entry) => {
                const route = routeById(entry.routeId);
                if (!route) return null;
                const Icon = entry.icon;
                return (
                  <Tooltip key={entry.routeId} label={entry.blurb} position="right" multiline w={280}>
                    <NavLink
                      component={RouterNavLink}
                      to={route.path}
                      label={entry.label}
                      leftSection={<Icon size={16} />}
                      active={location.pathname === route.path}
                      onClick={navHandlers.close}
                      data-testid={`nav-${route.id}`}
                    />
                  </Tooltip>
                );
              })}
            </Stack>

            {view.developer
              ? developerNavGroups().map(({ group, routes }) => (
                  <Stack key={group} gap={2} data-testid={`nav-developer-${group.toLowerCase()}`}>
                    <Text size="xs" c="dimmed" fw={700} tt="uppercase" px="xs">
                      {group}
                    </Text>
                    {routes.map((route) => {
                      const Icon = route.icon;
                      return (
                        <NavLink
                          key={route.id}
                          component={RouterNavLink}
                          to={route.path}
                          label={route.title}
                          leftSection={<Icon size={16} />}
                          active={location.pathname === route.path}
                          onClick={navHandlers.close}
                          data-testid={`nav-${route.id}`}
                        />
                      );
                    })}
                  </Stack>
                ))
              : null}

            <DeveloperSwitch developer={view.developer} onToggle={view.toggle} />
          </Stack>
        </ScrollArea>
      </AppShell.Navbar>

      <AppShell.Main>{children}</AppShell.Main>

      <AlertsDrawer opened={alertsOpen} onClose={alertsHandlers.close} />
      <CommandPalette opened={paletteOpen} onClose={paletteHandlers.close} />
    </AppShell>
  );
}

/**
 * The Developer switch, in the corner of the navigation.
 *
 * The five destinations above it never move.  This adds the sixteen builder-shaped
 * screens below them — the labs, the internals, the generated settings form, the audit
 * trail — grouped as the system is built.  It changes the navigation only: every route
 * stays mounted in both views, so a developer screen is still one URL or one Ctrl-K away
 * with the area closed, and the choice is remembered for this operator in `localStorage`.
 */
function DeveloperSwitch({ developer, onToggle }: { developer: boolean; onToggle: () => void }) {
  const extra = DEVELOPER_ROUTE_IDS.length;
  return (
    <Box pt="sm" mt="xs" style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
      <NavLink
        onClick={onToggle}
        label={developer ? 'Hide developer screens' : 'Developer'}
        description={
          developer
            ? 'Back to the five screens a demo run needs'
            : `The other ${extra} screens: labs, internals, audit`
        }
        leftSection={developer ? <IconLayoutGrid size={16} /> : <IconTool size={16} />}
        data-testid="view-mode-toggle"
        data-view={developer ? 'developer' : 'operator'}
      />
      <Text size="xs" c="dimmed" px="xs" pt={4}>
        {developer
          ? 'Showing every screen, grouped as the system is built.'
          : 'They are all still there: type the address, or press Ctrl-K.'}
      </Text>
    </Box>
  );
}
