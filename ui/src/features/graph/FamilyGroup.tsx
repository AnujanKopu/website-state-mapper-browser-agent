import type { Node, NodeProps } from "@xyflow/react";

export interface FamilyGroupData extends Record<string, unknown> {
  pattern: string;
  memberCount: number;
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

export function FamilyGroupView({ data }: NodeProps<FamilyFlowNode>) {
  return (
    <div className="family-group">
      <div className="family-group__heading">
        <span className="family-group__pattern" title={data.pattern}>
          {displayPattern(data.pattern)}
        </span>
        <span className="family-group__count">
          {data.memberCount} structural variants
        </span>
      </div>
    </div>
  );
}
