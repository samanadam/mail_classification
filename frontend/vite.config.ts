import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8080 (`mailcat api`); production serves the build from FastAPI.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8080" } },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 800 },
});
