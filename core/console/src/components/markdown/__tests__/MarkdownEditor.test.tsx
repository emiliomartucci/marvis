import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// CodeEditor pulls in CodeMirror dynamically; mock to a deterministic textarea so the
// raw-mode view renders synchronously in jsdom.
vi.mock("@/components/finder/viewers/CodeEditor", () => ({
  default: ({
    content,
    readOnly,
    onChange,
    onSave,
  }: {
    content: string;
    readOnly: boolean;
    onChange: (value?: string) => void;
    onSave: (value: string) => void;
  }) => (
    <textarea
      data-testid="raw-textarea"
      defaultValue={content}
      readOnly={readOnly}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "s") {
          e.preventDefault();
          onSave((e.target as HTMLTextAreaElement).value);
        }
      }}
    />
  ),
}));

import MarkdownEditor from "../MarkdownEditor";

const SAMPLE = "# Hello\n\nThis is **bold** and *italic*.\n\n- one\n- two\n";

describe("MarkdownEditor", () => {
  it("renders the markdown content as HTML in view mode", async () => {
    render(<MarkdownEditor content={SAMPLE} readOnly defaultMode="view" />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /hello/i, level: 1 }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/bold/i)).toBeInTheDocument();
  });

  it("preserves content when switching view -> raw -> wysiwyg", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor content={SAMPLE} defaultMode="view" />);

    // view -> raw
    await user.click(screen.getByRole("button", { name: /switch to raw mode/i }));
    const textarea = await screen.findByTestId("raw-textarea");
    expect((textarea as HTMLTextAreaElement).value).toContain("# Hello");
    expect((textarea as HTMLTextAreaElement).value).toContain("**bold**");

    // raw -> wysiwyg
    await user.click(
      screen.getByRole("button", { name: /switch to wysiwyg mode/i }),
    );
    await waitFor(() => {
      // ProseMirror renders the document inside a contenteditable div
      const root = document.querySelector(".ProseMirror");
      expect(root).not.toBeNull();
      expect(root?.textContent ?? "").toContain("Hello");
      expect(root?.textContent ?? "").toContain("bold");
    });
  });

  it("hides formatting toolbar when readOnly is true", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor content={SAMPLE} readOnly defaultMode="view" />);

    // Mode switch is always available
    expect(
      screen.getByRole("button", { name: /switch to wysiwyg mode/i }),
    ).toBeInTheDocument();

    // Switch to wysiwyg in read-only -> formatting buttons (Bold, etc.) absent
    await user.click(
      screen.getByRole("button", { name: /switch to wysiwyg mode/i }),
    );
    expect(screen.queryByRole("button", { name: /^bold$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /heading 1/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /insert table/i })).toBeNull();
  });
});
