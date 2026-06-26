import type { Counters } from "../../types/events";
import type { CounterFilterKey } from "./eventLogFilter";

interface CountersBarProps {
  counters: Counters;
  frontierSize: number;
  activeFilter: CounterFilterKey | null;
  onFilterChange: (filter: CounterFilterKey | null) => void;
}

interface Tile {
  key: CounterFilterKey;
  label: string;
  tone?: "warn" | "danger";
}

const TILES: Tile[] = [
  { key: "states", label: "States" },
  { key: "edges", label: "Edges" },
  { key: "inferred_edges", label: "Inferred" },
  { key: "frontier", label: "Pending" },
  { key: "surface_pending", label: "Surface" },
  { key: "actions_performed", label: "Actions" },
  { key: "deduped", label: "Deduped" },
  { key: "denied", label: "Denied", tone: "warn" },
  { key: "failed", label: "Failed", tone: "danger" },
];

export function CountersBar({
  counters,
  frontierSize,
  activeFilter,
  onFilterChange,
}: CountersBarProps) {
  return (
    <div className="counters" role="group" aria-label="Run counters">
      {TILES.map((tile) => {
        const value = tile.key === "frontier" ? frontierSize : (counters[tile.key] ?? 0);
        const isActive = activeFilter === tile.key;
        return (
          <button
            key={tile.key}
            type="button"
            className={`counter counter--${tile.tone ?? "default"}${isActive ? " is-active" : ""}`}
            aria-pressed={isActive}
            onClick={() => onFilterChange(isActive ? null : tile.key)}
          >
            <span className="counter__value">{value}</span>
            <span className="counter__label">{tile.label}</span>
          </button>
        );
      })}
    </div>
  );
}
