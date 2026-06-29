import { useMemo, useState } from "react";

import { runStatusLabel, stateTypeLabel } from "../../lib/format";
import type { GraphState } from "../../types/graph";
import type { RunState } from "../runs/runState";
import { CountersBar } from "./CountersBar";
import type { CounterFilterKey } from "./eventLogFilter";
import { EventLog } from "./EventLog";
import { ScreenshotFrame } from "./ScreenshotFrame";
import type { ExpandedScreenshot } from "./ScreenshotOverlay";

interface AgentViewProps {
  run: RunState;
  inspectedState?: GraphState | null;
  onExpandScreenshot: (screenshot: ExpandedScreenshot) => void;
}

const RUNNING = new Set(["running", "queued", "paused"]);

const FILTER_LABELS: Record<CounterFilterKey, string> = {
  states: "state",
  page_states: "page",
  substates: "substate",
  interaction_nodes: "interaction",
  surface_items_observed: "surface observation",
  interaction_capabilities: "interaction",
  edges: "edge",
  inferred_edges: "inferred",
  frontier: "pending",
  actions_performed: "action",
  deduped: "deduped",
  denied: "denied",
  failed: "failed",
  stale_actions: "stale",
  replay_failed_actions: "replay failed",
  known_state_actions: "known state",
  unresolved_discovery_obligations: "obligation",
  pending_representative_actions: "representative",
  noop: "noop",
  frontier_size: "pending",
  surface_pending: "surface",
};

export function AgentView({ run, inspectedState = null, onExpandScreenshot }: AgentViewProps) {
  const [logFilter, setLogFilter] = useState<CounterFilterKey | null>(null);
  const liveCurrent = run.viewportStateId ? run.nodes[run.viewportStateId] : null;
  const current = inspectedState ?? liveCurrent;
  const isLive = RUNNING.has(run.runStatus) && run.connection === "live";
  const action = run.currentAction;
  const filterLabel = useMemo(
    () => (logFilter ? FILTER_LABELS[logFilter] : undefined),
    [logFilter],
  );

  return (
    <section className="agent-view">
      <div className="agent-view__kicker">
        <span>{inspectedState ? "Selected state" : "Agent viewport"}</span>
        <span>{inspectedState ? "Graph inspection" : "Capture stream"}</span>
      </div>
      <header className="agent-view__header">
        <div className="agent-view__status">
          <span className={`pulse pulse--${isLive ? "live" : run.runStatus}`} aria-hidden />
          <span className="agent-view__status-text">
            {runStatusLabel(run.completionStatus ?? run.runStatus)}
          </span>
          {run.stopReason && <span className="agent-view__stop">{run.stopReason}</span>}
        </div>
        {current && (
          <div className="agent-view__current">
            <span className="badge badge--ghost">{stateTypeLabel(current.type)}</span>
            <span className="agent-view__current-title">{current.title || current.url}</span>
          </div>
        )}
      </header>

      <ScreenshotFrame state={current} onExpand={onExpandScreenshot} />

      <div className="agent-view__action">
        {action ? (
          <>
            <span className="agent-view__action-label">
              {action.outcome ? "Last action" : "Now"}: {action.label}
            </span>
            {action.outcome && (
              <span className={`tag tag--${action.outcome}`}>
                {action.reason ?? action.outcome}
              </span>
            )}
          </>
        ) : (
          <span className="agent-view__action-label agent-view__action-label--muted">
            Idle
          </span>
        )}
      </div>

      <CountersBar
        counters={run.counters}
        frontierSize={run.counters.frontier_size}
        activeFilter={logFilter}
        onFilterChange={setLogFilter}
      />

      <div className="agent-view__log-wrap">
        <div className="agent-view__log-head">
          <h3 className="agent-view__log-title">Event log</h3>
          {logFilter && (
            <button
              type="button"
              className="agent-view__log-clear"
              onClick={() => setLogFilter(null)}
            >
              Clear filter
            </button>
          )}
        </div>
        <EventLog entries={run.log} filter={logFilter} filterLabel={filterLabel} />
      </div>
    </section>
  );
}
