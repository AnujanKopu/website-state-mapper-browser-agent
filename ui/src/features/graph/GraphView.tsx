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
import {
  buildFlowEdges,
  collectNodeEdgeFocus,
  createGraphTopology,
  layoutTopology,
} from "./graphElements";
import { FamilyGroupView, type FamilyFlowNode } from "./FamilyGroup";
import {
  buildInteractionProjection,
  INTERACTION_NODE_HEIGHT,
  INTERACTION_NODE_WIDTH,
  pageAncestorId,
  projectPageGraph,
  type PageProjection,
} from "./graphLayers";
import { InteractionNodeView, type InteractionFlowNode } from "./InteractionNode";
import { NODE_HEIGHT, NODE_WIDTH } from "./layout";
import { StateNodeView, type StateFlowNode } from "./StateNode";

type GraphFlowNode = StateFlowNode | FamilyFlowNode | InteractionFlowNode;

const nodeTypes = {
  state: StateNodeView,
  family: FamilyGroupView,
  interaction: InteractionNodeView,
};
const FIT_PADDING = 0.18;

function nodeBounds(nodes: GraphFlowNode[]) {
  if (!nodes.length) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    const width = Number(node.width ?? node.measured?.width ?? NODE_WIDTH);
    const height = Number(node.height ?? node.measured?.height ?? NODE_HEIGHT);
    minX = Math.min(minX, node.position.x);
    minY = Math.min(minY, node.position.y);
    maxX = Math.max(maxX, node.position.x + width);
    maxY = Math.max(maxY, node.position.y + height);
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

interface GraphViewProps {
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  selectedId: string | null;
  currentId: string | null;
  isLive: boolean;
  onSelect: (id: string | null) => void;
}

interface GraphCanvasProps extends GraphViewProps {
  displayTopology: ReturnType<typeof createGraphTopology>;
  pageProjection: PageProjection;
  onInteractionChange: (active: boolean) => void;
}

function GraphCanvas({
  nodes,
  edges,
  selectedId,
  currentId,
  isLive,
  onSelect,
  displayTopology,
  pageProjection,
  onInteractionChange,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pendingFitRef = useRef(true);
  const fitFrameRef = useRef<number | null>(null);
  const explicitFitDurationRef = useRef<number | null>(null);
  const [following, setFollowing] = useState(true);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [selectedFamilyId, setSelectedFamilyId] = useState<string | null>(null);
  const [selectedInteractionId, setSelectedInteractionId] = useState<string | null>(null);
  const [nestedPageId, setNestedPageId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [interacting, setInteracting] = useState(false);
  const manualInteractionRef = useRef(false);
  const nodesInitialized = useNodesInitialized();
  const { fitBounds, getZoom, setCenter } = useReactFlow<GraphFlowNode, Edge>();

  const layout = useMemo(
    () => layoutTopology(displayTopology),
    [displayTopology.layoutKey],
  );
  const interactionProjection = useMemo(
    () => nestedPageId
      ? buildInteractionProjection(nodes, edges, nestedPageId)
      : null,
    [edges, nestedPageId, nodes],
  );
  const selectedPageProjection = useMemo(
    () => selectedId && pageProjection.nodes[selectedId]
      ? buildInteractionProjection(nodes, edges, selectedId)
      : null,
    [edges, nodes, pageProjection.nodes, selectedId],
  );

  const normalizedQuery = query.trim().toLowerCase();
  const nodeEdgeFocus = useMemo(
    () => nestedPageId
      ? null
      : collectNodeEdgeFocus(
        displayTopology,
        pageProjection.nodes,
        pageProjection.edges,
        selectedId,
      ),
    [displayTopology, nestedPageId, pageProjection, selectedId],
  );
  const pageCurrentId = currentId ? pageProjection.ownerByState[currentId] : null;
  const nestedCurrentId = currentId
    && nestedPageId
    && pageAncestorId(nodes, currentId) === nestedPageId
    ? currentId
    : nestedPageId;

  const rfNodes = useMemo<GraphFlowNode[]>(
    () => interactionProjection
      ? [
        ...interactionProjection.stateIds.flatMap((id): StateFlowNode[] => {
          const state = nodes[id];
          const position = interactionProjection.positions[id];
          if (!state || !position) return [];
          return [{
            id,
            type: "state" as const,
            position,
            selected: id === selectedId,
            zIndex: 1,
            width: NODE_WIDTH,
            height: NODE_HEIGHT,
            measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
            data: { state, current: id === nestedCurrentId },
            style: {
              opacity: normalizedQuery
                && !`${state.label ?? state.title} ${state.url}`.toLowerCase().includes(normalizedQuery)
                ? 0.32
                : 1,
            },
          }];
        }),
        ...Object.values(interactionProjection.capabilities).map(
          (capability): InteractionFlowNode => ({
            id: capability.id,
            type: "interaction",
            position: interactionProjection.positions[capability.id],
            selected: capability.id === selectedInteractionId,
            zIndex: 1,
            width: INTERACTION_NODE_WIDTH,
            height: INTERACTION_NODE_HEIGHT,
            measured: { width: INTERACTION_NODE_WIDTH, height: INTERACTION_NODE_HEIGHT },
            data: capability,
            style: {
              opacity: normalizedQuery
                && !`${capability.label} ${capability.kind}`.toLowerCase().includes(normalizedQuery)
                ? 0.32
                : 1,
            },
          }),
        ),
      ]
      : [
      ...layout.familyBoxes.map((family): FamilyFlowNode => ({
        id: family.id,
        type: "family",
        position: family.position,
        data: {
          pattern: family.pattern,
          memberCount: family.memberIds.length,
          label: family.label,
          kind: family.kind,
          discoveredCount: family.discoveredCount,
          checkedCount: family.checkedCount,
          representedCount: family.representedCount,
          skippedCount: family.skippedCount,
          sampleLabels: family.sampleLabels,
        },
        selected: family.id === selectedFamilyId,
        selectable: true,
        draggable: false,
        connectable: false,
        focusable: false,
        zIndex: 0,
        width: family.width,
        height: family.height,
        measured: { width: family.width, height: family.height },
        style: {
          width: family.width,
          height: family.height,
          pointerEvents: "auto",
          opacity: normalizedQuery && !`${family.label} ${family.pattern}`.toLowerCase().includes(normalizedQuery) ? 0.2 : 1,
        },
      })),
      ...displayTopology.nodeIds.flatMap((id): StateFlowNode[] => {
        const state = pageProjection.nodes[id];
        const position = layout.nodePositions[id];
        if (!state || !position) return [];
        return [{
          id,
          type: "state" as const,
          position,
          selected: id === selectedId,
          zIndex: 1,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
          data: { state, current: id === pageCurrentId },
          style: {
            opacity:
              (normalizedQuery
                && !`${state.label ?? state.title} ${state.url}`.toLowerCase().includes(normalizedQuery))
              || (nodeEdgeFocus && !nodeEdgeFocus.nodeIds.has(id))
                ? 0.38
                : 1,
          },
        }];
      }),
    ],
    [
      displayTopology.nodeIds,
      interactionProjection,
      layout,
      nestedCurrentId,
      nodeEdgeFocus,
      nodes,
      normalizedQuery,
      pageCurrentId,
      pageProjection.nodes,
      selectedFamilyId,
      selectedId,
      selectedInteractionId,
    ],
  );

  const rfEdges = useMemo(
    () => interactionProjection?.edges ?? buildFlowEdges(
      displayTopology,
      pageProjection.edges,
      selectedEdgeId,
      selectedId,
      pageProjection.nodes,
      !interacting && !document.hidden,
    ),
    [
      displayTopology,
      interacting,
      interactionProjection,
      pageProjection,
      selectedEdgeId,
      selectedId,
    ],
  );

  const canViewport = useCallback(() => {
    const container = containerRef.current;
    return Boolean(
      rfNodes.length > 0
      && container
      && container.clientWidth > 0
      && container.clientHeight > 0
      && !document.hidden,
    );
  }, [rfNodes.length]);

  const fitGraph = useCallback((duration: number) => {
    if (!canViewport()) {
      pendingFitRef.current = true;
      return;
    }
    const bounds = nodeBounds(rfNodes);
    if (!bounds) {
      pendingFitRef.current = true;
      return;
    }
    pendingFitRef.current = false;
    void fitBounds(bounds, { duration, padding: FIT_PADDING });
  }, [canViewport, fitBounds, rfNodes]);

  const followCurrent = useCallback((duration: number) => {
    if (!canViewport()) {
      pendingFitRef.current = true;
      return;
    }
    const targetId = interactionProjection
      ? nestedCurrentId
      : pageCurrentId ?? displayTopology.nodeIds.at(-1);
    if (!targetId) return;
    const position = interactionProjection
      ? interactionProjection.positions[targetId]
      : layout.nodePositions[targetId];
    if (!position) {
      pendingFitRef.current = true;
      return;
    }
    pendingFitRef.current = false;
    void setCenter(position.x + NODE_WIDTH / 2, position.y + NODE_HEIGHT / 2, {
      duration,
      zoom: getZoom(),
    });
  }, [
    canViewport,
    displayTopology.nodeIds,
    getZoom,
    interactionProjection,
    layout.nodePositions,
    nestedCurrentId,
    pageCurrentId,
    setCenter,
  ]);

  const scheduleViewport = useCallback((mode: "fit" | "follow", duration = 0) => {
    if (fitFrameRef.current !== null) window.cancelAnimationFrame(fitFrameRef.current);
    fitFrameRef.current = window.requestAnimationFrame(() => {
      fitFrameRef.current = window.requestAnimationFrame(() => {
        fitFrameRef.current = null;
        if (mode === "follow") followCurrent(duration);
        else fitGraph(duration);
      });
    });
  }, [fitGraph, followCurrent]);

  const scheduleFollow = useCallback((duration = 0) => {
    scheduleViewport("follow", duration);
  }, [scheduleViewport]);

  const scheduleFit = useCallback((duration = 0) => {
    scheduleViewport("fit", duration);
  }, [scheduleViewport]);

  const scheduleFollowingViewport = useCallback((duration = 0) => {
    if (isLive && (pageCurrentId || nestedCurrentId)) scheduleFollow(duration);
    else scheduleFit(duration);
  }, [isLive, nestedCurrentId, pageCurrentId, scheduleFit, scheduleFollow]);

  useEffect(() => {
    pendingFitRef.current = true;
    if (following && nodesInitialized && rfNodes.length > 0) {
      const duration = explicitFitDurationRef.current ?? 0;
      explicitFitDurationRef.current = null;
      scheduleFollowingViewport(duration);
    }
  }, [
    currentId,
    displayTopology.layoutKey,
    interactionProjection?.key,
    following,
    isLive,
    nodesInitialized,
    rfNodes.length,
    scheduleFollowingViewport,
  ]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      pendingFitRef.current = true;
      if (following && nodesInitialized && rfNodes.length > 0) scheduleFollowingViewport(0);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [following, nodesInitialized, rfNodes.length, scheduleFollowingViewport]);

  useEffect(() => {
    const refitOnForeground = () => {
      if (
        !document.hidden
        && pendingFitRef.current
        && following
        && nodesInitialized
        && rfNodes.length > 0
      ) {
        scheduleFollowingViewport(0);
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
  }, [following, nodesInitialized, rfNodes.length, scheduleFollowingViewport]);

  useEffect(() => () => {
    if (fitFrameRef.current !== null) window.cancelAnimationFrame(fitFrameRef.current);
  }, []);

  const followAndFit = () => {
    pendingFitRef.current = true;
    if (following) {
      scheduleFollowingViewport(250);
    } else {
      explicitFitDurationRef.current = 250;
      setFollowing(true);
    }
  };

  const handleFitGraph = () => {
    setFollowing(false);
    pendingFitRef.current = true;
    scheduleFit(250);
  };

  const leaveNestedLayer = useCallback(() => {
    const pageId = nestedPageId;
    setNestedPageId(null);
    setSelectedInteractionId(null);
    setSelectedEdgeId(null);
    if (pageId) onSelect(pageId);
  }, [nestedPageId, onSelect]);

  const clearSelection = useCallback(() => {
    setSelectedEdgeId(null);
    setSelectedFamilyId(null);
    setSelectedInteractionId(null);
    if (!nestedPageId) onSelect(null);
  }, [nestedPageId, onSelect]);

  const endInteraction = useCallback(() => {
    if (!manualInteractionRef.current) return;
    manualInteractionRef.current = false;
    setInteracting(false);
    onInteractionChange(false);
  }, [onInteractionChange]);

  useEffect(() => {
    const releaseOnVisibilityLoss = () => {
      if (document.hidden) endInteraction();
    };
    window.addEventListener("blur", endInteraction);
    document.addEventListener("visibilitychange", releaseOnVisibilityLoss);
    return () => {
      window.removeEventListener("blur", endInteraction);
      document.removeEventListener("visibilitychange", releaseOnVisibilityLoss);
    };
  }, [endInteraction]);

  useEffect(() => {
    if (selectedId !== null) return;
    setSelectedEdgeId(null);
    setSelectedFamilyId(null);
  }, [selectedId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const target = event.target;
      if (target instanceof HTMLElement && target.closest(".graph-search")) return;
      if (selectedInteractionId) {
        setSelectedInteractionId(null);
        return;
      }
      if (nestedPageId) {
        leaveNestedLayer();
        return;
      }
      if (!selectedId && !selectedEdgeId && !selectedFamilyId) return;
      clearSelection();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    clearSelection,
    leaveNestedLayer,
    nestedPageId,
    selectedEdgeId,
    selectedFamilyId,
    selectedId,
    selectedInteractionId,
  ]);

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
        panOnDrag
        selectionOnDrag={false}
        zoomOnScroll
        zoomOnPinch
        onMoveStart={(event) => {
          // fitBounds/setCenter also emit move callbacks, but without a source
          // event. Only real user input should freeze the live topology.
          if (!event) return;
          manualInteractionRef.current = true;
          setFollowing(false);
          setInteracting(true);
          onInteractionChange(true);
        }}
        onMoveEnd={endInteraction}
        onNodeClick={(_, node) => {
          if (node.type === "state") {
            setSelectedEdgeId(null);
            setSelectedFamilyId(null);
            setSelectedInteractionId(null);
            onSelect(node.id);
          } else if (node.type === "family") {
            setSelectedEdgeId(null);
            setSelectedFamilyId(node.id);
          } else if (node.type === "interaction") {
            setSelectedEdgeId(null);
            setSelectedInteractionId(node.id);
          }
        }}
        onEdgeClick={(_, edge) => setSelectedEdgeId(edge.id)}
        onPaneClick={clearSelection}
        minZoom={0.1}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1f2228" gap={22} />
        <Controls showFitView={false} showInteractive={false} />
        <Panel position="top-left" className="graph-label">
          {interactionProjection && (
            <button type="button" className="graph-layer-back" onClick={leaveNestedLayer}>
              {"\u2190"} Pages
            </button>
          )}
          <span>{interactionProjection ? "Page interactions" : "Page topology"}</span>
          <strong>
            {interactionProjection ? (
              <>
                {nodes[nestedPageId ?? ""]?.label || nodes[nestedPageId ?? ""]?.title}
                {` \u00b7 ${Object.keys(interactionProjection.capabilities).length} interactions`}
                {` \u00b7 ${interactionProjection.stateIds.length - 1} captured UI states`}
              </>
            ) : (
              <>
            {displayTopology.nodeIds.length} pages · {Object.keys(pageProjection.edges).length} transitions
            {rfEdges.length !== Object.keys(pageProjection.edges).length
              ? ` · ${rfEdges.length} visible transition bundles`
              : ""}
              </>
            )}
          </strong>
        </Panel>
        <Panel position="bottom-left" className="graph-search">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={interactionProjection ? "Find interaction or UI state" : "Find page, family, or URL"}
            aria-label={interactionProjection ? "Find interaction or UI state" : "Find page, family, or URL"}
          />
        </Panel>
        <Panel
          position="top-right"
          className="graph-actions"
          style={{ right: selectedId ? "calc(min(360px, 36vw) + 12px)" : undefined }}
        >
          {!interactionProjection
            && selectedPageProjection
            && (Object.keys(selectedPageProjection.capabilities).length > 0
              || selectedPageProjection.stateIds.length > 1)
            && (
            <button
              type="button"
              className="graph-follow graph-follow--active"
              onClick={() => {
                setNestedPageId(selectedPageProjection.pageId);
                setSelectedInteractionId(null);
                setSelectedEdgeId(null);
              }}
            >
              View interactions ({Object.keys(selectedPageProjection.capabilities).length})
            </button>
          )}
          <button
            type="button"
            className="graph-follow"
            onClick={handleFitGraph}
            disabled={rfNodes.length === 0}
          >
            Fit graph
          </button>
          {isLive && (
            <button
              type="button"
              className={`graph-follow${following ? " graph-follow--active" : ""}`}
              onClick={followAndFit}
              disabled={rfNodes.length === 0}
              aria-pressed={following}
            >
              {following ? "Following live" : "Follow live"}
            </button>
          )}
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
  const pageProjection = useMemo(
    () => projectPageGraph(props.nodes, props.edges),
    [props.edges, props.nodes],
  );
  const topology = useMemo(
    () => createGraphTopology(pageProjection.nodes, pageProjection.edges),
    [pageProjection],
  );
  const [displayTopology, setDisplayTopology] = useState(topology);
  const [interacting, setInteracting] = useState(false);
  const pendingTopologyRef = useRef(topology);
  const topologyFrameRef = useRef<number | null>(null);

  useEffect(() => {
    pendingTopologyRef.current = topology;
    if (interacting) return;
    if (!props.isLive || topology.nodeIds.length === 0) {
      setDisplayTopology(topology);
      return;
    }
    if (topologyFrameRef.current !== null) window.cancelAnimationFrame(topologyFrameRef.current);
    topologyFrameRef.current = window.requestAnimationFrame(() => {
      topologyFrameRef.current = null;
      setDisplayTopology(pendingTopologyRef.current);
    });
    return () => {
      if (topologyFrameRef.current !== null) {
        window.cancelAnimationFrame(topologyFrameRef.current);
        topologyFrameRef.current = null;
      }
    };
  }, [interacting, props.isLive, topology.key]);

  return (
    <ReactFlowProvider>
      <GraphCanvas
        {...props}
        displayTopology={displayTopology}
        pageProjection={pageProjection}
        onInteractionChange={setInteracting}
      />
    </ReactFlowProvider>
  );
}
