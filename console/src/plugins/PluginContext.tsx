import React, {
  createContext,
  useContext,
  useEffect,
  useState,
} from "react";
import { pluginSystem } from "./PluginSystem";
import { loadAllPlugins } from "./PluginLoader";
import type { PluginRouteConfig } from "./types";

// ── Context value shape ───────────────────────────────────────────────────

interface PluginContextValue {
  /** Merged map of all custom message-type renderers from all plugins */
  messageTypes: Record<string, React.ComponentType<any>>;
  /** Flat list of all custom page routes from all plugins */
  routes: PluginRouteConfig[];
  /** True once the initial plugin-load attempt has completed */
  ready: boolean;
}

const PluginContext = createContext<PluginContextValue>({
  messageTypes: {},
  routes: [],
  ready: false,
});

// ── Provider ──────────────────────────────────────────────────────────────

export function PluginProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [messageTypes, setMessageTypes] = useState<
    Record<string, React.ComponentType<any>>
  >({});
  const [routes, setRoutes] = useState<PluginRouteConfig[]>([]);

  useEffect(() => {
    // Subscribe to future plugin registration events (e.g., hot-reload)
    const unsubscribe = pluginSystem.subscribe(() => {
      setMessageTypes(pluginSystem.getMessageTypes());
      setRoutes(pluginSystem.getRoutes());
    });

    // Load all installed plugins on mount (non-fatal on failure)
    loadAllPlugins().finally(() => {
      // Sync state one more time after all scripts have executed
      setMessageTypes(pluginSystem.getMessageTypes());
      setRoutes(pluginSystem.getRoutes());
      setReady(true);
    });

    return unsubscribe;
  }, []);

  return (
    <PluginContext.Provider value={{ messageTypes, routes, ready }}>
      {children}
    </PluginContext.Provider>
  );
}

// ── Consumer hook ─────────────────────────────────────────────────────────

export function usePlugins(): PluginContextValue {
  return useContext(PluginContext);
}
