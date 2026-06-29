import type { StateType } from "../../types/graph";

// Subtle border/badge accents per state type (monochrome-first, accents only).
export const STATE_TYPE_ACCENT: Record<StateType, string> = {
  page: "#3b3f47",
  page_variant: "#5b708f",
  modal: "#6ea8fe",
  form: "#5bc8af",
  auth_wall: "#d8a657",
  paywall: "#e0883a",
  dropdown: "#7a7f88",
  tab: "#7a7f88",
  wizard_step: "#8aa2c2",
  error: "#e06c75",
  dead_end: "#9aa0a8",
  risky_terminal: "#e06c75",
  external: "#5f6368",
};

export function accentFor(type: StateType | string): string {
  return STATE_TYPE_ACCENT[type as StateType] ?? STATE_TYPE_ACCENT.page;
}
