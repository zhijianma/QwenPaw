import type React from "react";

// ── Plugin Manifest ────────────────────────────────────────────────────────

export interface PluginManifest {
  name: string;
  version: string;
  description?: string;
  author?: string;
  entry: {
    frontend?: string; // frontend JS path (relative, served via backend)
    backend?: string; // backend Python path
  };
}

// ── Plugin Route ───────────────────────────────────────────────────────────

export interface PluginRouteConfig {
  path: string;
  component: React.ComponentType;
  label?: string;
  icon?: string;
}

// ── Plugin Config (provided at registration time) ─────────────────────────

export interface PluginConfig {
  // Custom message-type renderers (keyed by tool/message type name)
  messageTypes?: Record<string, React.ComponentType<any>>;
  // Custom page routes injected into the main router
  routes?: PluginRouteConfig[];
}

// ── Registered Plugin ─────────────────────────────────────────────────────

export interface Plugin {
  manifest: PluginManifest;
  config: PluginConfig;
}
