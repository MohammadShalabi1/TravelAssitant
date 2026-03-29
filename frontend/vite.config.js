import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Every request starting with /api is forwarded to your FastAPI backend.
      // This means in your React code you write fetch('/api/chat') — no need
      // to hardcode http://localhost:8000 anywhere.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      }
    }
  }
})
