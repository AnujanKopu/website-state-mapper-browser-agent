import { useState } from "react";

import { authResume, authSkip } from "../../api/client";
import type { AuthGateInfo } from "./runState";

interface AuthGateBannerProps {
  runId: string;
  gate: AuthGateInfo;
}

export function AuthGateBanner({ runId, gate }: AuthGateBannerProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const hasCredentials = username.trim() || password.trim();

  async function handleResume() {
    setBusy(true);
    setLocalError(null);
    try {
      await authResume(
        runId,
        hasCredentials ? { username: username.trim() || null, password: password || null } : null,
      );
    } catch {
      setLocalError("Failed to resume — run may have already moved on.");
      setBusy(false);
    }
  }

  async function handleSkip() {
    setBusy(true);
    setLocalError(null);
    try {
      await authSkip(runId);
    } catch {
      setLocalError("Failed to skip.");
      setBusy(false);
    }
  }

  return (
    <div className="auth-gate-banner">
      <div className="auth-gate-banner__icon" aria-hidden>
        🔒
      </div>
      <div className="auth-gate-banner__body">
        <p className="auth-gate-banner__title">Authentication required</p>
        <p className="auth-gate-banner__url" title={gate.url}>
          {gate.url}
        </p>
        {gate.autofillAttempted && (
          <p className="auth-gate-banner__hint">
            Autofill was attempted but did not succeed. Provide correct credentials or authenticate
            manually in a headed browser.
          </p>
        )}

        {/* Credential fields (Slice 6) */}
        <div className="auth-gate-banner__creds">
          <input
            className="text-input text-input--small"
            type="text"
            placeholder="Username or email (optional)"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={busy}
            autoComplete="username"
          />
          <input
            className="text-input text-input--small"
            type="password"
            placeholder="Password (optional)"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            autoComplete="current-password"
          />
        </div>

        {localError && <p className="auth-gate-banner__error">{localError}</p>}

        <div className="auth-gate-banner__actions">
          <button
            className="button button--primary"
            onClick={handleResume}
            disabled={busy}
          >
            {hasCredentials ? "Autofill & Resume" : "Resume"}
          </button>
          <button className="button button--ghost" onClick={handleSkip} disabled={busy}>
            Skip
          </button>
        </div>
      </div>
    </div>
  );
}
