import type React from "react";
import type { Plugin, PluginConfig, PluginManifest, PluginRouteConfig } from "./types";

// ── PluginSystem ───────────────────────────────────────────────────────────

class PluginSystem {
    // Registered plugins keyed by name
    private plugins = new Map<string, Plugin>();

    // Change listeners (notifies React to re-render)
    private listeners = new Set<() => void>();

    // ── Register ──────────────────────────────────────────────────────────────

    register(manifest: PluginManifest, config: PluginConfig): void {
        this.plugins.set(manifest.name, { manifest, config });
        console.info(`[PluginSystem] registered: ${manifest.name}@${manifest.version}`);
        this.notify();
    }

    // ── Unregister ────────────────────────────────────────────────────────────

    unregister(pluginName: string): void {
        this.plugins.delete(pluginName);
        this.notify();
    }

    // ── Aggregated getters ────────────────────────────────────────────────────

    /** Merged map of all custom message-type renderers across all plugins */
    getMessageTypes(): Record<string, React.ComponentType<any>> {
        const result: Record<string, React.ComponentType<any>> = {};
        this.plugins.forEach((plugin) => {
            if (plugin.config.messageTypes) {
                Object.assign(result, plugin.config.messageTypes);
            }
        });
        return result;
    }

    /** Flat list of all custom routes across all plugins */
    getRoutes(): PluginRouteConfig[] {
        const result: PluginRouteConfig[] = [];
        this.plugins.forEach((plugin) => {
            if (plugin.config.routes) {
                result.push(...plugin.config.routes);
            }
        });
        return result;
    }

    /** All registered plugins as an array */
    getAll(): Plugin[] {
        return Array.from(this.plugins.values());
    }

    // ── Subscription (for React useSyncExternalStore / manual subscribe) ──────

    subscribe(listener: () => void): () => void {
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
    }

    private notify(): void {
        this.listeners.forEach((l) => l());
    }
}

// ── Global singleton ──────────────────────────────────────────────────────

export const pluginSystem = new PluginSystem();

// Expose to plugin JS bundles executing in the same JS environment
window.__registerPlugin = (manifest, config) => {
    pluginSystem.register(manifest, config);
};
