import { useCallback, useEffect, useReducer } from "react";

import { getGraph } from "../../api/client";
import { openRunStream, type RunStreamHandle } from "../../api/eventStream";
import { initialRunState, runReducer } from "./runState";
import type { RunState } from "./runState";
import type { GraphDocument } from "../../types/graph";

/** Runs that are persisted and no longer have a live in-memory event stream. */
const FINISHED_STATUSES = new Set(["done", "failed", "cancelled"]);

function shouldStream(graph: GraphDocument): boolean {
  return !FINISHED_STATUSES.has(graph.run.status) && graph.sync?.authoritative !== true;
}

export interface RunStreamResult {
  run: RunState;
  acknowledgeAuthResolved: () => void;
}

/** Hydrate a run, subscribe to live updates, and reconcile its terminal graph. */
export function useRunStream(runId: string | null): RunStreamResult {
  const [state, dispatch] = useReducer(runReducer, initialRunState);

  const acknowledgeAuthResolved = useCallback(() => {
    dispatch({ type: "authResolved" });
  }, []);

  useEffect(() => {
    if (!runId) return;
    const activeRunId = runId;
    let cancelled = false;
    let stream: RunStreamHandle | null = null;
    let hydrateInFlight: ReturnType<typeof getGraph> | null = null;
    let lastSequence = -1;
    let recovering = false;
    let settled = false;

    dispatch({ type: "reset", runId: activeRunId });

    const hydrate = () => {
      if (hydrateInFlight) return hydrateInFlight;
      hydrateInFlight = getGraph(activeRunId)
        .then((graph) => {
          if (!cancelled) dispatch({ type: "hydrate", graph });
          return graph;
        })
        .finally(() => {
          hydrateInFlight = null;
        });
      return hydrateInFlight;
    };

    const recoverStream = (failureMessage: string) => {
      if (cancelled || recovering) return;
      recovering = true;
      stream?.close();
      stream = null;
      hydrate()
        .then((graph) => {
          if (cancelled) return;
          if (shouldStream(graph)) attachStream();
          else {
            settled = true;
            dispatch({ type: "streamClosed" });
          }
        })
        .catch(() => {
          if (!cancelled) {
            dispatch({ type: "hydrateFailed", message: failureMessage });
            dispatch({ type: "streamClosed" });
          }
        })
        .finally(() => {
          recovering = false;
        });
    };

    function attachStream() {
      if (cancelled || stream) return;
      stream = openRunStream(activeRunId, {
        onOpen: () => {
          if (!cancelled) dispatch({ type: "streamOpen" });
        },
        onRetrying: () => {
          if (!cancelled) dispatch({ type: "streamReconnecting" });
        },
        onEvent: (envelope) => {
          if (cancelled || envelope.sequence <= lastSequence) return;
          if (envelope.sequence !== lastSequence + 1) {
            recoverStream("Graph recovery failed after an event gap.");
            return;
          }
          lastSequence = envelope.sequence;
          // Heartbeats carry no graph mutation; frontier/action events already
          // deliver counters. Keep the transport cursor without rerendering the UI.
          if (envelope.type === "heartbeat") return;
          dispatch({ type: "sse", envelope });
        },
        onProtocolError: () => {
          recoverStream("Graph recovery failed after an invalid event.");
        },
        onTerminal: () => {
          settled = true;
          stream = null;
          hydrate()
            .catch(() => {
              if (!cancelled) {
                dispatch({ type: "hydrateFailed", message: "Final graph refresh failed." });
              }
            })
            .finally(() => {
              if (!cancelled) dispatch({ type: "terminalReconciled" });
            });
        },
        onClosed: () => {
          stream = null;
          recoverStream("Graph refresh failed.");
        },
      }, lastSequence);
    }

    const reconcileForeground = () => {
      if (cancelled || document.hidden || stream || recovering || settled) return;
      hydrate()
        .then((graph) => {
          if (!cancelled && shouldStream(graph) && !stream) {
            attachStream();
          }
        })
        .catch(() => {
          if (!cancelled) {
            dispatch({ type: "hydrateFailed", message: "Graph refresh failed." });
          }
        });
    };

    const onVisibilityChange = () => {
      if (!document.hidden) reconcileForeground();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("focus", reconcileForeground);
    window.addEventListener("pageshow", reconcileForeground);

    hydrate()
      .then((graph) => {
        if (cancelled) return;
        if (!shouldStream(graph)) {
          settled = true;
          dispatch({ type: "terminalReconciled" });
          return;
        }
        attachStream();
      })
      .catch(() => {
        if (cancelled) return;
        // Immediately after POST /runs the database row can briefly be absent.
        dispatch({ type: "hydrateFailed", message: "Waiting for the run to become available." });
        attachStream();
      });

    return () => {
      cancelled = true;
      stream?.close();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", reconcileForeground);
      window.removeEventListener("pageshow", reconcileForeground);
    };
  }, [runId]);

  return { run: state, acknowledgeAuthResolved };
}
