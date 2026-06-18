// Idempotent run state: hydrate from /graph, then apply SSE events. Upserts are
// keyed by id and event sequence so replays (on reconnect) are no-ops.

import type {
  ActionFinishedPayload,
  ActionOutcome,
  ActionStartedPayload,
  AuthGatePayload,
  Counters,
  EdgeDiscoveredPayload,
  EventType,
  RunCompletedPayload,
  RunFailedPayload,
  RunStartedPayload,
  SSEEnvelope,
  StateDiscoveredPayload,
} from "../../types/events";
import type {
  GraphDocument,
  GraphEdge,
  GraphState,
  StateFlags,
  StateType,
  SurfaceItem,
} from "../../types/graph";

export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "closed" | "error";

export interface LogEntry {
  id: number;
  type: EventType;
  message: string;
  timestamp: string;
  outcome?: ActionOutcome;
}

export interface CurrentAction {
  label: string;
  fromIndex: number;
  selector: string;
  outcome?: ActionOutcome;
  reason?: string;
}

export interface AuthGateInfo {
  stateId: string;
  url: string;
  /** True if the server already tried autofill (Slice 6) and it didn't work. */
  autofillAttempted: boolean;
}

export interface RunState {
  runId: string | null;
  url: string;
  runStatus: string;
  connection: ConnectionStatus;
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  order: string[];
  counters: Counters;
  currentAction: CurrentAction | null;
  viewportStateId: string | null;
  stopReason: string | null;
  error: string | null;
  log: LogEntry[];
  config: RunStartedPayload["config"] | null;
  /** Non-null while the run is paused at an auth gate. */
  authGate: AuthGateInfo | null;
  /** Highest SSE sequence applied; reconnect history at or below this is ignored. */
  lastEventSequence: number;
}

export const emptyCounters: Counters = {
  states: 0,
  edges: 0,
  inferred_edges: 0,
  denied: 0,
  deduped: 0,
  noop: 0,
  failed: 0,
  actions_performed: 0,
  frontier_size: 0,
};

export const initialRunState: RunState = {
  runId: null,
  url: "",
  runStatus: "running",
  connection: "connecting",
  nodes: {},
  edges: {},
  order: [],
  counters: emptyCounters,
  currentAction: null,
  viewportStateId: null,
  stopReason: null,
  error: null,
  log: [],
  config: null,
  authGate: null,
  lastEventSequence: -1,
};

export type RunAction =
  | { type: "reset"; runId: string }
  | { type: "hydrate"; graph: GraphDocument }
  | { type: "sse"; envelope: SSEEnvelope }
  | { type: "streamOpen" }
  | { type: "streamReconnecting" }
  | { type: "streamClosed" }
  | { type: "hydrateFailed"; message: string }
  | { type: "terminalReconciled" }
  | { type: "authResolved" };

const LOG_CAP = 500;
const LOG_SKIP: EventType[] = ["heartbeat", "frontier_updated"];

function appendLog(log: LogEntry[], entry: LogEntry): LogEntry[] {
  const lastId = log.length ? log[log.length - 1].id : -1;
  if (entry.id <= lastId) return log; // replay of an already-applied event
  const next = [...log, entry];
  return next.length > LOG_CAP ? next.slice(next.length - LOG_CAP) : next;
}

function emptyNode(id: string): GraphState {
  return {
    id,
    type: "page",
    url: "",
    url_normalized: "",
    title: "",
    label: null,
    summary: null,
    fingerprint: "",
    depth: 0,
    screenshot: "",
    dom_snapshot: "",
    visible_ctas: [],
    surface_items: [],
    flags: {},
    path: [],
  };
}

function applyDiscovered(existing: GraphState | undefined, p: StateDiscoveredPayload): GraphState {
  const base = existing ?? emptyNode(p.state_id);
  // Live surface items arrive with provisional statuses; the terminal /graph
  // hydrate later overwrites them with the final explored/blocked/skipped set.
  const surfaceItems = p.surface_items
    ? (p.surface_items as unknown as SurfaceItem[])
    : base.surface_items;
  return {
    ...base,
    type: p.type as StateType,
    index: p.index,
    url: p.url,
    url_normalized: p.url_normalized,
    title: p.title || base.title,
    depth: p.depth,
    parent_state_id: p.parent_state_id ?? base.parent_state_id ?? null,
    screenshot: p.screenshot || base.screenshot,
    surface_items: surfaceItems,
    flags: { ...base.flags, ...((p.flags ?? {}) as StateFlags) },
  };
}

function countersFromStats(
  stats: Record<string, unknown>,
  nStates: number,
  nEdges: number,
): Counters {
  const num = (key: string, fallback: number): number =>
    typeof stats[key] === "number" ? (stats[key] as number) : fallback;
  return {
    states: num("states", nStates),
    edges: num("edges", nEdges),
    inferred_edges: num("inferred_edges", 0),
    denied: num("actions_denied", 0),
    deduped: num("dedup_hits", 0),
    noop: num("noop_actions", 0),
    failed: num("failed_actions", 0),
    actions_performed: num("actions_performed", 0),
    frontier_size: num("pending_actions", 0),
  };
}

function applyEvent(state: RunState, env: SSEEnvelope): RunState {
  const payload = env.payload as Record<string, unknown>;
  const counters: Counters = payload?.counters
    ? { ...state.counters, ...(payload.counters as Counters) }
    : state.counters;
  const connection: ConnectionStatus = "live";

  let { url, nodes, edges, order, viewportStateId, currentAction, runStatus, stopReason, error, config, authGate } =
    state;

  switch (env.type) {
    case "run_started": {
      const p = payload as unknown as RunStartedPayload;
      config = p.config ?? null;
      url = p.url || url;
      runStatus = "running";
      break;
    }
    case "state_discovered": {
      const p = payload as unknown as StateDiscoveredPayload;
      nodes = { ...nodes, [p.state_id]: applyDiscovered(nodes[p.state_id], p) };
      if (!state.nodes[p.state_id]) order = [...order, p.state_id];
      viewportStateId = p.state_id;
      break;
    }
    case "edge_discovered": {
      const p = payload as unknown as EdgeDiscoveredPayload;
      const sseEdge: GraphEdge = {
        id: p.edge_id,
        from: p.from,
        to: p.to,
        action: p.action,
        label: p.label,
        selector: p.selector,
        element_text: null,
        confidence: 0,
        collapsed_count: 1,
        via: p.via,
        surface_item_id: p.surface_item_id ?? null,
      };
      const existing = edges[p.edge_id];
      edges = { ...edges, [p.edge_id]: existing ? { ...sseEdge, ...existing } : sseEdge };
      break;
    }
    case "action_started": {
      const p = payload as unknown as ActionStartedPayload;
      currentAction = { label: p.label, fromIndex: p.from_index, selector: p.selector };
      break;
    }
    case "action_finished": {
      const p = payload as unknown as ActionFinishedPayload;
      if (currentAction) {
        currentAction = {
          ...currentAction,
          outcome: p.outcome,
          reason: typeof payload.message === "string" ? payload.message : undefined,
        };
      }
      break;
    }
    case "auth_gate": {
      const p = payload as unknown as AuthGatePayload;
      if (p.decision === null) {
        // Explorer just paused — show the prompt.
        runStatus = "paused";
        authGate = {
          stateId: p.state_id,
          url: p.url,
          autofillAttempted: p.autofill_attempted,
        };
      } else {
        // Auth resolved (autofilled or resumed) — clear the gate.
        authGate = null;
        runStatus = "running";
      }
      break;
    }
    case "run_completed": {
      const p = payload as unknown as RunCompletedPayload;
      runStatus = p.status || "done";
      stopReason = p.stop_reason ?? null;
      authGate = null;
      break;
    }
    case "run_failed": {
      const p = payload as unknown as RunFailedPayload;
      runStatus = "failed";
      stopReason = p.stop_reason ?? null;
      error = p.error || (typeof payload.message === "string" ? payload.message : "Run failed");
      authGate = null;
      break;
    }
    default:
      break; // frontier_updated/heartbeat (counters only), future event types
  }

  let log = state.log;
  if (!LOG_SKIP.includes(env.type) && typeof payload?.message === "string" && payload.message) {
    log = appendLog(log, {
      id: env.sequence,
      type: env.type,
      message: payload.message,
      timestamp: env.timestamp,
      outcome:
        env.type === "action_finished"
          ? (payload as unknown as ActionFinishedPayload).outcome
          : undefined,
    });
  }

  return {
    ...state,
    url,
    connection,
    counters,
    nodes,
    edges,
    order,
    viewportStateId,
    currentAction,
    runStatus,
    stopReason,
    error,
    config,
    authGate,
    log,
    lastEventSequence: env.sequence,
  };
}

function freshRunState(runId: string): RunState {
  return {
    ...initialRunState,
    runId,
    nodes: {},
    edges: {},
    order: [],
    counters: { ...emptyCounters },
    log: [],
  };
}

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case "reset":
      return freshRunState(action.runId);
    case "hydrate": {
      const { graph } = action;
      const nodes = { ...state.nodes };
      const order = [...state.order];
      for (const s of graph.states) {
        if (!nodes[s.id]) order.push(s.id);
        nodes[s.id] = { ...nodes[s.id], ...s };
      }
      const edges = { ...state.edges };
      for (const e of graph.edges) edges[e.id] = { ...edges[e.id], ...e };
      const counters = graph.run.stats
        ? countersFromStats(graph.run.stats, graph.states.length, graph.edges.length)
        : state.counters;
      return {
        ...state,
        url: graph.run.url || state.url,
        runStatus: graph.run.status || state.runStatus,
        nodes,
        edges,
        order,
        counters,
        viewportStateId:
          state.viewportStateId ?? (order.length ? order[order.length - 1] : null),
      };
    }
    case "sse":
      if (action.envelope.sequence <= state.lastEventSequence) return state;
      return applyEvent(state, action.envelope);
    case "streamOpen":
      return {
        ...state,
        connection: "live",
        error: state.runStatus === "failed" ? state.error : null,
      };
    case "streamReconnecting":
      return {
        ...state,
        connection: ["done", "failed", "cancelled"].includes(state.runStatus)
          ? state.connection
          : "reconnecting",
      };
    case "streamClosed": {
      const terminal = ["done", "failed", "cancelled"].includes(state.runStatus);
      if (state.order.length > 0 || terminal) {
        return { ...state, connection: "closed" };
      }
      return {
        ...state,
        connection: "error",
        error: state.error ?? "Run not found or no longer available.",
      };
    }
    case "hydrateFailed":
      return {
        ...state,
        connection: state.order.length > 0 ? state.connection : "error",
        error: action.message,
      };
    case "terminalReconciled":
      return { ...state, connection: "closed" };
    case "authResolved":
      return { ...state, authGate: null, runStatus: "running" };
    default:
      return state;
  }
}
