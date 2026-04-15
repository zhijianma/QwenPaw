import type { PluginManifest } from "./types";
import { getApiUrl } from "../api/config";

// ── PluginLoader ───────────────────────────────────────────────────────────

/** Track plugins whose <script> has already been injected */
const loadedPlugins = new Set<string>();

/**
 * Inject a single plugin's frontend JS as a <script> tag.
 * The script is expected to call window.__registerPlugin(...) on execution.
 */
async function loadPlugin(manifest: PluginManifest): Promise<void> {
  // Skip if already loaded or has no frontend entry
  if (loadedPlugins.has(manifest.name)) return;
  if (!manifest.entry?.frontend) return;

  return new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = getApiUrl(`/plugins/${encodeURIComponent(manifest.name)}/frontend`);
    script.onload = () => {
      loadedPlugins.add(manifest.name);
      resolve();
    };
    script.onerror = () => {
      reject(new Error(`Failed to load plugin frontend: ${manifest.name}`));
    };
    document.head.appendChild(script);
  });
}

/**
 * Fetch the plugin manifest list from the backend, then load each plugin's
 * frontend bundle in parallel (failures are isolated per plugin).
 */
export async function loadAllPlugins(): Promise<void> {
  try {
    const res = await fetch(getApiUrl("/plugins"));
    if (!res.ok) {
      console.warn(`[PluginLoader] /api/plugins returned ${res.status}`);
      return;
    }
    const manifests: PluginManifest[] = await res.json();

    const results = await Promise.allSettled(
      manifests.map((manifest) => loadPlugin(manifest)),
    );

    const failed = results.filter((r) => r.status === "rejected").length;
    if (failed > 0) {
      console.warn(`[PluginLoader] ${failed} plugin(s) failed to load`);
    }
    console.info(
      `[PluginLoader] loaded ${manifests.length - failed}/${manifests.length} plugins`,
    );
  } catch (err) {
    // Network error or JSON parse failure – non-fatal
    console.warn("[PluginLoader] failed to fetch plugin list:", err);
  }
}
