import { Form, Input } from "@agentscope-ai/design";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/test/common_setup";

import { SettingsSection } from "./SettingsSection";

describe("SettingsSection", () => {
  it("preserves explicit search targets", () => {
    renderWithProviders(
      <SettingsSection
        id="provider-settings"
        title="Provider"
        data-settings-target="voice.provider"
        data-settings-labels="provider,transcription"
      >
        <div>Content</div>
      </SettingsSection>,
    );

    const section = screen.getByRole("region", { name: "Provider" });
    expect(section).toHaveAttribute("data-settings-target", "voice.provider");
    expect(section).toHaveAttribute(
      "data-settings-labels",
      "provider,transcription",
    );
  });

  it("does not register another form item", () => {
    const { container } = renderWithProviders(
      <Form>
        <SettingsSection id="identity" title="Identity">
          <Form.Item name="displayName" label="Display name">
            <Input />
          </Form.Item>
        </SettingsSection>
      </Form>,
    );

    expect(container.querySelectorAll('[name="displayName"]')).toHaveLength(1);
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });
});
