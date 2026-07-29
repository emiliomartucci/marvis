import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the api module
vi.mock("@/lib/api", () => ({
  getProjectFile: vi.fn(),
  updateProjectFile: vi.fn(),
}));

import { getProjectFile, updateProjectFile } from "@/lib/api";
import FileViewerModal from "../FileViewerModal";

const mockGetProjectFile = vi.mocked(getProjectFile);
const mockUpdateProjectFile = vi.mocked(updateProjectFile);

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
    mockUpdateProjectFile.mockResolvedValue({
      content: "# Updated",
      filename: "test-plan.md",
      path: "docs/plans/test-plan.md",
      size: 10,
    });
  });

  it("loads and renders file content as markdown", async () => {
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    expect(mockGetProjectFile).toHaveBeenCalledWith(
      "test-project",
      "docs/plans/test-plan.md",
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
  });

  it("shows loading state", () => {
    mockGetProjectFile.mockReturnValue(new Promise(() => {})); // never resolves
    render(<FileViewerModal {...defaultProps} />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it("shows error on fetch failure", async () => {
    mockGetProjectFile.mockRejectedValue(new Error("File not found"));
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/file not found/i)).toBeInTheDocument();
    });
  });

  it("shows Edit button for .md files", async () => {
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /edit/i })).toBeInTheDocument();
    });
  });

  it("switches to edit mode with textarea on Edit click", async () => {
    const user = userEvent.setup();
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    await user.click(screen.getByRole("button", { name: /edit/i }));

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("# Test Plan\n\nSome content here.");
    expect(screen.getByRole("button", { name: /save/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("cancels edit and returns to view mode", async () => {
    const user = userEvent.setup();
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    await user.click(screen.getByRole("button", { name: /edit/i }));
    expect(screen.getByRole("textbox")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    // Back to view mode — heading visible again
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("saves edited content and returns to view mode", async () => {
    const user = userEvent.setup();
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    await user.click(screen.getByRole("button", { name: /edit/i }));

    const textarea = screen.getByRole("textbox");
    await user.clear(textarea);
    await user.type(textarea, "# Updated");

    await user.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      expect(mockUpdateProjectFile).toHaveBeenCalledWith(
        "test-project",
        "docs/plans/test-plan.md",
        "# Updated"
      );
    });
  });

  it("calls onClose when close button clicked", async () => {
    const user = userEvent.setup();
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("calls onClose when backdrop clicked", async () => {
    const user = userEvent.setup();
    const { container } = render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Test Plan");
    });

    // Click the backdrop (outermost div)
    const backdrop = container.firstChild as HTMLElement;
    await user.click(backdrop);
    expect(defaultProps.onClose).toHaveBeenCalled();
  });

  it("shows filename in header", async () => {
    render(<FileViewerModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText("test-plan.md")).toBeInTheDocument();
    });
  });
});
