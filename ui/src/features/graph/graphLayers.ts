import { MarkerType, type Edge, type XYPosition } from "@xyflow/react";

import type { GraphEdge, GraphState, SurfaceItem, SurfaceStatus } from "../../types/graph";
import { layoutItems, NODE_HEIGHT, NODE_WIDTH } from "./layout";

export const INTERACTION_NODE_WIDTH = 190;
export const INTERACTION_NODE_HEIGHT = 66;

export interface PageProjection {
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  ownerByState: Record<string, string>;
}

export interface InteractionCapability extends Record<string, unknown> {
  id: string;
  ownerStateId: string;
  label: string;
  kind: string;
  status: SurfaceStatus;
  count: number;
  items: SurfaceItem[];
}

export interface InteractionProjection {
  key: string;
  pageId: string;
  stateIds: string[];
  capabilities: Record<string, InteractionCapability>;
  positions: Record<string, XYPosition>;
  edges: Edge[];
}

export function pageAncestorId(nodes: Record<string, GraphState>, stateId: string): string {
  let current = stateId;
  const seen = new Set<string>();
  while (nodes[current]?.parent_state_id && !seen.has(current)) {
    seen.add(current);
    const parent = nodes[current].parent_state_id;
    if (!parent || !nodes[parent]) break;
    current = parent;
  }
  return current;
}

export function projectPageGraph(
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
): PageProjection {
  const pageNodes = Object.fromEntries(
    Object.entries(nodes).filter(([, state]) => !state.parent_state_id),
  );
  const ownerByState = Object.fromEntries(
    Object.keys(nodes).map((id) => [id, pageAncestorId(nodes, id)]),
  );
  const pageEdges: Record<string, GraphEdge> = {};
  const edgeIdByCapability = new Map<string, string>();
  for (const edge of Object.values(edges)) {
    if (edge.scope === "global_navigation") continue;
    const from = ownerByState[edge.from];
    const to = ownerByState[edge.to];
    if (!from || !to || from === to || !pageNodes[from] || !pageNodes[to]) continue;
    const controlKey = edge.evidence?.find((item) => item.control_key)?.control_key
      ?? edge.surface_item_id
      ?? edge.label;
    const capabilityKey = `${from}>${to}:${edge.transition_kind ?? "control"}:${controlKey}`;
    const existingId = edgeIdByCapability.get(capabilityKey);
    if (existingId) {
      const existing = pageEdges[existingId];
      pageEdges[existingId] = {
        ...existing,
        collapsed_count: (existing.collapsed_count || 1) + (edge.collapsed_count || 1),
        confidence: Math.max(existing.confidence, edge.confidence),
        provenance: Array.from(new Set([...(existing.provenance ?? []), ...(edge.provenance ?? [])])),
        evidence: [...(existing.evidence ?? []), ...(edge.evidence ?? [])].slice(0, 16),
      };
      continue;
    }
    const id = `page:${edge.id}`;
    pageEdges[id] = { ...edge, id, from, to };
    edgeIdByCapability.set(capabilityKey, id);
  }
  return { nodes: pageNodes, edges: pageEdges, ownerByState };
}

function stateOrder(a: GraphState, b: GraphState): number {
  return (a.index ?? Number.MAX_SAFE_INTEGER) - (b.index ?? Number.MAX_SAFE_INTEGER)
    || a.id.localeCompare(b.id);
}

function isPageNavigation(item: SurfaceItem): boolean {
  if (item.interaction_scope === "page_navigation") return true;
  if (item.interaction_scope) return false;
  const href = item.href?.trim();
  return Boolean(
    item.kind === "link"
    && href
    && !href.startsWith("#")
    && item.status !== "blocked",
  );
}

function capabilityStatus(items: SurfaceItem[]): SurfaceStatus {
  if (items.some((item) => item.status === "blocked")) return "blocked";
  if (items.some((item) => item.status === "explored")) return "explored";
  if (items.every((item) => item.status === "inventory_only")) return "inventory_only";
  return items[0]?.status ?? "inventory_only";
}

export function buildInteractionProjection(
  nodes: Record<string, GraphState>,
  graphEdges: Record<string, GraphEdge>,
  pageId: string,
): InteractionProjection {
  const stateIds = Object.values(nodes)
    .filter((state) => pageAncestorId(nodes, state.id) === pageId)
    .sort(stateOrder)
    .map((state) => state.id);
  const stateSet = new Set(stateIds);
  const localEdges = Object.values(graphEdges).filter(
    (edge) => stateSet.has(edge.from) && stateSet.has(edge.to),
  );
  const outcomeItems = new Set(
    localEdges
      .filter((edge) => Boolean(edge.surface_item_id))
      .map((edge) => `${edge.from}:${edge.surface_item_id}`),
  );
  const capabilities: Record<string, InteractionCapability> = {};
  const capabilityByItem = new Map<string, string>();

  for (const stateId of stateIds) {
    const state = nodes[stateId];
    const grouped = new Map<string, SurfaceItem[]>();
    for (const item of state.surface_items ?? []) {
      if (isPageNavigation(item)) continue;
      const itemId = item.item_id ?? `${item.kind}:${item.label}`;
      // Nested states may expose dozens of raw menu options. Keep controls
      // that produced an outcome; the complete inventory remains in NodePanel.
      if (stateId !== pageId && !outcomeItems.has(`${stateId}:${itemId}`)) {
        continue;
      }
      const groupKey = item.component_key ?? itemId;
      const key = `${groupKey}:${item.kind ?? "control"}`;
      const bucket = grouped.get(key) ?? [];
      bucket.push(item);
      grouped.set(key, bucket);
    }
    let fallbackIndex = 0;
    for (const [groupKey, items] of grouped) {
      const stablePart = items[0].component_key
        ?? items[0].item_id
        ?? `${groupKey}:${fallbackIndex++}`;
      const id = `interaction:${stateId}:${stablePart}`;
      const capability: InteractionCapability = {
        id,
        ownerStateId: stateId,
        label: items[0].component_label || items[0].label,
        kind: items[0].kind ?? "control",
        status: capabilityStatus(items),
        count: items.length,
        items,
      };
      capabilities[id] = capability;
      for (const item of items) {
        if (item.item_id) capabilityByItem.set(`${stateId}:${item.item_id}`, id);
      }
    }
  }

  const edges: Edge[] = [];
  for (const capability of Object.values(capabilities)) {
    edges.push({
      id: `owns:${capability.ownerStateId}:${capability.id}`,
      source: capability.ownerStateId,
      target: capability.id,
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--border-2)" },
      style: { stroke: "var(--border-2)", strokeWidth: 1 },
    });
  }
  for (const edge of localEdges) {
    const capabilityId = edge.surface_item_id
      ? capabilityByItem.get(`${edge.from}:${edge.surface_item_id}`)
      : undefined;
    edges.push({
      id: `nested:${edge.id}`,
      source: capabilityId ?? edge.from,
      target: edge.to,
      type: "smoothstep",
      label: capabilityId ? edge.label : undefined,
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--green)" },
      style: { stroke: "var(--green)", strokeWidth: 1.25 },
      labelStyle: { fill: "var(--text-secondary)", fontSize: 10 },
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
      labelBgStyle: { fill: "var(--bg)", fillOpacity: 0.9 },
    });
  }

  const positions = layoutItems(
    [
      ...stateIds.map((id) => ({ id, width: NODE_WIDTH, height: NODE_HEIGHT })),
      ...Object.keys(capabilities).map((id) => ({
        id,
        width: INTERACTION_NODE_WIDTH,
        height: INTERACTION_NODE_HEIGHT,
      })),
    ],
    edges,
  );
  const key = `${stateIds.join("|")}::${Object.keys(capabilities).join("|")}::${edges
    .map((edge) => `${edge.source}>${edge.target}`)
    .join("|")}`;
  return { key, pageId, stateIds, capabilities, positions, edges };
}
