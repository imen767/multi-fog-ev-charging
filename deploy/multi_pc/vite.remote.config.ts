import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: "127.0.0.1", // Ne pas exposer les commandes du dashboard sur le LAN.
      proxy: {
        "/orchestrator": {
          target: "http://127.0.0.1:18081",
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/orchestrator/, ""),
        },
        "/api/v1/simulator": {
          target: "http://127.0.0.1:18083",
          changeOrigin: true,
        },
        "/fog-a": {
          target: env.FOG_A_PROXY_TARGET || "http://127.0.0.1:18001",
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/fog-a/, ""),
        },
        "/fog-b": {
          target: env.FOG_B_PROXY_TARGET || "http://127.0.0.1:18002",
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/fog-b/, ""),
        },
        "/fog-c": {
          target: env.FOG_C_PROXY_TARGET || "http://127.0.0.1:18003",
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/fog-c/, ""),
        },
        "/chronos": {
          target: "http://127.0.0.1:18082",
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/chronos/, ""),
        },
      },
    },
  };
});
