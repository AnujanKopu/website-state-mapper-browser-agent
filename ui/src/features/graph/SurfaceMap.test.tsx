import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GraphEdge, GraphState } from "../../types/graph";
import { SurfaceMap } from "./SurfaceMap";

function pageState(overrides: Partial<GraphState> = {}): GraphState {
  return {
    id: "page",
    index: 0,
    type: "page",
    url: "https://example.com",
    url_normalized: "https://example.com",
    title: "Example",
    label: null,
    summary: null,
    fingerprint: "page",
    depth: 0,
    screenshot: "screens/page.png",
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
    evidence: { page: { document: { width: 1000, height: 2000 } } },
    ...overrides,
  };
}

describe("SurfaceMap", () => {
  it("places captured controls over the full-page screenshot and connects outcomes", () => {
    const page = pageState({
      surface_items: [{
        item_id: "search",
        label: "Search",
        kind: "button",
        region: "header",
        fold: 0,
        group_id: null,
        status: "explored",
        interaction_scope: "local_ui",
        execution_policy: "probe_local",
        page_box: { x: 100, y: 400, width: 200, height: 80 },
      }],
    });
    const result = pageState({ id: "result", index: 1, title: "Search results", parent_state_id: "page", type: "page_variant" });
    const edge: GraphEdge = {
      id: "search-result",
      from: "page",
      to: "result",
      action: "click",
      label: "Search",
      selector: "button",
      element_text: "Search",
      confidence: 1,
      collapsed_count: 1,
      surface_item_id: "search",
    };

    render(
      <SurfaceMap
        nodes={{ page, result }}
        edges={{ [edge.id]: edge }}
        pageId="page"
        selectedStateId="page"
        onBack={vi.fn()}
        onSelectState={vi.fn()}
      />,
    );

    const hotspot = screen.getByRole("button", { name: "Search, explored" });
    expect(hotspot).toHaveStyle({ left: "10%", top: "20%", width: "20%", height: "4%" });
    fireEvent.click(hotspot);
    expect(screen.getByRole("heading", { name: "Search" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Search results/ })).toBeInTheDocument();
  });

  it("keeps controls without live geometry in a clearly labelled fallback inventory", () => {
    const page = pageState({
      surface_items: [{
        item_id: "help",
        label: "Help",
        kind: "link",
        region: "footer",
        fold: 2,
        group_id: null,
        status: "inventory_only",
        interaction_scope: "page_navigation",
        execution_policy: "inventory_only",
      }],
    });

    render(
      <SurfaceMap
        nodes={{ page }}
        edges={{}}
        pageId="page"
        selectedStateId="page"
        onBack={vi.fn()}
        onSelectState={vi.fn()}
      />,
    );

    expect(screen.getByText("Region-only controls (1)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Help/ })).toHaveTextContent("footer · fold 3");
  });
});
