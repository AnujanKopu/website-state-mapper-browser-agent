import { useCallback, useEffect, useReducer } from "react";

import { getGraph } from "../../api/client";
import { openRunStream, type RunStreamHandle } from "../../api/eventStream";
import { initialRunState, runReducer } from "./runState";
import type { RunState } from "./runState";

/** Runs that are persisted and no longer have a live in-memory event stream. */
const FINISHED_STATUSES = new Set(["done", "failed", "cancelled"]);

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
    let cancelled = false;
    let stream: RunStreamHandle | null = null;

    dispatch({ type: "reset", runId });

    const hydrate = () =>
      getGraph(runId).then((graph) => {
        if (!cancelled) dispatch({ type: "hydrate", graph });
        return graph;
      });

    const attachStream = () => {
      stream = openRunStream(runId, {
        onOpen: () => {
          if (!cancelled) dispatch({ type: "streamOpen" });
        },
        onRetrying: () => {
          if (!cancelled) dispatch({ type: "streamReconnecting" });
        },
        onEvent: (envelope) => {
          if (!cancelled) dispatch({ type: "sse", envelope });
        },
        onTerminal: () => {
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
          hydrate()
            .catch(() => {
              if (!cancelled) {
                dispatch({ type: "hydrateFailed", message: "Graph refresh failed." });
              }
            })
            .finally(() => {
              if (!cancelled) dispatch({ type: "streamClosed" });
            });
        },
      });
    };

    hydrate()
      .then((graph) => {
        if (cancelled) return;
        if (FINISHED_STATUSES.has(graph.run.status)) {
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
    };
  }, [runId]);

  return { run: state, acknowledgeAuthResolved };
}
