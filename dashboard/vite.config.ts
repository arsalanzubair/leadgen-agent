import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    /**
     * The settings service runs as a separate process (see backend/). Proxying
     * `/api` to it in development means the client uses the same same-origin
     * path it will use in production behind a reverse proxy, so there is no
     * CORS special case and no environment-specific base URL to get wrong.
     */
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        /**
         * Recharts is only needed on Performance, so chunking it keeps the
         * initial load small and clears Vite's bundle-size advisory.
         */
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-charts": ["recharts"],
        },
      },
    },
  },
});
