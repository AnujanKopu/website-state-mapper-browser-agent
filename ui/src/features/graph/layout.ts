import dagre from "dagre";
import type { Edge, Node, XYPosition } from "@xyflow/react";

export const NODE_WIDTH = 210;
export const NODE_HEIGHT = 78;

export interface LayoutItem {
  id: string;
  width: number;
  height: number;
}

/** Layout variable-sized standalone or compound graph units. */
export function layoutItems(items: LayoutItem[], edges: Edge[]): Record<string, XYPosition> {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "LR", nodesep: 36, ranksep: 90, marginx: 24, marginy: 24 });

  for (const item of items) {
    graph.setNode(item.id, { width: item.width, height: item.height });
  }
  for (const edge of edges) {
    graph.setEdge(edge.source, edge.target);
  }

  dagre.layout(graph);
  return Object.fromEntries(
    items.map((item) => {
      const pos = graph.node(item.id);
      return [
        item.id,
        pos
          ? { x: pos.x - item.width / 2, y: pos.y - item.height / 2 }
          : { x: 0, y: 0 },
      ];
    }),
  );
}

/** Left-to-right layered layout via dagre; returns nodes with positions set. */
export function layoutGraph(nodes: Node[], edges: Edge[]): Node[] {
  const positions = layoutItems(
    nodes.map((node) => ({ id: node.id, width: NODE_WIDTH, height: NODE_HEIGHT })),
    edges,
  );

  return nodes.map((node) => {
    const pos = positions[node.id];
    if (!pos) {
      return node;
    }
    return {
      ...node,
      position: pos,
    };
  });
}
