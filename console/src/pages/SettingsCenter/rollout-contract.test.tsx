// @vitest-environment jsdom
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  communityStatus: vi.fn(),
  connectPassword: vi.fn(),
  getOffloadPolicy: vi.fn(),
  setOffloadPolicy: vi.fn(),
}));

vi.mock("@/api/modules/community", () => ({
  communityConnectionApi: {
    status: mocks.communityStatus,
    connectPassword: mocks.connectPassword,
  },
}));

vi.mock("@/api/modules/toolCalls", () => ({
  toolCallsApi: {
    getOffloadPolicy: mocks.getOffloadPolicy,
    setOffloadPolicy: mocks.setOffloadPolicy,
  },
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: {
      destroy: vi.fn(),
      error: vi.fn(),
    },
    modal: { confirm: vi.fn() },
  }),
}));

vi.mock("@/stores/agentStore", () => ({
  useAgentStore: Object.assign(() => ({ selectedAgent: "default" }), {
    subscribe: () => () => {},
    getState: () => ({ selectedAgent: "default" }),
  }),
}));

vi.mock("@/utils/inboxEvents", () => ({ notifyInboxChanged: vi.fn() }));
vi.mock("@/utils/communityAuthorization", () => ({
  reserveAuthorizationWindow: vi.fn(),
  openAuthorizationUrl: vi.fn(),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { useState } from "react";

import { SettingsPage } from "@/components/settings";
import CommunitySettings from "@/pages/Settings/Community";
import OffloadPolicyPage from "@/pages/Settings/OffloadPolicy";

describe("settings rollout contracts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.communityStatus.mockResolvedValue({
      status: "disconnected",
      sync_enabled: false,
      messages_enabled: true,
      password_login_supported: true,
      local_login_supported: false,
    });
    mocks.connectPassword.mockResolvedValue({ connected: true });
    mocks.getOffloadPolicy.mockResolvedValue({
      default_action: "keep_foreground",
    });
    mocks.setOffloadPolicy.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("preserves_manual_submit_on_community_connection", async () => {
    const { container } = render(<CommunitySettings />);
    const account = await screen.findByLabelText("community.loginAccount");
    const password = screen.getByLabelText("community.loginPassword");

    fireEvent.change(account, { target: { value: "alice@example.com" } });
    fireEvent.change(password, { target: { value: "secret" } });
    expect(mocks.connectPassword).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "community.saveAndConnect" }),
    );
    await waitFor(() => expect(mocks.connectPassword).toHaveBeenCalledOnce());
    expect(mocks.connectPassword).toHaveBeenCalledWith(
      "alice@example.com",
      "secret",
    );
    expect(container.querySelectorAll("[data-settings-page]")).toHaveLength(1);
  });

  it("preserves_offload_save_schedule", async () => {
    const { container } = render(<OffloadPolicyPage />);
    const offload = await screen.findByRole("radio", {
      name: /agentConfig\.offloadPolicy\.offload/,
    });

    vi.useFakeTimers();
    fireEvent.click(offload);
    expect(mocks.setOffloadPolicy).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(mocks.setOffloadPolicy).toHaveBeenCalledOnce();
    expect(mocks.setOffloadPolicy).toHaveBeenCalledWith("offload");
    expect(container.querySelectorAll("[data-settings-page]")).toHaveLength(1);
  });

  it("retains_editors_when_feedback_changes", () => {
    function FeedbackFixture() {
      const [feedback, setFeedback] = useState("Ready");
      return (
        <SettingsPage
          header={{ current: "Editor" }}
          feedback={<div>{feedback}</div>}
        >
          <label htmlFor="draft">Draft</label>
          <input id="draft" />
          <button onClick={() => setFeedback("Could not save")}>Fail</button>
        </SettingsPage>
      );
    }

    render(<FeedbackFixture />);
    const editor = screen.getByLabelText("Draft");
    fireEvent.change(editor, { target: { value: "unfinished draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));

    expect(screen.getByLabelText("Draft")).toBe(editor);
    expect(editor).toHaveValue("unfinished draft");
    expect(screen.getByText("Could not save")).toBeInTheDocument();
  });
});
