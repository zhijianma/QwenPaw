import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react({
      // Classic JSX transform – uses React.createElement, no jsx-runtime UMD global needed
      jsxRuntime: "classic",
    }),
  ],
  define: {
    // Prevent react dev build from referencing `process` (not in browser)
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    lib: {
      entry: "src/index.tsx",
      name: "TokenUsagePlugin",
      formats: ["umd"],
      fileName: () => "index.umd.js",
    },
    rollupOptions: {
      // Re-use the host app's React – do NOT bundle it
      external: ["react", "react-dom"],
      output: {
        globals: {
          react: "React",
          "react-dom": "ReactDOM",
        },
      },
    },
  },
});
