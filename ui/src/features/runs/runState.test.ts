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
  it("replaces stale live entities with an authoritative snapshot", () => {
    const live = runReducer(resetState(), {
      type: "hydrate",
      graph: graph([graphState("kept", 0), graphState("provisional", 1)]),
    });
    const authoritative = {
      ...graph([graphState("kept", 0, { title: "Final" })], { status: "done" }),
      sync: {
        schema_version: 4,
        snapshot_sequence: 5,
        authoritative: true,
        latest_state_id: "kept",
      },
    } satisfies GraphDocument;

    const hydrated = runReducer(live, { type: "hydrate", graph: authoritative });

    expect(Object.keys(hydrated.nodes)).toEqual(["kept"]);
    expect(hydrated.nodes.kept.title).toBe("Final");
    expect(hydrated.order).toEqual(["kept"]);
  });

  it("applies authoritative live surface status updates", () => {
    const discovered = runReducer(resetState(), {
      type: "sse",
      envelope: event(1, "state_discovered", {
        state_id: "s0",
        index: 0,
        url: "https://example.com",
        url_normalized: "https://example.com",
        title: "Home",
        type: "page",
        depth: 0,
        parent_state_id: null,
        screenshot: "",
        flags: {},
        denied_count: 0,
        surface_items: [{
          item_id: "chart",
          label: "Chart",
          kind: "button",
          region: "main",
          fold: 0,
          group_id: null,
          status: "pending",
        }],
      }),
    });
    const updated = runReducer(discovered, {
      type: "sse",
      envelope: event(2, "surface_items_discovered", {
        state_id: "s0",
        surface_items: [{
          item_id: "chart",
          label: "Chart",
          kind: "button",
          region: "main",
          fold: 0,
          group_id: null,
          status: "explored",
          execution_policy: "probe_local",
        }],
        exploration: { explored: 1, pending: 0 },
      }),
    });

    expect(updated.nodes.s0.surface_items?.[0].status).toBe("explored");
    expect(updated.nodes.s0.exploration?.pending).toBe(0);
  });

  it("preserves hydrated control geometry across geometry-free live status updates", () => {
    const hydratedGraph = graph([graphState("s0", 0, {
      surface_items: [{
        item_id: "chart",
        label: "Chart",
        kind: "button",
        region: "main",
        fold: 0,
        group_id: null,
        status: "pending",
        page_box: { x: 12, y: 24, width: 160, height: 40 },
      }],
    })]);
    hydratedGraph.sync = {
      schema_version: 4,
      snapshot_sequence: 4,
      authoritative: false,
      latest_state_id: "s0",
    };
    const hydrated = runReducer(resetState(), { type: "hydrate", graph: hydratedGraph });
    const updated = runReducer(hydrated, {
      type: "sse",
      envelope: event(5, "surface_items_discovered", {
        state_id: "s0",
        surface_items: [{
          item_id: "chart",
          label: "Chart",
          kind: "button",
          region: "main",
          fold: 0,
          group_id: null,
          status: "explored",
        }],
      }),
    });

    expect(hydrated.lastEventSequence).toBe(4);
    expect(updated.nodes.s0.surface_items?.[0]).toMatchObject({
      status: "explored",
      page_box: { x: 12, y: 24, width: 160, height: 40 },
    });
  });

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

  it("adds missing entities from a stale snapshot without overwriting newer live data", () => {
    const live = runReducer(resetState(), {
      type: "sse",
      envelope: event(4, "state_discovered", {
        state_id: "s1",
        index: 1,
        url: "https://example.com/one",
        url_normalized: "https://example.com/one",
        title: "Newest live title",
        type: "page",
        depth: 1,
        parent_state_id: null,
        screenshot: "live.png",
        flags: {},
        denied_count: 0,
      }),
    });
    const stale = graph([graphState("s1", 1, { title: "Stale title" }), graphState("s2", 2)]);
    stale.sync = {
      schema_version: 4,
      snapshot_sequence: 2,
      authoritative: false,
      latest_state_id: "s2",
    };

    const hydrated = runReducer(live, { type: "hydrate", graph: stale });
    expect(hydrated.nodes.s1.title).toBe("Newest live title");
    expect(hydrated.nodes.s2.title).toBe("State 2");
    expect(hydrated.viewportStateId).toBe("s1");
    expect(hydrated.order).toEqual(["s1", "s2"]);
  });

  it("uses a current snapshot's latest state for foreground catch-up", () => {
    const live = runReducer(resetState(), {
      type: "sse",
      envelope: event(1, "state_discovered", {
        state_id: "s1",
        index: 1,
        url: "https://example.com/one",
        url_normalized: "https://example.com/one",
        title: "One",
        type: "page",
        depth: 1,
        parent_state_id: null,
        screenshot: "",
        flags: {},
        denied_count: 0,
      }),
    });
    const current = graph([graphState("s1", 1), graphState("s2", 2)]);
    current.sync = {
      schema_version: 4,
      snapshot_sequence: 3,
      authoritative: false,
      latest_state_id: "s2",
    };

    const hydrated = runReducer(live, { type: "hydrate", graph: current });
    expect(hydrated.viewportStateId).toBe("s2");
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
          frontier_actions: 3,
          surface_pending_items: 17,
          failed_actions: 2,
          completion_status: "budget_limited",
        },
      }),
    });
    const closed = runReducer(finished, { type: "terminalReconciled" });

    expect(started.url).toBe("https://example.com");
    expect(finished.counters.frontier_size).toBe(3);
    expect(finished.counters.surface_pending).toBe(17);
    expect(finished.counters.failed).toBe(2);
    expect(finished.completionStatus).toBe("budget_limited");
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

  it("converges after replaying pending and authoritative resolved auth events", () => {
    const pending = event(3, "auth_gate", {
      state_id: "s-auth",
      url: "https://example.com/login",
      title: "Login",
      screenshot: "",
      decision: null,
      autofill_attempted: false,
      suggested_actions: ["resume", "skip"],
    });
    const resolution = event(4, "auth_gate", {
      ...pending.payload,
      decision: "resume",
      suggested_actions: [],
    });

    const gated = runReducer(resetState(), { type: "sse", envelope: pending });
    const resolved = runReducer(gated, { type: "sse", envelope: resolution });
    const replayed = runReducer(resolved, { type: "sse", envelope: resolution });

    expect(gated.authGate).not.toBeNull();
    expect(resolved.authGate).toBeNull();
    expect(resolved.runStatus).toBe("running");
    expect(replayed).toBe(resolved);
  });

  it("applies live route-family metadata to discovered states", () => {
    const discovered = event(1, "state_discovered", {
      state_id: "game-a",
      index: 1,
      url: "https://example.com/game/1/a",
      url_normalized: "https://example.com/game/:id/a",
      title: "Game A",
      type: "page",
      depth: 1,
      parent_state_id: null,
      screenshot: "",
      flags: {},
      denied_count: 0,
      route_family: "https://example.com/game/:id/:param",
    });

    const next = runReducer(resetState(), { type: "sse", envelope: discovered });
    expect(next.nodes["game-a"].exploration?.route_family).toBe(
      "https://example.com/game/:id/:param",
    );
  });

  it("upgrades an inferred edge in place and converges with terminal hydration", () => {
    const inferred = event(1, "edge_discovered", {
      edge_id: "edge-stable",
      operation: "created",
      from: "a",
      to: "b",
      from_index: 0,
      to_index: 1,
      action: "click",
      label: "Open Pricing",
      selector: "#pricing",
      via: "inferred",
      transition_key: "transition-stable",
      transition_kind: "link",
      scope: "global_navigation",
      reversible: false,
      provenance: ["inferred"],
      evidence: [{ mode: "inferred", validated: false }],
    });
    const performed = event(2, "edge_discovered", {
      ...inferred.payload,
      operation: "updated",
      via: "performed",
      provenance: ["inferred", "performed"],
      evidence: [
        { mode: "inferred", validated: false },
        { mode: "performed", validated: true },
      ],
    });

    const live = runReducer(
      runReducer(resetState(), { type: "sse", envelope: inferred }),
      { type: "sse", envelope: performed },
    );
    expect(Object.keys(live.edges)).toEqual(["edge-stable"]);
    expect(live.edges["edge-stable"].via).toBe("performed");
    expect(live.edges["edge-stable"].provenance).toEqual(["inferred", "performed"]);

    const authoritative: GraphDocument = {
      ...graph([graphState("a", 0), graphState("b", 1)], { status: "done" }),
      edges: [live.edges["edge-stable"]],
    };
    const hydrated = runReducer(live, { type: "hydrate", graph: authoritative });
    expect(hydrated.edges).toEqual(live.edges);
    expect(Object.keys(hydrated.edges)).toHaveLength(1);
  });
});
