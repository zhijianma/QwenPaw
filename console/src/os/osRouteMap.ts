/**
 * osRouteMap.ts — Route id <-> path helpers for the Desktop OS.
 *
 * Each OS window runs its own MemoryRouter seeded at an app "base" path (the
 * registry route path minus any splat). When a shared page component navigates
 * to a path that belongs to a DIFFERENT app, the window bridge maps that path
 * back to a route id so the OS can open/focus the correct window instead of
 * breaking out of the desktop. Pure functions — safe to call in render.
 */

/** Route id of the aggregate System Settings window. */
export const SETTINGS_APP_ID = "os.settings";

/** Minimal route shape needed here (matches ResolvedRoute from the registry). */
export interface RouteLike {
  id: string;
  path: string;
}

/**
 * Settings routes surfaced inside the System Settings window (mirrors
 * SETTINGS_ITEMS in SettingsApp.tsx). Cross-app navigation to any of these
 * opens System Settings and selects the matching pane.
 */
export const SETTINGS_ROUTE_IDS = new Set<string>([
  "core.agents",
  "core.models",
  "core.skill-pool",
  "core.environments",
  "core.security",
  "core.token-usage",
  "core.backups",
  "core.voice-transcription",
  "core.debug",
  "core.plugin-manager",
]);

/**
 * Turn a registry route path into the router base for a window.
 * "/chat/*" -> "/chat", "/models" -> "/models". Falls back to "/".
 */
export function baseFromRoutePath(path: string | undefined): string {
  if (!path) return "/";
  const clean = path.replace(/\/\*$/, "").replace(/\/:.*$/, "");
  return clean || "/";
}

/** First path segment, e.g. "/chat/abc" -> "chat", "/models?x=1" -> "models". */
export function topSegment(pathname: string): string {
  const noQuery = pathname.split("?")[0];
  return noQuery.replace(/^\/+/, "").split("/")[0] || "";
}

/**
 * Resolve a navigation target pathname to a route id by matching its top
 * segment against the registered routes' bases. Returns undefined when no
 * app owns the path.
 */
export function pathToRouteId(
  pathname: string,
  routes: RouteLike[],
): string | undefined {
  const seg = topSegment(pathname);
  if (!seg) return undefined;
  for (const r of routes) {
    if (topSegment(baseFromRoutePath(r.path)) === seg) return r.id;
  }
  return undefined;
}
