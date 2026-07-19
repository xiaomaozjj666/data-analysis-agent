import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  build: {
    target: "es2020",
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Vite 8 / Rolldown 要求 manualChunks 为函数形式。
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (id.includes("react-markdown") || id.includes("remark-gfm") || id.includes("micromark") || id.includes("mdast") || id.includes("hast") || id.includes("unist")) return "markdown";
            if (id.includes("lucide-react")) return "icons";
            if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/")) return "react";
          }
          return undefined;
        },
      },
    },
  },
});
