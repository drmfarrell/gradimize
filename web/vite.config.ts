import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// All backend calls go through /api, which is proxied to the FastAPI
// server on :8000 in dev. In production the SPA is served separately and
// VITE_API_BASE can point at the real API origin (see src/api.ts).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Localhost ONLY: dev mode has no HTTPS and no token auth, so it must
    // never be reachable from the LAN. Network-facing serving goes through
    // `serve.sh --secure` exclusively. For remote dev over SSH, tunnel:
    //   ssh -L 5173:localhost:5173 <server>
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
