import { useState } from "react";

import { contextUrl, exportUrl } from "../../api/client";
import { runStatusLabel, truncate } from "../../lib/format";
import { AgentView } from "../agent-view/AgentView";
import { ScreenshotOverlay } from "../agent-view/ScreenshotOverlay";
import { GraphView } from "../graph/GraphView";
import { NodePanel } from "../graph/NodePanel";
import { AuthGateBanner } from "./AuthGateBanner";
import { useRunStream } from "./useRunStream";

interface WorkspaceProps {
  runId: string;
  onNewRun: () => void;
}

export function Workspace({ runId, onNewRun }: WorkspaceProps) {
  const run = useRunStream(runId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expandedUrl, setExpandedUrl] = useState<string | null>(null);

  const selected = selectedId ? (run.nodes[selectedId] ?? null) : null;
  const selectedParent =
    selected?.parent_state_id ? (run.nodes[selected.parent_state_id] ?? null) : null;

  return (
    <div className="workspace">
      <header className="topbar">
        <div className="topbar__left">
          <button className="button button--ghost" onClick={onNewRun}>
            {"\u2190"} New run
          </button>
          <span className="topbar__url" title={run.url}>
            {truncate(run.url || runId, 60)}
          </span>
        </div>
        <div className="topbar__right">
          <span className={`topbar__status topbar__status--${run.runStatus}`}>
            {runStatusLabel(run.runStatus)}
          </span>
          <details className="export-menu">
            <summary className="button button--ghost">Export {"\u25BE"}</summary>
            <div className="export-menu__items">
              <a href={contextUrl(runId, "markdown")} download>
                Context pack (.md)
              </a>
              <a href={contextUrl(runId, "json")} download>
                Context pack (.json)
              </a>
              <a href={exportUrl(runId)} download>
                Graph (.json)
              </a>
            </div>
          </details>
        </div>
      </header>

      {run.connection === "error" && (
        <div className="banner banner--error">{run.error ?? "Run is not available."}</div>
      )}

      {run.authGate && (
        <AuthGateBanner runId={runId} gate={run.authGate} />
      )}

      <div className="panes">
        <div className="panes__left">
          <AgentView run={run} onExpandScreenshot={setExpandedUrl} />
        </div>
        <div className="panes__right">
          <GraphView
            nodes={run.nodes}
            edges={run.edges}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
          {selected && (
            <NodePanel
              state={selected}
              parent={selectedParent}
              onClose={() => setSelectedId(null)}
              onExpandScreenshot={setExpandedUrl}
            />
          )}
        </div>
      </div>

      <ScreenshotOverlay url={expandedUrl} onClose={() => setExpandedUrl(null)} />
    </div>
  );
}
