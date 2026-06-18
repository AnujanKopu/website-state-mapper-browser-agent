import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useNodesInitialized,
  useReactFlow,
  type Edge,
} from "@xyflow/react";

import type { GraphEdge, GraphState } from "../../types/graph";
import { buildFlowEdges, createGraphTopology, layoutTopology } from "./graphElements";
import { StateNodeView, type StateFlowNode } from "./StateNode";

const nodeTypes = { state: StateNodeView };
const LIVE_LAYOUT_DEBOUNCE_MS = 150;
const FIT_PADDING = 0.18;

interface GraphViewProps {
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  selectedId: string | null;
  currentId: string | null;
  isLive: boolean;
  onSelect: (id: string) => void;
}

interface GraphCanvasProps extends GraphViewProps {
  displayTopology: ReturnType<typeof createGraphTopology>;
}

function GraphCanvas({
  nodes,
  edges,
  selectedId,
  currentId,
  isLive,
  onSelect,
  displayTopology,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pendingFitRef = useRef(true);
  const fitFrameRef = useRef<number | null>(null);
  const explicitFitDurationRef = useRef<number | null>(null);
  const [following, setFollowing] = useState(true);
  const nodesInitialized = useNodesInitialized();
  const { fitView } = useReactFlow<StateFlowNode, Edge>();

  const positions = useMemo(
    () => layoutTopology(displayTopology),
    [displayTopology],
  );

  const rfNodes = useMemo<StateFlowNode[]>(
    () =>
      displayTopology.nodeIds.flatMap((id) => {
        const state = nodes[id];
        const position = positions[id];
        if (!state || !position) return [];
        return [{
          id,
          type: "state" as const,
          position,
          selected: id === selectedId,
          data: { state, current: id === currentId },
        }];
      }),
    [currentId, displayTopology.nodeIds, nodes, positions, selectedId],
  );

  const rfEdges = useMemo(
    () => buildFlowEdges(displayTopology, edges),
    [displayTopology, edges],
  );

  const fitGraph = useCallback((duration: number) => {
    const container = containerRef.current;
    if (
      rfNodes.length === 0
      || !container
      || container.clientWidth === 0
      || container.clientHeight === 0
      || document.hidden
    ) {
      pendingFitRef.current = true;
      return;
    }
    pendingFitRef.current = false;
    void fitView({ duration, padding: FIT_PADDING });
  }, [fitView, rfNodes.length]);

  const scheduleFit = useCallback((duration = 0) => {
    if (fitFrameRef.current !== null) window.cancelAnimationFrame(fitFrameRef.current);
    fitFrameRef.current = window.requestAnimationFrame(() => {
      fitFrameRef.current = null;
      fitGraph(duration);
    });
  }, [fitGraph]);

  useEffect(() => {
    pendingFitRef.current = true;
    if (following && nodesInitialized && rfNodes.length > 0) {
      const duration = explicitFitDurationRef.current ?? 0;
      explicitFitDurationRef.current = null;
      scheduleFit(duration);
    }
  }, [displayTopology.key, following, nodesInitialized, rfNodes.length, scheduleFit]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      pendingFitRef.current = true;
      if (following && nodesInitialized && rfNodes.length > 0) scheduleFit(0);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [following, nodesInitialized, rfNodes.length, scheduleFit]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.hidden) {
        pendingFitRef.current = true;
      } else if (following && nodesInitialized && pendingFitRef.current) {
        scheduleFit(0);
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [following, nodesInitialized, scheduleFit]);

  useEffect(() => () => {
    if (fitFrameRef.current !== null) window.cancelAnimationFrame(fitFrameRef.current);
  }, []);

  const followAndFit = () => {
    pendingFitRef.current = true;
    if (following) {
      scheduleFit(250);
    } else {
      explicitFitDurationRef.current = 250;
      setFollowing(true);
    }
  };

  return (
    <div ref={containerRef} className="graph-view">
      <ReactFlow<StateFlowNode, Edge>
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        colorMode="dark"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onlyRenderVisibleElements
        onMoveStart={(event) => {
          if (event) setFollowing(false);
        }}
        onNodeClick={(_, node) => onSelect(node.id)}
        minZoom={0.1}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1f2228" gap={22} />
        <Controls showFitView={false} showInteractive={false} />
        <Panel position="top-right">
          <button
            type="button"
            className={`graph-follow${following && isLive ? " graph-follow--active" : ""}`}
            onClick={followAndFit}
            disabled={rfNodes.length === 0}
            aria-pressed={isLive ? following : undefined}
          >
            {isLive && following ? "Following live" : isLive ? "Follow live" : "Recenter"}
          </button>
        </Panel>
      </ReactFlow>

      {rfNodes.length === 0 && (
        <div className="graph-empty graph-empty--overlay" role="status">
          Waiting for the first state to be discovered{"\u2026"}
        </div>
      )}
    </div>
  );
}

export function GraphView(props: GraphViewProps) {
  const topology = useMemo(
    () => createGraphTopology(props.nodes, props.edges),
    [props.nodes, props.edges],
  );
  const [displayTopology, setDisplayTopology] = useState(topology);

  useEffect(() => {
    if (!props.isLive || topology.nodeIds.length === 0) {
      setDisplayTopology(topology);
      return;
    }
    const timer = window.setTimeout(() => setDisplayTopology(topology), LIVE_LAYOUT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [props.isLive, topology.key]);

  return (
    <ReactFlowProvider>
      <GraphCanvas {...props} displayTopology={displayTopology} />
    </ReactFlowProvider>
  );
}
