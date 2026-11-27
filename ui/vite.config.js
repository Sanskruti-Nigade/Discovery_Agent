import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the FastAPI app (frontend/app.py, port 8080) serves /api and /lib; Vite proxies to it.
// Prod: `npm run build` writes ui/dist, which frontend/app.py serves itself on :8080.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_API_TARGET || "http://localhost:8080";
  return {
    plugins: [react()],
    server: { port: 5173, proxy: { "/api": target, "/lib": target } },
  };
});
