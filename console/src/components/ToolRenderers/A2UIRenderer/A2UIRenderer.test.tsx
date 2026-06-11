import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import A2UIRenderer from "./index";
import FormBlock from "./blocks/FormBlock";
import ChoiceBlock from "./blocks/ChoiceBlock";
import { A2UISubmitContext } from "./A2UISubmitContext";

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

// Mock @ant-design/plots (heavy chart library)
vi.mock("@ant-design/plots", () => ({
  Line: ({ data }: { data: unknown[] }) => <div data-testid="mock-line-chart">Line:{data?.length}</div>,
  Column: () => <div data-testid="mock-column-chart" />,
  Bar: () => <div data-testid="mock-bar-chart" />,
  Pie: () => <div data-testid="mock-pie-chart" />,
  Area: () => <div data-testid="mock-area-chart" />,
  Scatter: () => <div data-testid="mock-scatter-chart" />,
  Radar: () => <div data-testid="mock-radar-chart" />,
  Gauge: () => <div data-testid="mock-gauge-chart" />,
}));

// Mock @react-three/fiber and @react-three/drei (heavy 3D libraries)
vi.mock("@react-three/fiber", () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => <div data-testid="mock-canvas">{children}</div>,
}));
vi.mock("@react-three/drei", () => ({
  OrbitControls: () => null,
  Stage: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useGLTF: () => ({ scene: {} }),
  Center: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Text3D: () => null,
}));

// Mock MermaidCodeBlock
vi.mock("@/components/MermaidCodeBlock/MermaidCodeBlock", () => ({
  MermaidCodeBlock: ({ chart }: { chart: string }) => <div data-testid="mock-mermaid">{chart}</div>,
}));

// Mock @xyflow/react
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ nodes, edges }: { nodes: { id: string; data: { label: string } }[]; edges: unknown[] }) => (
    <div data-testid="mock-reactflow">
      {nodes?.map((n) => <div key={n.id}>{n.data?.label}</div>)}
      <span data-testid="edge-count">{(edges as unknown[])?.length}</span>
    </div>
  ),
  Background: () => null,
  Controls: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
}));

// Mock dagre
vi.mock("dagre", () => {
  const nodes: Record<string, { x: number; y: number; width: number; height: number }> = {};
  let nodeIdx = 0;
  return {
    default: {
      graphlib: {
        Graph: class {
          setDefaultEdgeLabel() {}
          setGraph() {}
          setNode(id: string, meta: { width: number; height: number }) {
            nodes[id] = { x: nodeIdx * 200, y: nodeIdx * 100, ...meta };
            nodeIdx++;
          }
          setEdge() {}
          node(id: string) { return nodes[id] || { x: 0, y: 0, width: 150, height: 50 }; }
        },
      },
      layout: () => {},
    },
  };
});

function makeMessage(args: Record<string, unknown>) {
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
  };
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

  it("handles arguments as JSON string (real API format)", () => {
    const data = {
      id: "test-str",
      role: "assistant",
      type: "plugin_call",
      status: "completed",
      content: [
        {
          type: "data",
          data: {
            name: "a2ui",
            arguments: JSON.stringify({
              title: "String Args Title",
              blocks: [{ type: "text", content: "from string args" }],
            }),
          },
        },
      ],
    };
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("String Args Title")).toBeInTheDocument();
    expect(screen.getByText(/from string args/)).toBeInTheDocument();
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

  it("renders a chart block with title", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "chart",
          chartType: "line",
          title: "Sales Trend",
          data: [
            { month: "Jan", value: 100 },
            { month: "Feb", value: 200 },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Sales Trend")).toBeInTheDocument();
    expect(screen.getByTestId("mock-line-chart")).toBeInTheDocument();
  });

  it("renders an alert block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "alert",
          message: "Deployment complete",
          description: "All services are up.",
          alertType: "success",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Deployment complete")).toBeInTheDocument();
    expect(screen.getByText("All services are up.")).toBeInTheDocument();
  });

  it("renders a stat block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "stat",
          stats: [
            { label: "Users", value: 1234 },
            { label: "Revenue", value: "$56K", trend: "up", trendValue: "+12%" },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Users")).toBeInTheDocument();
    expect(screen.getByText("Revenue")).toBeInTheDocument();
  });

  it("renders a file block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "file",
          url: "https://example.com/report.pdf",
          filename: "report.pdf",
          size: "2.3 MB",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText("2.3 MB")).toBeInTheDocument();
  });

  it("renders a divider block", () => {
    const data = makeMessage({
      blocks: [
        { type: "divider", text: "Section Break" },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Section Break")).toBeInTheDocument();
  });

  it("renders a collapse block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "collapse",
          items: [
            { title: "Details", content: "Hidden content here" },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Details")).toBeInTheDocument();
  });

  it("renders an image_buttons block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "image_buttons",
          url: "https://example.com/photo.jpg",
          buttons: [
            { label: "Like", value: "like", position: [50, 80] },
            { label: "Share", value: "share", position: [70, 80] },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Like")).toBeInTheDocument();
    expect(screen.getByText("Share")).toBeInTheDocument();
  });

  it("renders a scene3d block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "scene3d",
          title: "3D Preview",
          shapes: [
            { shape: "box", color: "#ff0000" },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("3D Preview")).toBeInTheDocument();
    expect(screen.getByTestId("mock-canvas")).toBeInTheDocument();
  });

  it("renders a mermaid block", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "mermaid",
          code: "graph LR\n  A-->B",
          title: "My Flow",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("My Flow")).toBeInTheDocument();
    expect(screen.getByTestId("mock-mermaid")).toBeInTheDocument();
  });

  it("renders a canvas block with elements", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "canvas",
          title: "Arch Diagram",
          width: 600,
          height: 300,
          grid: true,
          elements: [
            { shape: "rect", x: 10, y: 10, width: 100, height: 50, text: "Box" },
            { shape: "circle", cx: 200, cy: 50, r: 30, text: "Circle" },
          ],
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Arch Diagram")).toBeInTheDocument();
    expect(screen.getByText("Box")).toBeInTheDocument();
    expect(screen.getByText("Circle")).toBeInTheDocument();
  });

  it("renders a dag block with nodes and edges", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "dag",
          title: "Pipeline",
          nodes: [
            { id: "1", label: "Ingest", status: "completed" },
            { id: "2", label: "Transform", status: "running" },
          ],
          edges: [{ source: "1", target: "2" }],
          direction: "LR",
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Pipeline")).toBeInTheDocument();
    expect(screen.getByTestId("mock-reactflow")).toBeInTheDocument();
  });

  it("renders a mindmap block with tree structure", () => {
    const data = makeMessage({
      blocks: [
        {
          type: "mindmap",
          title: "Knowledge Map",
          root: {
            label: "Root",
            children: [
              { label: "Child A", children: [{ label: "Leaf 1" }] },
              { label: "Child B" },
            ],
          },
        },
      ],
    });
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Knowledge Map")).toBeInTheDocument();
    expect(screen.getByTestId("mock-reactflow")).toBeInTheDocument();
  });

  it("extracts a2ui blocks from tool output (decorator format)", () => {
    const marker = "\n<!-- __a2ui_visual__ -->\n";
    const payload = JSON.stringify({
      __a2ui__: true,
      title: "Edit: test.py",
      blocks: [{ type: "text", content: "Decorated output" }],
    });
    const data = {
      id: "test-decorator",
      role: "assistant",
      type: "plugin_call",
      status: "completed",
      content: [
        {
          type: "data",
          data: { name: "edit_file", arguments: { file_path: "test.py", old_text: "a", new_text: "b" } },
        },
        {
          type: "data",
          data: { output: "Successfully replaced text." + marker + payload },
        },
      ],
    };
    render(<A2UIRenderer data={data} />);
    expect(screen.getByText("Edit: test.py")).toBeInTheDocument();
    expect(screen.getByText(/Decorated output/)).toBeInTheDocument();
  });

  it("FormBlock shows Q&A summary card after submission", async () => {
    const mockSubmit = vi.fn();
    const block = {
      title: "Please provide details:",
      fields: [
        { name: "time", label: "What time?", field_type: "text" },
        { name: "title", label: "Report title?", field_type: "text" },
      ],
      submit_label: "Confirm",
      skip_label: "Skip",
      result_header: "You provided:",
    };

    const { container } = render(
      <A2UISubmitContext.Provider value={mockSubmit}>
        <FormBlock block={block} />
      </A2UISubmitContext.Provider>,
    );

    // Before submission: title, numbered labels, skip + submit buttons visible
    expect(screen.getByText("Please provide details:")).toBeInTheDocument();
    expect(screen.getByText("1. What time?")).toBeInTheDocument();
    expect(screen.getByText("2. Report title?")).toBeInTheDocument();
    expect(screen.getByText("Confirm")).toBeInTheDocument();
    expect(screen.getByText("Skip")).toBeInTheDocument();

    // Fill in form fields
    const inputs = container.querySelectorAll("input");
    await act(async () => {
      fireEvent.change(inputs[0], { target: { value: "08:00" } });
      fireEvent.change(inputs[1], { target: { value: "AI Report" } });
    });

    // Submit the form
    await act(async () => {
      fireEvent.click(screen.getByText("Confirm"));
    });

    // After submission: result header + Q&A summary card
    await waitFor(() => {
      expect(screen.getByText("08:00")).toBeInTheDocument();
    });
    expect(screen.getByText("You provided:")).toBeInTheDocument();
    expect(screen.getByText("What time?")).toBeInTheDocument();
    expect(screen.getByText("Report title?")).toBeInTheDocument();
    expect(screen.getByText("AI Report")).toBeInTheDocument();

    // Buttons should be gone
    expect(screen.queryByText("Confirm")).not.toBeInTheDocument();
    expect(screen.queryByText("Skip")).not.toBeInTheDocument();

    expect(mockSubmit).toHaveBeenCalledWith(
      JSON.stringify({ time: "08:00", title: "AI Report" }),
    );
  });

  it("ChoiceBlock shows only selected option after single-select submission", () => {
    const mockSubmit = vi.fn();
    const block = {
      prompt: "Pick one:",
      options: [
        { label: "Option A", value: "a", description: "First option" },
        { label: "Option B", value: "b", description: "Second option" },
      ],
    };

    render(
      <A2UISubmitContext.Provider value={mockSubmit}>
        <ChoiceBlock block={block} />
      </A2UISubmitContext.Provider>,
    );

    // Before: both options visible
    expect(screen.getByText("Option A")).toBeInTheDocument();
    expect(screen.getByText("Option B")).toBeInTheDocument();

    // Click option A
    fireEvent.click(screen.getByText("Option A"));

    // After: only selected option visible with checkmark
    expect(screen.getByText("Option A")).toBeInTheDocument();
    expect(screen.getByText("First option")).toBeInTheDocument();
    expect(screen.queryByText("Option B")).not.toBeInTheDocument();
    expect(screen.getByText("✓")).toBeInTheDocument();

    expect(mockSubmit).toHaveBeenCalledWith("a");
  });
});
