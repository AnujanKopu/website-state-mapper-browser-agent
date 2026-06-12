import { runStatusLabel, stateTypeLabel } from "../../lib/format";
import type { RunState } from "../runs/runState";
import { CountersBar } from "./CountersBar";
import { EventLog } from "./EventLog";
import { ScreenshotFrame } from "./ScreenshotFrame";

interface AgentViewProps {
  run: RunState;
  onExpandScreenshot: (url: string) => void;
}

const RUNNING = new Set(["running", "queued", "paused"]);

export function AgentView({ run, onExpandScreenshot }: AgentViewProps) {
  const current = run.viewportStateId ? run.nodes[run.viewportStateId] : null;
  const isLive = RUNNING.has(run.runStatus) && run.connection === "live";
  const action = run.currentAction;

  return (
    <section className="agent-view">
      <header className="agent-view__header">
        <div className="agent-view__status">
          <span className={`pulse pulse--${isLive ? "live" : run.runStatus}`} aria-hidden />
          <span className="agent-view__status-text">{runStatusLabel(run.runStatus)}</span>
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

      <CountersBar counters={run.counters} frontierSize={run.counters.frontier_size} />

      <div className="agent-view__log-wrap">
        <h3 className="agent-view__log-title">Event log</h3>
        <EventLog entries={run.log} />
      </div>
    </section>
  );
}
