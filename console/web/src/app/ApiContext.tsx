import { createContext, useContext, useMemo, type ReactNode } from 'react';

import { api, type ApiClient } from '../api/client';
import { endpoints, type Endpoints } from '../api/endpoints';

const ApiContext = createContext<ApiClient>(api);

/** Makes the API client injectable so tests can drive the shell with a fake fetch. */
export function ApiProvider({ client = api, children }: { client?: ApiClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}

export function useApi(): ApiClient {
  return useContext(ApiContext);
}

export function useEndpoints(): Endpoints {
  const client = useApi();
  return useMemo(() => endpoints(client), [client]);
}
