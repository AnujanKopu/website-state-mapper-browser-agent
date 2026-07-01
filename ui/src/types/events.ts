// Mirrors the backend SSE contract v1 (engine/events.py + api/manager.py).

import type { FamilyMetadata, NameMetadata, NavCapability, PageBox } from "./graph";

export type EventType =
  | "run_started"
  | "state_discovered"
  | "edge_discovered"
  | "state_updated"
  | "surface_items_discovered"
  | "action_started"
  | "action_finished"
  | "frontier_updated"
  | "auth_gate"
  | "state_labeled"
  | "run_completed"
  | "run_failed"
  | "heartbeat";

export const EVENT_TYPES: EventType[] = [
  "run_started",
  "state_discovered",
  "edge_discovered",
  "state_updated",
  "surface_items_discovered",
  "action_started",
  "action_finished",
  "frontier_updated",
  "auth_gate",
  "state_labeled",
  "run_completed",
  "run_failed",
  "heartbeat",
];

export const TERMINAL_EVENT_TYPES: EventType[] = ["run_completed", "run_failed"];

export type ActionOutcome =
  | "new_state"
  | "known_state"
  | "deduped"
  | "noop"
  | "stale"
  | "failed"
  | "replay_failed"
  | "blocked"
  | "explored";

export interface Counters {
  states: number;
  page_states?: number;
  substates?: number;
  interaction_nodes?: number;
  surface_items_observed?: number;
  interaction_capabilities?: number;
  edges: number;
  inferred_edges: number;
  denied: number;
  deduped: number;
  noop: number;
  failed: number;
  stale_actions?: number;
  replay_failed_actions?: number;
  known_state_actions?: number;
  unresolved_discovery_obligations?: number;
  pending_representative_actions?: number;
  actions_performed: number;
  frontier_size: number;
  surface_pending?: number;
}

export interface SurfaceItemLite {
  item_id: string | null;
  label: string;
  kind: string | null;
  region: string | null;
  fold: number;
  group_id: string | null;
  status: string;
  tag?: string | null;
  role?: string | null;
  href?: string | null;
  aria_selected?: boolean | null;
  aria_expanded?: boolean | null;
  aria_controls?: string | null;
  aria_haspopup?: string | null;
  aria_pressed?: boolean | null;
  checked?: boolean | null;
  placeholder?: string | null;
  name?: string | null;
  associated_label?: string | null;
  input_type?: string | null;
  required?: boolean;
  autocomplete?: string | null;
  form_action?: string | null;
  form_method?: string | null;
  control_key?: string;
  container_key?: string | null;
  container_type?: string | null;
  controlled_surface?: {
    id: string;
    role?: string | null;
    visible?: boolean | null;
  } | null;
  component_key?: string | null;
  component_label?: string | null;
  icon_label?: string | null;
  probe_reason?: string | null;
  interaction_scope?: "page_navigation" | "local_ui" | "external" | "unknown";
  execution_policy?: "navigate" | "probe_local" | "inventory_only" | "blocked";
  safety_category?: string | null;
  /** Captured document-space geometry used by the screenshot-grounded surface map. */
  page_box?: PageBox | null;
}

export interface BasePayload {
  message?: string;
  counters?: Counters;
}

export interface RunStartedPayload extends BasePayload {
  url: string;
  config: {
    max_states: number;
    max_actions: number;
    max_depth: number;
    max_wall_seconds: number;
    auth_mode?: "guest" | "login";
  };
}

export interface StateDiscoveredPayload extends BasePayload {
  state_id: string;
  index: number;
  url: string;
  url_normalized: string;
  title: string;
  type: string;
  depth: number;
  parent_state_id: string | null;
  screenshot: string;
  flags: Record<string, unknown>;
  denied_count: number;
  surface_items?: SurfaceItemLite[];
  route_family?: string | null;
  route_surface_key?: string | null;
  page_anchor_id?: string | null;
  variant_kind?: string | null;
  inherited_surface_state_id?: string | null;
  family_variant_key?: string | null;
  family_representative_state_id?: string | null;
  auth_context?: "guest" | "authenticated" | "unknown";
  page_role?: "home" | "hub" | "detail" | "results" | "flow_step" | "boundary";
  page_depth?: number;
  substate_depth?: number;
  return_state_id?: string | null;
  label?: string;
  name?: NameMetadata;
  family?: FamilyMetadata | null;
  nav_capabilities?: NavCapability[];
  surface_families?: FamilyMetadata[];
  evidence?: import("./graph").StateEvidence;
}

export interface EdgeDiscoveredPayload extends BasePayload {
  edge_id: string;
  from: string;
  to: string;
  from_index: number;
  to_index: number;
  action: string;
  label: string;
  selector: string;
  via: string;
  surface_item_id?: string | null;
  operation?: "created" | "updated";
  element_text?: string | null;
  confidence?: number;
  collapsed_count?: number;
  transition_key?: string;
  transition_kind?: string;
  scope?: "local" | "global_navigation";
  reversible?: boolean;
  provenance?: string[];
  evidence?: import("./graph").TransitionEvidence[];
}

export interface ActionStartedPayload extends BasePayload {
  from_state_id: string;
  from_index: number;
  label: string;
  selector: string;
  score: number;
}

export interface ActionFinishedPayload extends BasePayload {
  outcome: ActionOutcome;
  from_state_id?: string;
  to_state_id?: string;
}

export interface FrontierUpdatedPayload extends BasePayload {
  frontier_size: number;
  pending_actions: number;
}

export interface SurfaceItemsDiscoveredPayload extends BasePayload {
  state_id: string;
  surface_items: SurfaceItemLite[];
  exploration?: {
    explored?: number;
    pending?: number;
    blocked?: number;
    noop?: number;
    skipped_duplicate?: number;
    inventory_only?: number;
  };
}

export interface AuthGatePayload extends BasePayload {
  state_id: string;
  url: string;
  title: string;
  screenshot: string;
  /** null while pending, "resume"|"skip"|"autofilled" once resolved */
  decision: string | null;
  autofill_attempted: boolean;
  autofill_submitted?: boolean;
  observed_url?: string;
  suggested_actions: string[];
  post_auth_state_id?: string | null;
}

export interface RunCompletedPayload extends BasePayload {
  status: string;
  stop_reason: string | null;
  completion_status?: string | null;
  stats: Record<string, unknown>;
}

export interface RunFailedPayload extends BasePayload {
  error: string;
  stop_reason: string | null;
  completion_status?: string | null;
}

export interface SSEEnvelope<P = BasePayload> {
  event_id: string;
  run_id: string;
  sequence: number;
  timestamp: string;
  type: EventType;
  payload: P;
}
