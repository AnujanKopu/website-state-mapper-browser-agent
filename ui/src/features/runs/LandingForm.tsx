import { useRef, useState } from "react";
import type { FormEvent } from "react";

import { createRun } from "../../api/client";
import { useMagneticHover } from "../../lib/pointerMotion";
import type { CreateRunInput } from "../../types/graph";

interface LandingFormProps {
  onStarted: (runId: string) => void;
  inputId?: string;
}

export function LandingForm({ onStarted, inputId = "target-url" }: LandingFormProps) {
  const submitRef = useRef<HTMLButtonElement>(null);
  const [url, setUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [authMode, setAuthMode] = useState<"guest" | "login">("guest");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [maxStates, setMaxStates] = useState("");
  const [maxActions, setMaxActions] = useState("");
  const [maxDepth, setMaxDepth] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useMagneticHover(submitRef);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !url.trim()
      || submitting
      || (authMode === "login" && (!username.trim() || !password))
    ) return;
    setSubmitting(true);
    setError(null);

    // UI-driven runs only consume screenshots + graph metadata, so skip the
    // raw DOM HTML artifacts to save disk.
    const input: CreateRunInput = {
      url: url.trim(),
      auth_mode: authMode,
      save_dom_snapshots: false,
    };
    if (authMode === "login") {
      input.credentials = { username: username.trim(), password };
    }
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
        <form className="landing__form" onSubmit={submit}>
          <div className="landing__input-row">
            <span className="landing__protocol" aria-hidden>URL</span>
            <input
              id={inputId}
              className="text-input"
              type="text"
              placeholder="https://example.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
              spellCheck={false}
            />
            <button
              ref={submitRef}
              className="button button--primary landing__submit"
              type="submit"
              disabled={
                submitting
                || (authMode === "login" && (!username.trim() || !password))
              }
            >
              {submitting ? "Starting\u2026" : "Start mapping"}
              {!submitting && <span aria-hidden>↗</span>}
            </button>
          </div>

          <fieldset className="landing__auth-mode">
            <legend>Session mode</legend>
            <label className={authMode === "guest" ? "is-selected" : ""}>
              <input
                type="radio"
                name="auth-mode"
                value="guest"
                checked={authMode === "guest"}
                onChange={() => setAuthMode("guest")}
              />
              Without login
            </label>
            <label className={authMode === "login" ? "is-selected" : ""}>
              <input
                type="radio"
                name="auth-mode"
                value="login"
                checked={authMode === "login"}
                onChange={() => setAuthMode("login")}
              />
              With login
            </label>
          </fieldset>

          {authMode === "login" && (
            <div className="landing__credentials">
              <label>
                Username or email
                <input
                  className="text-input text-input--small"
                  type="text"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                />
              </label>
              <label>
                Password
                <input
                  className="text-input text-input--small"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </label>
              <p>Credentials are held in memory for this run and are never exported.</p>
            </div>
          )}

          <button
            type="button"
            className="landing__advanced-toggle"
            onClick={() => setShowAdvanced((v) => !v)}
          >
            <span aria-hidden>{showAdvanced ? "−" : "+"}</span>
            {showAdvanced ? "Hide" : "Show"} exploration limits
          </button>

          {showAdvanced && (
            <div className="landing__advanced">
              <label>
                Max states
                <input
                  className="text-input text-input--small"
                  type="number"
                  min={1}
                  placeholder="250"
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
                  placeholder="1000"
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
                  placeholder="8"
                  value={maxDepth}
                  onChange={(e) => setMaxDepth(e.target.value)}
                />
              </label>
            </div>
          )}

          {error && <p className="landing__error">{error}</p>}
        </form>
  );
}
