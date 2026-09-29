const pluginId = "task-insights";
const host = window.QwenPaw;

if (!host?.slot || !host.host?.React) {
  throw new Error("QwenPaw Slot SDK is unavailable");
}

const { React } = host.host;
const pillStyle = {
  display: "inline-flex",
  alignItems: "center",
  padding: "5px 9px",
  border: "1px solid #dfe5da",
  borderRadius: "999px",
  color: "#536653",
  fontSize: "11px",
  background: "#f7f9f4",
};

function pill(text) {
  return React.createElement("span", { style: pillStyle }, text);
}

host.slot.fill(
  pluginId,
  "ui.task.toolbar",
  (_default, context) =>
    pill(context?.task ? "Task Insights active" : "Task Insights ready"),
  { id: "task-insights.toolbar", order: 200 },
);

host.slot.fill(
  pluginId,
  "ui.task.tab",
  (_default, context) => {
    const eventCount = Array.isArray(context?.events)
      ? context.events.length
      : 0;
    return pill(`${eventCount} durable events`);
  },
  { id: "task-insights.tab", order: 200 },
);

host.slot.fill(
  pluginId,
  "ui.task.inspector",
  (_default, context) => {
    const objective = context?.task?.objective;
    return pill(
      typeof objective === "string"
        ? `${objective.length} objective characters`
        : "No task selected",
    );
  },
  { id: "task-insights.inspector", order: 200 },
);

host.slot.fill(
  pluginId,
  "ui.artifact.preview",
  (_default, context) => {
    const artifact = context?.artifact;
    return artifact
      ? pill(`${artifact.kind} · ${artifact.size_bytes} bytes`)
      : null;
  },
  { id: "task-insights.artifact-preview", order: 200 },
);
