import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  getProjectFile: vi.fn(),
}));

import { getProjectFile } from "@/lib/api";
import FileViewerModal from "../FileViewerModal";

const mockGetProjectFile = vi.mocked(getProjectFile);
const defaultProps = {
  slug: "test-project",
  filePath: "docs/plans/test-plan.md",
  filename: "test-plan.md",
  onClose: vi.fn(),
};

describe("FileViewerModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetProjectFile.mockResolvedValue({
      content: "# Test Plan\n\nSome content here.",
      filename: "test-plan.md",
      path: "docs/plans/test-plan.md",
      size: 32,
    });
  });

  it("loads and renders file content as markdown", async () => {
    render(<FileViewerModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan"));
    expect(mockGetProjectFile).toHaveBeenCalledWith("test-project", "docs/plans/test-plan.md", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("is inspection-only and has no file mutation controls", async () => {
    render(<FileViewerModal {...defaultProps} />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan"));
    expect(screen.queryByRole("button", { name: /^edit$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const user = userEvent.setup();
    render(<FileViewerModal {...defaultProps} />);
    await screen.findByRole("heading", { level: 1 });
    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(defaultProps.onClose).toHaveBeenCalled();
  });
});
