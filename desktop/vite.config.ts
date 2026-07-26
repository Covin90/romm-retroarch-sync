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
    alias: [
      { find: "@decky/ui", replacement: resolve(__dirname, "src/shim/ui.tsx") },
      { find: "@decky/api", replacement: resolve(__dirname, "src/shim/api.tsx") },
      // decky_plugin/src/index.tsx lives outside this root and there is no
      // node_modules next to it, so Node resolution walks past desktop/ and
      // never finds react/react-icons. Pin them to ours.
      { find: /^react$/, replacement: resolve(__dirname, "node_modules/react") },
      { find: /^react-dom$/, replacement: resolve(__dirname, "node_modules/react-dom") },
      { find: /^react\/jsx-runtime$/, replacement: resolve(__dirname, "node_modules/react/jsx-runtime") },
      { find: /^react-icons\//, replacement: resolve(__dirname, "node_modules/react-icons") + "/" },
    ],
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
