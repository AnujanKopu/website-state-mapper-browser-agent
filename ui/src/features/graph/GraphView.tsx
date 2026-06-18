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
import { FamilyGroupView, type FamilyFlowNode } from "./FamilyGroup";
import { StateNodeView, type StateFlowNode } from "./StateNode";

type GraphFlowNode = StateFlowNode | FamilyFlowNode;

const nodeTypes = { state: StateNodeView, family: FamilyGroupView };
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
  const { fitView } = useReactFlow<GraphFlowNode, Edge>();

  const layout = useMemo(
    () => layoutTopology(displayTopology),
    [displayTopology],
  );

  const rfNodes = useMemo<GraphFlowNode[]>(
    () => [
      ...layout.familyBoxes.map((family): FamilyFlowNode => ({
        id: family.id,
        type: "family",
        position: family.position,
        data: { pattern: family.pattern, memberCount: family.memberIds.length },
        selectable: false,
        draggable: false,
        connectable: false,
        focusable: false,
        zIndex: 0,
        style: {
          width: family.width,
          height: family.height,
          pointerEvents: "none",
        },
      })),
      ...displayTopology.nodeIds.flatMap((id): StateFlowNode[] => {
        const state = nodes[id];
        const position = layout.nodePositions[id];
        if (!state || !position) return [];
        return [{
          id,
          type: "state" as const,
          position,
          selected: id === selectedId,
          zIndex: 1,
          data: { state, current: id === currentId },
        }];
      }),
    ],
    [currentId, displayTopology.nodeIds, layout, nodes, selectedId],
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
      fitFrameRef.current = window.requestAnimationFrame(() => {
        fitFrameRef.current = null;
        fitGraph(duration);
      });
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
    const refitOnForeground = () => {
      if (document.hidden) {
        pendingFitRef.current = true;
      } else if (following && nodesInitialized && rfNodes.length > 0) {
        pendingFitRef.current = true;
        scheduleFit(0);
      }
    };
    document.addEventListener("visibilitychange", refitOnForeground);
    window.addEventListener("focus", refitOnForeground);
    window.addEventListener("pageshow", refitOnForeground);
    return () => {
      document.removeEventListener("visibilitychange", refitOnForeground);
      window.removeEventListener("focus", refitOnForeground);
      window.removeEventListener("pageshow", refitOnForeground);
    };
  }, [following, nodesInitialized, rfNodes.length, scheduleFit]);

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
      <ReactFlow<GraphFlowNode, Edge>
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        colorMode="dark"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onMoveStart={(event) => {
          if (event) setFollowing(false);
        }}
        onNodeClick={(_, node) => {
          if (node.type === "state") onSelect(node.id);
        }}
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
