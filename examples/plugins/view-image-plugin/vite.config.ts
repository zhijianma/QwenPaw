import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react({
      // Use classic JSX transform so the bundle calls React.createElement
      // directly — no dependency on react/jsx-runtime which doesn't exist
      // as a standalone UMD global on the host page.
      jsxRuntime: "classic",
    }),
  ],
  define: {
    // Prevent react dev build from referencing `process` (not available in browser)
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    lib: {
      entry: "src/index.tsx",
      name: "ViewImagePlugin",
      formats: ["umd"],
      fileName: () => "index.umd.js",
    },
    rollupOptions: {
      // Re-use the host app's React — do NOT bundle it
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
