import { useEffect, useRef } from "react";

import { shortTime } from "../../lib/format";
import type { LogEntry } from "../runs/runState";

interface EventLogProps {
  entries: LogEntry[];
}

function toneFor(entry: LogEntry): string {
  if (entry.type === "run_failed") return "danger";
  if (entry.outcome === "failed") return "danger";
  if (entry.outcome === "blocked") return "warn";
  if (entry.outcome === "deduped" || entry.outcome === "noop") return "muted";
  if (entry.type === "state_discovered") return "accent";
  return "default";
}

export function EventLog({ entries }: EventLogProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [entries.length]);

  return (
    <div className="event-log" role="log" aria-live="polite">
      {entries.length === 0 && <div className="event-log__empty">No events yet.</div>}
      {entries.map((entry) => (
        <div key={entry.id} className={`event-log__row event-log__row--${toneFor(entry)}`}>
          <span className="event-log__time">{shortTime(entry.timestamp)}</span>
          <span className="event-log__type">{entry.type}</span>
          <span className="event-log__msg">{entry.message}</span>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}
