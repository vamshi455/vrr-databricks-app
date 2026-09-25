import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to FastAPI, so the browser sees one origin and the
// frontend never needs to know the backend's port. `make web` runs this; in production
// FastAPI serves the built dist/ itself and the proxy is not involved.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", sourcemap: true },
});
