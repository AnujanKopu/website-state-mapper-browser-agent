import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";
import type { CSSProperties } from "react";

import { stateTypeLabel, truncate } from "../../lib/format";
import type { GraphState } from "../../types/graph";
import { accentForState } from "./nodeStyles";

export type StateFlowNode = Node<{ state: GraphState | null; current: boolean }, "state">;

function StateGlyph({ type }: { type: GraphState["type"] }) {
  if (["auth_wall", "paywall", "risky_terminal"].includes(type)) {
    return <svg viewBox="0 0 16 16" aria-hidden><path d="M4 7V5.5a4 4 0 0 1 8 0V7M3 7.5h10v6H3z" /></svg>;
  }
  if (["modal", "dropdown", "tab", "page_variant"].includes(type)) {
    return <svg viewBox="0 0 16 16" aria-hidden><path d="M2.5 3.5h8v8h-8zM5.5 1.5h8v8" /></svg>;
  }
  if (type === "form") {
    return <svg viewBox="0 0 16 16" aria-hidden><path d="M3 2.5h10v11H3zM5 5h6M5 8h6M5 11h3" /></svg>;
  }
  return <svg viewBox="0 0 16 16" aria-hidden><path d="M3 1.5h7l3 3v10H3zM10 1.5v3h3" /></svg>;
}

export function StateNodeView({ data, selected }: NodeProps<StateFlowNode>) {
  const { state } = data;
  if (!state) return null;
  const accent = accentForState(state);
  const indexLabel = typeof state.index === "number" ? `s${state.index}` : "";
  const terminal = ["auth_wall", "paywall", "risky_terminal", "dead_end", "error"].includes(state.type);

  return (
    <div
      className={`state-node${data.current ? " state-node--current" : ""}${selected ? " is-selected" : ""}${terminal ? " is-terminal" : ""}`}
      style={{ "--node-accent": accent } as CSSProperties}
    >
      <Handle type="target" position={Position.Left} className="state-node__handle" />
      <div className="state-node__top">
        <span className="state-node__type">
          <span className="state-node__glyph"><StateGlyph type={state.type} /></span>
          {terminal ? "Boundary" : stateTypeLabel(state.type)}
        </span>
        {indexLabel && <span className="state-node__index">{indexLabel}</span>}
      </div>
      <div className="state-node__title">{truncate(state.label || state.title || state.url_normalized, 42)}</div>
      <div className="state-node__meta">
        {state.exploration?.page_role ?? "state"} · depth {state.exploration?.page_depth ?? state.depth}
        {state.parent_state_id ? " · nested state" : ""}
      </div>
      <Handle type="source" position={Position.Right} className="state-node__handle" />
    </div>
  );
}
