import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ createRun: vi.fn() }));

vi.mock("../../api/client", () => ({ createRun: mocks.createRun }));

import { LandingForm } from "./LandingForm";

beforeEach(() => mocks.createRun.mockReset());
afterEach(cleanup);

describe("LandingForm", () => {
  it("starts a screenshot-focused run and transitions to the workspace", async () => {
    mocks.createRun.mockResolvedValue({
      run_id: "run-42",
      url: "https://example.com",
      status: "queued",
      events_url: "/events",
      graph_url: "/graph",
    });
    const onStarted = vi.fn();
    render(<LandingForm onStarted={onStarted} />);

    fireEvent.change(screen.getByPlaceholderText("https://example.com"), {
      target: { value: " https://example.com " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Show exploration limits" }));
    fireEvent.change(screen.getByLabelText("Max states"), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Max actions"), { target: { value: "24" } });
    fireEvent.change(screen.getByLabelText("Max depth"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Start mapping" }));

    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith("run-42"));
    expect(mocks.createRun).toHaveBeenCalledWith({
      url: "https://example.com",
      auth_mode: "guest",
      save_dom_snapshots: false,
      max_states: 12,
      max_actions: 24,
      max_depth: 2,
    });
  });

  it("sends credentials only for an explicit login run", async () => {
    mocks.createRun.mockResolvedValue({ run_id: "auth-run" });
    render(<LandingForm onStarted={vi.fn()} />);
    fireEvent.change(screen.getByPlaceholderText("https://example.com"), {
      target: { value: "https://example.com" },
    });
    fireEvent.click(screen.getByLabelText("With login"));
    fireEvent.change(screen.getByLabelText("Username or email"), {
      target: { value: "person@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Start mapping" }));

    await waitFor(() => expect(mocks.createRun).toHaveBeenCalled());
    expect(mocks.createRun).toHaveBeenCalledWith({
      url: "https://example.com",
      auth_mode: "login",
      credentials: { username: "person@example.com", password: "secret" },
      save_dom_snapshots: false,
    });
  });
});
