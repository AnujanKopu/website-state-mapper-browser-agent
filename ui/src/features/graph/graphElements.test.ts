import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphState } from "../../types/graph";
import { buildFlowEdges, createGraphTopology, layoutTopology } from "./graphElements";
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

  it("produces finite fixed-dimension layouts and readable inferred edges", () => {
    const edges = { e1: edge("e1", "a", "b", "inferred") };
    const topology = createGraphTopology({ a: state("a", 0), b: state("b", 1) }, edges);
    const positions = layoutTopology(topology);
    const flowEdges = buildFlowEdges(topology, edges);

    expect(NODE_WIDTH).toBe(210);
    expect(NODE_HEIGHT).toBe(78);
    expect(Number.isFinite(positions.a.x)).toBe(true);
    expect(Number.isFinite(positions.b.y)).toBe(true);
    expect(positions.b.x).toBeGreaterThan(positions.a.x);
    expect(flowEdges[0].animated).toBe(true);
    expect(String(flowEdges[0].label).endsWith("\u2026")).toBe(true);
  });
});
