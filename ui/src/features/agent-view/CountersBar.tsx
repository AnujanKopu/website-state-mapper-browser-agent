import type { Counters } from "../../types/events";

interface CountersBarProps {
  counters: Counters;
  frontierSize: number;
}

interface Tile {
  key: keyof Counters | "frontier";
  label: string;
  tone?: "warn" | "danger";
}

const TILES: Tile[] = [
  { key: "states", label: "States" },
  { key: "edges", label: "Edges" },
  { key: "inferred_edges", label: "Inferred" },
  { key: "frontier", label: "Pending" },
  { key: "actions_performed", label: "Actions" },
  { key: "deduped", label: "Deduped" },
  { key: "denied", label: "Denied", tone: "warn" },
  { key: "failed", label: "Failed", tone: "danger" },
];

export function CountersBar({ counters, frontierSize }: CountersBarProps) {
  return (
    <div className="counters">
      {TILES.map((tile) => {
        const value = tile.key === "frontier" ? frontierSize : counters[tile.key];
        return (
          <div key={tile.key} className={`counter counter--${tile.tone ?? "default"}`}>
            <span className="counter__value">{value}</span>
            <span className="counter__label">{tile.label}</span>
          </div>
        );
      })}
    </div>
  );
}
