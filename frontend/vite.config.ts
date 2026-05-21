import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    // Vite 5: must be boolean `true` to disable host check (the string 'all'
    // is a Vite 6+ syntax — Vite 5 treats it as a literal hostname match and
    // silently rejects everything else with a 403). Without this, SignalWire
    // POSTs to /api/queues/... via ngrok get blocked at Vite before the
    // /api proxy can forward them to the backend. Dev-only — production
    // build never runs the Vite dev server.
    allowedHosts: true,
    // Bind-mounted source on Windows/Mac → Linux container: inotify doesn't
    // fire reliably across the bridge, so HMR misses file changes. Polling
    // is the standard workaround. Slight CPU cost in dev; production
    // builds are unaffected.
    watch: {
      usePolling: true,
      interval: 500,
    },
    proxy: {
      '/api': {
        target: 'http://backend:5000',
        changeOrigin: true,
        // Forward X-Forwarded-* headers from ngrok so backend knows external URL
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req) => {
            // Pass through X-Forwarded headers from ngrok
            const forwardedHost = req.headers['x-forwarded-host'];
            const forwardedProto = req.headers['x-forwarded-proto'];
            if (forwardedHost) {
              proxyReq.setHeader('X-Forwarded-Host', forwardedHost);
            }
            if (forwardedProto) {
              proxyReq.setHeader('X-Forwarded-Proto', forwardedProto);
            }
          });
        },
      },
      '/socket.io': {
        target: 'http://backend:5000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})