import { useMemo } from "react";
import {
  ReactFlow,
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

interface MindmapNodeDef {
  id?: string;
  label: string;
  color?: string;
  value?: string;
  children?: MindmapNodeDef[];
}

interface MindmapNodeData {
  label: string;
  color: string;
  value?: string;
  isRoot: boolean;
  interactive?: boolean;
  depth: number;
  [key: string]: unknown;
}

interface MindmapBlockProps {
  block: {
    title?: string;
    root?: MindmapNodeDef;
    direction?: "LR" | "TB";
    height?: number;
    interactive?: boolean;
  };
}

const DEFAULT_COLORS = [
  "#1890ff", "#52c41a", "#fa8c16", "#722ed1",
  "#eb2f96", "#13c2c2", "#faad14", "#2f54eb",
];

const NODE_WIDTH = 150;
const NODE_HEIGHT_ROOT = 44;
const NODE_HEIGHT_CHILD = 36;
const PRO_OPTIONS = { hideAttribution: true };

/**
 * Flatten tree structure into React Flow nodes and edges.
 */
function flattenTree(
  root: MindmapNodeDef,
  interactive: boolean,
): { nodes: Node<MindmapNodeData>[]; edges: Edge[] } {
  const nodes: Node<MindmapNodeData>[] = [];
  const edges: Edge[] = [];
  let autoId = 0;

  function walk(
    node: MindmapNodeDef,
    parentId: string | null,
    depth: number,
    inheritedColor: string,
  ) {
    const id = node.id || `mm-${autoId++}`;
    const color = node.color || inheritedColor;

    nodes.push({
      id,
      type: "mindmapNode",
      position: { x: 0, y: 0 },
      data: {
        label: node.label,
        color,
        value: node.value,
        isRoot: depth === 0,
        interactive,
        depth,
      },
    });

    if (parentId) {
      edges.push({
        id: `e-${parentId}-${id}`,
        source: parentId,
        target: id,
        style: { stroke: color, strokeWidth: 2 },
        type: "smoothstep",
      });
    }

    node.children?.forEach((child, i) => {
      const childColor = child.color || (depth === 0 ? DEFAULT_COLORS[i % DEFAULT_COLORS.length] : color);
      walk(child, id, depth + 1, childColor);
    });
  }

  walk(root, null, 0, DEFAULT_COLORS[0]);
  return { nodes, edges };
}

function getNodeHeight(node: Node): number {
  return (node.data as MindmapNodeData).isRoot ? NODE_HEIGHT_ROOT : NODE_HEIGHT_CHILD;
}

function MindmapNode({ data }: NodeProps<Node<MindmapNodeData>>) {
  const submit = useA2UISubmit();
  const isClickable = data.interactive && data.value;

  const handleClick = () => {
    if (isClickable && submit) submit(data.value!);
  };

  const fontSize = data.isRoot ? 14 : data.depth <= 1 ? 13 : 12;
  const fontWeight = data.depth <= 1 ? 600 : 400;
  const padding = data.isRoot ? "8px 16px" : "5px 12px";
  const borderRadius = data.isRoot ? 22 : 16;
  const bgAlpha = data.isRoot ? "22" : data.depth <= 1 ? "18" : "12";

  return (
    <div
      className={styles.mindmapNode}
      style={{
        borderColor: data.color,
        background: `${data.color}${bgAlpha}`,
        cursor: isClickable ? "pointer" : "default",
        padding,
        borderRadius,
        fontSize,
        fontWeight,
      }}
      onClick={handleClick}
    >
      <Handle type="target" position={Position.Left} className={styles.mindmapHandle} />
      <span style={{ color: data.isRoot ? data.color : "#333" }}>{data.label}</span>
      <Handle type="source" position={Position.Right} className={styles.mindmapHandle} />
    </div>
  );
}

const nodeTypes = { mindmapNode: MindmapNode };

export default function MindmapBlock({ block }: MindmapBlockProps) {
  const direction = block.direction || "LR";
  const height = block.height || 400;

  const { layoutedNodes, layoutedEdges } = useMemo(() => {
    if (!block.root) return { layoutedNodes: [], layoutedEdges: [] };
    const { nodes, edges } = flattenTree(block.root, !!block.interactive);
    const result = layoutWithDagre(nodes, edges, direction, NODE_WIDTH, getNodeHeight, {
      nodesep: 30,
      ranksep: 60,
    });
    return { layoutedNodes: result.nodes, layoutedEdges: result.edges };
  }, [block.root, direction, block.interactive]);

  if (!block.root) return null;

  return (
    <div className={styles.mindmapBlock}>
      {block.title && <div className={styles.mindmapTitle}>{block.title}</div>}
      <div className={styles.mindmapCanvas} style={{ height }}>
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
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}
