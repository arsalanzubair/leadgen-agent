import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(({ mode }) => {
  /**
   * The project-root `.env` -- the one the Python side reads.
   *
   * Vite only loads `.env` from its own root (`dashboard/`), and only exposes
   * `VITE_`-prefixed keys. `BACKEND_PORT` lives one level up with the rest of
   * the project's configuration, so it is loaded explicitly with an empty
   * prefix. Without this, setting `BACKEND_PORT` in `.env` would move the
   * backend and quietly leave the proxy pointing at the old port.
   *
   * A real environment variable still wins over the file, matching
   * `load_dotenv(override=False)` on the Python side.
   */
  const rootEnv = loadEnv(mode, path.resolve(__dirname, ".."), "");
  const backendHost = process.env.BACKEND_HOST || rootEnv.BACKEND_HOST || "127.0.0.1";
  const backendPort = process.env.BACKEND_PORT || rootEnv.BACKEND_PORT || "8000";

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      port: 5173,
      /**
       * The settings service runs as a separate process (see backend/).
       * Proxying `/api` to it in development means the client uses the same
       * same-origin path it will use in production behind a reverse proxy, so
       * there is no CORS special case and no environment-specific base URL to
       * get wrong.
       */
      proxy: {
        "/api": {
          // VITE_API_PROXY_TARGET still wins outright, for pointing the
          // dashboard at a backend somewhere else entirely.
          target:
            process.env.VITE_API_PROXY_TARGET ||
            rootEnv.VITE_API_PROXY_TARGET ||
            `http://${backendHost}:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          /**
           * Recharts is only needed on the charts, so chunking it keeps the
           * initial load small and clears Vite's bundle-size advisory.
           */
          manualChunks: {
            "vendor-react": ["react", "react-dom", "react-router-dom"],
            "vendor-charts": ["recharts"],
          },
        },
      },
    },
  };
});
