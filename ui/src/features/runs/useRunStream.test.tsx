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
  openRunStream: (
    runId: string,
    handlers: Record<string, (...args: never[]) => void>,
    afterSequence: number,
  ) => {
    mocks.openRunStream(runId, handlers, afterSequence);
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
  it("leaves a healthy live stream alone on foreground", async () => {
    mocks.getGraph.mockResolvedValue(graph([state("a", 0)]));

    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(mocks.openRunStream).toHaveBeenCalledTimes(1);

    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(mocks.getGraph).toHaveBeenCalledTimes(1);
    expect(mocks.openRunStream).toHaveBeenCalledTimes(1);
  });

  it("starts the live stream after the hydrated snapshot watermark", async () => {
    const current = graph([state("a", 0)]);
    current.sync = {
      schema_version: 4,
      snapshot_sequence: 7,
      authoritative: false,
      latest_state_id: "a",
    };
    mocks.getGraph.mockResolvedValue(current);

    render(<Probe />);
    await waitFor(() => expect(mocks.openRunStream).toHaveBeenCalledTimes(1));

    expect(mocks.openRunStream).toHaveBeenCalledWith("run-1", expect.any(Object), 7);
  });

  it("hydrates and immediately reattaches a hard-closed live stream", async () => {
    mocks.getGraph.mockResolvedValue(graph([state("a", 0)]));
    render(<Probe />);
    await waitFor(() => expect(mocks.openRunStream).toHaveBeenCalledTimes(1));

    mocks.handlers[0].onClosed?.();
    await waitFor(() => expect(mocks.getGraph).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mocks.openRunStream).toHaveBeenCalledTimes(2));
  });

  it("does not attach or rehydrate an authoritative persisted snapshot", async () => {
    const persisted = graph([state("a", 0)], "running");
    persisted.sync = {
      schema_version: 2,
      snapshot_sequence: null,
      authoritative: true,
      latest_state_id: "a",
    };
    mocks.getGraph.mockResolvedValue(persisted);

    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(mocks.openRunStream).not.toHaveBeenCalled();

    window.dispatchEvent(new Event("focus"));
    expect(mocks.getGraph).toHaveBeenCalledTimes(1);
  });
});
