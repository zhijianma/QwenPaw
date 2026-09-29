// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildSlotNamespace } from "./registry/sdk";
import { routeRegistry, slotRegistry } from "./registry/store";
import {
  loadAllPlugins,
  loadPawApp,
  reloadFrontendPlugin,
  reloadPawApp,
  resetPawAppLoaderForTests,
} from "./usePluginLoader";
import { resetUiContributionActivationForTests } from "./uiContributionActivation";

const originalCreateObjectUrl = URL.createObjectURL;
const originalRevokeObjectUrl = URL.revokeObjectURL;

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function plugin(id: string, type: string) {
  return {
    id,
    name: id,
    plugin_type: type,
    frontend_entry: "dist/index.js",
  };
}

function strictUiPlugin(id: string, slots: string[]) {
  return {
    ...plugin(id, "frontend"),
    schema_version: "qwenpaw.plugin.v2",
    ui_contributions: slots.map((slot, index) => ({
      id: `ui-${index}`,
      slot,
      entrypoint: "dist/index.js",
    })),
  };
}

describe("frontend plugin loader", () => {
  beforeEach(() => {
    resetPawAppLoaderForTests();
    resetUiContributionActivationForTests();
    routeRegistry.__resetForTests();
    slotRegistry.__resetForTests();
    vi.restoreAllMocks();
    URL.createObjectURL = vi.fn(
      () => `data:text/javascript,${encodeURIComponent("export default true")}`,
    );
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
    delete (globalThis as typeof globalThis & { __registerNotes?: () => void })
      .__registerNotes;
    delete (
      globalThis as typeof globalThis & { __registerUiSlots?: () => void }
    ).__registerUiSlots;
    delete (
      globalThis as typeof globalThis & {
        __registerQueuedUi?: (pluginId: string) => void;
      }
    ).__registerQueuedUi;
  });

  it("loads every installed frontend plugin during startup", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([
          plugin("global-tools", "frontend"),
          plugin("notes", "app"),
        ]),
      )
      .mockImplementation(async () => new Response("export default true"));

    await expect(loadAllPlugins()).resolves.toEqual({
      loaded: 2,
      failed: [],
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("loads declared UI slots through the scoped host activation", async () => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerUiSlots?: () => void;
    };
    runtimeGlobal.__registerUiSlots = () => {
      buildSlotNamespace().fill(
        "insights",
        "ui.task.inspector",
        () => null,
      );
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerUiSlots(); export const version = 2",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([
          strictUiPlugin("insights", ["ui.task.inspector"]),
        ]),
      )
      .mockResolvedValueOnce(
        new Response("globalThis.__registerUiSlots()"),
      );

    await expect(loadAllPlugins()).resolves.toEqual({
      loaded: 1,
      failed: [],
    });
    expect(slotRegistry.snapshotAll()).toMatchObject([
      {
        name: "ui.task.inspector",
        source: "insights",
      },
    ]);
  });

  it("keeps the old UI generation when replacement violates its manifest", async () => {
    slotRegistry.fill("insights", "ui.task.inspector", () => null, {
      id: "old-generation",
    });
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerUiSlots?: () => void;
    };
    runtimeGlobal.__registerUiSlots = () => {
      buildSlotNamespace().fill(
        "insights",
        "ui.task.toolbar",
        () => null,
      );
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerUiSlots()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse([
          strictUiPlugin("insights", ["ui.task.inspector"]),
        ]),
      )
      .mockResolvedValueOnce(
        new Response("globalThis.__registerUiSlots()"),
      );

    await expect(reloadFrontendPlugin("insights")).rejects.toThrow(
      "did not declare UI slot",
    );
    expect(slotRegistry.snapshotAll()).toMatchObject([
      {
        name: "ui.task.inspector",
        source: "insights",
        id: "old-generation",
      },
    ]);
  });

  it("serializes concurrent strict UI activations", async () => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerQueuedUi?: (pluginId: string) => void;
    };
    runtimeGlobal.__registerQueuedUi = (pluginId) => {
      buildSlotNamespace().fill(
        pluginId,
        "ui.task.inspector",
        () => null,
      );
    };
    let bundleIndex = 0;
    URL.createObjectURL = vi.fn(() => {
      const pluginId = bundleIndex === 0 ? "one" : "two";
      bundleIndex += 1;
      return `data:text/javascript,${encodeURIComponent(
        `globalThis.__registerQueuedUi("${pluginId}")`,
      )}`;
    });
    const plugins = [
      strictUiPlugin("one", ["ui.task.inspector"]),
      strictUiPlugin("two", ["ui.task.inspector"]),
    ];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      return url.endsWith("/frontend_plugin")
        ? jsonResponse(plugins)
        : new Response("bundle");
    });

    await expect(
      Promise.all([
        reloadFrontendPlugin("one"),
        reloadFrontendPlugin("two"),
      ]),
    ).resolves.toEqual([true, true]);
    expect(
      slotRegistry
        .snapshotAll()
        .map((item) => item.source)
        .sort(),
    ).toEqual(["one", "two"]);
  });

  it("loads a newly installed PawApp and exposes its route immediately", async () => {
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerNotes?: () => void;
    };
    runtimeGlobal.__registerNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: () => null,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("globalThis.__registerNotes()"));

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
    expect(routeRegistry.snapshot()).toMatchObject([
      { id: "notes.page", path: "/apps/notes", source: "notes" },
    ]);
  });

  it("deduplicates concurrent loads and allows retry after failure", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("missing", { status: 503 }));

    const first = loadPawApp("notes");
    expect(loadPawApp("notes")).toBe(first);
    await expect(first).rejects.toThrow("HTTP 503");

    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockImplementationOnce(async () => {
        routeRegistry.add("notes", {
          id: "notes.page",
          path: "/apps/notes",
          component: () => null,
        });
        return new Response("export default true");
      });

    await expect(loadPawApp("notes")).resolves.toBeUndefined();
  });

  it("force reloads an already registered PawApp route", async () => {
    const oldComponent = () => null;
    const newComponent = () => null;
    routeRegistry.add("notes", {
      id: "notes.page",
      path: "/apps/notes",
      component: oldComponent,
    });
    const runtimeGlobal = globalThis as typeof globalThis & {
      __registerUpdatedNotes?: () => void;
    };
    runtimeGlobal.__registerUpdatedNotes = () => {
      routeRegistry.add("notes", {
        id: "notes.page",
        path: "/apps/notes",
        component: newComponent,
      });
    };
    URL.createObjectURL = vi.fn(
      () =>
        `data:text/javascript,${encodeURIComponent(
          "globalThis.__registerUpdatedNotes()",
        )}`,
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse([plugin("notes", "app")]))
      .mockResolvedValueOnce(new Response("updated"));

    await expect(reloadPawApp("notes")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(routeRegistry.snapshot()[0].Component).toBe(newComponent);
    expect(routeRegistry.snapshot()[0].Component).not.toBe(oldComponent);
  });
});
