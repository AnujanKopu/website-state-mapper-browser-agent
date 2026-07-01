// Mirrors the backend graph export (engine/export.py) and run schemas.

export type StateType =
  | "page"
  | "page_variant"
  | "modal"
  | "form"
  | "auth_wall"
  | "paywall"
  | "dropdown"
  | "tab"
  | "wizard_step"
  | "error"
  | "dead_end"
  | "risky_terminal"
  | "external";

export interface DeniedAction {
  label: string;
  category: string | null;
  reason: string;
}

export interface StateFlags {
  modal_open?: boolean;
  auth_required?: boolean;
  payment_required?: boolean;
  form_count?: number;
  dead_end?: boolean;
  risky_terminal?: boolean;
  denied_actions?: DeniedAction[];
  [key: string]: unknown;
}

export interface ActionStep {
  kind: string;
  url?: string | null;
  selector?: string | null;
  label?: string | null;
  role?: string | null;
  href?: string | null;
  control_key?: string | null;
  locator?: Record<string, unknown> | null;
}

export type SurfaceStatus =
  | "pending"
  | "explored"
  | "blocked"
  | "noop"
  | "known_state"
  | "stale"
  | "failed"
  | "replay_failed"
  | "skipped_duplicate"
  | "inventory_only";

export interface PageBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface SurfaceItem {
  item_id: string | null;
  label: string;
  kind: string | null;
  region: string | null;
  fold: number;
  group_id: string | null;
  status: SurfaceStatus;
  href?: string | null;
  tag?: string | null;
  role?: string | null;
  in_nav?: boolean;
  in_form?: boolean;
  in_modal?: boolean;
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
  /** Existing graph-export geometry in full-page screenshot coordinates. */
  page_box?: PageBox | null;
}

export interface ExplorationSummary {
  explored?: number;
  pending?: number;
  blocked?: number;
  noop?: number;
  skipped_duplicate?: number;
  visit_status?: "fully_explored" | "partially_explored";
  route_family?: string;
  route_surface_key?: string;
  page_anchor_id?: string;
  variant_kind?: string;
  inherited_surface_state_id?: string;
  family_variant_key?: string;
  family_representative_state_id?: string;
  family_sampled?: number;
  family_skipped?: number;
  auth_context?: "guest" | "authenticated" | "unknown";
  page_role?: "home" | "hub" | "detail" | "results" | "flow_step" | "boundary";
  page_depth?: number;
  substate_depth?: number;
  name?: NameMetadata;
  family?: FamilyMetadata;
  nav_capabilities?: NavCapability[];
  surface_families?: FamilyMetadata[];
  return_state_id?: string;
}

export interface NameMetadata {
  text: string;
  source: "heuristic" | "llm" | "user";
  confidence: number;
  key: string;
}

export interface FamilyMetadata {
  id: string;
  label: string;
  kind: string;
  pattern: string;
  label_source: string;
  confidence: number;
  discovered_count: number;
  checked_count?: number;
  represented_count?: number;
  skipped_count?: number;
  sample_labels?: string[];
  sample_urls?: string[];
  status?: "provisional" | "confirmed" | "rejected";
  support_sources?: string[];
  variant_count?: number;
  dynamic_slots?: string[];
  family_kind?: "entity_family" | "collection_variant_family" | string;
  sample_state_ids?: string[];
  variant_state_ids?: string[];
  required_samples?: number;
  completed_samples?: number;
  deferred_samples?: number;
}

export interface NavCapability {
  id: string;
  label: string;
  kind?: string | null;
  href?: string | null;
  target_url?: string | null;
  target_state_id?: string | null;
  region?: string | null;
  control_key?: string;
  container_key?: string | null;
  surface_item_id?: string | null;
}

export interface GraphState {
  id: string;
  type: StateType;
  index?: number;
  url: string;
  url_normalized: string;
  title: string;
  label: string | null;
  summary: string | null;
  fingerprint: string;
  depth: number;
  parent_state_id?: string | null;
  screenshot: string;
  dom_snapshot: string;
  visible_ctas: string[];
  surface_items?: SurfaceItem[];
  exploration?: ExplorationSummary;
  evidence?: StateEvidence;
  flags: StateFlags;
  path: ActionStep[];
}

export interface GraphEdge {
  id: string;
  from: string;
  to: string;
  action: string;
  label: string;
  selector: string;
  element_text: string | null;
  confidence: number;
  collapsed_count: number;
  via?: string;
  surface_item_id?: string | null;
  transition_key?: string;
  transition_kind?: "link" | "tab" | "open" | "close" | "cancel" | "back" | "return" | "auth" | "control";
  scope?: "local" | "global_navigation";
  reversible?: boolean;
  provenance?: string[];
  evidence?: TransitionEvidence[];
}

export interface TransitionEvidence {
  mode: string;
  surface_item_id?: string | null;
  selector?: string;
  href?: string | null;
  region?: string | null;
  control_key?: string;
  container_key?: string | null;
  mechanism?: string;
  expected_state_id?: string;
  restored_state_id?: string | null;
  validated?: boolean;
}

export interface RunInfo {
  id: string;
  url: string;
  status: string;
  stats: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface StateEvidence {
  page?: {
    language?: string | null;
    canonical_url?: string | null;
    viewport?: { width: number; height: number; dpr: number };
    document?: { width: number; height: number };
  };
  forms?: Array<{
    label?: string | null;
    method: string;
    action?: string | null;
    fields: Array<{
      label?: string | null;
      tag: string;
      type?: string | null;
      name?: string | null;
      required: boolean;
      autocomplete?: string | null;
    }>;
  }>;
  visuals?: Array<{
    kind: string;
    label?: string | null;
    width: number;
    height: number;
  }>;
  substate_hints?: Array<{
    surface_item_id?: string | null;
    label: string;
    kind?: string | null;
    controls?: string | null;
    popup?: string | null;
    expanded?: boolean | null;
  }>;
}

export interface GraphSync {
  schema_version: number;
  /** Lower-bound SSE sequence represented by this database snapshot. */
  snapshot_sequence: number | null;
  /** True for terminal or persisted runs that have no newer live authority. */
  authoritative: boolean;
  latest_state_id: string | null;
}

export interface GraphDocument {
  run: RunInfo;
  states: GraphState[];
  edges: GraphEdge[];
  sync?: GraphSync;
}

export interface CreateRunResponse {
  run_id: string;
  url: string;
  status: string;
  events_url: string;
  graph_url: string;
}

export interface RunStatusResponse {
  run_id: string;
  url: string;
  status: string;
  error: string | null;
  stats: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface CreateRunInput {
  url: string;
  auth_mode?: "guest" | "login";
  credentials?: { username: string; password: string };
  max_states?: number;
  max_actions?: number;
  max_depth?: number;
  save_dom_snapshots?: boolean;
}
