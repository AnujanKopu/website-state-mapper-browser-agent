import { artifactUrl } from "../../lib/constants";
import { stateTypeLabel } from "../../lib/format";
import type { GraphState, SurfaceItem, SurfaceStatus } from "../../types/graph";
import { accentFor } from "./nodeStyles";

interface NodePanelProps {
  state: GraphState | null;
  parent: GraphState | null;
  onClose: () => void;
  onExpandScreenshot: (url: string) => void;
}

const FLAG_LABELS: Record<string, string> = {
  modal_open: "Modal open",
  auth_required: "Auth required",
  payment_required: "Payment required",
  dead_end: "Dead end",
  risky_terminal: "Risky terminal",
};

const REGION_ORDER = ["nav", "header", "main", "aside", "modal", "footer"];

const STATUS_CLASS: Record<SurfaceStatus, string> = {
  explored: "chip--ok",
  pending: "chip--pending",
  blocked: "chip--blocked",
  noop: "chip--muted",
  skipped_duplicate: "chip--skip",
};

const STATUS_LABEL: Record<SurfaceStatus, string> = {
  explored: "explored",
  pending: "pending",
  blocked: "blocked",
  noop: "no-op",
  skipped_duplicate: "duplicate",
};

interface SurfaceRow {
  label: string;
  status: SurfaceStatus;
  count: number;
}

function groupSurface(items: SurfaceItem[]): [string, SurfaceRow[]][] {
  const byRegion = new Map<string, SurfaceRow[]>();
  const seenGroup = new Map<string, SurfaceRow>();
  for (const item of items) {
    if (item.group_id && seenGroup.has(item.group_id)) {
      seenGroup.get(item.group_id)!.count += 1;
      continue;
    }
    const region = item.region ?? "other";
    const row: SurfaceRow = { label: item.label, status: item.status, count: 1 };
    if (item.group_id) seenGroup.set(item.group_id, row);
    if (!byRegion.has(region)) byRegion.set(region, []);
    byRegion.get(region)!.push(row);
  }
  const ordered: [string, SurfaceRow[]][] = [];
  for (const region of REGION_ORDER) {
    if (byRegion.has(region)) ordered.push([region, byRegion.get(region)!]);
  }
  for (const [region, rows] of byRegion) {
    if (!REGION_ORDER.includes(region)) ordered.push([region, rows]);
  }
  return ordered;
}

export function NodePanel({ state, parent, onClose, onExpandScreenshot }: NodePanelProps) {
  if (!state) return null;
  const accent = accentFor(state.type);
  const screenshot = artifactUrl(state.screenshot);
  const activeFlags = Object.entries(FLAG_LABELS).filter(([key]) => Boolean(state.flags[key]));
  const denied = state.flags.denied_actions ?? [];
  const formCount = typeof state.flags.form_count === "number" ? state.flags.form_count : 0;
  const surface = groupSurface(state.surface_items ?? []);
  const exploration = state.exploration;

  return (
    <aside className="node-panel">
      <header className="node-panel__header">
        <span className="badge" style={{ color: accent, borderColor: accent }}>
          {stateTypeLabel(state.type)}
        </span>
        <button className="icon-button" onClick={onClose} aria-label="Close panel">
          {"\u00d7"}
        </button>
      </header>

      <h2 className="node-panel__title">{state.label || state.title || state.url_normalized}</h2>
      <a className="node-panel__url" href={state.url} target="_blank" rel="noreferrer">
        {state.url}
      </a>

      {parent && (
        <p className="node-panel__substate">
          {"\u21B3 "}Sub-state of {typeof parent.index === "number" ? `s${parent.index}` : "parent"}
          {" \u00b7 "}
          {parent.title || parent.url_normalized}
        </p>
      )}

      {screenshot && (
        <button
          className="node-panel__shot"
          onClick={() => onExpandScreenshot(screenshot)}
          title="Expand screenshot"
        >
          <img src={screenshot} alt={`Screenshot of ${state.title}`} loading="lazy" />
        </button>
      )}

      <dl className="node-panel__facts">
        <div>
          <dt>Type</dt>
          <dd>{stateTypeLabel(state.type)}</dd>
        </div>
        <div>
          <dt>Depth</dt>
          <dd>{state.depth}</dd>
        </div>
        <div>
          <dt>Forms</dt>
          <dd>{formCount}</dd>
        </div>
      </dl>

      {state.summary && <p className="node-panel__summary">{state.summary}</p>}

      {activeFlags.length > 0 && (
        <section className="node-panel__section">
          <h3>Detected flags</h3>
          <div className="chips">
            {activeFlags.map(([key, label]) => (
              <span key={key} className="chip">
                {label}
              </span>
            ))}
          </div>
        </section>
      )}

      {surface.length > 0 && (
        <section className="node-panel__section">
          <h3>Surface items ({state.surface_items?.length ?? 0})</h3>
          {exploration?.visit_status && (
            <p className="node-panel__coverage">
              {exploration.visit_status === "fully_explored"
                ? "Fully explored"
                : "Partially explored"}
              {typeof exploration.pending === "number" ? ` \u00b7 ${exploration.pending} pending` : ""}
              {exploration.blocked ? ` \u00b7 ${exploration.blocked} blocked` : ""}
            </p>
          )}
          {surface.map(([region, rows]) => (
            <div key={region} className="surface-group">
              <span className="surface-group__region">{region}</span>
              <div className="chips">
                {rows.map((row, i) => (
                  <span
                    key={`${region}-${i}`}
                    className={`chip ${STATUS_CLASS[row.status] ?? "chip--muted"}`}
                    title={STATUS_LABEL[row.status] ?? row.status}
                  >
                    {row.label}
                    {row.count > 1 ? ` \u00d7${row.count}` : ""}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </section>
      )}

      {state.path.length > 0 && (
        <section className="node-panel__section">
          <h3>How the agent got here</h3>
          <ol className="path-list">
            {state.path.map((step, i) => (
              <li key={i}>
                {step.kind === "goto" ? (
                  <span>
                    Go to <code>{step.url}</code>
                  </span>
                ) : (
                  <span>Click {step.label ? `"${step.label}"` : <code>{step.selector}</code>}</span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {denied.length > 0 && (
        <section className="node-panel__section">
          <h3>Denied / risky actions</h3>
          <ul className="denied-list">
            {denied.map((action, i) => (
              <li key={i}>
                <span className="denied-list__label">{action.label}</span>
                <span className="denied-list__reason">
                  {action.category ? `${action.category}: ` : ""}
                  {action.reason}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}
