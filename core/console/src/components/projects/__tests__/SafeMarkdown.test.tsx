import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import SafeMarkdown from "../SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders headings", () => {
    const md = `# Heading 1

## Heading 2`;
    render(<SafeMarkdown content={md} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Heading 1");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Heading 2");
  });

  it("renders unordered lists", () => {
    const md = `- Item A
- Item B
- Item C`;
    render(<SafeMarkdown content={md} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Item A");
  });

  it("renders code blocks", () => {
    const md = `\`\`\`python
print('hello')
\`\`\``;
    render(<SafeMarkdown content={md} />);
    expect(screen.getByText("print('hello')")).toBeInTheDocument();
  });

  it("renders tables with GFM", () => {
    const md = `| Col A | Col B |
|-------|-------|
| 1 | 2 |`;
    render(<SafeMarkdown content={md} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("Col A")).toBeInTheDocument();
  });

  it("renders links with target=_blank", () => {
    render(<SafeMarkdown content="[Example](https://example.com)" />);
    const link = screen.getByRole("link", { name: "Example" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("sanitizes script tags", () => {
    const { container } = render(
      <SafeMarkdown content='<script>alert("xss")</script>' />
    );
    expect(container.querySelector("script")).toBeNull();
  });

  it("renders bold and italic", () => {
    render(<SafeMarkdown content="**bold** and *italic*" />);
    expect(screen.getByText("bold")).toBeInTheDocument();
    expect(screen.getByText("italic")).toBeInTheDocument();
  });
});
