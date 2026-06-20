import { MarkerType, type Edge, type XYPosition } from "@xyflow/react";

import { truncate } from "../../lib/format";
import type { GraphEdge, GraphState } from "../../types/graph";
import { layoutItems, NODE_HEIGHT, NODE_WIDTH } from "./layout";

export const FAMILY_PAD_X = 24;
export const FAMILY_HEADER_HEIGHT = 40;
export const FAMILY_PAD_BOTTOM = 22;
export const FAMILY_MEMBER_GAP = 16;
export const FAMILY_MIN_WIDTH = 380;

export interface FamilyGroup {
  id: string;
  pattern: string;
  memberIds: string[];
  width: number;
  height: number;
  label: string;
  kind: string;
  discoveredCount: number;
  checkedCount: number;
  representedCount: number;
  skippedCount: number;
  sampleLabels: string[];
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
  layoutKey: string;
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
    width: Math.max(FAMILY_MIN_WIDTH, NODE_WIDTH + FAMILY_PAD_X * 2),
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
    .filter(([, members]) => {
      const discovered = members[0]?.exploration?.family?.discovered_count ?? 0;
      return members.length >= 2 || discovered > 1;
    })
    .map(([pattern, members]) => {
      const metadata = members[0]?.exploration?.family;
      return {
        id: metadata?.id ? `family-${metadata.id}` : familyId(pattern),
        pattern,
        memberIds: members.map((state) => state.id),
        label: metadata?.label ?? displayFamilyPattern(pattern),
        kind: metadata?.kind ?? "items",
        discoveredCount: metadata?.discovered_count ?? members.length,
        checkedCount: metadata?.checked_count ?? members.length,
        representedCount: metadata?.represented_count ?? members.length,
        skippedCount: metadata?.skipped_count ?? 0,
        sampleLabels: metadata?.sample_labels ?? [],
        ...familyDimensions(members.length),
      };
    })
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
    const inferredOnly = edge.via === "inferred"
      && !(edge.provenance ?? []).includes("performed");
    if (edge.scope === "global_navigation" && inferredOnly) continue;
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
  const layoutKey = nodeIds.map((id) => `${id}@${ownerByNode[id]}`).join("|");

  return { key, layoutKey, nodeIds, edgeIds, layoutEdges, ownerByNode, families };
}

function displayFamilyPattern(pattern: string): string {
  try {
    const url = new URL(pattern);
    const literal = url.pathname.split("/").filter((part) => part && !part.startsWith(":"))[0];
    if (!literal) return "Items";
    const label = literal.replaceAll("-", " ");
    return label.endsWith("s") ? label : `${label}s`;
  } catch {
    return "Items";
  }
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
        x: position.x + (family.width - NODE_WIDTH) / 2,
        y: position.y + FAMILY_HEADER_HEIGHT + index * (NODE_HEIGHT + FAMILY_MEMBER_GAP),
      };
    });
    return { ...family, position };
  });

  return { nodePositions, familyBoxes };
}

function edgeBundleId(topology: GraphTopology, edge: GraphEdge): string | null {
  const source = topology.ownerByNode[edge.from] ?? edge.from;
  const target = topology.ownerByNode[edge.to] ?? edge.to;
  if (source === target) return null;
  return `bundle:${source}>${target}`;
}

function isDescendantOf(
  nodes: Record<string, GraphState>,
  nodeId: string,
  ancestorId: string,
): boolean {
  return nodes[nodeId]?.parent_state_id === ancestorId;
}

function edgeTargetsNode(
  _topology: GraphTopology,
  nodes: Record<string, GraphState>,
  edge: GraphEdge,
  nodeId: string,
): boolean {
  if (edge.to === nodeId) return true;
  if (isDescendantOf(nodes, edge.to, nodeId)) return true;
  return false;
}

function edgeSourcesNode(
  _topology: GraphTopology,
  nodes: Record<string, GraphState>,
  edge: GraphEdge,
  nodeId: string,
): boolean {
  if (edge.from === nodeId) return true;
  if (isDescendantOf(nodes, edge.from, nodeId)) return true;
  return false;
}

export type EdgeFocusDirection = "inbound" | "outbound" | "both";

export interface NodeEdgeFocus {
  nodeIds: Set<string>;
  bundleIds: Set<string>;
  bundleDirections: Map<string, EdgeFocusDirection>;
}

/** Selected node and its exact one-hop incoming/outgoing neighborhood. */
export function collectNodeEdgeFocus(
  topology: GraphTopology,
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
  selectedId: string | null,
): NodeEdgeFocus | null {
  if (!selectedId) return null;

  const nodeIds = new Set<string>([selectedId]);
  const bundleIds = new Set<string>();
  const bundleDirections = new Map<string, EdgeFocusDirection>();

  const markBundle = (edge: GraphEdge, direction: Exclude<EdgeFocusDirection, "both">) => {
    const bundleId = edgeBundleId(topology, edge);
    if (!bundleId) return;
    bundleIds.add(bundleId);
    nodeIds.add(edge.from);
    nodeIds.add(edge.to);
    const existing = bundleDirections.get(bundleId);
    if (!existing || existing === direction) bundleDirections.set(bundleId, direction);
    else bundleDirections.set(bundleId, "both");
  };

  for (const edge of Object.values(edges)) {
    const outbound = edgeSourcesNode(topology, nodes, edge, selectedId);
    const inbound = edgeTargetsNode(topology, nodes, edge, selectedId);
    if (outbound) markBundle(edge, "outbound");
    if (inbound) markBundle(edge, "inbound");
  }

  return { nodeIds, bundleIds, bundleDirections };
}

function bundleVisualRole(
  nodes: Record<string, GraphState>,
  bundle: GraphEdge[],
  focusedNodeId: string | null,
  direction: EdgeFocusDirection | undefined,
): EdgeFocusDirection | null {
  if (!direction) return null;
  if (direction === "both") return "both";
  if (
    focusedNodeId
    && bundle.some((item) =>
      isDescendantOf(nodes, item.to, focusedNodeId)
      || isDescendantOf(nodes, item.from, focusedNodeId),
    )
  ) {
    return "both";
  }
  return direction;
}

/** Presentation details are derived separately so metadata updates do not re-run Dagre. */
export function buildFlowEdges(
  topology: GraphTopology,
  edges: Record<string, GraphEdge>,
  selectedBundleId: string | null = null,
  focusedNodeId: string | null = null,
  graphNodes: Record<string, GraphState> = {},
): Edge[] {
  const focus = collectNodeEdgeFocus(topology, graphNodes, edges, focusedNodeId);
  const bundles = new Map<string, GraphEdge[]>();
  for (const id of topology.edgeIds) {
    const edge = edges[id];
    if (!edge) continue;
    const inferredOnly = edge.via === "inferred"
      && !(edge.provenance ?? []).includes("performed");
    if (
      edge.scope === "global_navigation"
      && inferredOnly
      && !focusedNodeId
      && selectedBundleId !== edgeBundleId(topology, edge)
    ) continue;
    const bundleId = edgeBundleId(topology, edge);
    if (!bundleId) continue;
    const bucket = bundles.get(bundleId) ?? [];
    bucket.push(edge);
    bundles.set(bundleId, bucket);
  }
  return [...bundles.entries()].map(([id, bundle]) => {
    const edge = bundle[0];
    const inferred = bundle.every((item) => item.via === "inferred");
    const reversible = bundle.some((item) => item.reversible);
    const globalNavigation = bundle.every((item) => item.scope === "global_navigation");
    const reverseId = `bundle:${topology.ownerByNode[edge.to] ?? edge.to}>${topology.ownerByNode[edge.from] ?? edge.from}`;
    const cyclic = bundles.has(reverseId);
    const connected = focus?.bundleIds.has(id) ?? false;
    const direction = focus?.bundleDirections.get(id);
    const visualRole = connected
      ? bundleVisualRole(graphNodes, bundle, focusedNodeId, direction)
      : null;
    const dimmed = Boolean(focus && !connected);
    const stroke = visualRole === "inbound"
      ? "var(--edge-inbound)"
      : visualRole === "outbound"
        ? "var(--edge-outbound)"
        : visualRole === "both"
          ? "var(--edge-connected)"
          : inferred
            ? "var(--text-muted)"
            : reversible
              ? "var(--green)"
            : "var(--border-2)";
    const label = bundle.length > 1
      ? `${bundle.length} paths: ${truncate(edge.label, 30)}`
      : truncate(edge.label, 42);
    return {
      id,
      source: topology.ownerByNode[edge.from] ?? edge.from,
      target: topology.ownerByNode[edge.to] ?? edge.to,
      type: cyclic ? "default" : "smoothstep",
      label: selectedBundleId === id || connected ? label : undefined,
      animated: inferred && !connected,
      className: visualRole === "inbound"
        ? "graph-edge--inbound"
        : visualRole === "outbound"
          ? "graph-edge--outbound"
          : visualRole === "both"
            ? "graph-edge--connected"
            : dimmed
              ? "graph-edge--dimmed"
              : reversible
                ? "graph-edge--reversible"
                : globalNavigation
                  ? "graph-edge--global"
                  : undefined,
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 14, height: 14 },
      style: {
        stroke,
        strokeWidth: visualRole === "inbound" ? 2.5 : visualRole ? 2 : inferred ? 1 : 1.25,
        strokeDasharray: inferred && !connected ? "5 4" : undefined,
        opacity: dimmed ? 0.16 : 1,
      },
      labelStyle: { fill: "var(--text-secondary)", fontSize: 10 },
      labelBgPadding: [4, 2] as [number, number],
      labelBgBorderRadius: 3,
      labelBgStyle: { fill: "var(--bg)", fillOpacity: 0.9 },
      data: {
        edgeIds: bundle.map((item) => item.id),
        count: bundle.length,
        cyclic,
        reversible,
        globalNavigation,
      },
      zIndex: visualRole === "inbound" ? 3 : visualRole ? 2 : 0,
    };
  });
}
