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
  configureFamilyExpansion,
  createGraphTopology,
  layoutTopology,
} from "./graphElements";
import { FamilyGroupView, type FamilyFlowNode } from "./FamilyGroup";
import { pageAncestorId, projectPageGraph, type PageProjection } from "./graphLayers";
import { NODE_HEIGHT, NODE_WIDTH } from "./layout";
import { StateNodeView, type StateFlowNode } from "./StateNode";
import { SurfaceMap } from "./SurfaceMap";

type GraphFlowNode = StateFlowNode | FamilyFlowNode;

const nodeTypes = {
  state: StateNodeView,
  family: FamilyGroupView,
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
  onSurfaceModeChange?: (active: boolean) => void;
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
  onSurfaceModeChange,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const pendingFitRef = useRef(true);
  const fitFrameRef = useRef<number | null>(null);
  const explicitFitDurationRef = useRef<number | null>(null);
  const manualInteractionRef = useRef(false);
  const [following, setFollowing] = useState(true);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [selectedFamilyId, setSelectedFamilyId] = useState<string | null>(null);
  const [collapsedFamilyIds, setCollapsedFamilyIds] = useState<Set<string>>(() => new Set());
  const [nestedPageId, setNestedPageId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [interacting, setInteracting] = useState(false);
  const nodesInitialized = useNodesInitialized();
  const { fitBounds, getZoom, setCenter } = useReactFlow<GraphFlowNode, Edge>();

  const expandedFamilyIds = useMemo(
    () => new Set(
      displayTopology.families
        .filter((family) => !collapsedFamilyIds.has(family.id))
        .map((family) => family.id),
    ),
    [collapsedFamilyIds, displayTopology.families],
  );
  const topology = useMemo(
    () => configureFamilyExpansion(displayTopology, expandedFamilyIds),
    [displayTopology, expandedFamilyIds],
  );
  const layout = useMemo(() => layoutTopology(topology), [topology.layoutKey]);
  const normalizedQuery = query.trim().toLowerCase();
  const pageCurrentId = currentId ? pageProjection.ownerByState[currentId] : null;
  const nodeEdgeFocus = useMemo(
    () => collectNodeEdgeFocus(topology, pageProjection.nodes, pageProjection.edges, selectedId),
    [pageProjection, selectedId, topology],
  );
  const selectedPage = selectedId ? pageProjection.nodes[selectedId] : null;
  const selectedSurfaceCount = selectedPage?.surface_items?.length ?? 0;
  const selectedCapturedCount = selectedId
    ? Object.values(nodes).filter((state) => pageAncestorId(nodes, state.id) === selectedId).length - 1
    : 0;
  const largeMode = topology.nodeIds.length > 60 || topology.edgeIds.length > 200;

  useEffect(() => {
    if (!selectedId) return;
    const owner = topology.ownerByNode[selectedId];
    if (!owner || owner === selectedId || !collapsedFamilyIds.has(owner)) return;
    setCollapsedFamilyIds((current) => {
      const next = new Set(current);
      next.delete(owner);
      return next;
    });
  }, [collapsedFamilyIds, selectedId, topology.ownerByNode]);

  useEffect(() => {
    if (!normalizedQuery) return;
    const matchingOwners = topology.nodeIds.flatMap((id) => {
      const state = pageProjection.nodes[id];
      if (!state || !`${state.label ?? state.title} ${state.url}`.toLowerCase().includes(normalizedQuery)) return [];
      const owner = topology.ownerByNode[id];
      return owner && owner !== id ? [owner] : [];
    });
    if (!matchingOwners.length) return;
    setCollapsedFamilyIds((current) => {
      if (matchingOwners.every((owner) => !current.has(owner))) return current;
      const next = new Set(current);
      for (const owner of matchingOwners) next.delete(owner);
      return next;
    });
  }, [normalizedQuery, pageProjection.nodes, topology.nodeIds, topology.ownerByNode]);

  const rfNodes = useMemo<GraphFlowNode[]>(() => [
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
        expanded: family.expanded,
        active: family.id === selectedFamilyId,
      },
      // React Flow raises selected wrappers above siblings. Families are
      // organizational backdrops, so render the active state inside the
      // custom node and keep member nodes above the container.
      selected: false,
      selectable: true,
      draggable: false,
      connectable: false,
      focusable: true,
      zIndex: 0,
      width: family.width,
      height: family.height,
      measured: { width: family.width, height: family.height },
      style: {
        width: family.width,
        height: family.height,
        pointerEvents: "auto",
        opacity: normalizedQuery && !`${family.label} ${family.pattern}`.toLowerCase().includes(normalizedQuery) ? 0.28 : 1,
      },
      ariaLabel: `${family.label} family, ${family.discoveredCount} discovered, ${family.expanded ? "expanded" : "collapsed"}`,
    })),
    ...topology.nodeIds.flatMap((id): StateFlowNode[] => {
      const state = pageProjection.nodes[id];
      const position = layout.nodePositions[id];
      const owner = topology.ownerByNode[id];
      if (!state || !position || (owner !== id && !expandedFamilyIds.has(owner))) return [];
      const queryMiss = normalizedQuery
        && !`${state.label ?? state.title} ${state.url}`.toLowerCase().includes(normalizedQuery);
      const focusMiss = nodeEdgeFocus && !nodeEdgeFocus.nodeIds.has(id);
      return [{
        id,
        type: "state",
        position,
        selected: id === selectedId,
        zIndex: 1,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
        data: { state, current: id === pageCurrentId },
        style: { opacity: queryMiss || focusMiss ? 0.3 : 1 },
        ariaLabel: `${state.label || state.title || state.url_normalized}, ${state.type.replaceAll("_", " ")}`,
      }];
    }),
  ], [
    expandedFamilyIds,
    layout,
    nodeEdgeFocus,
    normalizedQuery,
    pageCurrentId,
    pageProjection.nodes,
    selectedFamilyId,
    selectedId,
    topology.nodeIds,
    topology.ownerByNode,
  ]);

  const rfEdges = useMemo(
    () => buildFlowEdges(
      topology,
      pageProjection.edges,
      selectedEdgeId,
      selectedId,
      pageProjection.nodes,
      !largeMode && !interacting && !document.hidden,
    ),
    [interacting, largeMode, pageProjection, selectedEdgeId, selectedId, topology],
  );

  const canViewport = useCallback(() => {
    const container = containerRef.current;
    return Boolean(
      !nestedPageId
      && rfNodes.length > 0
      && container
      && container.clientWidth > 0
      && container.clientHeight > 0
      && !document.hidden,
    );
  }, [nestedPageId, rfNodes.length]);

  const fitGraph = useCallback((duration: number) => {
    if (!canViewport()) {
      pendingFitRef.current = true;
      return;
    }
    const bounds = nodeBounds(rfNodes);
    if (!bounds) return;
    pendingFitRef.current = false;
    void fitBounds(bounds, { duration, padding: FIT_PADDING });
  }, [canViewport, fitBounds, rfNodes]);

  const followCurrent = useCallback((duration: number) => {
    if (!canViewport()) {
      pendingFitRef.current = true;
      return;
    }
    const targetId = pageCurrentId ?? selectedId ?? topology.nodeIds[0];
    if (!targetId) return;
    const owner = topology.ownerByNode[targetId] ?? targetId;
    const family = layout.familyBoxes.find((item) => item.id === owner);
    const position = layout.nodePositions[targetId] ?? family?.position;
    if (!position) return;
    const width = family?.width ?? NODE_WIDTH;
    const height = family?.height ?? NODE_HEIGHT;
    const preferredZoom = Math.max(0.65, Math.min(1, getZoom()));
    pendingFitRef.current = false;
    void setCenter(position.x + width / 2, position.y + height / 2, {
      duration,
      zoom: preferredZoom,
    });
  }, [canViewport, getZoom, layout, pageCurrentId, selectedId, setCenter, topology.nodeIds, topology.ownerByNode]);

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

  const scheduleFollowingViewport = useCallback((duration = 0) => {
    if (pageCurrentId || selectedId || topology.nodeIds.length > 12) scheduleViewport("follow", duration);
    else scheduleViewport("fit", duration);
  }, [pageCurrentId, scheduleViewport, selectedId, topology.nodeIds.length]);

  useEffect(() => {
    if (nestedPageId) return;
    pendingFitRef.current = true;
    if (following && nodesInitialized && rfNodes.length > 0) {
      const duration = explicitFitDurationRef.current ?? 0;
      explicitFitDurationRef.current = null;
      scheduleFollowingViewport(duration);
    }
  }, [
    currentId,
    following,
    nestedPageId,
    nodesInitialized,
    rfNodes.length,
    scheduleFollowingViewport,
    topology.layoutKey,
  ]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined" || nestedPageId) return;
    const observer = new ResizeObserver(() => {
      pendingFitRef.current = true;
      if (following && nodesInitialized && rfNodes.length > 0) scheduleFollowingViewport(0);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [following, nestedPageId, nodesInitialized, rfNodes.length, scheduleFollowingViewport]);

  useEffect(() => {
    const refitOnForeground = () => {
      if (!document.hidden && pendingFitRef.current && following && nodesInitialized && rfNodes.length > 0) {
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

  const followAndFocus = () => {
    pendingFitRef.current = true;
    if (following) scheduleFollowingViewport(250);
    else {
      explicitFitDurationRef.current = 250;
      setFollowing(true);
    }
  };

  const handleFitGraph = () => {
    setFollowing(false);
    pendingFitRef.current = true;
    scheduleViewport("fit", 250);
  };

  const leaveNestedLayer = useCallback(() => {
    const pageId = nestedPageId;
    setNestedPageId(null);
    onSurfaceModeChange?.(false);
    setSelectedEdgeId(null);
    if (pageId) onSelect(pageId);
  }, [nestedPageId, onSelect, onSurfaceModeChange]);

  const enterNestedLayer = useCallback((pageId: string) => {
    setNestedPageId(pageId);
    onSurfaceModeChange?.(true);
  }, [onSurfaceModeChange]);

  useEffect(() => () => onSurfaceModeChange?.(false), [onSurfaceModeChange]);

  const clearSelection = useCallback(() => {
    setSelectedEdgeId(null);
    setSelectedFamilyId(null);
    onSelect(null);
  }, [onSelect]);

  const endInteraction = useCallback(() => {
    if (!manualInteractionRef.current) return;
    manualInteractionRef.current = false;
    setInteracting(false);
    onInteractionChange(false);
  }, [onInteractionChange]);

  useEffect(() => {
    const release = () => {
      if (document.hidden) endInteraction();
    };
    window.addEventListener("blur", endInteraction);
    document.addEventListener("visibilitychange", release);
    return () => {
      window.removeEventListener("blur", endInteraction);
      document.removeEventListener("visibilitychange", release);
    };
  }, [endInteraction]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const target = event.target;
      if (target instanceof HTMLElement && target.closest("input")) return;
      if (nestedPageId) leaveNestedLayer();
      else clearSelection();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [clearSelection, leaveNestedLayer, nestedPageId]);

  if (nestedPageId) {
    return (
      <div ref={containerRef} className="graph-view graph-view--surface">
        <SurfaceMap
          nodes={nodes}
          edges={edges}
          pageId={nestedPageId}
          selectedStateId={selectedId}
          onBack={leaveNestedLayer}
          onSelectState={onSelect}
        />
      </div>
    );
  }

  return (
    <div ref={containerRef} className={`graph-view${largeMode ? " graph-view--large" : ""}`}>
      <ReactFlow<GraphFlowNode, Edge>
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        colorMode="dark"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        nodesFocusable
        edgesFocusable
        autoPanOnNodeFocus
        onlyRenderVisibleElements
        panOnDrag
        selectionOnDrag={false}
        zoomOnScroll
        zoomOnPinch
        onMoveStart={(event) => {
          if (!event) return;
          manualInteractionRef.current = true;
          setFollowing(false);
          setInteracting(true);
          onInteractionChange(true);
        }}
        onMoveEnd={endInteraction}
        onNodeClick={(_, node) => {
          setSelectedEdgeId(null);
          if (node.type === "state") {
            setSelectedFamilyId(null);
            onSelect(node.id);
          } else if (node.type === "family") {
            setSelectedFamilyId(node.id);
            setCollapsedFamilyIds((current) => {
              const next = new Set(current);
              if (next.has(node.id)) next.delete(node.id);
              else next.add(node.id);
              return next;
            });
          }
        }}
        onEdgeClick={(_, edge) => setSelectedEdgeId(edge.id)}
        onPaneClick={clearSelection}
        minZoom={0.15}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#343A36" gap={24} size={1} />
        <Controls showFitView={false} showInteractive={false} />
        <Panel position="top-left" className="graph-label">
          <span>Page topology</span>
          <strong>
            {topology.nodeIds.length} pages · {Object.keys(pageProjection.edges).length} transitions
            {rfEdges.length !== Object.keys(pageProjection.edges).length ? ` · ${rfEdges.length} bundles` : ""}
          </strong>
        </Panel>
        <Panel position="bottom-left" className="graph-search">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a page, family, or URL"
            aria-label="Find a page, family, or URL"
          />
        </Panel>
        <Panel
          position="top-right"
          className="graph-actions"
          style={{ right: selectedId ? "calc(min(380px, 38vw) + 12px)" : undefined }}
        >
          {selectedPage && (selectedSurfaceCount > 0 || selectedCapturedCount > 0) && (
            <button
              type="button"
              className="toolbar-button toolbar-button--spectrum"
              onClick={() => enterNestedLayer(selectedPage.id)}
            >
              Surface map <span>{selectedSurfaceCount}</span>
            </button>
          )}
          <button type="button" className="toolbar-button" onClick={handleFitGraph} disabled={!rfNodes.length}>
            Fit graph
          </button>
          {isLive && (
            <button
              type="button"
              className={`toolbar-button${following ? " is-active" : ""}`}
              onClick={followAndFocus}
              disabled={!rfNodes.length}
              aria-pressed={following}
            >
              {following ? "Following live" : "Follow live"}
            </button>
          )}
        </Panel>
      </ReactFlow>

      {rfNodes.length === 0 && (
        <div className="graph-empty graph-empty--overlay" role="status">
          Waiting for the first state to be discovered…
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
