import { useEffect, useReducer } from "react";

import { getGraph } from "../../api/client";
import { openRunStream, type RunStreamHandle } from "../../api/eventStream";
import { initialRunState, runReducer } from "./runState";
import type { RunState } from "./runState";

/** Runs that are persisted and no longer have a live in-memory event stream. */
const FINISHED_STATUSES = new Set(["done", "failed", "cancelled"]);

/**
 * Subscribe to a run and maintain its live state.
 *
 * Reconnect protocol:
 * 1. Hydrate from `/graph` first.
 * 2. Open SSE only for live runs (`running` / `paused`). Finished runs never
 *    hit `/events` — that endpoint only exists while the server holds the run
 *    in memory (404 after restart or when revisiting an old hash URL).
 * 3. If SSE closes (404 or disconnect), re-hydrate from `/graph` then mark
 *    the stream closed without treating it as an error when graph data exists.
 */
export function useRunStream(runId: string | null): RunState {
  const [state, dispatch] = useReducer(runReducer, initialRunState);

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
        onEvent: (envelope) => {
          if (!cancelled) dispatch({ type: "sse", envelope });
        },
        onTerminal: () => {
          hydrate().catch(() => undefined);
        },
        onClosed: () => {
          hydrate()
            .catch(() => undefined)
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
          dispatch({ type: "streamClosed" });
          return;
        }
        attachStream();
      })
      .catch(() => {
        if (cancelled) return;
        // Race after POST /runs: row not queryable yet — SSE drives discovery.
        attachStream();
      });

    return () => {
      cancelled = true;
      stream?.close();
    };
  }, [runId]);

  return state;
}
