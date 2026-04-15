import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.tsx";
import "./i18n";

// Expose React globals so UMD plugin bundles can resolve their externals.
// Plugin UMD wrappers look for `globalThis.React` and `globalThis.ReactDOM`.
(window as any).React = React;
(window as any).ReactDOM = ReactDOM;

if (typeof window !== "undefined") {
  const originalError = console.error;
  const originalWarn = console.warn;

  console.error = function (...args: any[]) {
    const msg = args[0]?.toString() || "";
    if (msg.includes(":first-child") || msg.includes("pseudo class")) {
      return;
    }
    originalError.apply(console, args);
  };

  console.warn = function (...args: any[]) {
    const msg = args[0]?.toString() || "";
    if (
      msg.includes(":first-child") ||
      msg.includes("pseudo class") ||
      msg.includes("potentially unsafe")
    ) {
      return;
    }
    originalWarn.apply(console, args);
  };
}

ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
