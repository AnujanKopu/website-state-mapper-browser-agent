import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  authResume: vi.fn(),
  authSkip: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {
    status = 500;
  },
  authResume: mocks.authResume,
  authSkip: mocks.authSkip,
  getRun: mocks.getRun,
}));

import { AuthGateBanner } from "./AuthGateBanner";

beforeEach(() => {
  mocks.authResume.mockReset().mockResolvedValue({ status: "running", run_id: "run-1" });
  mocks.authSkip.mockReset().mockResolvedValue({ status: "running", run_id: "run-1" });
  mocks.getRun.mockReset();
});
afterEach(cleanup);

describe("AuthGateBanner", () => {
  it("resumes with optional credentials from the compact checkpoint", async () => {
    const onResolved = vi.fn();
    render(
      <AuthGateBanner
        runId="run-1"
        gate={{ stateId: "s2", url: "https://example.com/login", autofillAttempted: false }}
        onResolved={onResolved}
      />,
    );

    expect(screen.getByText("Authentication checkpoint")).toBeInTheDocument();
    expect(screen.getByText("Agent paused")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Username or email (optional)"), { target: { value: "user@example.com" } });
    fireEvent.change(screen.getByPlaceholderText("Password (optional)"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Autofill & Resume" }));

    await waitFor(() => expect(mocks.authResume).toHaveBeenCalledWith("run-1", {
      username: "user@example.com",
      password: "secret",
    }));
    expect(onResolved).toHaveBeenCalledTimes(1);
  });

  it("keeps the existing skip behavior", async () => {
    const onResolved = vi.fn();
    render(
      <AuthGateBanner
        runId="run-1"
        gate={{ stateId: "s2", url: "https://example.com/login", autofillAttempted: true }}
        onResolved={onResolved}
      />,
    );

    expect(screen.getByText(/Autofill was attempted/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    await waitFor(() => expect(mocks.authSkip).toHaveBeenCalledWith("run-1"));
    expect(onResolved).toHaveBeenCalledTimes(1);
  });
});
