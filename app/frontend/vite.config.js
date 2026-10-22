import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      // ws: the integrated terminal streams over a WebSocket under /api.
      '/api': { target: 'http://127.0.0.1:8790', ws: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
