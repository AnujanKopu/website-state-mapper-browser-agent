import { useMemo, useState } from "react";
import type { CSSProperties } from "react";

import { artifactUrl } from "../../lib/constants";
import { truncate } from "../../lib/format";
import type { GraphEdge, GraphState, PageBox, SurfaceItem, SurfaceStatus } from "../../types/graph";
import { pageAncestorId } from "./graphLayers";

interface SurfaceMapProps {
  nodes: Record<string, GraphState>;
  edges: Record<string, GraphEdge>;
  pageId: string;
  selectedStateId: string | null;
  onBack: () => void;
  onSelectState: (id: string) => void;
}

interface SurfaceCapability {
  id: string;
  label: string;
  kind: string;
  status: SurfaceStatus;
  scope: SurfaceItem["interaction_scope"];
  region: string;
  fold: number;
  items: SurfaceItem[];
  boxes: PageBox[];
  destinationIds: string[];
}

type SurfaceFilter = "all" | "mapped" | "blocked";

function validBox(box: PageBox | null | undefined): box is PageBox {
  return Boolean(
    box
    && Number.isFinite(box.x)
    && Number.isFinite(box.y)
    && Number.isFinite(box.width)
    && Number.isFinite(box.height)
    && box.width > 0
    && box.height > 0,
  );
}

function statusRank(status: SurfaceStatus): number {
  if (["blocked", "failed", "replay_failed"].includes(status)) return 4;
  if (["explored", "known_state"].includes(status)) return 3;
  if (status === "pending") return 2;
  return 1;
}

function capabilityStatus(items: SurfaceItem[]): SurfaceStatus {
  return [...items].sort((left, right) => statusRank(right.status) - statusRank(left.status))[0]?.status ?? "inventory_only";
}

function statusLabel(status: SurfaceStatus): string {
  return status.replaceAll("_", " ");
}

function buildCapabilities(
  state: GraphState,
  edges: Record<string, GraphEdge>,
): SurfaceCapability[] {
  const grouped = new Map<string, SurfaceItem[]>();
  for (const item of state.surface_items ?? []) {
    const stable = item.component_key ?? item.item_id ?? `${item.kind}:${item.label}`;
    const key = `${stable}:${item.kind ?? "control"}`;
    const bucket = grouped.get(key) ?? [];
    bucket.push(item);
    grouped.set(key, bucket);
  }

  return [...grouped.entries()].map(([key, items]) => {
    const itemIds = new Set(items.flatMap((item) => item.item_id ? [item.item_id] : []));
    const destinationIds = Object.values(edges)
      .filter((edge) => edge.from === state.id && Boolean(edge.surface_item_id && itemIds.has(edge.surface_item_id)))
      .map((edge) => edge.to);
    return {
      id: `surface:${state.id}:${key}`,
      label: items[0].component_label || items[0].label || items[0].associated_label || items[0].placeholder || "Unlabelled control",
      kind: items[0].kind ?? items[0].role ?? "control",
      status: capabilityStatus(items),
      scope: items[0].interaction_scope,
      region: items[0].region ?? (items[0].in_nav ? "nav" : items[0].in_modal ? "modal" : "other"),
      fold: Math.min(...items.map((item) => item.fold ?? 0)),
      items,
      boxes: items.flatMap((item) => validBox(item.page_box) ? [item.page_box] : []),
      destinationIds: Array.from(new Set(destinationIds)),
    };
  }).sort((left, right) => left.fold - right.fold || left.region.localeCompare(right.region) || left.label.localeCompare(right.label));
}

function documentSize(state: GraphState, capabilities: SurfaceCapability[]) {
  const evidence = state.evidence?.page;
  const allBoxes = capabilities.flatMap((capability) => capability.boxes);
  const maxRight = Math.max(0, ...allBoxes.map((box) => box.x + box.width));
  const maxBottom = Math.max(0, ...allBoxes.map((box) => box.y + box.height));
  const width = Math.max(1, evidence?.document?.width ?? evidence?.viewport?.width ?? (maxRight || 1366));
  const height = Math.max(1, evidence?.document?.height ?? evidence?.viewport?.height ?? (maxBottom || 768));
  return { width: Math.max(width, maxRight), height: Math.max(height, maxBottom) };
}

export function SurfaceMap({
  nodes,
  edges,
  pageId,
  selectedStateId,
  onBack,
  onSelectState,
}: SurfaceMapProps) {
  const states = useMemo(
    () => Object.values(nodes)
      .filter((state) => pageAncestorId(nodes, state.id) === pageId)
      .sort((left, right) => (left.index ?? 0) - (right.index ?? 0)),
    [nodes, pageId],
  );
  const [activeStateId, setActiveStateId] = useState(
    selectedStateId && states.some((state) => state.id === selectedStateId) ? selectedStateId : pageId,
  );
  const [selectedCapabilityId, setSelectedCapabilityId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<SurfaceFilter>("all");
  const state = nodes[activeStateId] ?? nodes[pageId];
  const capabilities = useMemo(() => buildCapabilities(state, edges), [edges, state]);
  const size = useMemo(() => documentSize(state, capabilities), [capabilities, state]);
  const normalizedQuery = query.trim().toLowerCase();
  const visible = capabilities.filter((capability) => {
    if (normalizedQuery && !`${capability.label} ${capability.kind} ${capability.region}`.toLowerCase().includes(normalizedQuery)) return false;
    if (filter === "blocked") return ["blocked", "failed", "replay_failed"].includes(capability.status);
    if (filter === "mapped") return capability.destinationIds.length > 0 || ["explored", "known_state"].includes(capability.status);
    return true;
  });
  const selected = capabilities.find((capability) => capability.id === selectedCapabilityId) ?? null;
  const placed = visible.filter((capability) => capability.boxes.length > 0);
  const unplaced = visible.filter((capability) => capability.boxes.length === 0);
  const screenshot = artifactUrl(state.screenshot);
  const counts = {
    mapped: capabilities.filter((item) => item.destinationIds.length > 0 || ["explored", "known_state"].includes(item.status)).length,
    blocked: capabilities.filter((item) => ["blocked", "failed", "replay_failed"].includes(item.status)).length,
    navigation: capabilities.filter((item) => item.scope === "page_navigation").length,
  };

  return (
    <section className="surface-map" aria-label={`Interactions on ${state.title || state.url}`}>
      <header className="surface-map__header">
        <div>
          <button type="button" className="toolbar-button toolbar-button--back" onClick={onBack}>← Topology</button>
          <span className="surface-map__kicker">Screenshot-grounded interaction map</span>
          <h2>{state.label || state.title || state.url_normalized}</h2>
        </div>
        <div className="surface-map__summary" aria-label="Interaction summary">
          <span><strong>{capabilities.length}</strong> controls</span>
          <span><strong>{counts.mapped}</strong> mapped</span>
          <span><strong>{counts.blocked}</strong> blocked</span>
          <span><strong>{counts.navigation}</strong> navigation</span>
        </div>
      </header>

      <div className="surface-map__state-tabs" role="tablist" aria-label="Captured UI states">
        {states.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={item.id === state.id}
            className={item.id === state.id ? "is-active" : ""}
            onClick={() => {
              setActiveStateId(item.id);
              setSelectedCapabilityId(null);
              onSelectState(item.id);
            }}
          >
            <span>{item.id === pageId ? "Page" : item.type.replaceAll("_", " ")}</span>
            {truncate(item.label || item.title || item.url_normalized, 28)}
          </button>
        ))}
      </div>

      <div className="surface-map__toolbar">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Find a control, region, or action"
          aria-label="Find an interaction"
        />
        <div className="surface-map__filters" role="group" aria-label="Interaction filters">
          {(["all", "mapped", "blocked"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={filter === value ? "is-active" : ""}
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {value}
            </button>
          ))}
        </div>
      </div>

      <div className="surface-map__body">
        <div className="surface-map__viewport">
          {screenshot ? (
            <div
              className="surface-map__canvas"
              style={{ aspectRatio: `${size.width} / ${size.height}` }}
            >
              <img
                src={screenshot}
                alt={`Full-page screenshot of ${state.title}`}
                decoding="async"
              />
              {placed.flatMap((capability) => capability.boxes.map((box, index) => (
                <button
                  key={`${capability.id}:${index}`}
                  type="button"
                  className={`surface-hotspot surface-hotspot--${capability.status}${capability.scope === "page_navigation" ? " is-navigation" : ""}${selected?.id === capability.id ? " is-selected" : ""}`}
                  style={{
                    left: `${Math.max(0, box.x) / size.width * 100}%`,
                    top: `${Math.max(0, box.y) / size.height * 100}%`,
                    width: `${Math.min(box.width, size.width) / size.width * 100}%`,
                    height: `${Math.min(box.height, size.height) / size.height * 100}%`,
                  } as CSSProperties}
                  onClick={() => setSelectedCapabilityId(capability.id)}
                  aria-label={`${capability.label}, ${statusLabel(capability.status)}`}
                  title={`${capability.label} · ${statusLabel(capability.status)}`}
                >
                  {selected?.id === capability.id && index === 0 && <span>{capability.label}</span>}
                </button>
              )))}
            </div>
          ) : (
            <div className="surface-map__missing">No screenshot is available for this state.</div>
          )}
        </div>

        <aside className="surface-inspector" aria-live="polite">
          {selected ? (
            <>
              <span className="surface-inspector__status">{statusLabel(selected.status)}</span>
              <h3>{selected.label}</h3>
              <dl>
                <div><dt>Kind</dt><dd>{selected.kind}</dd></div>
                <div><dt>Region</dt><dd>{selected.region}</dd></div>
                <div><dt>Fold</dt><dd>{selected.fold + 1}</dd></div>
                <div><dt>Instances</dt><dd>{selected.items.length}</dd></div>
              </dl>
              {selected.destinationIds.length > 0 && (
                <section>
                  <h4>Captured outcomes</h4>
                  <div className="surface-outcomes">
                    {selected.destinationIds.map((id) => {
                      const target = nodes[id];
                      if (!target) return null;
                      const targetShot = artifactUrl(target.screenshot);
                      return (
                        <button key={id} type="button" onClick={() => onSelectState(id)}>
                          {targetShot && <img src={targetShot} alt="" loading="lazy" decoding="async" />}
                          <span>{target.label || target.title || target.url_normalized}</span>
                          <small>{target.type.replaceAll("_", " ")}</small>
                        </button>
                      );
                    })}
                  </div>
                </section>
              )}
            </>
          ) : (
            <>
              <span className="surface-inspector__status">Surface inventory</span>
              <h3>Select a highlighted control</h3>
              <p>Controls retain their captured position. Colour and line style describe scope and exploration outcome.</p>
              <div className="surface-legend">
                <span className="is-mapped">Mapped outcome</span>
                <span className="is-navigation">Page navigation</span>
                <span className="is-pending">Pending</span>
                <span className="is-blocked">Blocked or failed</span>
              </div>
            </>
          )}

          {unplaced.length > 0 && (
            <section className="surface-unplaced">
              <h4>Region-only controls ({unplaced.length})</h4>
              <p>Exact coordinates were not present in the live snapshot.</p>
              {unplaced.map((capability) => (
                <button key={capability.id} type="button" onClick={() => setSelectedCapabilityId(capability.id)}>
                  <span>{capability.label}</span>
                  <small>{capability.region} · fold {capability.fold + 1}</small>
                </button>
              ))}
            </section>
          )}
        </aside>
      </div>
    </section>
  );
}
