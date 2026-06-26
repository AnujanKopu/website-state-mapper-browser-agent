import { useRef, useState } from "react";

import { downloadRunExport, type ExportKind } from "../../api/client";
import { runStatusLabel, truncate } from "../../lib/format";
import { AgentView } from "../agent-view/AgentView";
import { ScreenshotOverlay, type ExpandedScreenshot } from "../agent-view/ScreenshotOverlay";
import { GraphView } from "../graph/GraphView";
import { NodePanel } from "../graph/NodePanel";
import { AuthGateBanner } from "./AuthGateBanner";
import { useRunStream } from "./useRunStream";

interface WorkspaceProps {
  runId: string;
  onNewRun: () => void;
}

export function Workspace({ runId, onNewRun }: WorkspaceProps) {
  const { run, acknowledgeAuthResolved } = useRunStream(runId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expandedScreenshot, setExpandedScreenshot] = useState<ExpandedScreenshot | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<ExportKind | null>(null);
  const exportMenuRef = useRef<HTMLDetailsElement | null>(null);

  const handleExport = async (kind: ExportKind) => {
    setExportError(null);
    setExporting(kind);
    try {
      await downloadRunExport(runId, kind);
      if (exportMenuRef.current) exportMenuRef.current.open = false;
    } catch {
      setExportError("Export failed. The run may still be loading.");
    } finally {
      setExporting(null);
    }
  };

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
          <span className="topbar__run-id">RUN / {runId.slice(0, 8)}</span>
        </div>
        <div className="topbar__right">
          <span className={`topbar__status topbar__status--${run.runStatus}`}>
            <i aria-hidden />
            {runStatusLabel(run.completionStatus ?? run.runStatus)}
          </span>
          <details className="export-menu" ref={exportMenuRef}>
            <summary className="button button--ghost">Export {"\u25BE"}</summary>
            <div className="export-menu__items">
              <button
                type="button"
                className="export-menu__item"
                disabled={exporting !== null}
                onClick={() => void handleExport("context-markdown")}
              >
                {exporting === "context-markdown" ? "Downloading\u2026" : "Context pack (.md)"}
              </button>
              <button
                type="button"
                className="export-menu__item"
                disabled={exporting !== null}
                onClick={() => void handleExport("context-json")}
              >
                {exporting === "context-json" ? "Downloading\u2026" : "Context pack (.json)"}
              </button>
              <button
                type="button"
                className="export-menu__item"
                disabled={exporting !== null}
                onClick={() => void handleExport("graph")}
              >
                {exporting === "graph" ? "Downloading\u2026" : "Graph (.json)"}
              </button>
              {exportError && <p className="export-menu__error">{exportError}</p>}
            </div>
          </details>
        </div>
      </header>

      {run.connection === "error" && (
        <div className="banner banner--error">{run.error ?? "Run is not available."}</div>
      )}

      {run.authGate && (
        <AuthGateBanner
          runId={runId}
          gate={run.authGate}
          onResolved={acknowledgeAuthResolved}
        />
      )}

      <div className="panes">
        <div className="panes__left">
          <AgentView
            run={run}
            inspectedState={selected}
            onExpandScreenshot={setExpandedScreenshot}
          />
        </div>
        <div className="panes__right">
          <GraphView
            nodes={run.nodes}
            edges={run.edges}
            selectedId={selectedId}
            currentId={run.viewportStateId}
            isLive={["queued", "running", "paused"].includes(run.runStatus)}
            onSelect={setSelectedId}
          />
          {selected && (
            <NodePanel
              state={selected}
              parent={selectedParent}
              states={run.nodes}
              edges={run.edges}
              onClose={() => setSelectedId(null)}
              onExpandScreenshot={setExpandedScreenshot}
            />
          )}
        </div>
      </div>

      <ScreenshotOverlay
        screenshot={expandedScreenshot}
        onClose={() => setExpandedScreenshot(null)}
      />
    </div>
  );
}
