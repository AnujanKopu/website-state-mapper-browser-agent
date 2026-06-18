import { MarkerType, type Edge, type XYPosition } from "@xyflow/react";

import { truncate } from "../../lib/format";
import type { GraphEdge, GraphState } from "../../types/graph";
import { layoutItems, NODE_HEIGHT, NODE_WIDTH } from "./layout";

export const FAMILY_PAD_X = 24;
export const FAMILY_HEADER_HEIGHT = 40;
export const FAMILY_PAD_BOTTOM = 22;
export const FAMILY_MEMBER_GAP = 16;

export interface FamilyGroup {
  id: string;
  pattern: string;
  memberIds: string[];
  width: number;
  height: number;
}

export interface FamilyBox extends FamilyGroup {
  position: XYPosition;
}

export interface GraphLayout {
  nodePositions: Record<string, XYPosition>;
  familyBoxes: FamilyBox[];
}

export interface GraphTopology {
  key: string;
  nodeIds: string[];
  edgeIds: string[];
  layoutEdges: Edge[];
  ownerByNode: Record<string, string>;
  families: FamilyGroup[];
}

function stateOrder(a: GraphState, b: GraphState): number {
  const aIndex = typeof a.index === "number" ? a.index : Number.MAX_SAFE_INTEGER;
  const bIndex = typeof b.index === "number" ? b.index : Number.MAX_SAFE_INTEGER;
  return aIndex - bIndex || a.id.localeCompare(b.id);
}

function familyId(pattern: string): string {
  let hash = 2166136261;
  for (let i = 0; i < pattern.length; i += 1) {
    hash ^= pattern.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return `family-${(hash >>> 0).toString(36)}`;
}

function familyDimensions(memberCount: number): { width: number; height: number } {
  return {
    width: NODE_WIDTH + FAMILY_PAD_X * 2,
    height:
      FAMILY_HEADER_HEIGHT
      + memberCount * NODE_HEIGHT
      + Math.max(0, memberCount - 1) * FAMILY_MEMBER_GAP
      + FAMILY_PAD_BOTTOM,
  };
}

/** Stable graph structure used for both Dagre and live-update batching. */
export function createGraphTopology(
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
): GraphTopology {
  const nodeIds = Object.values(nodes).sort(stateOrder).map((state) => state.id);
  const nodeSet = new Set(nodeIds);
  const familyMembers = new Map<string, GraphState[]>();
  for (const state of Object.values(nodes).sort(stateOrder)) {
    const pattern = state.parent_state_id ? null : state.exploration?.route_family;
    if (!pattern) continue;
    const members = familyMembers.get(pattern) ?? [];
    members.push(state);
    familyMembers.set(pattern, members);
  }
  const families: FamilyGroup[] = [...familyMembers.entries()]
    .filter(([, members]) => members.length >= 2)
    .map(([pattern, members]) => ({
      id: familyId(pattern),
      pattern,
      memberIds: members.map((state) => state.id),
      ...familyDimensions(members.length),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));

  const ownerByNode: Record<string, string> = Object.fromEntries(
    nodeIds.map((id) => [id, id]),
  );
  for (const family of families) {
    for (const memberId of family.memberIds) ownerByNode[memberId] = family.id;
  }

  const validEdges = Object.values(edges)
    .filter((edge) => nodeSet.has(edge.from) && nodeSet.has(edge.to))
    .sort((a, b) => a.id.localeCompare(b.id));

  const seenLayoutEdges = new Set<string>();
  const layoutEdges: Edge[] = [];
  for (const edge of validEdges) {
    const source = ownerByNode[edge.from];
    const target = ownerByNode[edge.to];
    if (source === target) continue;
    const key = `${source}>${target}`;
    if (seenLayoutEdges.has(key)) continue;
    seenLayoutEdges.add(key);
    layoutEdges.push({ id: `layout:${key}`, source, target });
  }
  const edgeIds = validEdges.map((edge) => edge.id);
  const key = `${nodeIds.map((id) => `${id}@${ownerByNode[id]}`).join("|")}::${validEdges
    .map((edge) => `${edge.id}:${edge.from}>${edge.to}`)
    .join("|")}`;

  return { key, nodeIds, edgeIds, layoutEdges, ownerByNode, families };
}

export function layoutTopology(topology: GraphTopology): GraphLayout {
  const familyById = new Map(topology.families.map((family) => [family.id, family]));
  const unitIds = new Set(Object.values(topology.ownerByNode));
  const unitPositions = layoutItems(
    [...unitIds].map((id) => {
      const family = familyById.get(id);
      return family
        ? { id, width: family.width, height: family.height }
        : { id, width: NODE_WIDTH, height: NODE_HEIGHT };
    }),
    topology.layoutEdges,
  );

  const nodePositions: Record<string, XYPosition> = {};
  for (const nodeId of topology.nodeIds) {
    if (topology.ownerByNode[nodeId] === nodeId) {
      nodePositions[nodeId] = unitPositions[nodeId];
    }
  }

  const familyBoxes: FamilyBox[] = topology.families.map((family) => {
    const position = unitPositions[family.id];
    family.memberIds.forEach((memberId, index) => {
      nodePositions[memberId] = {
        x: position.x + FAMILY_PAD_X,
        y: position.y + FAMILY_HEADER_HEIGHT + index * (NODE_HEIGHT + FAMILY_MEMBER_GAP),
      };
    });
    return { ...family, position };
  });

  return { nodePositions, familyBoxes };
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
