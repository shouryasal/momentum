import { Center, Loader, Stack, Text, Title } from '@mantine/core';
import { Suspense, useMemo } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { AppLayout } from './app/AppLayout';
import { LoginPage } from './app/LoginPage';
import { NotBuiltYet } from './app/NotBuiltYet';
import { useSession } from './app/SessionContext';
import { ROUTES, resolvePageComponent, type RouteDef } from './routes';

function PageFallback() {
  return (
    <Center py="xl">
      <Loader size="sm" />
    </Center>
  );
}

function RouteElement({ route }: { route: RouteDef }) {
  const Page = useMemo(() => resolvePageComponent(route.id), [route.id]);
  if (!Page) return <NotBuiltYet route={route} />;
  return (
    <Suspense fallback={<PageFallback />}>
      <Page />
    </Suspense>
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

/** Login gate + the 21 routes; pages are resolved lazily from `src/pages/**`. */
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
