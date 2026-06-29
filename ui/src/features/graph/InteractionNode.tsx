import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import { truncate } from "../../lib/format";
import type { InteractionCapability } from "./graphLayers";

export type InteractionFlowNode = Node<InteractionCapability, "interaction">;

export function InteractionNodeView({ data, selected }: NodeProps<InteractionFlowNode>) {
  return (
    <div
      className={`interaction-node interaction-node--${data.status}${selected ? " is-selected" : ""}`}
    >
      <Handle type="target" position={Position.Left} className="state-node__handle" />
      <div className="interaction-node__top">
        <span>{data.kind.replaceAll("_", " ")}</span>
        <span>{data.status === "inventory_only" ? "captured" : data.status}</span>
      </div>
      <div className="interaction-node__title">{truncate(data.label, 36)}</div>
      {data.count > 1 && <span className="interaction-node__count">{data.count} similar</span>}
      <Handle type="source" position={Position.Right} className="state-node__handle" />
    </div>
  );
}
