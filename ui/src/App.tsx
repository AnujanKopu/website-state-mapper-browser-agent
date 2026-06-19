import { useEffect, useState } from "react";

import { LandingPage } from "./features/runs/LandingPage";
import { Workspace } from "./features/runs/Workspace";

function runIdFromHash(): string | null {
  const match = window.location.hash.match(/^#\/run\/([A-Za-z0-9_-]+)$/);
  return match ? match[1] : null;
}

export default function App() {
  const [runId, setRunId] = useState<string | null>(runIdFromHash);

  useEffect(() => {
    const onHashChange = () => setRunId(runIdFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const startRun = (id: string) => {
    window.location.hash = `#/run/${id}`;
  };
  const newRun = () => {
    window.location.hash = "";
  };

  if (runId) {
    return <Workspace key={runId} runId={runId} onNewRun={newRun} />;
  }
  return <LandingPage onStarted={startRun} />;
}
