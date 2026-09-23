# `console/web` — Earn console frontend

The React shell for the local FastAPI console (spec sections 5 and 12). Package **F0c** owns
the shell; the 21 feature pages come from **P1…P7** and live under `src/pages/**`.

```bash
npm install
npm run dev        # Vite on 127.0.0.1:5173, /api proxied to 127.0.0.1:8787
npm run typecheck  # tsc --noEmit for the app and the config files
npm run test       # vitest (jsdom)
npm run build      # -> ../static, served by FastAPI
```

In WSL use the repo helper so the Windows tree is mirrored to ext4 first:
`earn-web <workspace> install|typecheck|test|build|all`.

## Locked versions

react 19.3.0 · react-dom 19.3.0 · react-router-dom 7.18.4 · @mantine/{core,hooks,form,notifications,dates,charts}
9.6.2 · @tabler/icons-react 3.48.0 · @tanstack/react-query 5.103.2 · lightweight-charts 5.2.1 ·
@uiw/react-codemirror 4.25.11 · @codemirror/{state 6.7.6, lang-json 6.0.2, lang-yaml 6.1.3,
lang-markdown 6.5.2, lang-python 6.2.1} · recharts 3.10.1 · dayjs 1.11.23 — dev: vite 8.3.0 ·
@vitejs/plugin-react 6.1.1 · vitest 5.0.1 · typescript 5.9.3 · jsdom 30.1.1 · @testing-library/{react
16.3.3, user-event 14.6.7, jest-dom 7.0.1} · @types/react(-dom) 19.3.0 · @types/node 22.19.4.

Every dependency is pinned exactly (no `^`), and `package-lock.json` is committed.

## Layout

| Path | What lives there |
|---|---|
| `src/main.tsx` | entry: Mantine CSS, `BrowserRouter`, `AppProviders`, `App` |
| `src/App.tsx` | login gate + the 21 routes with lazy page resolution |
| `src/routes.tsx` | the route registry (id, path, title, nav group, icon, owning package) |
| `src/theme.ts` | Mantine theme, mode/status/severity colours |
| `src/app/**` | global chrome: `AppLayout`, header widgets, safety strip, command palette, alerts drawer, session & SSE contexts, login, `NotBuiltYet` |
| `src/api/**` | typed fetch client, error normalisation, React Query setup, SSE hook, DTO mirror of `console/contracts.py` |
| `src/components/**` | shared components: `SchemaForm`, `DiffView`, `ConfirmDialog`, `DataTable`, `StatCard`, `SSEIndicator`, `KillButton`, `EmptyState`, `JsonViewer`, `CodeEditor` |
| `src/lib/**` | pure helpers: time/number formatting, cron, line diff, immutable object paths, storage |
| `src/test/**` | vitest setup, render helpers, fakes, shell/component/api/SSE suites |

Import shared code through the `@` alias: `@/components`, `@/api`, `@/lib/format`,
`@/app/ApiContext`.

## Adding a page (P1…P7)

1. Create **one** of:
   - `src/pages/<route id>/index.tsx` (preferred),
   - `src/pages/<route id>.tsx`,
   - `src/pages/<route id>/<RouteId>Page.tsx`,
   each with a **default-exported component**.
2. That is all. `src/routes.tsx` discovers the module with `import.meta.glob('/src/pages/**/*.tsx')`
   and lazy-loads it; until then the route renders the `NotBuiltYet` placeholder. Nothing in
   `App.tsx` or `routes.tsx` needs editing, and no package edits another's files.
3. Register Ctrl-K entries from inside the page with
   `useRegisterCommands([{ id, title, group, run }])` (`src/app/commandRegistry.ts`).
4. Subscribe to SSE topics with `useEvents().subscribe(['signal'], handler)`; the shell keeps a
   single `/api/stream` connection and already invalidates the shell queries.

## API client

- `useEndpoints()` / `useApi()` (`src/app/ApiContext.tsx`) hand out the injected `ApiClient`; use
  them instead of the `api` singleton so tests can drive a page with a fake `fetch`.
- Every non-GET carries `X-Earn-CSRF` and `Content-Type: application/json`; cookies are
  `same-origin`. 401 clears the session and shows the login screen. Errors arrive as `ApiError`
  with `code`, `status`, `detail` and the `needsStepUp` / `isConflict` / `isLocked` helpers.
- Dangerous actions use `ConfirmDialog` with `confirmPhrase` and `requireStepUp`; it collects the
  token when the step-up window is closed and hands it back to the caller.

## DTO mirror and drift

`src/api/contracts.ts` is a hand-written mirror of `console/contracts.py` plus a runtime
registry (`PY_CONTRACT_MODELS`). `src/test/contracts.drift.test.ts` parses the python file and
fails when a mirrored model's field names differ. The python file lives outside `console/web`,
so in a `console/web`-only workspace the comparison is skipped with a warning; point
`EARN_CONTRACTS_PY` at `console/contracts.py` to force it.

## Serving

`npm run build` writes `console/static` (`base` overridable with `EARN_CONSOLE_BASE`). The dev
proxy rewrites `Host`, `Origin` and `Referer` to the API origin because the backend enforces
`TrustedHostMiddleware` and an Origin allow-list; override the target with
`EARN_CONSOLE_DEV_API`.
