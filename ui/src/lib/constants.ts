const envBase = import.meta.env.VITE_API_BASE as string | undefined;

/** Base URL of the FlowState API (no trailing slash). */
export const API_BASE = (envBase ?? "http://localhost:8077").replace(/\/$/, "");

/** Root for static artifacts (screenshots). Node screenshot paths are relative. */
export const ARTIFACT_BASE = `${API_BASE}/artifacts`;

/** Build a browser-usable URL for a stored artifact path (e.g. a screenshot). */
export function artifactUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  return `${ARTIFACT_BASE}/${path.replace(/^\//, "")}`;
}
