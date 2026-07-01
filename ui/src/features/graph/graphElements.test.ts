import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphState } from "../../types/graph";
import { buildFlowEdges, collectNodeEdgeFocus, configureFamilyExpansion, createGraphTopology, layoutTopology } from "./graphElements";
import { NODE_HEIGHT, NODE_WIDTH } from "./layout";

function state(id: string, index: number, title = id): GraphState {
  return {
    id,
    index,
    type: "page",
    url: `https://example.com/${id}`,
    url_normalized: `https://example.com/${id}`,
    title,
    label: null,
    summary: null,
    fingerprint: id,
    depth: index,
    screenshot: "",
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
  };
}

function edge(id: string, from: string, to: string, via = "performed"): GraphEdge {
  return {
    id,
    from,
    to,
    action: "click",
    label: `Clicked a deliberately long transition label for ${id}`,
    selector: "button",
    element_text: null,
    confidence: 1,
    collapsed_count: 1,
    via,
  };
}

describe("graph element derivation", () => {
  it("is deterministic and excludes edges with missing endpoints", () => {
    const nodesA = { second: state("second", 2), first: state("first", 1) };
    const nodesB = { first: state("first", 1), second: state("second", 2) };
    const edgesA = {
      dangling: edge("dangling", "first", "missing"),
      valid: edge("valid", "first", "second"),
    };
    const edgesB = {
      valid: edge("valid", "first", "second"),
      dangling: edge("dangling", "first", "missing"),
    };

    const topologyA = createGraphTopology(nodesA, edgesA);
    const topologyB = createGraphTopology(nodesB, edgesB);

    expect(topologyA.nodeIds).toEqual(["first", "second"]);
    expect(topologyA.edgeIds).toEqual(["valid"]);
    expect(topologyA.key).toBe(topologyB.key);
    expect(layoutTopology(topologyA)).toEqual(layoutTopology(topologyB));
  });

  it("does not change topology or positions for metadata-only updates", () => {
    const edges = { e1: edge("e1", "a", "b") };
    const before = createGraphTopology({ a: state("a", 0), b: state("b", 1) }, edges);
    const after = createGraphTopology(
      { a: state("a", 0, "Renamed state"), b: state("b", 1) },
      edges,
    );

    expect(after.key).toBe(before.key);
    expect(layoutTopology(after)).toEqual(layoutTopology(before));
  });

  it("invalidates layout when a structural edge arrives after its nodes", () => {
    const nodes = { a: state("a", 0), b: state("b", 1) };
    const before = createGraphTopology(nodes, {});
    const after = createGraphTopology(nodes, { e1: edge("e1", "a", "b") });

    expect(after.layoutKey).not.toBe(before.layoutKey);
    expect(after.layoutEdges).toHaveLength(1);
  });

  it("produces finite fixed-dimension layouts and readable inferred edges", () => {
    const edges = { e1: edge("e1", "a", "b", "inferred") };
    const topology = createGraphTopology({ a: state("a", 0), b: state("b", 1) }, edges);
    const layout = layoutTopology(topology);
    const positions = layout.nodePositions;
    const flowEdges = buildFlowEdges(topology, edges, "bundle:a>b");

    expect(NODE_WIDTH).toBe(210);
    expect(NODE_HEIGHT).toBe(78);
    expect(Number.isFinite(positions.a.x)).toBe(true);
    expect(Number.isFinite(positions.b.y)).toBe(true);
    expect(positions.b.x).toBeGreaterThan(positions.a.x);
    expect(flowEdges[0].animated).toBe(true);
    expect(String(flowEdges[0].label).endsWith("\u2026")).toBe(true);
  });

  it("places structural variants inside one deterministic family box", () => {
    const family = "https://example.com/game/:id/:param";
    const nodes = {
      first: state("first", 1),
      second: state("second", 2),
      other: state("other", 3),
    };
    nodes.first.exploration = { route_family: family };
    nodes.second.exploration = { route_family: family };
    const edges = {
      e1: edge("e1", "other", "first"),
      e2: edge("e2", "other", "second"),
    };

    const topology = createGraphTopology(nodes, edges);
    const layout = layoutTopology(configureFamilyExpansion(topology, new Set([topology.families[0].id])));

    expect(topology.families).toHaveLength(1);
    expect(topology.families[0].memberIds).toEqual(["first", "second"]);
    expect(topology.layoutEdges).toHaveLength(1);
    expect(layout.familyBoxes).toHaveLength(1);
    const box = layout.familyBoxes[0];
    for (const id of box.memberIds) {
      const position = layout.nodePositions[id];
      expect(position.x).toBeGreaterThan(box.position.x);
      expect(position.y).toBeGreaterThan(box.position.y);
      expect(position.x + NODE_WIDTH).toBeLessThanOrEqual(box.position.x + box.width);
      expect(position.y + NODE_HEIGHT).toBeLessThanOrEqual(box.position.y + box.height);
    }
    expect(layout.nodePositions.other.x).toBeLessThan(box.position.x);
  });

  it("retains exact family samples as evidence but shows one node per structural variant", () => {
    const family = "https://example.com/game/:param/:optional";
    const nodes = {
      first: state("first", 1),
      second: state("second", 2),
      third: state("third", 3),
    };
    for (const node of Object.values(nodes)) {
      node.exploration = {
        route_family: family,
        family_variant_key: "same-shell",
        family_representative_state_id: "first",
        family: {
          id: "games",
          label: "Games",
          kind: "items",
          pattern: family,
          label_source: "heuristic",
          confidence: 0.92,
          discovered_count: 127,
          checked_count: 3,
          represented_count: 1,
          sample_state_ids: ["first", "second", "third"],
          status: "confirmed",
        },
      };
    }

    const topology = createGraphTopology(nodes, {});

    expect(topology.nodeIds).toEqual(["first"]);
    expect(topology.families[0].memberIds).toEqual(["first"]);
    expect(topology.families[0].checkedCount).toBe(3);
    expect(topology.families[0].representedCount).toBe(1);
    expect(topology.ownerByNode.second).toBe(topology.families[0].id);
    expect(topology.ownerByNode.third).toBe(topology.families[0].id);
  });

  it("canonicalizes placeholder names by position without inventing optional slots", () => {
    const nodes = {
      withSlug: state("withSlug", 1),
      legacySlug: state("legacySlug", 2),
      noSlug: state("noSlug", 3),
    };
    nodes.withSlug.exploration = { route_family: "https://example.com/game/:id/:param" };
    nodes.legacySlug.exploration = { route_family: "https://example.com/game/:param/:param" };
    nodes.noSlug.exploration = { route_family: "https://example.com/game/:param" };

    const topology = createGraphTopology(nodes, {});

    expect(topology.families).toHaveLength(1);
    expect(topology.families[0]).toMatchObject({
      pattern: "https://example.com/game/:param/:param",
      memberIds: ["withSlug", "legacySlug"],
    });
    expect(topology.ownerByNode.noSlug).toBe("noSlug");
  });

  it("canonicalizes legacy path and query placeholders generically", () => {
    const nodes = {
      shortA: state("shortA", 1),
      shortB: state("shortB", 2),
      watchA: state("watchA", 3),
      watchB: state("watchB", 4),
    };
    nodes.shortA.exploration = { route_family: "https://www.youtube.com/shorts/:param" };
    nodes.shortB.exploration = { route_family: "https://www.youtube.com/shorts/:id" };
    nodes.watchA.exploration = { route_family: "https://www.youtube.com/watch?v=:param" };
    nodes.watchB.exploration = { route_family: "https://www.youtube.com/watch?v=:id" };

    const topology = createGraphTopology(nodes, {});

    expect(topology.families.map((family) => family.pattern).sort()).toEqual([
      "https://www.youtube.com/shorts/:param",
      "https://www.youtube.com/watch?v=:param",
    ]);
  });

  it("uses the backend family id as the authoritative grouping key", () => {
    const first = state("first", 1);
    const second = state("second", 2);
    first.exploration = {
      route_family: "https://example.com/entry/:param",
      family: {
        id: "authoritative",
        label: "Entries",
        kind: "items",
        pattern: "https://example.com/entry/:param",
        label_source: "heuristic",
        confidence: 0.9,
        discovered_count: 5,
        status: "confirmed",
      },
    };
    second.exploration = {
      route_family: "https://example.com/legacy/:id",
      family: {
        ...first.exploration.family!,
        pattern: "https://example.com/legacy/:id",
      },
    };

    const topology = createGraphTopology({ first, second }, {});
    expect(topology.families).toHaveLength(1);
    expect(topology.families[0]).toMatchObject({
      id: "family-authoritative",
      memberIds: ["first", "second"],
    });
  });

  it("does not create a box for one-member families or substates", () => {
    const lone = state("lone", 1);
    lone.exploration = { route_family: "https://example.com/users/:param" };
    const substate = state("sub", 2);
    substate.parent_state_id = "lone";
    substate.exploration = { route_family: "https://example.com/users/:param" };

    const topology = createGraphTopology({ lone, substate }, {});
    expect(topology.families).toEqual([]);
    expect(layoutTopology(topology).familyBoxes).toEqual([]);
  });

  it("highlights inbound and outbound edge bundles for a selected node", () => {
    const graphNodes = { a: state("a", 0), b: state("b", 1), c: state("c", 2) };
    const edges = {
      in: edge("in", "a", "b"),
      out: edge("out", "b", "c"),
      other: edge("other", "a", "c"),
    };
    const topology = createGraphTopology(graphNodes, edges);
    const focus = collectNodeEdgeFocus(topology, graphNodes, edges, "b");
    const flowEdges = buildFlowEdges(topology, edges, null, "b", graphNodes);

    expect(focus?.nodeIds).toEqual(new Set(["a", "b", "c"]));
    expect(focus?.bundleDirections.get("bundle:a>b")).toBe("inbound");
    expect(focus?.bundleDirections.get("bundle:b>c")).toBe("outbound");
    expect(focus?.bundleIds.has("bundle:a>c")).toBe(false);

    const inbound = flowEdges.find((item) => item.id === "bundle:a>b");
    const outbound = flowEdges.find((item) => item.id === "bundle:b>c");
    const unrelated = flowEdges.find((item) => item.id === "bundle:a>c");
    expect(inbound?.className).toContain("graph-edge--inbound");
    expect(inbound?.className).toContain("graph-edge--path");
    expect(outbound?.className).toBe("graph-edge--outbound");
    expect(inbound?.label).toBeTruthy();
    expect(outbound?.label).toBeTruthy();
    expect(unrelated?.className).toBe("graph-edge--dimmed");
    expect(inbound?.style).toMatchObject({ strokeWidth: 2.6, opacity: 1 });
  });

  it("treats child-target edges as inbound when the parent hub is selected", () => {
    const tab = state("tab", 2);
    tab.parent_state_id = "hub";
    tab.type = "tab";
    const graphNodes = {
      root: state("root", 0),
      hub: state("hub", 1),
      tab,
    };
    const edges = { rootTab: edge("rootTab", "root", "tab") };
    const topology = createGraphTopology(graphNodes, edges);
    const focus = collectNodeEdgeFocus(topology, graphNodes, edges, "hub");
    const flowEdges = buildFlowEdges(topology, edges, null, "hub", graphNodes);

    expect(focus?.bundleIds.has("bundle:root>tab")).toBe(true);
    expect(focus?.nodeIds).toEqual(new Set(["root", "hub", "tab"]));
    expect(flowEdges[0].className).toBe("graph-edge--connected");
    expect(flowEdges[0].style).toMatchObject({ stroke: "var(--edge-connected)" });
  });

  it("does not borrow another family member's transitions when one member is selected", () => {
    const family = "https://example.com/game/:id/:param";
    const graphNodes = {
      hub: state("hub", 0),
      first: state("first", 1, "Games Stats"),
      second: state("second", 2, "Avatar Items Stats"),
    };
    graphNodes.first.exploration = { route_family: family };
    graphNodes.second.exploration = { route_family: family };
    const edges = { hubSecond: edge("hubSecond", "hub", "second") };
    const topology = createGraphTopology(graphNodes, edges);
    const focus = collectNodeEdgeFocus(topology, graphNodes, edges, "first");
    const flowEdges = buildFlowEdges(topology, edges, null, "first", graphNodes);

    expect(focus?.bundleIds.size).toBe(0);
    expect(flowEdges[0].className).toBe("graph-edge--dimmed");
    expect(focus?.nodeIds.has("hub")).toBe(false);
  });

  it("keeps global navigation capabilities out of canvas topology", () => {
    const graphNodes = { a: state("a", 0), b: state("b", 1), c: state("c", 2) };
    const global = edge("global", "a", "b", "inferred");
    global.scope = "global_navigation";
    global.provenance = ["inferred"];
    const local = edge("local", "b", "c");
    const edges = { global, local };
    const topology = createGraphTopology(graphNodes, edges);

    expect(buildFlowEdges(topology, edges)).toHaveLength(1);
    const focused = buildFlowEdges(topology, edges, null, "a", graphNodes);
    expect(focused).toHaveLength(1);
    expect(focused.some((item) => item.id === "bundle:a>b")).toBe(false);
  });

  it("marks opposing directed transitions as a visible cycle", () => {
    const graphNodes = { a: state("a", 0), b: state("b", 1) };
    const edges = { forward: edge("forward", "a", "b"), back: edge("back", "b", "a") };
    edges.back.reversible = true;
    edges.back.transition_kind = "back";
    const topology = createGraphTopology(graphNodes, edges);
    const rendered = buildFlowEdges(topology, edges, null, "a", graphNodes);

    expect(rendered).toHaveLength(2);
    expect(rendered.every((item) => item.type === "default")).toBe(true);
    expect(rendered.every((item) => item.data?.cyclic === true)).toBe(true);
  });

  it("boxes one retained representative when a repeated cohort was discovered", () => {
    const representative = state("game", 1);
    representative.exploration = {
      route_family: "https://example.com/game/:id/:param",
      family: {
        id: "games",
        label: "Games",
        kind: "game",
        pattern: "https://example.com/game/:id/:param",
        label_source: "heuristic",
        confidence: 0.9,
        discovered_count: 10,
      },
    };
    expect(createGraphTopology({ game: representative }, {}).families).toHaveLength(1);
  });

  it("derives one-representative family boxes from source surface families", () => {
    const hub = state("hub", 0);
    const representative = state("game", 1);
    const pattern = "https://example.com/games/:param";
    hub.exploration = {
      surface_families: [{
        id: "games",
        label: "Games",
        kind: "game",
        pattern,
        label_source: "heuristic",
        confidence: 0.9,
        discovered_count: 12,
        represented_count: 1,
        skipped_count: 9,
        sample_labels: ["Game A", "Game B"],
      }],
    };
    representative.exploration = { route_family: pattern };

    const topology = createGraphTopology({ hub, game: representative }, {});

    expect(topology.families).toHaveLength(1);
    expect(topology.families[0]).toMatchObject({
      id: "family-games",
      label: "Games",
      discoveredCount: 12,
      representedCount: 1,
      skippedCount: 9,
      memberIds: ["game"],
    });
  });
});
