import type { GraphState, StateType } from "../../types/graph";

// Subtle border/badge accents per state type (monochrome-first, accents only).
export const GRAPH_PALETTE = [
  "#62C6FF",
  "#8B9CFF",
  "#B78CFF",
  "#E07AC6",
  "#FF8F9C",
  "#F2B75C",
  "#5FD3C0",
] as const;

export const STATE_TYPE_ACCENT: Record<StateType, string> = {
  page: GRAPH_PALETTE[0],
  page_variant: GRAPH_PALETTE[1],
  modal: GRAPH_PALETTE[2],
  form: GRAPH_PALETTE[6],
  auth_wall: GRAPH_PALETTE[5],
  paywall: "#FF9C61",
  dropdown: GRAPH_PALETTE[3],
  tab: GRAPH_PALETTE[1],
  wizard_step: GRAPH_PALETTE[4],
  error: "#FF6F83",
  dead_end: "#A8AEA5",
  risky_terminal: "#FF6F83",
  external: "#8A9188",
};

function hash(value: string): number {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

export function colorForKey(key: string): string {
  return GRAPH_PALETTE[hash(key) % GRAPH_PALETTE.length];
}

export function accentFor(type: StateType | string): string {
  return STATE_TYPE_ACCENT[type as StateType] ?? STATE_TYPE_ACCENT.page;
}

export function accentForState(state: GraphState): string {
  const family = state.exploration?.route_family ?? state.exploration?.family?.id;
  return family ? colorForKey(family) : accentFor(state.type);
}
