import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.AURORA_DEV_API_URL || 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
});
