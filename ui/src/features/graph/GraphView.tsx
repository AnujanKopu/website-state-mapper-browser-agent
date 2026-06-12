import { useEffect, useMemo, useRef } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
} from "@xyflow/react";

import type { GraphEdge, GraphState } from "../../types/graph";
import { layoutGraph } from "./layout";
import { StateNodeView, type StateFlowNode } from "./StateNode";

const nodeTypes = { state: StateNodeView };

interface GraphViewProps {
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function GraphView({ nodes, edges, selectedId, onSelect }: GraphViewProps) {
  const fitViewRef = useRef<(() => void) | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { laidOutNodes, rfEdges, nodeCount, edgeCount } = useMemo(() => {
    const stateList = Object.values(nodes);
    const nodeIds = new Set(stateList.map((s) => s.id));

    const baseNodes: StateFlowNode[] = stateList.map((state) => ({
      id: state.id,
      type: "state",
      position: { x: 0, y: 0 },
      data: { state },
    }));

    const flowEdges: Edge[] = Object.values(edges)
      .filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to))
      .map((edge) => ({
        id: edge.id,
        source: edge.from,
        target: edge.to,
        label: edge.label,
        animated: edge.via === "inferred",
        style: edge.via === "inferred" ? { strokeDasharray: "5 4" } : undefined,
        labelStyle: { fill: "var(--text-secondary)", fontSize: 10 },
        labelBgStyle: { fill: "var(--bg)", fillOpacity: 0.85 },
      }));

    return {
      laidOutNodes: layoutGraph(baseNodes, flowEdges),
      rfEdges: flowEdges,
      nodeCount: baseNodes.length,
      edgeCount: flowEdges.length,
    };
  }, [nodes, edges]);

  const rfNodes: StateFlowNode[] = useMemo(
    () =>
      laidOutNodes.map((node) => ({
        ...node,
        selected: node.id === selectedId,
      })) as StateFlowNode[],
    [laidOutNodes, selectedId],
  );

  // Re-fit when the graph topology changes. Dagre re-layout runs on every edge
  // update; without refitting, new edges can push nodes off-screen (blank graph).
  useEffect(() => {
    const fitView = fitViewRef.current;
    const container = containerRef.current;
    if (!fitView || nodeCount === 0 || !container) return;

    const timer = window.setTimeout(() => {
      if (container.clientWidth === 0 || container.clientHeight === 0) return;
      fitView();
    }, 120);

    return () => window.clearTimeout(timer);
  }, [nodeCount, edgeCount]);

  if (nodeCount === 0) {
    return <div className="graph-empty">Waiting for the first state to be discovered{"\u2026"}</div>;
  }

  return (
    <div ref={containerRef} className="graph-view">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        colorMode="dark"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onInit={(instance) => {
          fitViewRef.current = () => instance.fitView({ duration: 300, padding: 0.2 });
        }}
        onNodeClick={(_, node) => onSelect(node.id)}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        maxZoom={1.8}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#1f2228" gap={22} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
