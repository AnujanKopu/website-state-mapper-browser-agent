import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphEdge, GraphState } from "../../types/graph";
import { resetResizeObservers, triggerResizeObservers } from "../../test/setup";

const flowMock = vi.hoisted(() => ({
  fitBounds: vi.fn(),
  fitView: vi.fn(),
  getZoom: vi.fn(() => 0.75),
  setCenter: vi.fn(),
  latestProps: null as Record<string, unknown> | null,
  nodesInitialized: true,
}));

vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Left: "left", Right: "right" },
  Handle: () => null,
  Background: () => null,
  Controls: () => null,
  Panel: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  ReactFlowProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ReactFlow: (props: Record<string, unknown> & { children?: React.ReactNode }) => {
    flowMock.latestProps = props;
    return <div data-testid="react-flow">{props.children}</div>;
  },
  useNodesInitialized: () => flowMock.nodesInitialized,
  useReactFlow: () => ({
    fitBounds: flowMock.fitBounds,
    fitView: flowMock.fitView,
    getZoom: flowMock.getZoom,
    setCenter: flowMock.setCenter,
  }),
}));

import { GraphView } from "./GraphView";

type GraphViewProps = React.ComponentProps<typeof GraphView>;

function state(id: string, index: number, overrides: Partial<GraphState> = {}): GraphState {
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
    ...overrides,
  };
}

function edge(id: string, from: string, to: string): GraphEdge {
  return {
    id,
    from,
    to,
    action: "click",
    label: "Clicked",
    selector: "button",
    element_text: null,
    confidence: 1,
    collapsed_count: 1,
    via: "performed",
  };
}

function graphProps(
  nodes: Record<string, GraphState>,
  edges: Record<string, GraphEdge> = {},
  overrides: Partial<GraphViewProps> = {},
): GraphViewProps {
  return {
    nodes,
    edges,
    selectedId: null,
    currentId: null,
    isLive: true,
    onSelect: vi.fn(),
    ...overrides,
  };
}

function renderedNodes(): unknown[] {
  return (flowMock.latestProps?.nodes as unknown[]) ?? [];
}

beforeEach(() => {
  vi.useFakeTimers();
  flowMock.fitBounds.mockReset();
  flowMock.fitView.mockReset();
  flowMock.getZoom.mockClear();
  flowMock.setCenter.mockReset();
  flowMock.latestProps = null;
  flowMock.nodesInitialized = true;
  resetResizeObservers();
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => 1000,
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get: () => 700,
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("GraphView viewport lifecycle", () => {
  it("keeps React Flow mounted and batches the first live topology", () => {
    const view = render(<GraphView {...graphProps({})} />);

    expect(screen.getByTestId("react-flow")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for the first state");

    view.rerender(<GraphView {...graphProps({ a: state("a", 0) })} />);
    expect(renderedNodes()).toHaveLength(0);

    act(() => vi.runAllTimers());
    expect(renderedNodes()).toHaveLength(1);
    expect(renderedNodes()[0]).toMatchObject({
      width: 210,
      height: 78,
      measured: { width: 210, height: 78 },
    });
    expect(flowMock.latestProps?.onlyRenderVisibleElements).toBeUndefined();
    expect(flowMock.latestProps?.nodesDraggable).toBe(false);
    expect(flowMock.latestProps?.panOnDrag).toBe(true);
    act(() => vi.runAllTimers());
    expect(flowMock.fitBounds).toHaveBeenCalledTimes(1);
    expect(flowMock.fitBounds).toHaveBeenLastCalledWith(
      expect.objectContaining({ width: expect.any(Number), height: expect.any(Number) }),
      { duration: 0, padding: 0.18 },
    );
  });

  it("queues live topology changes until mouse panning ends", () => {
    const view = render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    expect(renderedNodes()).toHaveLength(1);

    act(() => {
      (flowMock.latestProps?.onMoveStart as (event: object) => void)({ type: "pointer" });
    });
    view.rerender(
      <GraphView {...graphProps({ a: state("a", 0), b: state("b", 1) })} />,
    );
    act(() => vi.runAllTimers());
    expect(renderedNodes()).toHaveLength(1);

    act(() => {
      (flowMock.latestProps?.onMoveEnd as () => void)();
    });
    act(() => {
      vi.runAllTimers();
    });
    expect(renderedNodes()).toHaveLength(2);
  });

  it("does not freeze topology for programmatic viewport movement", () => {
    const view = render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());

    act(() => {
      (flowMock.latestProps?.onMoveStart as (event: null) => void)(null);
    });
    view.rerender(
      <GraphView {...graphProps({ a: state("a", 0), b: state("b", 1) })} />,
    );
    act(() => vi.runAllTimers());

    expect(renderedNodes()).toHaveLength(2);
  });

  it("flushes topology when a manual interaction loses window focus", () => {
    const view = render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    act(() => {
      (flowMock.latestProps?.onMoveStart as (event: object) => void)({ type: "pointer" });
    });
    view.rerender(
      <GraphView {...graphProps({ a: state("a", 0), b: state("b", 1) })} />,
    );
    act(() => vi.runAllTimers());
    expect(renderedNodes()).toHaveLength(1);

    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    act(() => {
      vi.runAllTimers();
    });
    expect(renderedNodes()).toHaveLength(2);
  });

  it("follows the current node while live", () => {
    const view = render(
      <GraphView
        {...graphProps(
          { a: state("a", 0), b: state("b", 1) },
          { ab: edge("ab", "a", "b") },
          { currentId: "a" },
        )}
      />,
    );
    act(() => vi.runAllTimers());
    flowMock.fitBounds.mockClear();

    view.rerender(
      <GraphView
        {...graphProps(
          { a: state("a", 0), b: state("b", 1) },
          { ab: edge("ab", "a", "b") },
          { currentId: "b" },
        )}
      />,
    );
    act(() => vi.runAllTimers());
    expect(flowMock.setCenter).toHaveBeenCalled();
    expect(flowMock.setCenter).toHaveBeenLastCalledWith(
      expect.any(Number),
      expect.any(Number),
      { duration: 0, zoom: 0.75 },
    );
  });

  it("stops following after manual movement and resumes from the control", () => {
    const view = render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    const initialFits = flowMock.fitBounds.mock.calls.length;

    act(() => {
      (flowMock.latestProps?.onMoveStart as (event: object) => void)({ type: "wheel" });
      (flowMock.latestProps?.onMoveEnd as () => void)();
    });
    view.rerender(
      <GraphView
        {...graphProps(
          { a: state("a", 0), b: state("b", 1) },
          { ab: edge("ab", "a", "b") },
        )}
      />,
    );
    act(() => vi.runAllTimers());
    expect(flowMock.fitBounds).toHaveBeenCalledTimes(initialFits);

    act(() => {
      window.dispatchEvent(new Event("focus"));
      vi.runAllTimers();
    });
    expect(flowMock.fitBounds).toHaveBeenCalledTimes(initialFits);

    fireEvent.click(screen.getByRole("button", { name: "Follow live" }));
    act(() => vi.runAllTimers());
    expect(flowMock.fitBounds).toHaveBeenLastCalledWith(
      expect.objectContaining({ width: expect.any(Number), height: expect.any(Number) }),
      { duration: 250, padding: 0.18 },
    );
    expect(screen.getByRole("button", { name: "Following live" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("clears node selection when the pane is clicked or Escape is pressed", () => {
    const onSelect = vi.fn<(id: string | null) => void>();
    const view = render(
      <GraphView
        {...graphProps({ a: state("a", 0) }, {}, { selectedId: "a", onSelect })}
      />,
    );
    act(() => vi.runAllTimers());

    act(() => {
      (flowMock.latestProps?.onPaneClick as () => void)();
    });
    expect(onSelect).toHaveBeenLastCalledWith(null);

    view.rerender(
      <GraphView
        {...graphProps({ a: state("a", 0) }, {}, { selectedId: "a", onSelect })}
      />,
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("shows only Fit graph after the run finishes", () => {
    render(<GraphView {...graphProps({ a: state("a", 0) })} isLive={false} />);
    expect(screen.getByRole("button", { name: "Fit graph" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Follow live" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Following live" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recenter" })).not.toBeInTheDocument();
  });

  it("fits the whole graph from the control", () => {
    render(<GraphView {...graphProps({ a: state("a", 0), b: state("b", 1) }, {}, { currentId: "b" })} />);
    act(() => vi.runAllTimers());
    flowMock.fitBounds.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Fit graph" }));
    act(() => vi.runAllTimers());
    expect(flowMock.fitBounds).toHaveBeenLastCalledWith(
      expect.objectContaining({ width: expect.any(Number), height: expect.any(Number) }),
      { duration: 250, padding: 0.18 },
    );
    expect(screen.getByRole("button", { name: "Follow live" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("defers fitting at zero size and retries after a resize", () => {
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 0,
    });
    render(<GraphView {...graphProps({ a: state("a", 0) })} isLive={false} />);
    act(() => vi.runOnlyPendingTimers());
    expect(flowMock.fitBounds).not.toHaveBeenCalled();

    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 1000,
    });
    act(() => {
      triggerResizeObservers();
      vi.runAllTimers();
    });
    expect(flowMock.fitBounds).toHaveBeenCalledWith(
      expect.objectContaining({ width: expect.any(Number), height: expect.any(Number) }),
      { duration: 0, padding: 0.18 },
    );
  });

  it("retries a pending fit when a backgrounded tab becomes visible", () => {
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    expect(flowMock.fitBounds).not.toHaveBeenCalled();

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
      vi.runAllTimers();
    });
    expect(flowMock.fitBounds).toHaveBeenCalledWith(
      expect.objectContaining({ width: expect.any(Number), height: expect.any(Number) }),
      { duration: 0, padding: 0.18 },
    );
  });

  it("renders an interactive family container behind structural variants", () => {
    const routeFamily = "https://example.com/game/:id/:param";
    render(
      <GraphView
        {...graphProps({
          a: state("a", 0, { exploration: { route_family: routeFamily } }),
          b: state("b", 1, { exploration: { route_family: routeFamily } }),
        })}
      />,
    );
    act(() => vi.runAllTimers());

    const nodes = renderedNodes() as Array<Record<string, unknown>>;
    expect(nodes).toHaveLength(3);
    const family = nodes.find((node) => node.type === "family");
    expect(family).toMatchObject({
      selectable: true,
      draggable: false,
      connectable: false,
      zIndex: 0,
    });
    expect(nodes.filter((node) => node.type === "state")).toHaveLength(2);
  });
});
