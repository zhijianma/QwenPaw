// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import { buildSlotNamespace } from "./registry/sdk";
import { slotRegistry } from "./registry/store";
import {
  resetUiContributionActivationForTests,
  UiContributionActivation,
} from "./uiContributionActivation";

const sdk = buildSlotNamespace();
const declaration = {
  id: "inspector",
  slot: "ui.task.inspector",
  entrypoint: "frontend/index.js",
};

beforeEach(() => {
  resetUiContributionActivationForTests();
  slotRegistry.__resetForTests();
});

describe("UI Contribution activation", () => {
  it("commits a declared plugin slot through the public SDK", () => {
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);
    sdk.fill("insights", declaration.slot, () => "new");

    expect(slotRegistry.snapshotAll()).toEqual([]);
    activation.commit();

    expect(slotRegistry.snapshotAll()).toMatchObject([
      {
        name: declaration.slot,
        source: "insights",
        kind: "fill",
      },
    ]);
  });

  it("rejects an undeclared slot and preserves the old generation", () => {
    slotRegistry.fill("insights", declaration.slot, () => "old", {
      id: "old",
    });
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);

    expect(() =>
      sdk.fill("insights", "ui.task.toolbar", () => "unsafe"),
    ).toThrow("did not declare UI slot");
    activation.rollback();

    expect(slotRegistry.snapshotAll()).toMatchObject([
      { name: declaration.slot, source: "insights", id: "old" },
    ]);
  });

  it("rejects plugin identity spoofing during activation", () => {
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);

    expect(() =>
      sdk.fill("another-plugin", declaration.slot, () => "unsafe"),
    ).toThrow("cannot register as");
    activation.rollback();
    expect(slotRegistry.snapshotAll()).toEqual([]);
  });

  it("fails closed when a declared slot is not registered", () => {
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);

    expect(() => activation.commit()).toThrow(
      "did not register declared UI slots",
    );
    activation.rollback();
    expect(slotRegistry.snapshotAll()).toEqual([]);
  });

  it("keeps enforcing declarations after activation", () => {
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);
    sdk.fill("insights", declaration.slot, () => "new");
    activation.commit();

    expect(() =>
      sdk.fill("insights", "ui.task.toolbar", () => "unsafe"),
    ).toThrow("did not declare UI slot");
    expect(() =>
      sdk.fill("insights", declaration.slot, () => "late"),
    ).toThrow("during host activation");
  });

  it("blocks delayed registration after a failed activation", () => {
    const activation = new UiContributionActivation("insights", [
      declaration,
    ]);
    activation.rollback();

    expect(() =>
      sdk.fill("insights", declaration.slot, () => "late"),
    ).toThrow("during host activation");
    expect(slotRegistry.snapshotAll()).toEqual([]);
  });
});
