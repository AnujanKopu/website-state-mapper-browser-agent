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
  FrontierUpdatedPayload,
  RunCompletedPayload,
  RunFailedPayload,
  RunStartedPayload,
  SSEEnvelope,
  StateDiscoveredPayload,
  SurfaceItemsDiscoveredPayload,
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
  completionStatus: string | null;
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
  surface_pending: 0,
};

export const initialRunState: RunState = {
  runId: null,
  url: "",
  runStatus: "running",
  completionStatus: null,
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
const LOG_SKIP: EventType[] = ["heartbeat"];

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

function mergeSurfaceItems(
  existing: SurfaceItem[] | undefined,
  incoming: SurfaceItem[],
): SurfaceItem[] {
  if (!existing?.length) return incoming;
  const byId = new Map(
    existing
      .filter((item) => item.item_id)
      .map((item) => [item.item_id, item] as const),
  );
  return incoming.map((item) => {
    const previous = item.item_id ? byId.get(item.item_id) : undefined;
    return previous ? { ...previous, ...item } : item;
  });
}

function applyDiscovered(existing: GraphState | undefined, p: StateDiscoveredPayload): GraphState {
  const base = existing ?? emptyNode(p.state_id);
  // Live surface items arrive with provisional statuses; the terminal /graph
  // hydrate later overwrites them with the final explored/blocked/skipped set.
  const surfaceItems = p.surface_items
    ? mergeSurfaceItems(base.surface_items, p.surface_items as unknown as SurfaceItem[])
    : base.surface_items;
  return {
    ...base,
    type: p.type as StateType,
    index: p.index,
    url: p.url,
    url_normalized: p.url_normalized,
    title: p.title || base.title,
    label: p.label ?? base.label,
    depth: p.depth,
    parent_state_id: p.parent_state_id ?? base.parent_state_id ?? null,
    screenshot: p.screenshot || base.screenshot,
    surface_items: surfaceItems,
    exploration: {
      ...base.exploration,
      ...(p.route_family ? { route_family: p.route_family } : {}),
      ...(p.route_surface_key ? { route_surface_key: p.route_surface_key } : {}),
      ...(p.page_anchor_id ? { page_anchor_id: p.page_anchor_id } : {}),
      ...(p.variant_kind ? { variant_kind: p.variant_kind } : {}),
      ...(p.inherited_surface_state_id
        ? { inherited_surface_state_id: p.inherited_surface_state_id }
        : {}),
      ...(p.family_variant_key ? { family_variant_key: p.family_variant_key } : {}),
      ...(p.family_representative_state_id
        ? { family_representative_state_id: p.family_representative_state_id }
        : {}),
      ...(p.auth_context ? { auth_context: p.auth_context } : {}),
      ...(p.page_role ? { page_role: p.page_role } : {}),
      ...(typeof p.page_depth === "number" ? { page_depth: p.page_depth } : {}),
      ...(typeof p.substate_depth === "number" ? { substate_depth: p.substate_depth } : {}),
      ...(p.return_state_id ? { return_state_id: p.return_state_id } : {}),
      ...(p.name ? { name: p.name } : {}),
      ...(p.family ? { family: p.family } : {}),
      ...(p.nav_capabilities ? { nav_capabilities: p.nav_capabilities } : {}),
      ...(p.surface_families ? { surface_families: p.surface_families } : {}),
    },
    flags: { ...base.flags, ...((p.flags ?? {}) as StateFlags) },
    evidence: p.evidence ?? base.evidence,
  };
}

function countersFromStats(
  stats: Record<string, unknown>,
  nStates: number,
  nEdges: number,
  nPages: number,
  nInteractions: number,
): Counters {
  const num = (key: string, fallback: number): number =>
    typeof stats[key] === "number" ? (stats[key] as number) : fallback;
  return {
    states: num("states", nStates),
    page_states: num("page_states", nPages),
    substates: num("substates", nStates - nPages),
    interaction_nodes: num("interaction_nodes", nInteractions),
    edges: num("edges", nEdges),
    inferred_edges: num("inferred_edges", 0),
    denied: num("actions_denied", 0),
    deduped: num("dedup_hits", 0),
    noop: num("noop_actions", 0),
    failed: num("failed_actions", 0),
    actions_performed: num("actions_performed", 0),
    frontier_size: num("frontier_actions", num("pending_actions", 0)),
    surface_pending: num("surface_pending_items", 0),
  };
}

function applyEvent(state: RunState, env: SSEEnvelope): RunState {
  const payload = env.payload as Record<string, unknown>;
  const counters: Counters = payload?.counters
    ? { ...state.counters, ...(payload.counters as Counters) }
    : state.counters;
  const connection: ConnectionStatus = "live";

  let {
    url,
    nodes,
    edges,
    order,
    viewportStateId,
    currentAction,
    runStatus,
    completionStatus,
    stopReason,
    error,
    config,
    authGate,
  } =
    state;

  switch (env.type) {
    case "run_started": {
      const p = payload as unknown as RunStartedPayload;
      config = p.config ?? null;
      url = p.url || url;
      runStatus = "running";
      completionStatus = null;
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
        element_text: p.element_text ?? null,
        confidence: p.confidence ?? 0,
        collapsed_count: p.collapsed_count ?? 1,
        via: p.via,
        surface_item_id: p.surface_item_id ?? null,
        transition_key: p.transition_key,
        transition_kind: p.transition_kind as GraphEdge["transition_kind"],
        scope: p.scope,
        reversible: p.reversible ?? false,
        provenance: p.provenance ?? [p.via],
        evidence: p.evidence ?? [],
      };
      const existing = edges[p.edge_id];
      edges = { ...edges, [p.edge_id]: existing ? { ...existing, ...sseEdge } : sseEdge };
      break;
    }
    case "state_updated": {
      const p = env.payload as Partial<StateDiscoveredPayload> & { state_id: string };
      const existing = nodes[p.state_id];
      if (!existing) break;
      nodes = {
        ...nodes,
        [p.state_id]: {
          ...existing,
          ...(p.type ? { type: p.type as StateType } : {}),
          ...(p.parent_state_id !== undefined
            ? { parent_state_id: p.parent_state_id }
            : {}),
          exploration: {
            ...existing.exploration,
            ...(p.route_family ? { route_family: p.route_family } : {}),
            ...(p.page_role ? { page_role: p.page_role } : {}),
            ...(p.page_anchor_id ? { page_anchor_id: p.page_anchor_id } : {}),
            ...(p.variant_kind ? { variant_kind: p.variant_kind } : {}),
            ...(p.family_variant_key ? { family_variant_key: p.family_variant_key } : {}),
            ...(p.family_representative_state_id
              ? { family_representative_state_id: p.family_representative_state_id }
              : {}),
            ...(p.family ? { family: p.family } : {}),
            ...(p.surface_families ? { surface_families: p.surface_families } : {}),
          },
        },
      };
      break;
    }
    case "surface_items_discovered": {
      const p = payload as unknown as SurfaceItemsDiscoveredPayload;
      const existing = nodes[p.state_id];
      if (existing) {
        nodes = {
          ...nodes,
          [p.state_id]: {
            ...existing,
            surface_items: mergeSurfaceItems(
              existing.surface_items,
              p.surface_items as unknown as SurfaceItem[],
            ),
            exploration: { ...existing.exploration, ...(p.exploration ?? {}) },
          },
        };
      }
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
      completionStatus = p.completion_status ?? null;
      stopReason = p.stop_reason ?? null;
      authGate = null;
      break;
    }
    case "run_failed": {
      const p = payload as unknown as RunFailedPayload;
      runStatus = "failed";
      completionStatus = p.completion_status ?? "failed";
      stopReason = p.stop_reason ?? null;
      error = p.error || (typeof payload.message === "string" ? payload.message : "Run failed");
      authGate = null;
      break;
    }
    default:
      break; // frontier_updated/heartbeat (counters only), future event types
  }

  let log = state.log;
  if (!LOG_SKIP.includes(env.type)) {
    let message: string | undefined;
    if (typeof payload?.message === "string" && payload.message) {
      message = payload.message;
    } else if (env.type === "frontier_updated") {
      const pending = (payload as unknown as FrontierUpdatedPayload).pending_actions;
      message = `${pending} action${pending === 1 ? "" : "s"} pending`;
    }
    if (message) {
      log = appendLog(log, {
        id: env.sequence,
        type: env.type,
        message,
        timestamp: env.timestamp,
        outcome:
          env.type === "action_finished"
            ? (payload as unknown as ActionFinishedPayload).outcome
            : undefined,
      });
    }
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
    completionStatus,
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
    completionStatus: null,
    log: [],
  };
}

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case "reset":
      return freshRunState(action.runId);
    case "hydrate": {
      const { graph } = action;
      if (state.runId && graph.run.id !== state.runId) return state;
      const snapshotSequence = graph.sync?.snapshot_sequence;
      const snapshotIsCurrent =
        graph.sync === undefined
        || graph.sync.authoritative
        || snapshotSequence === undefined
        || snapshotSequence === null
        || snapshotSequence >= state.lastEventSequence;
      // A terminal authoritative graph is a replacement snapshot.  Merging it
      // leaves provisional family members and stale SSE-only edges visible.
      const authoritative = graph.sync?.authoritative === true;
      let nodes = authoritative ? {} : snapshotIsCurrent ? { ...state.nodes } : state.nodes;
      let order = authoritative ? [] : snapshotIsCurrent ? [...state.order] : state.order;
      for (const s of graph.states) {
        if (!nodes[s.id]) {
          if (nodes === state.nodes) nodes = { ...state.nodes };
          if (order === state.order) order = [...state.order];
          order.push(s.id);
        }
        if (!nodes[s.id] || snapshotIsCurrent) {
          nodes[s.id] = { ...nodes[s.id], ...s };
        }
      }
      let edges = authoritative ? {} : snapshotIsCurrent ? { ...state.edges } : state.edges;
      for (const e of graph.edges) {
        if (!edges[e.id]) {
          if (edges === state.edges) edges = { ...state.edges };
          edges[e.id] = e;
        } else if (snapshotIsCurrent) {
          edges[e.id] = { ...edges[e.id], ...e };
        }
      }
      if (order !== state.order) {
        const originalPosition = new Map(order.map((id, index) => [id, index]));
        order.sort((a, b) => {
          const aIndex = nodes[a]?.index;
          const bIndex = nodes[b]?.index;
          if (typeof aIndex === "number" && typeof bIndex === "number") return aIndex - bIndex;
          if (typeof aIndex === "number") return -1;
          if (typeof bIndex === "number") return 1;
          return (originalPosition.get(a) ?? 0) - (originalPosition.get(b) ?? 0);
        });
      }
      const counters = graph.run.stats
        && snapshotIsCurrent
        ? countersFromStats(
          graph.run.stats,
          Object.keys(nodes).length,
          Object.keys(edges).length,
          Object.values(nodes).filter((node) => !node.parent_state_id).length,
          Object.values(nodes).reduce(
            (total, node) => total + (node.surface_items ?? []).filter(
              (item) => item.interaction_scope !== "page_navigation",
            ).length,
            0,
          ),
        )
        : state.counters;
      return {
        ...state,
        url: graph.run.url || state.url,
        runStatus: snapshotIsCurrent ? (graph.run.status || state.runStatus) : state.runStatus,
        completionStatus:
          snapshotIsCurrent && typeof graph.run.stats?.completion_status === "string"
            ? (graph.run.stats.completion_status as string)
            : state.completionStatus,
        nodes,
        edges,
        order,
        counters,
        viewportStateId: snapshotIsCurrent && graph.sync?.latest_state_id
          ? graph.sync.latest_state_id
          : state.viewportStateId ?? (order.length ? order[order.length - 1] : null),
        lastEventSequence:
          snapshotIsCurrent && typeof snapshotSequence === "number"
            ? Math.max(state.lastEventSequence, snapshotSequence)
            : state.lastEventSequence,
      };
    }
    case "sse":
      if (state.runId && action.envelope.run_id !== state.runId) return state;
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
      return { ...state, authGate: null, runStatus: "running", completionStatus: null };
    default:
      return state;
  }
}
