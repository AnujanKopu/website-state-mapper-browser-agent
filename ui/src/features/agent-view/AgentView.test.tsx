import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GraphState } from "../../types/graph";
import { initialRunState, type RunState } from "../runs/runState";
import { AgentView } from "./AgentView";

function state(id: string, title: string): GraphState {
  return {
    id,
    type: "page",
    url: `https://example.com/${id}`,
    url_normalized: `https://example.com/${id}`,
    title,
    label: null,
    summary: null,
    fingerprint: id,
    depth: 0,
    screenshot: `runs/demo/screenshots/${id}.png`,
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
  };
}

describe("AgentView inspection", () => {
  it("shows the selected graph state instead of the latest live state", () => {
    Element.prototype.scrollIntoView = vi.fn();
    const live = state("live", "Latest live state");
    const selected = state("selected", "Selected graph state");
    const run: RunState = {
      ...initialRunState,
      runStatus: "running",
      connection: "live",
      viewportStateId: live.id,
      nodes: {
        [live.id]: live,
        [selected.id]: selected,
      },
    };

    render(
      <AgentView
        run={run}
        inspectedState={selected}
        onExpandScreenshot={vi.fn()}
      />,
    );

    expect(screen.getByText("Selected state")).toBeInTheDocument();
    expect(screen.getByText("Selected graph state")).toBeInTheDocument();
    expect(screen.getByTitle("https://example.com/selected")).toBeInTheDocument();
    expect(screen.queryByText("Latest live state")).not.toBeInTheDocument();
  });
});
