import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../api/client", () => ({ createRun: vi.fn() }));

import { LandingPage } from "./LandingPage";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("LandingPage", () => {
  it("presents the interactive product story with truthful system metrics", () => {
    render(<LandingPage onStarted={vi.fn()} />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("See every");
    expect(screen.getByRole("heading", { name: "Watch the map take shape." })).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("13")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("state types understood")).toBeInTheDocument();
  });

  it("cycles through all four complete hero phrases", () => {
    vi.useFakeTimers();
    const view = render(<LandingPage onStarted={vi.fn()} />);
    const activeHeadline = () => view.container.querySelector(".hero-headline.is-active");

    expect(activeHeadline()).toHaveTextContent("Map what livesbetween the pages");
    act(() => vi.advanceTimersByTime(4200));
    expect(activeHeadline()).toHaveTextContent("See everystate");
    act(() => vi.advanceTimersByTime(4200));
    expect(activeHeadline()).toHaveTextContent("Trace everyuser path");
    act(() => vi.advanceTimersByTime(4200));
    expect(activeHeadline()).toHaveTextContent("Surface everyproduct boundary");
  });

  it("keeps the requested hero cycle active when the system reports reduced motion", () => {
    vi.useFakeTimers();
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: query === "(prefers-reduced-motion: reduce)",
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });

    const view = render(<LandingPage onStarted={vi.fn()} />);
    expect(view.container.querySelector(".hero-headline.is-active")).toHaveTextContent("Map what livesbetween the pages");
    act(() => vi.advanceTimersByTime(4200));
    expect(view.container.querySelector(".hero-headline.is-active")).toHaveTextContent("See everystate");
    Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
  });

  it("switches the mapping walkthrough and export preview", () => {
    render(<LandingPage onStarted={vi.fn()} />);

    const connectLabel = screen.getByText("Connect");
    const connectTab = connectLabel.closest("button");
    expect(connectTab).not.toBeNull();
    fireEvent.click(connectTab!);
    expect(screen.getByRole("heading", { name: "Keep the path that made it reachable." })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "JSON" }));
    expect(screen.getByText(/"states": 5/)).toBeInTheDocument();
  });
});
