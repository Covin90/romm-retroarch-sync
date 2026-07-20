import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// The whole point of this config: `@decky/ui` and `@decky/api` resolve to the
// shim instead of the real packages, so decky_plugin/src/index.tsx is consumed
// BYTE-IDENTICAL by both builds. Never fork that file — if something it imports
// is missing here, add it to the shim.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@decky/ui": resolve(__dirname, "src/shim/ui.tsx"),
      "@decky/api": resolve(__dirname, "src/shim/api.tsx"),
    },
  },
  server: {
    port: 5173,
    // The Python backend serves the RPC endpoints the shim's `callable` hits.
    proxy: {
      "/api": { target: "http://127.0.0.1:8723", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
