import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/common_setup";

import { SettingsPage } from "./SettingsPage";

describe("SettingsPage", () => {
  it("keeps editor mounted during feedback updates", async () => {
    const user = userEvent.setup();
    const { rerender } = renderWithProviders(
      <SettingsPage
        header={{ current: "Settings" }}
        feedback={<div>Saving</div>}
      >
        <input aria-label="Editor" />
      </SettingsPage>,
    );
    const editor = screen.getByRole("textbox", { name: "Editor" });
    await user.type(editor, "draft");

    rerender(
      <SettingsPage
        header={{ current: "Settings" }}
        feedback={<div>Could not save</div>}
      >
        <input aria-label="Editor" />
      </SettingsPage>,
    );

    expect(screen.getByRole("textbox", { name: "Editor" })).toBe(editor);
    expect(editor).toHaveValue("draft");
  });

  it("renders one existing header", () => {
    const { container } = renderWithProviders(
      <SettingsPage header={{ current: "Settings" }}>
        <div>Content</div>
      </SettingsPage>,
    );

    expect(container.querySelectorAll("[data-page-header]")).toHaveLength(1);
  });
});
