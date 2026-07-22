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
    emptyOutDir: true,
    rolldownOptions: {
      output: {
        // Vite 8 / Rolldown：codeSplitting 默认 void 0（等同于 false），
        // 动态 import() 会被内联到主 chunk。
        // codeSplitting: true 是 no-op（bindingifyCodeSplitting 中 true 分支不设置 effectiveChunksOption），
        // 必须传对象 { groups, ... } 才能激活 advancedChunks/manualCodeSplitting 机制。
        codeSplitting: {
          includeDependenciesRecursively: true,
          groups: [
            { name: "vendor-react", test: /node_modules[/\\](react|react-dom|scheduler)[/\\]/, priority: 100 },
            { name: "vendor-markdown", test: /node_modules[/\\](react-markdown|remark-gfm|micromark|mdast|hast|unist|property-information|space-separated-tokens|comma-separated-tokens|trim-lines|decode-named-character-reference|character-entities|html-url-attributes|bail|extend|is-plain-obj|trough|vfile|unist-util-visit|unist-util-position|estree-util-.*|estree-walker|zwitch|ccount|escape-string-regexp|markdown-table|longest-streak|highlight\.js|prismjs)[/\\]/, priority: 90 },
            { name: "vendor-icons", test: /node_modules[/\\]lucide-react[/\\]/, priority: 80 },
          ],
        },
      },
    },
  },
});
