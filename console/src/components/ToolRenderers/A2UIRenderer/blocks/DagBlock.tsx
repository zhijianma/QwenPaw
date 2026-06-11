import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
} from "@xyflow/react";
import { layoutWithDagre } from "./dagreLayout";
import { useA2UISubmit } from "../A2UISubmitContext";
import styles from "../index.module.less";
import "@xyflow/react/dist/style.css";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface DagNodeData {
  label: string;
  icon?: string;
  description?: string;
  status?: "completed" | "running" | "pending" | "error";
  value?: string;
  interactive?: boolean;
  targetPosition: Position;
  sourcePosition: Position;
  [key: string]: unknown;
}

interface DagEdgeDef {
  source: string;
  target: string;
  label?: string;
  animated?: boolean;
}

interface DagBlockProps {
  block: {
    title?: string;
    nodes?: { id: string; label: string; icon?: string; description?: string; status?: string; value?: string }[];
    edges?: DagEdgeDef[];
    direction?: "TB" | "LR" | "BT" | "RL";
    height?: number;
    interactive?: boolean;
  };
}

const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  completed: { bg: "#f6ffed", border: "#52c41a", text: "#389e0d" },
  running: { bg: "#e6f7ff", border: "#1890ff", text: "#096dd9" },
  pending: { bg: "#fafafa", border: "#d9d9d9", text: "#8c8c8c" },
  error: { bg: "#fff2f0", border: "#ff4d4f", text: "#cf1322" },
};

const NODE_WIDTH = 180;
const NODE_HEIGHT = 60;
const PRO_OPTIONS = { hideAttribution: true };

function DagNode({ data }: NodeProps<Node<DagNodeData>>) {
  const submit = useA2UISubmit();
  const status = data.status || "pending";
  const colors = STATUS_COLORS[status] || STATUS_COLORS.pending;
  const isClickable = data.interactive && data.value;

  const handleClick = () => {
    if (isClickable && submit) submit(data.value!);
  };

  return (
    <div
      className={styles.dagNode}
      style={{
        background: colors.bg,
        borderColor: colors.border,
        cursor: isClickable ? "pointer" : "default",
      }}
      onClick={handleClick}
    >
      <Handle type="target" position={data.targetPosition} className={styles.dagHandle} />
      <div className={styles.dagNodeContent}>
        {data.icon && <span className={styles.dagNodeIcon}>{data.icon}</span>}
        <div className={styles.dagNodeText}>
          <div className={styles.dagNodeLabel} style={{ color: colors.text }}>
            {data.label}
          </div>
          {data.description && (
            <div className={styles.dagNodeDesc}>{data.description}</div>
          )}
        </div>
      </div>
      <Handle type="source" position={data.sourcePosition} className={styles.dagHandle} />
    </div>
  );
}

const nodeTypes = { dagNode: DagNode };

export default function DagBlock({ block }: DagBlockProps) {
  const direction = block.direction || "TB";
  const isHorizontal = direction === "LR" || direction === "RL";
  const height = block.height || 400;

  const targetPos = isHorizontal ? Position.Left : Position.Top;
  const sourcePos = isHorizontal ? Position.Right : Position.Bottom;

  const { layoutedNodes, layoutedEdges } = useMemo(() => {
    const rawNodes: Node<DagNodeData>[] = (block.nodes || []).map((n) => ({
      id: n.id,
      type: "dagNode",
      position: { x: 0, y: 0 },
      data: {
        label: n.label,
        icon: n.icon,
        description: n.description,
        status: n.status as DagNodeData["status"],
        value: n.value,
        interactive: !!block.interactive,
        targetPosition: targetPos,
        sourcePosition: sourcePos,
      },
    }));

    const rawEdges: Edge[] = (block.edges || []).map((e, i) => ({
      id: `e-${e.source}-${e.target}-${i}`,
      source: e.source,
      target: e.target,
      label: e.label,
      animated: e.animated,
      style: { stroke: "#b1b1b7", strokeWidth: 1.5 },
      labelStyle: { fontSize: 11, fill: "#666" },
    }));

    const result = layoutWithDagre(rawNodes, rawEdges, direction, NODE_WIDTH, NODE_HEIGHT);
    return { layoutedNodes: result.nodes, layoutedEdges: result.edges };
  }, [block.nodes, block.edges, direction, block.interactive, targetPos, sourcePos]);

  if (!block.nodes?.length) return null;

  return (
    <div className={styles.dagBlock}>
      {block.title && <div className={styles.dagTitle}>{block.title}</div>}
      <div className={styles.dagCanvas} style={{ height }}>
        <ReactFlow
          nodes={layoutedNodes}
          edges={layoutedEdges}
          nodeTypes={nodeTypes}
          fitView
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable={false}
          proOptions={PRO_OPTIONS}
        >
          <Background gap={20} size={1} color="rgba(0,0,0,0.05)" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}
