import { describe, expect, it } from "vitest";

import type { LogEntry } from "../runs/runState";
import { filterLogEntries, matchesCounterFilter } from "./eventLogFilter";

function entry(
  partial: Partial<LogEntry> & Pick<LogEntry, "type" | "message">,
): LogEntry {
  return {
    id: 1,
    timestamp: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

describe("eventLogFilter", () => {
  it("filters states, edges, and inferred events", () => {
    const entries: LogEntry[] = [
      entry({ id: 1, type: "state_discovered", message: "Found page" }),
      entry({ id: 2, type: "edge_discovered", message: "s0 -> s1: Clicked 'Pricing'" }),
      entry({ id: 3, type: "edge_discovered", message: "s0 -> s2: Links to 'Home'" }),
      entry({ id: 4, type: "action_finished", message: "Inferred 'Home' -> known s2", outcome: "deduped" }),
    ];

    expect(filterLogEntries(entries, "states")).toHaveLength(1);
    expect(filterLogEntries(entries, "edges")).toHaveLength(1);
    expect(filterLogEntries(entries, "inferred_edges")).toHaveLength(2);
  });

  it("filters action outcomes without mixing inferred dedupes", () => {
    const entries: LogEntry[] = [
      entry({ id: 1, type: "action_started", message: "Click Pricing" }),
      entry({ id: 2, type: "action_finished", message: "Deduped to s3", outcome: "deduped" }),
      entry({ id: 3, type: "action_finished", message: "Inferred 'Home' -> known s2", outcome: "deduped" }),
      entry({ id: 4, type: "action_finished", message: "Blocked payment", outcome: "blocked" }),
      entry({ id: 5, type: "action_finished", message: "Click failed", outcome: "failed" }),
    ];

    expect(filterLogEntries(entries, "actions_performed")).toHaveLength(1);
    expect(filterLogEntries(entries, "deduped")).toHaveLength(1);
    expect(filterLogEntries(entries, "denied")).toHaveLength(1);
    expect(filterLogEntries(entries, "failed")).toHaveLength(1);
    expect(matchesCounterFilter(entries[2], "deduped")).toBe(false);
  });

  it("returns all entries when filter is cleared", () => {
    const entries = [entry({ type: "state_discovered", message: "Found page" })];
    expect(filterLogEntries(entries, null)).toEqual(entries);
  });
});
