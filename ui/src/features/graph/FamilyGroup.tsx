import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";

export interface FamilyGroupData extends Record<string, unknown> {
  pattern: string;
  memberCount: number;
  label: string;
  kind: string;
  discoveredCount: number;
  checkedCount: number;
  representedCount: number;
  skippedCount: number;
  sampleLabels: string[];
}

export type FamilyFlowNode = Node<FamilyGroupData, "family">;

function displayPattern(pattern: string): string {
  try {
    const url = new URL(pattern);
    return `${url.pathname}${url.search}`;
  } catch {
    return pattern;
  }
}

export function FamilyGroupView({ data, selected }: NodeProps<FamilyFlowNode>) {
  return (
    <div className={`family-group${selected ? " family-group--selected" : ""}`}>
      <Handle type="target" position={Position.Left} className="family-group__handle" />
      <div className="family-group__heading">
        <div>
          <strong>{data.label}</strong>
          <span className="family-group__pattern" title={data.pattern}>
            {displayPattern(data.pattern)}
          </span>
        </div>
        <span className="family-group__count" title={data.sampleLabels.join(", ")}>
          {data.discoveredCount} found · {data.checkedCount} checked · {data.representedCount} shown
          {data.skippedCount > 0 ? ` · ${data.skippedCount} skipped` : ""}
        </span>
      </div>
      <Handle type="source" position={Position.Right} className="family-group__handle" />
    </div>
  );
}
