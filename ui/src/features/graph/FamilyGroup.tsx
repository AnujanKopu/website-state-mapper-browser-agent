import { Handle, Position } from "@xyflow/react";
import type { Node, NodeProps } from "@xyflow/react";
import type { CSSProperties } from "react";

import { colorForKey } from "./nodeStyles";

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
  expanded?: boolean;
  active?: boolean;
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
  const accent = colorForKey(data.pattern);
  const active = selected || data.active;
  return (
    <div
      className={`family-group${active ? " family-group--selected" : ""}${data.expanded ? " is-expanded" : ""}`}
      style={{ "--family-accent": accent } as CSSProperties}
    >
      <Handle type="target" position={Position.Left} className="family-group__handle" />
      <div className="family-group__heading">
        <div className="family-group__identity">
          <svg className="family-group__glyph" viewBox="0 0 16 16" aria-hidden>
            <rect x="1.5" y="1.5" width="5" height="5" />
            <rect x="9.5" y="1.5" width="5" height="5" />
            <rect x="1.5" y="9.5" width="5" height="5" />
            <rect x="9.5" y="9.5" width="5" height="5" />
          </svg>
          <div>
            <span className="family-group__kind">{data.kind} family</span>
          <strong>{data.label}</strong>
          <span className="family-group__pattern" title={data.pattern}>
            {displayPattern(data.pattern)}
          </span>
          </div>
        </div>
        <span className="family-group__count" title={data.sampleLabels.join(", ")}>
          {data.discoveredCount} found · {data.representedCount} mapped
          {data.skippedCount > 0 ? ` · ${data.skippedCount} grouped` : ""}
        </span>
        <span className="family-group__action">{data.expanded ? "Collapse" : "Expand"}</span>
      </div>
      <Handle type="source" position={Position.Right} className="family-group__handle" />
    </div>
  );
}
