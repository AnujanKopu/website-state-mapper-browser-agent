import { MarkerType, type Edge, type XYPosition } from "@xyflow/react";

import { truncate } from "../../lib/format";
import type { GraphEdge, GraphState } from "../../types/graph";
import { layoutGraph } from "./layout";
import type { StateFlowNode } from "./StateNode";

export interface GraphTopology {
  key: string;
  nodeIds: string[];
  edgeIds: string[];
  layoutEdges: Edge[];
}

function stateOrder(a: GraphState, b: GraphState): number {
  const aIndex = typeof a.index === "number" ? a.index : Number.MAX_SAFE_INTEGER;
  const bIndex = typeof b.index === "number" ? b.index : Number.MAX_SAFE_INTEGER;
  return aIndex - bIndex || a.id.localeCompare(b.id);
}

/** Stable graph structure used for both Dagre and live-update batching. */
export function createGraphTopology(
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
): GraphTopology {
  const nodeIds = Object.values(nodes).sort(stateOrder).map((state) => state.id);
  const nodeSet = new Set(nodeIds);
  const validEdges = Object.values(edges)
    .filter((edge) => nodeSet.has(edge.from) && nodeSet.has(edge.to))
    .sort((a, b) => a.id.localeCompare(b.id));

  const layoutEdges: Edge[] = validEdges.map((edge) => ({
    id: edge.id,
    source: edge.from,
    target: edge.to,
  }));
  const edgeIds = validEdges.map((edge) => edge.id);
  const key = `${nodeIds.join("|")}::${validEdges
    .map((edge) => `${edge.id}:${edge.from}>${edge.to}`)
    .join("|")}`;

  return { key, nodeIds, edgeIds, layoutEdges };
}

export function layoutTopology(topology: GraphTopology): Record<string, XYPosition> {
  const baseNodes: StateFlowNode[] = topology.nodeIds.map((id) => ({
    id,
    type: "state",
    position: { x: 0, y: 0 },
    data: { state: null, current: false },
  }));

  return Object.fromEntries(
    layoutGraph(baseNodes, topology.layoutEdges).map((node) => [node.id, node.position]),
  );
}

/** Presentation details are derived separately so metadata updates do not re-run Dagre. */
export function buildFlowEdges(
  topology: GraphTopology,
  edges: Record<string, GraphEdge>,
): Edge[] {
  return topology.edgeIds.flatMap((id) => {
    const edge = edges[id];
    if (!edge) return [];
    const inferred = edge.via === "inferred";
    const stroke = inferred ? "var(--text-muted)" : "var(--border-2)";
    return [{
      id: edge.id,
      source: edge.from,
      target: edge.to,
      type: "smoothstep",
      label: truncate(edge.label, 42),
      animated: inferred,
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 14, height: 14 },
      style: {
        stroke,
        strokeWidth: inferred ? 1 : 1.25,
        strokeDasharray: inferred ? "5 4" : undefined,
      },
      labelStyle: { fill: "var(--text-secondary)", fontSize: 10 },
      labelBgPadding: [4, 2] as [number, number],
      labelBgBorderRadius: 3,
      labelBgStyle: { fill: "var(--bg)", fillOpacity: 0.9 },
    }];
  });
}
