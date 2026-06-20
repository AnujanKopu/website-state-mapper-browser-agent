import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphEdge, GraphState } from "../../types/graph";
import { resetResizeObservers, triggerResizeObservers } from "../../test/setup";

const flowMock = vi.hoisted(() => ({
  fitView: vi.fn(),
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
  useReactFlow: () => ({ fitView: flowMock.fitView }),
}));

import { GraphView } from "./GraphView";

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

function graphProps(nodes: Record<string, GraphState>, edges: Record<string, GraphEdge> = {}) {
  return {
    nodes,
    edges,
    selectedId: null,
    currentId: null,
    isLive: true,
    onSelect: vi.fn(),
  };
}

function renderedNodes(): unknown[] {
  return (flowMock.latestProps?.nodes as unknown[]) ?? [];
}

beforeEach(() => {
  vi.useFakeTimers();
  flowMock.fitView.mockReset();
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
    act(() => vi.advanceTimersByTime(299));
    expect(renderedNodes()).toHaveLength(0);

    act(() => vi.advanceTimersByTime(1));
    expect(renderedNodes()).toHaveLength(1);
    expect(flowMock.latestProps?.onlyRenderVisibleElements).toBe(true);
    act(() => vi.runAllTimers());
    expect(flowMock.fitView).toHaveBeenCalledTimes(1);
    expect(flowMock.fitView).toHaveBeenLastCalledWith({ duration: 0, padding: 0.18 });
  });

  it("stops following after manual movement and resumes from the control", () => {
    const view = render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    const initialFits = flowMock.fitView.mock.calls.length;

    act(() => {
      (flowMock.latestProps?.onMoveStart as (event: object) => void)({ type: "wheel" });
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
    expect(flowMock.fitView).toHaveBeenCalledTimes(initialFits);

    act(() => {
      window.dispatchEvent(new Event("focus"));
      vi.runAllTimers();
    });
    expect(flowMock.fitView).toHaveBeenCalledTimes(initialFits);

    fireEvent.click(screen.getByRole("button", { name: "Follow live" }));
    act(() => vi.runAllTimers());
    expect(flowMock.fitView).toHaveBeenLastCalledWith({ duration: 250, padding: 0.18 });
    expect(screen.getByRole("button", { name: "Following live" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("defers fitting at zero size and retries after a resize", () => {
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 0,
    });
    render(<GraphView {...graphProps({ a: state("a", 0) })} isLive={false} />);
    act(() => vi.runOnlyPendingTimers());
    expect(flowMock.fitView).not.toHaveBeenCalled();

    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => 1000,
    });
    act(() => {
      triggerResizeObservers();
      vi.runAllTimers();
    });
    expect(flowMock.fitView).toHaveBeenCalledWith({ duration: 0, padding: 0.18 });
  });

  it("retries a pending fit when a backgrounded tab becomes visible", () => {
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    render(<GraphView {...graphProps({ a: state("a", 0) })} />);
    act(() => vi.runAllTimers());
    expect(flowMock.fitView).not.toHaveBeenCalled();

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
      vi.runAllTimers();
    });
    expect(flowMock.fitView).toHaveBeenCalledWith({ duration: 0, padding: 0.18 });
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
