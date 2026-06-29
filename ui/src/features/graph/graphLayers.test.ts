import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphState, SurfaceItem } from "../../types/graph";
import { buildInteractionProjection, projectPageGraph } from "./graphLayers";

function state(id: string, index: number, parent_state_id: string | null = null): GraphState {
  return {
    id,
    index,
    type: parent_state_id ? "modal" : "page",
    url: `https://example.com/${id}`,
    url_normalized: `https://example.com/${id}`,
    title: id,
    label: null,
    summary: null,
    fingerprint: id,
    depth: index,
    parent_state_id,
    screenshot: "",
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
  };
}

function edge(id: string, from: string, to: string, surfaceItemId?: string): GraphEdge {
  return {
    id,
    from,
    to,
    action: "click",
    label: "Open",
    selector: "button",
    element_text: null,
    confidence: 1,
    collapsed_count: 1,
    surface_item_id: surfaceItemId,
  };
}

function item(id: string, overrides: Partial<SurfaceItem> = {}): SurfaceItem {
  return {
    item_id: id,
    label: id,
    kind: "button",
    region: "main",
    fold: 0,
    group_id: null,
    status: "inventory_only",
    interaction_scope: "local_ui",
    execution_policy: "inventory_only",
    ...overrides,
  };
}

describe("graph layer projections", () => {
  it("keeps substates off the page topology and rolls cross-page edges to their owner", () => {
    const home = state("home", 0);
    const modal = state("modal", 1, "home");
    const docs = state("docs", 2);
    const projection = projectPageGraph(
      { home, modal, docs },
      {
        local: edge("local", "home", "modal"),
        onward: edge("onward", "modal", "docs"),
      },
    );

    expect(Object.keys(projection.nodes)).toEqual(["home", "docs"]);
    expect(Object.values(projection.edges)).toHaveLength(1);
    expect(Object.values(projection.edges)[0]).toMatchObject({ from: "home", to: "docs" });
  });

  it("derives local capability nodes without duplicating page navigation links", () => {
    const home = state("home", 0);
    const modal = state("modal", 1, "home");
    home.surface_items = [
      item("open-modal"),
      item("docs-link", {
        kind: "link",
        href: "https://example.com/docs",
        status: "explored",
        interaction_scope: "page_navigation",
        execution_policy: "navigate",
      }),
    ];
    modal.surface_items = [
      item("open-modal"),
      item("nested-download", { status: "blocked" }),
    ];
    const projection = buildInteractionProjection(
      { home, modal },
      { open: edge("open", "home", "modal", "open-modal") },
      "home",
    );

    expect(projection.stateIds).toEqual(["home", "modal"]);
    expect(Object.values(projection.capabilities)).toHaveLength(1);
    expect(Object.values(projection.capabilities)[0].label).toBe("open-modal");
    expect(projection.edges.some((item) => item.source.startsWith("interaction:") && item.target === "modal"))
      .toBe(true);
  });

  it("coalesces repeated nested transitions by owner, control family, and target", () => {
    const home = state("home", 0);
    const menu = state("menu", 1, "home");
    const docs = state("docs", 2);
    const first = edge("first", "home", "docs", "docs-control");
    const second = edge("second", "menu", "docs", "docs-control");

    const projection = projectPageGraph({ home, menu, docs }, { first, second });

    expect(Object.values(projection.edges)).toHaveLength(1);
    expect(Object.values(projection.edges)[0].collapsed_count).toBe(2);
  });

  it("groups controls by component keys without using route-family groups", () => {
    const home = state("home", 0);
    home.surface_items = [
      item("search-input", { component_key: "search", component_label: "Search" }),
      item("search-icon", { component_key: "search", component_label: "Search" }),
      item("download", {
        component_key: "download",
        component_label: "Download",
        status: "blocked",
      }),
    ];
    const projection = buildInteractionProjection({ home }, {}, "home");
    const capabilities = Object.values(projection.capabilities);

    expect(capabilities).toHaveLength(2);
    expect(capabilities.find((capability) => capability.label === "Search")?.count).toBe(2);
    expect(capabilities.find((capability) => capability.label === "Download")?.status).toBe("blocked");
  });
});
