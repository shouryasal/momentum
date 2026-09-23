import { Center, Loader, Stack, Text, Title } from '@mantine/core';
import { Suspense, useMemo } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { AppLayout } from './app/AppLayout';
import { LoginPage } from './app/LoginPage';
import { NotBuiltYet } from './app/NotBuiltYet';
import { RouteErrorBoundary } from './app/RouteErrorBoundary';
import { useSession } from './app/SessionContext';
import { ROUTES, resolvePageComponent, type RouteDef } from './routes';

function PageFallback() {
  return (
    <Center py="xl">
      <Loader size="sm" />
    </Center>
  );
}

/**
 * One route's page, inside its own error boundary.
 *
 * A page that throws used to take the whole console down with it: React unmounts the tree,
 * and the operator gets a white screen with no header, no mode badges and — worst of all —
 * no kill switch.  The boundary keeps the chrome up and replaces only the screen, so a
 * broken page reads as a broken page instead of a dead console.
 */
function RouteElement({ route }: { route: RouteDef }) {
  const Page = useMemo(() => resolvePageComponent(route.id), [route.id]);
  return (
    <RouteErrorBoundary title={route.title} resetKey={route.id}>
      {Page ? (
        <Suspense fallback={<PageFallback />}>
          <Page />
        </Suspense>
      ) : (
        <NotBuiltYet route={route} />
      )}
    </RouteErrorBoundary>
  );
}

function NotFound() {
  return (
    <Stack>
      <Title order={2}>Not found</Title>
      <Text c="dimmed">That page is not part of the console.</Text>
    </Stack>
  );
}

/**
 * Login gate + the 21 routes; pages are resolved lazily from `src/pages/**`.
 *
 * Every route is mounted in both views.  The Simple/Advanced toggle changes what the
 * navigation offers, never what the router serves — an Advanced-only screen stays reachable
 * by typing its URL and by Ctrl-K, which is the whole reason simplifying the console does
 * not cost anything.
 */
export function App() {
  const { authenticated, loading } = useSession();

  if (loading) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  if (!authenticated) return <LoginPage />;

  return (
    <AppLayout>
      <Routes>
        {ROUTES.map((route) => (
          <Route key={route.id} path={route.path} element={<RouteElement route={route} />} />
        ))}
        <Route path="/index.html" element={<Navigate to="/" replace />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AppLayout>
  );
}

export default App;
