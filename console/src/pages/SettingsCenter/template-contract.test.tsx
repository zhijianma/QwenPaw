import { useRef, useState, type ComponentType } from "react";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsPage, SettingsSection } from "@/components/settings";
import { useAgentStore } from "@/stores/agentStore";
import { renderWithProviders } from "@/test/common_setup";

import { useSettingsSearchTarget } from "./useSettingsSearchTarget";

const registry = vi.hoisted(() => ({
  routes: [] as Array<{
    id: string;
    path: string;
    Component: ComponentType;
  }>,
}));

vi.mock("@/plugins/registry/hooks", () => ({
  useRoutes: () => registry.routes,
  useMenuItems: () => [],
}));

vi.mock("@/plugins/PluginContext", () => ({
  usePlugins: () => ({ loading: false }),
}));

import SettingsCenter from ".";

describe("settings template contract", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });
    registry.routes = [];
    useAgentStore.setState({
      selectedAgent: "native",
      agents: [
        {
          id: "native",
          name: "Native agent",
          description: "",
          workspace_dir: "",
          enabled: true,
          backend: "qwenpaw",
        },
      ],
    });
  });

  it("marks a migrated host page and renders one header", () => {
    registry.routes = [
      {
        id: "core.heartbeat",
        path: "/heartbeat",
        Component: () => (
          <SettingsPage header={{ current: "Heartbeat" }}>
            <div>Heartbeat settings</div>
          </SettingsPage>
        ),
      },
    ];

    const { container } = renderWithProviders(<SettingsCenter />, {
      initialEntries: ["/settings/heartbeat"],
    });

    expect(container.querySelector("main")).toHaveAttribute(
      "data-settings-template",
      "true",
    );
    expect(container.querySelectorAll("[data-page-header]")).toHaveLength(1);
  });

  it("opens a hidden target without replacing its editor", async () => {
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      {},
    ] as unknown as DOMRectList);
    const user = userEvent.setup();

    function Fixture() {
      const root = useRef<HTMLDivElement>(null);
      const [open, setOpen] = useState(false);
      const [feedback, setFeedback] = useState("Saving");
      useSettingsSearchTarget(
        root,
        {
          page: "example",
          label: "Timeout",
          targetId: "example.timeout",
          surface: "example.advanced",
        },
        "example",
      );
      return (
        <div ref={root}>
          <button
            data-settings-open="example.advanced"
            aria-expanded={open}
            onClick={() => setOpen(true)}
          >
            Advanced settings
          </button>
          <button onClick={() => setFeedback("Could not save")}>Fail</button>
          <SettingsPage
            header={{ current: "Example" }}
            feedback={<div>{feedback}</div>}
          >
            <div hidden={!open}>
              <SettingsSection
                id="example-timeout"
                title="Timeout"
                data-settings-target="example.timeout"
              >
                <input aria-label="Timeout" />
              </SettingsSection>
            </div>
          </SettingsPage>
        </div>
      );
    }

    renderWithProviders(<Fixture />);
    await waitFor(() =>
      expect(screen.getByRole("region", { name: "Timeout" })).toHaveAttribute(
        "data-search-highlight",
        "true",
      ),
    );
    const editor = screen.getByRole("textbox", { name: "Timeout" });
    await user.type(editor, "30");
    await user.click(screen.getByRole("button", { name: "Fail" }));
    expect(screen.getByRole("textbox", { name: "Timeout" })).toBe(editor);
    expect(editor).toHaveValue("30");
  });
});
