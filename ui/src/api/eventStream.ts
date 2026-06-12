import { API_BASE } from "../lib/constants";
import { EVENT_TYPES, TERMINAL_EVENT_TYPES } from "../types/events";
import type { EventType, SSEEnvelope } from "../types/events";

export interface RunStreamHandlers {
  /** Fired for every parsed event envelope, in stream order. */
  onEvent: (envelope: SSEEnvelope) => void;
  /** Fired once when a terminal event (run_completed/run_failed) arrives. */
  onTerminal?: (envelope: SSEEnvelope) => void;
  /** Fired when the stream closes hard (e.g. 404: run not live / unknown). */
  onClosed?: () => void;
}

export interface RunStreamHandle {
  close: () => void;
}

const isTerminal = (type: EventType): boolean => TERMINAL_EVENT_TYPES.includes(type);

/**
 * Subscribe to a run's SSE stream.
 *
 * The server replays buffered history from sequence 0, then streams live.
 * We close the EventSource ourselves on a terminal event so the browser does
 * not auto-reconnect and replay the whole history again.
 */
export function openRunStream(runId: string, handlers: RunStreamHandlers): RunStreamHandle {
  const source = new EventSource(`${API_BASE}/api/runs/${runId}/events`);
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    source.close();
  };

  const handle = (raw: string) => {
    let envelope: SSEEnvelope;
    try {
      envelope = JSON.parse(raw) as SSEEnvelope;
    } catch {
      return;
    }
    handlers.onEvent(envelope);
    if (isTerminal(envelope.type)) {
      handlers.onTerminal?.(envelope);
      close();
    }
  };

  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (event) => handle((event as MessageEvent).data));
  }

  source.onerror = () => {
    if (closed) return;
    // Per the SSE spec, a non-2xx/invalid response sets readyState=CLOSED and
    // does not auto-reconnect; that is our "run not live / unknown" signal.
    // CONNECTING means a transient drop the browser will retry on its own.
    if (source.readyState === EventSource.CLOSED) {
      closed = true;
      handlers.onClosed?.();
    }
  };

  return { close };
}
