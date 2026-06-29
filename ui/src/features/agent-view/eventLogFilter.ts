import type { Counters } from "../../types/events";
import type { LogEntry } from "../runs/runState";

export type CounterFilterKey = keyof Counters | "frontier";

export function matchesCounterFilter(entry: LogEntry, filter: CounterFilterKey): boolean {
  switch (filter) {
    case "states":
    case "page_states":
    case "substates":
    case "interaction_nodes":
    case "surface_items_observed":
    case "interaction_capabilities":
      return entry.type === "state_discovered";
    case "edges":
      return entry.type === "edge_discovered" && entry.message.includes("Clicked");
    case "inferred_edges":
      return (
        (entry.type === "action_finished" && entry.message.startsWith("Inferred")) ||
        (entry.type === "edge_discovered" && entry.message.includes("Links to"))
      );
    case "frontier":
    case "surface_pending":
      return entry.type === "frontier_updated";
    case "actions_performed":
      return entry.type === "action_started";
    case "deduped":
      return (
        entry.type === "action_finished" &&
        entry.outcome === "deduped" &&
        !entry.message.startsWith("Inferred")
      );
    case "denied":
      return entry.type === "action_finished" && entry.outcome === "blocked";
    case "failed":
      return entry.type === "action_finished" && entry.outcome === "failed";
    case "stale_actions":
      return entry.type === "action_finished" && entry.outcome === "stale";
    case "replay_failed_actions":
      return entry.type === "action_finished" && entry.outcome === "replay_failed";
    case "known_state_actions":
      return entry.type === "action_finished" && entry.outcome === "known_state";
    case "unresolved_discovery_obligations":
    case "pending_representative_actions":
      return entry.type === "frontier_updated";
    default:
      return true;
  }
}

export function filterLogEntries(entries: LogEntry[], filter: CounterFilterKey | null): LogEntry[] {
  if (!filter) return entries;
  return entries.filter((entry) => matchesCounterFilter(entry, filter));
}
