import dagre from "dagre";
import type { Node, Edge } from "@xyflow/react";

/**
 * Shared dagre auto-layout for DAG and Mindmap blocks.
 * Positions nodes using the dagre graph layout algorithm.
 */
export function layoutWithDagre(
  nodes: Node[],
  edges: Edge[],
  direction: string,
  nodeWidth: number,
  nodeHeight: number | ((node: Node) => number),
  opts?: { nodesep?: number; ranksep?: number },
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: opts?.nodesep ?? 50,
    ranksep: opts?.ranksep ?? 80,
  });

  for (const node of nodes) {
    const h = typeof nodeHeight === "function" ? nodeHeight(node) : nodeHeight;
    g.setNode(node.id, { width: nodeWidth, height: h });
  }

  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id);
    const h = typeof nodeHeight === "function" ? nodeHeight(node) : nodeHeight;
    return {
      ...node,
      position: {
        x: pos.x - nodeWidth / 2,
        y: pos.y - h / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}
