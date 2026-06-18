import { describe, expect, it } from "vitest";

import type { SSEEnvelope } from "../../types/events";
import type { GraphDocument, GraphState } from "../../types/graph";
import { initialRunState, runReducer } from "./runState";

function graphState(id: string, index: number, overrides: Partial<GraphState> = {}): GraphState {
  return {
    id,
    index,
    type: "page",
    url: `https://example.com/${id}`,
    url_normalized: `https://example.com/${id}`,
    title: `State ${index}`,
    label: null,
    summary: null,
    fingerprint: id,
    depth: index,
    screenshot: "",
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
    ...overrides,
  };
}

function graph(states: GraphState[], overrides: Partial<GraphDocument["run"]> = {}): GraphDocument {
  return {
    run: {
      id: "run-1",
      url: "https://example.com",
      status: "running",
      stats: null,
      started_at: null,
      finished_at: null,
      ...overrides,
    },
    states,
    edges: [],
  };
}

function event(sequence: number, type: SSEEnvelope["type"], payload: object): SSEEnvelope {
  return {
    event_id: `event-${sequence}`,
    run_id: "run-1",
    sequence,
    timestamp: "2026-06-18T12:00:00Z",
    type,
    payload,
  } as SSEEnvelope;
}

function resetState() {
  return runReducer(initialRunState, { type: "reset", runId: "run-1" });
}

describe("runReducer", () => {
  it("deduplicates replayed and out-of-order SSE envelopes", () => {
    const discovered = event(5, "state_discovered", {
      state_id: "s1",
      index: 1,
      url: "https://example.com/one",
      url_normalized: "https://example.com/one",
      title: "Original",
      type: "page",
      depth: 1,
      parent_state_id: null,
      screenshot: "",
      flags: {},
      denied_count: 0,
      message: "Discovered original",
    });
    const applied = runReducer(resetState(), { type: "sse", envelope: discovered });
    const replayed = runReducer(applied, {
      type: "sse",
      envelope: event(5, "state_discovered", {
        ...discovered.payload,
        title: "Replay should be ignored",
      }),
    });
    const older = runReducer(replayed, {
      type: "sse",
      envelope: event(4, "run_started", { url: "https://stale.example", config: {} }),
    });

    expect(older).toBe(replayed);
    expect(older.nodes.s1.title).toBe("Original");
    expect(older.lastEventSequence).toBe(5);
    expect(older.log).toHaveLength(1);
  });

  it("merges persisted detail over live placeholders without dropping live-only nodes", () => {
    const live = runReducer(resetState(), {
      type: "sse",
      envelope: event(1, "state_discovered", {
        state_id: "s1",
        index: 1,
        url: "https://example.com/one",
        url_normalized: "https://example.com/one",
        title: "Live title",
        type: "page",
        depth: 1,
        parent_state_id: null,
        screenshot: "live.png",
        flags: {},
        denied_count: 0,
      }),
    });
    const withSecondLiveNode = runReducer(live, {
      type: "sse",
      envelope: event(2, "state_discovered", {
        state_id: "s2",
        index: 2,
        url: "https://example.com/two",
        url_normalized: "https://example.com/two",
        title: "Newer live node",
        type: "page",
        depth: 2,
        parent_state_id: null,
        screenshot: "",
        flags: {},
        denied_count: 0,
      }),
    });
    const hydrated = runReducer(withSecondLiveNode, {
      type: "hydrate",
      graph: graph([graphState("s1", 1, { summary: "Persisted summary", title: "Saved" })]),
    });

    expect(hydrated.nodes.s1.title).toBe("Saved");
    expect(hydrated.nodes.s1.summary).toBe("Persisted summary");
    expect(hydrated.nodes.s2.title).toBe("Newer live node");
    expect(hydrated.order).toEqual(["s1", "s2"]);
    expect(hydrated.lastEventSequence).toBe(2);
  });

  it("applies run metadata, terminal stats, and explicit connection transitions", () => {
    const started = runReducer(resetState(), {
      type: "sse",
      envelope: event(0, "run_started", {
        url: "https://example.com",
        config: { max_states: 10, max_actions: 20, max_depth: 3, max_wall_seconds: 60 },
      }),
    });
    const finished = runReducer(started, {
      type: "hydrate",
      graph: graph([graphState("s0", 0)], {
        status: "done",
        stats: {
          states: 1,
          edges: 0,
          pending_actions: 17,
          failed_actions: 2,
        },
      }),
    });
    const closed = runReducer(finished, { type: "terminalReconciled" });

    expect(started.url).toBe("https://example.com");
    expect(finished.counters.frontier_size).toBe(17);
    expect(finished.counters.failed).toBe(2);
    expect(closed.connection).toBe("closed");

    const retrying = runReducer(started, { type: "streamReconnecting" });
    expect(retrying.connection).toBe("reconnecting");
    expect(runReducer(retrying, { type: "streamOpen" }).connection).toBe("live");
  });

  it("clears a resolved auth gate immediately", () => {
    const gated = runReducer(resetState(), {
      type: "sse",
      envelope: event(3, "auth_gate", {
        state_id: "s-auth",
        url: "https://example.com/login",
        title: "Login",
        screenshot: "",
        decision: null,
        autofill_attempted: false,
        suggested_actions: ["resume", "skip"],
      }),
    });
    const resolved = runReducer(gated, { type: "authResolved" });

    expect(gated.runStatus).toBe("paused");
    expect(resolved.authGate).toBeNull();
    expect(resolved.runStatus).toBe("running");
  });
});
