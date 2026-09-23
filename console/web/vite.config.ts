import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

/**
 * The FastAPI console serves the build output from `console/static` (spec 5.1) and
 * listens on 127.0.0.1 only (spec 5.4).  In dev the Vite server proxies `/api` to it.
 *
 * The proxy rewrites Host and Origin to the API origin on purpose: the backend runs
 * `TrustedHostMiddleware` (anything but `127.0.0.1:<port>` / `localhost:<port>` -> 421)
 * and rejects non-GET requests whose `Origin` is not in the allowed set (spec 5.4).
 */
const API_TARGET = process.env.EARN_CONSOLE_DEV_API ?? 'http://127.0.0.1:8787';

export default defineConfig({
  // `EARN_CONSOLE_BASE` lets the backend mount the bundle somewhere other than "/".
  base: process.env.EARN_CONSOLE_BASE ?? '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    // Served by FastAPI: console/web/../static
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('origin', API_TARGET);
            proxyReq.setHeader('referer', `${API_TARGET}/`);
          });
        },
      },
    },
  },
});
