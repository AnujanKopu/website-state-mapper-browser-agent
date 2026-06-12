import { useState } from "react";
import type { FormEvent } from "react";

import { createRun } from "../../api/client";
import type { CreateRunInput } from "../../types/graph";

interface LandingFormProps {
  onStarted: (runId: string) => void;
}

export function LandingForm({ onStarted }: LandingFormProps) {
  const [url, setUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [maxStates, setMaxStates] = useState("");
  const [maxActions, setMaxActions] = useState("");
  const [maxDepth, setMaxDepth] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!url.trim() || submitting) return;
    setSubmitting(true);
    setError(null);

    // UI-driven runs only consume screenshots + graph metadata, so skip the
    // raw DOM HTML artifacts to save disk.
    const input: CreateRunInput = { url: url.trim(), save_dom_snapshots: false };
    const asInt = (value: string): number | undefined => {
      const n = Number.parseInt(value, 10);
      return Number.isFinite(n) && n > 0 ? n : undefined;
    };
    if (asInt(maxStates)) input.max_states = asInt(maxStates);
    if (asInt(maxActions)) input.max_actions = asInt(maxActions);
    if (maxDepth !== "" && Number.parseInt(maxDepth, 10) >= 0) {
      input.max_depth = Number.parseInt(maxDepth, 10);
    }

    try {
      const response = await createRun(input);
      onStarted(response.run_id);
    } catch {
      setError("Could not start the run. Is the API running on the configured port?");
      setSubmitting(false);
    }
  };

  return (
    <div className="landing">
      <div className="landing__inner">
        <h1 className="landing__title">FlowState</h1>
        <p className="landing__subtitle">
          Map a live web app into an interactive state graph. Enter a URL and watch the agent
          explore.
        </p>

        <form className="landing__form" onSubmit={submit}>
          <div className="landing__input-row">
            <input
              className="text-input"
              type="text"
              placeholder="https://example.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
              spellCheck={false}
            />
            <button className="button button--primary" type="submit" disabled={submitting}>
              {submitting ? "Starting\u2026" : "Map it"}
            </button>
          </div>

          <button
            type="button"
            className="landing__advanced-toggle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? "Hide" : "Show"} advanced controls
          </button>

          {showAdvanced && (
            <div className="landing__advanced">
              <label>
                Max states
                <input
                  className="text-input text-input--small"
                  type="number"
                  min={1}
                  placeholder="60"
                  value={maxStates}
                  onChange={(e) => setMaxStates(e.target.value)}
                />
              </label>
              <label>
                Max actions
                <input
                  className="text-input text-input--small"
                  type="number"
                  min={1}
                  placeholder="150"
                  value={maxActions}
                  onChange={(e) => setMaxActions(e.target.value)}
                />
              </label>
              <label>
                Max depth
                <input
                  className="text-input text-input--small"
                  type="number"
                  min={0}
                  placeholder="4"
                  value={maxDepth}
                  onChange={(e) => setMaxDepth(e.target.value)}
                />
              </label>
            </div>
          )}

          {error && <p className="landing__error">{error}</p>}
        </form>
      </div>
    </div>
  );
}
