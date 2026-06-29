import type { StateType } from "../types/graph";

const STATE_TYPE_LABELS: Record<StateType, string> = {
  page: "Page",
  page_variant: "Page variant",
  modal: "Modal",
  form: "Form",
  auth_wall: "Auth wall",
  paywall: "Paywall",
  dropdown: "Dropdown",
  tab: "Tab",
  wizard_step: "Wizard step",
  error: "Error",
  dead_end: "Dead end",
  risky_terminal: "Risky terminal",
  external: "External",
};

export function stateTypeLabel(type: StateType | string): string {
  return STATE_TYPE_LABELS[type as StateType] ?? type;
}

export function shortTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour12: false });
}

export function truncate(value: string, max = 60): string {
  return value.length > max ? `${value.slice(0, max - 1)}\u2026` : value;
}

const STATUS_LABELS: Record<string, string> = {
  running: "Running",
  paused: "Paused — auth required",
  done: "Done",
  complete: "Complete",
  budget_limited: "Budget limited",
  novelty_exhausted: "Novelty exhausted",
  failed: "Failed",
  cancelled: "Cancelled",
  queued: "Queued",
};

export function runStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
