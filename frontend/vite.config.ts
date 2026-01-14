import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
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