import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import A2UIRenderer from "./index";
import type { IAgentScopeRuntimeMessage } from "@agentscope-ai/chat";

// Mock @ant-design/x-markdown
vi.mock("@ant-design/x-markdown", () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

// Mock Monaco DiffEditor (heavy dependency)
vi.mock("@monaco-editor/react", () => ({
  DiffEditor: () => <div data-testid="mock-diff-editor" />,
}));

// Mock theme context
vi.mock("@/contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

function makeMessage(
  args: Record<string, unknown>,
): IAgentScopeRuntimeMessage {
  return {
    id: "test-1",
    role: "assistant",
    type: "function_call",
    status: "completed",
    content: [
      {
        type: "data",
        status: "completed",
        data: { name: "a2ui", arguments: args },
      },
    ],
  } as unknown as IAgentScopeRuntimeMessage;
}

describe("A2UIRenderer", () => {
  it("renders a text block with markdown content", () => {
    const data = makeMessage({
      blocks: [{ type: "text", content: "Hello **world**" }],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText(/Hello/)).toBeInTheDocument();
  });

  it("renders a title when provided", () => {
    const data = makeMessage({
      title: "My Title",
      blocks: [{ type: "text", content: "body" }],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("My Title")).toBeInTheDocument();
  });

  it("renders a table block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "table",
          headers: ["Name", "Score"],
          rows: [["Alice", "95"]],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("95")).toBeInTheDocument();
  });

  it("renders a code block with filename", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "code",
          language: "python",
          content: "x = 1",
          filename: "test.py",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("test.py")).toBeInTheDocument();
    expect(screen.getByText("x = 1")).toBeInTheDocument();
  });

  it("renders a progress block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "progress",
          label: "Building",
          value: 50,
          max: 100,
          status: "running",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Building")).toBeInTheDocument();
  });

  it("renders a diff block (mocked Monaco)", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "diff",
          file: "main.py",
          old_content: "a = 1",
          new_content: "a = 2",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("main.py")).toBeInTheDocument();
    expect(screen.getByTestId("mock-diff-editor")).toBeInTheDocument();
  });

  it("renders fallback for unknown block types", () => {
    const data = makeMessage({
      blocks: [{ type: "unknown_widget", foo: "bar" }],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Unknown block type:")).toBeInTheDocument();
  });

  it("renders nothing when blocks is empty", () => {
    const data = makeMessage({ blocks: [] });
    const { container } = render(<A2UIRenderer data={data} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders buttons block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "buttons",
          buttons: [
            { label: "Confirm", value: "ok", style: "primary" },
            { label: "Cancel", value: "cancel", style: "default" },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Confirm")).toBeInTheDocument();
    expect(screen.getByText("Cancel")).toBeInTheDocument();
  });
});
