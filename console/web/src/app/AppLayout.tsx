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
  IconLogout,
  IconMoon,
  IconSearch,
  IconSun,
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
import { NAV_GROUPS, ROUTES } from '../routes';
import { AlertsDrawer } from './AlertsDrawer';
import { useEndpoints } from './ApiContext';
import { pendingApprovalCount } from './approvals';
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
              <ModeBadges modes={meta.data?.modes ?? []} />
              {killed ? (
                <Badge color="red" variant="filled" data-testid="kill-banner">
                  KILL{killReason ? `: ${killReason}` : ''}
                </Badge>
              ) : null}
            </Group>

            <Group gap="xs" wrap="nowrap">
              <Clock />
              <HealthDot summary={healthSummaryFrom(health.data)} />
              <ProviderChip data={providers.data ?? null} />
              <Tooltip label={`${pending} pending approval(s)`}>
                <Indicator label={pending > 0 ? String(pending) : undefined} disabled={pending === 0} size={16}>
                  <ActionIcon
                    variant="subtle"
                    aria-label="pending approvals"
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
          <SafetyStrip strip={safetyStrip.data ?? null} invariants={meta.data?.invariants ?? []} />
        </Box>
      </AppShell.Header>

      <AppShell.Navbar p="xs">
        <ScrollArea>
          <Stack gap="xs">
            {NAV_GROUPS.map((group) => (
              <Stack key={group} gap={2}>
                <Text size="xs" c="dimmed" fw={700} tt="uppercase" px="xs">
                  {group}
                </Text>
                {ROUTES.filter((route) => route.group === group).map((route) => {
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
            ))}
          </Stack>
        </ScrollArea>
      </AppShell.Navbar>

      <AppShell.Main>{children}</AppShell.Main>

      <AlertsDrawer opened={alertsOpen} onClose={alertsHandlers.close} />
      <CommandPalette opened={paletteOpen} onClose={paletteHandlers.close} />
    </AppShell>
  );
}
