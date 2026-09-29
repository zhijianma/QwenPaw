/** Frontend plugin loading utilities. */

import { getApiToken, getApiUrl } from "../api/config";
import { removePluginRuntime } from "./pluginRuntimeCleanup";
import { routeRegistry } from "./registry/store";
import {
  UiContributionActivation,
  type UiContributionDeclaration,
} from "./uiContributionActivation";

interface FrontendPluginInfo {
  id: string;
  name: string;
  plugin_type?: string;
  schema_version?: string;
  frontend_entry?: string;
  ui_contributions?: UiContributionDeclaration[];
}

export interface PluginLoadSummary {
  loaded: number;
  failed: string[];
}

const loadingPlugins = new Map<string, Promise<void>>();
let uiActivationTail: Promise<void> = Promise.resolve();

function enqueueUiActivation<T>(operation: () => Promise<T>): Promise<T> {
  const run = uiActivationTail.then(operation, operation);
  uiActivationTail = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

function authHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function resolveUrl(pluginId: string, apiPath: string): string {
  return getApiUrl(`frontend_plugin/${pluginId}/files/${apiPath}`);
}

async function fetchFrontendPlugins(): Promise<FrontendPluginInfo[]> {
  const response = await fetch(getApiUrl("/frontend_plugin"), {
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`Failed to list frontend plugins (${response.status})`);
  }
  return response.json();
}

async function executePluginScript(entryUrl: string): Promise<void> {
  const response = await fetch(entryUrl, { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${entryUrl}`);
  }

  const blobUrl = URL.createObjectURL(
    new Blob([await response.text()], { type: "application/javascript" }),
  );
  try {
    await import(/* @vite-ignore */ blobUrl);
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

function frontendEntries(plugin: FrontendPluginInfo): string[] {
  const entries = [
    plugin.frontend_entry,
    ...(plugin.ui_contributions ?? []).map((item) => item.entrypoint),
  ].filter((entry): entry is string => Boolean(entry));
  return [...new Set(entries)];
}

function isStrictUiPlugin(plugin: FrontendPluginInfo): boolean {
  return (
    plugin.schema_version === "qwenpaw.plugin.v2" &&
    (plugin.ui_contributions?.length ?? 0) > 0
  );
}

async function executeFrontendPlugin(
  plugin: FrontendPluginInfo,
  force = false,
): Promise<void> {
  const entries = frontendEntries(plugin);
  if (entries.length === 0) return;

  if (!isStrictUiPlugin(plugin)) {
    if (force) removePluginRuntime(plugin.id);
    try {
      for (const entry of entries) {
        await executePluginScript(resolveUrl(plugin.id, entry));
      }
    } catch (error) {
      removePluginRuntime(plugin.id);
      throw error;
    }
    return;
  }

  return enqueueUiActivation(async () => {
    const activation = new UiContributionActivation(
      plugin.id,
      plugin.ui_contributions ?? [],
    );
    try {
      for (const entry of entries) {
        await executePluginScript(resolveUrl(plugin.id, entry));
      }
      activation.commit();
    } catch (error) {
      activation.rollback();
      throw error;
    }
  });
}

/** Load every installed frontend plugin during Console startup. */
export async function loadAllPlugins(): Promise<PluginLoadSummary> {
  let plugins: FrontendPluginInfo[];
  try {
    plugins = await fetchFrontendPlugins();
  } catch (error) {
    console.warn("[PluginLoader] failed to fetch plugin list:", error);
    return { loaded: 0, failed: [] };
  }

  const loadable = plugins.filter(
    (plugin) => frontendEntries(plugin).length > 0,
  );
  const failed: string[] = [];
  // UI activation is intentionally serial. The host scopes synchronous
  // registrations to one manifest and commits its declared slots together.
  for (const plugin of loadable) {
    try {
      await executeFrontendPlugin(plugin);
    } catch (error) {
      failed.push(`${plugin.id}: ${String(error)}`);
    }
  }
  return { loaded: loadable.length - failed.length, failed };
}

interface LoadPluginOptions {
  force?: boolean;
  expectedType?: "app";
  entryPage?: string;
}

function loadFrontendPlugin(
  pluginId: string,
  options: LoadPluginOptions = {},
): Promise<void> {
  const registered = () =>
    routeRegistry
      .snapshot()
      .some(
        (route) =>
          route.source === pluginId &&
          route.path.startsWith("/apps/") &&
          (!options.entryPage || route.path === options.entryPage),
      );

  if (!options.force && options.expectedType === "app" && registered()) {
    return Promise.resolve();
  }

  const promise = (async () => {
    const plugins = await fetchFrontendPlugins();
    const plugin = plugins.find((item) => item.id === pluginId);
    if (!plugin || frontendEntries(plugin).length === 0) {
      if (options.expectedType === "app") {
        throw new Error(`PawApp frontend plugin not found: ${pluginId}`);
      }
      return;
    }
    if (options.expectedType && plugin.plugin_type !== options.expectedType) {
      throw new Error(`PawApp frontend plugin not found: ${pluginId}`);
    }
    try {
      await executeFrontendPlugin(plugin, options.force);
      if (options.expectedType === "app" && !registered()) {
        throw new Error(`PawApp ${pluginId} did not register its app route`);
      }
    } catch (error) {
      if (!isStrictUiPlugin(plugin)) removePluginRuntime(pluginId);
      throw error;
    }
  })().finally(() => {
    loadingPlugins.delete(pluginId);
  });

  loadingPlugins.set(pluginId, promise);
  return promise;
}

/** Load one newly installed PawApp without reloading the page. */
export function loadPawApp(
  appId: string,
  entryPage?: string,
  options: { force?: boolean } = {},
): Promise<void> {
  if (options.force) return reloadPawApp(appId, entryPage);
  const pending = loadingPlugins.get(appId);
  if (pending) return pending;
  return loadFrontendPlugin(appId, {
    expectedType: "app",
    entryPage,
    force: options.force,
  });
}

/** Force reload an installed PawApp after an update. */
export function reloadPawApp(appId: string, entryPage?: string): Promise<void> {
  const pending = loadingPlugins.get(appId);
  if (pending) {
    return pending.then(() =>
      loadFrontendPlugin(appId, {
        expectedType: "app",
        entryPage,
        force: true,
      }),
    );
  }
  return loadFrontendPlugin(appId, {
    expectedType: "app",
    entryPage,
    force: true,
  });
}

/** Reload a frontend plugin after installation or update. */
export function reloadFrontendPlugin(pluginId: string): Promise<boolean> {
  const pending = loadingPlugins.get(pluginId);
  if (pending) {
    return pending.then(() =>
      loadFrontendPlugin(pluginId, { force: true }).then(() => true),
    );
  }
  return loadFrontendPlugin(pluginId, { force: true }).then(() => true);
}

/** Reset pending loads between unit tests. */
export function resetPawAppLoaderForTests(): void {
  loadingPlugins.clear();
  uiActivationTail = Promise.resolve();
}
