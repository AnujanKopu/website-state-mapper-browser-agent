import { MarkerType, type Edge, type XYPosition } from "@xyflow/react";

import { truncate } from "../../lib/format";
import type { GraphEdge, GraphState } from "../../types/graph";
import { layoutItems, NODE_HEIGHT, NODE_WIDTH } from "./layout";
import { colorForKey } from "./nodeStyles";

export const FAMILY_PAD_X = 22;
export const FAMILY_HEADER_HEIGHT = 64;
export const FAMILY_PAD_BOTTOM = 22;
export const FAMILY_MEMBER_GAP = 14;
export const FAMILY_COLLAPSED_WIDTH = 260;
export const FAMILY_COLLAPSED_HEIGHT = 88;

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
  expanded: boolean;
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

const placeholder = /^(?::(?:id|param|slug|value)|\{[^}]+\})$/i;
const optionalPlaceholder = /^(?::optional|:(?:id|param|slug|value)\?|\{[^}]+\?\})$/i;

function canonicalFamilyPattern(pattern: string): string {
  try {
    const url = new URL(pattern);
    const segments = url.pathname.split("/").filter(Boolean).map((segment) => {
      const decoded = decodeURIComponent(segment);
      if (optionalPlaceholder.test(decoded)) return ":optional";
      if (placeholder.test(decoded)) return ":param";
      return decoded;
    });
    url.pathname = segments.length ? `/${segments.join("/")}` : "/";
    const query = [...url.searchParams.entries()]
      .map(([key, value]) => [
        key,
        optionalPlaceholder.test(value)
          ? ":optional"
          : placeholder.test(value) ? ":param" : value,
      ] as const)
      .sort(([left], [right]) => left.localeCompare(right));
    url.search = "";
    for (const [key, value] of query) url.searchParams.append(key, value);
    return url.toString()
      .replace(/%3A(param|optional)/gi, ":$1")
      .replace(/\/$/, "");
  } catch {
    // Keep non-URL or legacy custom patterns untouched.
  }
  return pattern;
}

function familyDimensions(memberCount: number, expanded = false): { width: number; height: number } {
  if (!expanded) return { width: FAMILY_COLLAPSED_WIDTH, height: FAMILY_COLLAPSED_HEIGHT };
  const columns = memberCount > 1 ? 2 : 1;
  const rows = Math.ceil(memberCount / columns);
  return {
    width: columns * NODE_WIDTH + (columns - 1) * FAMILY_MEMBER_GAP + FAMILY_PAD_X * 2,
    height:
      FAMILY_HEADER_HEIGHT
      + rows * NODE_HEIGHT
      + Math.max(0, rows - 1) * FAMILY_MEMBER_GAP
      + FAMILY_PAD_BOTTOM,
  };
}

/** Apply UI-only family expansion without changing the persisted topology. */
export function configureFamilyExpansion(
  topology: GraphTopology,
  expandedIds: ReadonlySet<string>,
): GraphTopology {
  const families = topology.families.map((family) => {
    const expanded = expandedIds.has(family.id);
    return { ...family, expanded, ...familyDimensions(family.memberIds.length, expanded) };
  });
  const expansionKey = families.map((family) => `${family.id}:${family.expanded ? 1 : 0}`).join("|");
  return { ...topology, families, layoutKey: `${topology.layoutKey}::${expansionKey}` };
}

/** Stable graph structure used for both Dagre and live-update batching. */
export function createGraphTopology(
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
): GraphTopology {
  const allNodeIds = Object.values(nodes).sort(stateOrder).map((state) => state.id);
  const nodeSet = new Set(allNodeIds);
  const hiddenEquivalentSamples = new Set(
    Object.values(nodes)
      .filter((state) => {
        const representative = state.exploration?.family_representative_state_id;
        return Boolean(
          !state.parent_state_id
          && state.exploration?.route_family
          && representative
          && representative !== state.id
          && nodes[representative],
        );
      })
      .map((state) => state.id),
  );
  const nodeIds = allNodeIds.filter((id) => !hiddenEquivalentSamples.has(id));
  const familyMembers = new Map<
    string,
    {
      pattern: string;
      members: GraphState[];
      metadata?: NonNullable<GraphState["exploration"]>["family"];
    }
  >();
  const familyKeyByPattern = new Map<string, string>();
  const registerFamily = (
    pattern: string,
    metadata?: NonNullable<GraphState["exploration"]>["family"],
  ) => {
    const canonicalPattern = canonicalFamilyPattern(pattern);
    const previousKey = familyKeyByPattern.get(canonicalPattern);
    const key = metadata?.id
      ? `id:${metadata.id}`
      : previousKey ?? `pattern:${canonicalPattern}`;
    if (previousKey && previousKey !== key) {
      const previous = familyMembers.get(previousKey);
      const authoritative = familyMembers.get(key);
      if (previous) {
        familyMembers.set(key, {
          pattern: canonicalPattern,
          members: [...(authoritative?.members ?? []), ...previous.members],
          metadata: authoritative?.metadata ?? previous.metadata,
        });
        familyMembers.delete(previousKey);
      }
    }
    familyKeyByPattern.set(canonicalPattern, key);
    const current = familyMembers.get(key) ?? { pattern: canonicalPattern, members: [] };
    if (metadata) {
      const existing = current.metadata;
      current.metadata = {
        ...existing,
        ...metadata,
        id: existing?.id ?? metadata.id,
        discovered_count: Math.max(
          existing?.discovered_count ?? 0,
          metadata.discovered_count ?? 0,
        ),
        checked_count: Math.max(existing?.checked_count ?? 0, metadata.checked_count ?? 0),
        represented_count: Math.max(
          existing?.represented_count ?? 0,
          metadata.represented_count ?? 0,
        ),
        skipped_count: Math.max(existing?.skipped_count ?? 0, metadata.skipped_count ?? 0),
        sample_labels: Array.from(
          new Set([...(existing?.sample_labels ?? []), ...(metadata.sample_labels ?? [])]),
        ).slice(0, 8),
        sample_urls: Array.from(
          new Set([...(existing?.sample_urls ?? []), ...(metadata.sample_urls ?? [])]),
        ).slice(0, 8),
      };
      current.metadata.pattern = canonicalPattern;
    }
    familyMembers.set(key, current);
    return current;
  };
  for (const state of Object.values(nodes).sort(stateOrder)) {
    for (const family of state.exploration?.surface_families ?? []) {
      if (family.pattern) registerFamily(family.pattern, family);
    }
    const pattern = state.parent_state_id ? null : state.exploration?.route_family;
    if (!pattern) continue;
    const entry = registerFamily(pattern, state.exploration?.family);
    if (!hiddenEquivalentSamples.has(state.id)) entry.members.push(state);
  }
  const families: FamilyGroup[] = [...familyMembers.entries()]
    .filter(([, entry]) => {
      if (entry.metadata?.status && entry.metadata.status !== "confirmed") return false;
      const discovered = entry.metadata?.discovered_count ?? 0;
      return entry.members.length > 0 && (entry.members.length >= 2 || discovered > 1);
    })
    .map(([, entry]) => {
      const pattern = entry.pattern;
      const members = entry.members.sort(stateOrder);
      const metadata = entry.metadata;
      return {
        id: metadata?.id ? `family-${metadata.id}` : familyId(pattern),
        pattern,
        memberIds: members.map((state) => state.id),
        label: metadata?.label ?? displayFamilyPattern(pattern),
        kind: metadata?.kind ?? "items",
        discoveredCount: metadata?.discovered_count ?? members.length,
        checkedCount: metadata?.checked_count ?? members.length,
        representedCount: Math.max(metadata?.represented_count ?? members.length, members.length),
        skippedCount: metadata?.skipped_count ?? 0,
        sampleLabels: metadata?.sample_labels ?? [],
        expanded: false,
        ...familyDimensions(members.length),
      };
    })
    .sort((a, b) => a.id.localeCompare(b.id));

  const ownerByNode: Record<string, string> = Object.fromEntries(
    allNodeIds.map((id) => [id, id]),
  );
  for (const family of families) {
    for (const memberId of family.memberIds) ownerByNode[memberId] = family.id;
  }
  // Exact sample states stay in persisted evidence, but structurally equivalent
  // samples route through the same visual family owner as their representative.
  for (const stateId of hiddenEquivalentSamples) {
    const representative = nodes[stateId].exploration?.family_representative_state_id;
    if (representative) ownerByNode[stateId] = ownerByNode[representative] ?? representative;
  }

  const validEdges = Object.values(edges)
    .filter((edge) => nodeSet.has(edge.from) && nodeSet.has(edge.to))
    .sort((a, b) => a.id.localeCompare(b.id));

  const seenLayoutEdges = new Set<string>();
  const layoutEdges: Edge[] = [];
  for (const edge of validEdges) {
    if (edge.scope === "global_navigation") continue;
    const source = ownerByNode[edge.from];
    const target = ownerByNode[edge.to];
    if (source === target) continue;
    const key = `${source}>${target}`;
    if (seenLayoutEdges.has(key)) continue;
    seenLayoutEdges.add(key);
    layoutEdges.push({ id: `layout:${key}`, source, target });
  }
  const edgeIds = validEdges.map((edge) => edge.id);
  const familyKey = families
    .map((family) => [
      family.id,
      family.label,
      family.discoveredCount,
      family.checkedCount,
      family.representedCount,
      family.skippedCount,
    ].join(":"))
    .join("|");
  const key = `${nodeIds.map((id) => `${id}@${ownerByNode[id]}`).join("|")}::${validEdges
    .map((edge) => `${edge.id}:${edge.from}>${edge.to}`)
    .join("|")}::${familyKey}`;
  const layoutKey = `${nodeIds.map((id) => `${id}@${ownerByNode[id]}`).join("|")}::${layoutEdges
    .map((edge) => `${edge.source}>${edge.target}`)
    .join("|")}`;

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
    if (family.expanded) {
      const columns = family.memberIds.length > 1 ? 2 : 1;
      family.memberIds.forEach((memberId, index) => {
        const column = index % columns;
        const row = Math.floor(index / columns);
        nodePositions[memberId] = {
          x: position.x + FAMILY_PAD_X + column * (NODE_WIDTH + FAMILY_MEMBER_GAP),
          y: position.y + FAMILY_HEADER_HEIGHT + row * (NODE_HEIGHT + FAMILY_MEMBER_GAP),
        };
      });
    }
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
  pathNodeIds: Set<string>;
  pathBundleIds: Set<string>;
}

function collectFocusPath(
  topology: GraphTopology,
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge>,
  selectedId: string,
): { nodeIds: Set<string>; bundleIds: Set<string> } {
  const orderedStates = Object.values(nodes).sort(stateOrder);
  const minimumDepth = Math.min(
    ...orderedStates.map((state) => state.exploration?.page_depth ?? state.depth),
  );
  const roots = orderedStates
    .filter((state) => (state.exploration?.page_depth ?? state.depth) === minimumDepth)
    .map((state) => state.id);
  const outgoing = new Map<string, GraphEdge[]>();
  for (const edge of Object.values(edges)) {
    if (edge.scope === "global_navigation") continue;
    const bucket = outgoing.get(edge.from) ?? [];
    bucket.push(edge);
    outgoing.set(edge.from, bucket);
  }
  for (const bucket of outgoing.values()) {
    bucket.sort((left, right) => {
      const leftPerformed = left.provenance?.includes("performed") || left.via === "performed" ? 1 : 0;
      const rightPerformed = right.provenance?.includes("performed") || right.via === "performed" ? 1 : 0;
      return rightPerformed - leftPerformed || right.confidence - left.confidence || left.id.localeCompare(right.id);
    });
  }

  const queue = [...roots];
  const visited = new Set(queue);
  const previous = new Map<string, GraphEdge>();
  while (queue.length) {
    const current = queue.shift()!;
    if (current === selectedId) break;
    for (const edge of outgoing.get(current) ?? []) {
      if (visited.has(edge.to)) continue;
      visited.add(edge.to);
      previous.set(edge.to, edge);
      queue.push(edge.to);
    }
  }

  const nodeIds = new Set<string>();
  const bundleIds = new Set<string>();
  if (!visited.has(selectedId)) return { nodeIds, bundleIds };
  let cursor = selectedId;
  nodeIds.add(cursor);
  while (previous.has(cursor)) {
    const edge = previous.get(cursor)!;
    const bundleId = edgeBundleId(topology, edge);
    if (bundleId) bundleIds.add(bundleId);
    nodeIds.add(edge.from);
    cursor = edge.from;
  }
  return { nodeIds, bundleIds };
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
  const path = collectFocusPath(topology, nodes, edges, selectedId);
  for (const id of path.nodeIds) nodeIds.add(id);

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

  return {
    nodeIds,
    bundleIds,
    bundleDirections,
    pathNodeIds: path.nodeIds,
    pathBundleIds: path.bundleIds,
  };
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
  animateInferred = true,
): Edge[] {
  const focus = collectNodeEdgeFocus(topology, graphNodes, edges, focusedNodeId);
  const bundles = new Map<string, GraphEdge[]>();
  for (const id of topology.edgeIds) {
    const edge = edges[id];
    if (!edge) continue;
    if (edge.scope === "global_navigation") continue;
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
    const onPath = focus?.pathBundleIds.has(id) ?? false;
    const direction = focus?.bundleDirections.get(id);
    const visualRole = connected
      ? bundleVisualRole(graphNodes, bundle, focusedNodeId, direction)
      : null;
    const dimmed = Boolean(focus && !connected && !onPath);
    const targetState = graphNodes[edge.to];
    const edgeColor = colorForKey(
      targetState?.exploration?.route_family
      ?? `${targetState?.type ?? "transition"}:${topology.ownerByNode[edge.to] ?? edge.to}`,
    );
    const stroke = onPath
      ? edgeColor
      : visualRole === "inbound"
      ? "var(--edge-inbound)"
      : visualRole === "outbound"
        ? "var(--edge-outbound)"
        : visualRole === "both"
          ? "var(--edge-connected)"
          : edgeColor;
    const label = bundle.length > 1
      ? `${bundle.length} paths: ${truncate(edge.label, 30)}`
      : truncate(edge.label, 42);
    return {
      id,
      source: topology.ownerByNode[edge.from] ?? edge.from,
      target: topology.ownerByNode[edge.to] ?? edge.to,
      type: cyclic ? "default" : "smoothstep",
      label: selectedBundleId === id || connected || onPath ? label : undefined,
      animated: animateInferred && inferred && !connected && !onPath,
      className: [
        visualRole === "inbound"
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
        onPath ? "graph-edge--path" : undefined,
      ].filter(Boolean).join(" ") || undefined,
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 14, height: 14 },
      markerStart: reversible
        ? { type: MarkerType.ArrowClosed, color: stroke, width: 11, height: 11 }
        : undefined,
      style: {
        stroke,
        strokeWidth: onPath ? 2.6 : visualRole === "inbound" ? 2.5 : visualRole ? 2 : inferred ? 1.2 : 1.6,
        strokeDasharray: inferred && !connected ? "5 4" : undefined,
        opacity: dimmed ? 0.14 : inferred && !connected ? 0.5 : connected || onPath ? 1 : 0.86,
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
      zIndex: onPath ? 4 : visualRole === "inbound" ? 3 : visualRole ? 2 : 0,
    };
  });
}
