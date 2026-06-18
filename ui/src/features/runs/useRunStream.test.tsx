import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphDocument, GraphState } from "../../types/graph";

const mocks = vi.hoisted(() => ({
  getGraph: vi.fn(),
  openRunStream: vi.fn(),
  handlers: [] as Array<Record<string, (...args: never[]) => void>>,
}));

vi.mock("../../api/client", () => ({
  getGraph: mocks.getGraph,
}));

vi.mock("../../api/eventStream", () => ({
  openRunStream: (runId: string, handlers: Record<string, (...args: never[]) => void>) => {
    mocks.openRunStream(runId, handlers);
    mocks.handlers.push(handlers);
    return { close: vi.fn() };
  },
}));

import { useRunStream } from "./useRunStream";

function state(id: string, index: number): GraphState {
  return {
    id,
    index,
    type: "page",
    url: `https://example.com/${id}`,
    url_normalized: `https://example.com/${id}`,
    title: id,
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

function graph(states: GraphState[], status = "running"): GraphDocument {
  return {
    run: {
      id: "run-1",
      url: "https://example.com",
      status,
      stats: null,
      started_at: null,
      finished_at: null,
    },
    states,
    edges: [],
  };
}

function Probe() {
  const { run } = useRunStream("run-1");
  return <span data-testid="count">{run.order.length}</span>;
}

beforeEach(() => {
  mocks.getGraph.mockReset();
  mocks.openRunStream.mockReset();
  mocks.handlers.length = 0;
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(cleanup);

describe("useRunStream foreground reconciliation", () => {
  it("hydrates on foreground without opening a second live stream", async () => {
    mocks.getGraph
      .mockResolvedValueOnce(graph([state("a", 0)]))
      .mockResolvedValueOnce(graph([state("a", 0), state("b", 1)]));

    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(mocks.openRunStream).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
    expect(mocks.getGraph).toHaveBeenCalledTimes(2);
    expect(mocks.openRunStream).toHaveBeenCalledTimes(1);
  });

  it("reattaches a hard-closed stream only after foreground reconciliation", async () => {
    mocks.getGraph.mockResolvedValue(graph([state("a", 0)]));
    render(<Probe />);
    await waitFor(() => expect(mocks.openRunStream).toHaveBeenCalledTimes(1));

    mocks.handlers[0].onClosed?.();
    await waitFor(() => expect(mocks.getGraph).toHaveBeenCalledTimes(2));
    expect(mocks.openRunStream).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(mocks.openRunStream).toHaveBeenCalledTimes(2));
  });
});
