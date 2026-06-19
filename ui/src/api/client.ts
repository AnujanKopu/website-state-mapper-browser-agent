import { API_BASE } from "../lib/constants";
import type {
  CreateRunInput,
  CreateRunResponse,
  GraphDocument,
  RunStatusResponse,
} from "../types/graph";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    throw new ApiError(`${init?.method ?? "GET"} ${path} -> ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export function createRun(input: CreateRunInput): Promise<CreateRunResponse> {
  return request<CreateRunResponse>("/api/runs", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getRun(runId: string): Promise<RunStatusResponse> {
  return request<RunStatusResponse>(`/api/runs/${runId}`);
}

export function getGraph(runId: string): Promise<GraphDocument> {
  return request<GraphDocument>(`/api/runs/${runId}/graph`);
}

/** Direct download URL for the run's graph JSON export. */
export function exportUrl(runId: string): string {
  return `${API_BASE}/api/runs/${runId}/export`;
}

/** Direct download URL for the deterministic context pack (markdown | json). */
export function contextUrl(runId: string, format: "markdown" | "json"): string {
  return `${API_BASE}/api/runs/${runId}/context?format=${format}`;
}

export type ExportKind = "graph" | "context-markdown" | "context-json";

const EXPORT_TARGETS: Record<
  ExportKind,
  { path: (runId: string) => string; filename: (runId: string) => string }
> = {
  graph: {
    path: (runId) => `/api/runs/${runId}/export`,
    filename: (runId) => `flowstate-${runId}.json`,
  },
  "context-markdown": {
    path: (runId) => `/api/runs/${runId}/context?format=markdown`,
    filename: (runId) => `flowstate-${runId}-context.md`,
  },
  "context-json": {
    path: (runId) => `/api/runs/${runId}/context?format=json`,
    filename: (runId) => `flowstate-${runId}-context.json`,
  },
};

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

/** Fetch an export from the API and save it locally (cross-origin safe). */
export async function downloadRunExport(runId: string, kind: ExportKind): Promise<void> {
  const target = EXPORT_TARGETS[kind];
  const response = await fetch(`${API_BASE}${target.path(runId)}`);
  if (!response.ok) {
    throw new ApiError(`GET ${target.path(runId)} -> ${response.status}`, response.status);
  }
  const blob = await response.blob();
  triggerBrowserDownload(blob, target.filename(runId));
}

export interface AuthCredentials {
  username?: string | null;
  password?: string | null;
}

/** Resume a run paused at an auth gate, optionally supplying credentials for autofill. */
export function authResume(
  runId: string,
  credentials?: AuthCredentials | null,
): Promise<{ status: string; run_id: string }> {
  return request(`/api/runs/${runId}/auth/resume`, {
    method: "POST",
    body: JSON.stringify({ credentials: credentials ?? null }),
  });
}

/** Skip the auth wall and continue exploration without authenticating. */
export function authSkip(runId: string): Promise<{ status: string; run_id: string }> {
  return request(`/api/runs/${runId}/auth/skip`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
