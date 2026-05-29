import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DirectionDiff, type DirectionPair } from "../DirectionDiff";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const pair: DirectionPair = {
  current: { summary: "Current summary text", out_of_scope: "Current oos text" },
  proposed: { summary: "Proposed new direction", out_of_scope: "Proposed oos" },
};

describe("DirectionDiff", () => {
  it("renders both sides side-by-side", () => {
    render(
      <DirectionDiff
        findingId="fnd_1"
        projectSlug="demo"
        pair={pair}
        urgencyScore={5}
        confidence="high"
      />,
    );
    expect(screen.getByText("Current summary text")).toBeInTheDocument();
    expect(screen.getByText("Proposed new direction")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText(/conf: high/i)).toBeInTheDocument();
  });

  it("shows bootstrap placeholder when current is null", () => {
    render(
      <DirectionDiff
        findingId="fnd_2"
        projectSlug="demo"
        pair={{ current: null, proposed: pair.proposed }}
      />,
    );
    expect(screen.getByText(/Nessuna direction esistente/)).toBeInTheDocument();
  });

  it("renders urgency badge color tier", () => {
    render(
      <DirectionDiff findingId="fnd_3" projectSlug="demo" pair={pair} urgencyScore={8} />,
    );
    expect(screen.getByLabelText("urgency alta")).toBeInTheDocument();
  });

  it("approve calls the API endpoint and triggers onApproved", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ finding_id: "fnd_4" }),
    });
    const onApproved = vi.fn();
    render(
      <DirectionDiff
        findingId="fnd_4"
        projectSlug="demo"
        pair={pair}
        onApproved={onApproved}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("direction-diff-approve"));
    await waitFor(() => expect(onApproved).toHaveBeenCalledWith("fnd_4"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/brain/findings/fnd_4/approve",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("surfaces 403 error in an alert region", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      text: async () => "super_admin required",
    });
    render(
      <DirectionDiff findingId="fnd_5" projectSlug="demo" pair={pair} />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("direction-diff-approve"));
    await waitFor(() =>
      expect(screen.getByTestId("direction-diff-error")).toHaveTextContent(/403/),
    );
  });

  it("edit mode submits edited content", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ finding_id: "fnd_6" }),
    });
    const onEdited = vi.fn();
    render(
      <DirectionDiff findingId="fnd_6" projectSlug="demo" pair={pair} onEdited={onEdited} />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const summaryArea = screen.getByTestId("edit-summary");
    await user.clear(summaryArea);
    await user.type(summaryArea, "Edited new summary");

    const oosArea = screen.getByTestId("edit-oos");
    await user.clear(oosArea);
    await user.type(oosArea, "Edited new oos");

    await user.click(screen.getByTestId("direction-diff-save"));
    await waitFor(() => expect(onEdited).toHaveBeenCalledWith("fnd_6"));

    const callArgs = fetchMock.mock.calls[0];
    expect(callArgs[0]).toBe("/api/v1/brain/findings/fnd_6/edit");
    const body = JSON.parse(callArgs[1].body);
    expect(body.edited_summary).toBe("Edited new summary");
    expect(body.edited_out_of_scope).toBe("Edited new oos");
  });

  it("blocks save when edit fields empty", async () => {
    render(<DirectionDiff findingId="fnd_7" projectSlug="demo" pair={pair} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const summaryArea = screen.getByTestId("edit-summary");
    await user.clear(summaryArea);
    await user.click(screen.getByTestId("direction-diff-save"));
    expect(screen.getByTestId("direction-diff-error")).toHaveTextContent(
      /must not be empty/,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
