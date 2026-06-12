import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";

import { stateTypeLabel, truncate } from "../../lib/format";
import type { GraphState } from "../../types/graph";
import { accentFor } from "./nodeStyles";

export type StateFlowNode = Node<{ state: GraphState }, "state">;

export function StateNodeView({ data, selected }: NodeProps<StateFlowNode>) {
  const { state } = data;
  const accent = accentFor(state.type);
  const indexLabel = typeof state.index === "number" ? `s${state.index}` : "";

  return (
    <div
      className="state-node"
      style={{
        borderColor: selected ? accent : "var(--border)",
        boxShadow: selected ? `0 0 0 1px ${accent}` : "none",
      }}
    >
      <Handle type="target" position={Position.Left} className="state-node__handle" />
      <div className="state-node__top">
        <span className="state-node__badge" style={{ color: accent, borderColor: accent }}>
          {stateTypeLabel(state.type)}
        </span>
        {indexLabel && <span className="state-node__index">{indexLabel}</span>}
      </div>
      <div className="state-node__title">{truncate(state.title || state.url_normalized, 40)}</div>
      <div className="state-node__meta">
        depth {state.depth}
        {state.parent_state_id ? ` \u00b7 \u21B3 sub` : ""}
      </div>
      <Handle type="source" position={Position.Right} className="state-node__handle" />
    </div>
  );
}
