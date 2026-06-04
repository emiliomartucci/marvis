// Test Inspector Cosmo — project mode + dir mode smoke.
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GraphInspector } from "../GraphInspector";

// Mock lib/api — nessuna fetch reale.
vi.mock("@/lib/api", () => {
  return {
    fetchAPIValidated: vi.fn(async () => []),
    getProjectDetail: vi.fn(async () => ({
      slug: "marvisx",
      name: "Marvis X",
      program: "marvis",
      language: "TypeScript",
      lifecycle: "active",
      phase: "build",
      scope: null,
      description: null,
      type: "system",
      repo_path: "~/code/marvisx",
      metadata_path: "/data/projects/marvisx",
      context_md: "Snippet di contesto breve.",
      config: {},
      deploy: null,
      handoffs: [],
      plans: [],
      solutions: [],
    })),
    getProjectHandoffs: vi.fn(async () => []),
    getProjectDocs: vi.fn(async () => [
      {
        filename: "docs/plans/2026-04-24-feat-x.md",
        date: "2026-04-24",
        title: "feat x",
        category: "plans",
      },
    ]),
    getProjectGitLog: vi.fn(async () => []),
    listTasks: vi.fn(async () => []),
  };
});

describe("GraphInspector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("null selected + null selectedDir → nulla renderizzato", () => {
    const { container } = render(
      <GraphInspector selected={null} selectedDir={null} onClose={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("project mode → mostra slug + info section", async () => {
    render(
      <GraphInspector selected="marvisx" selectedDir={null} onClose={() => undefined} />,
    );
    await waitFor(() => {
      expect(screen.getByText("project.marvisx")).toBeTruthy();
    });
    expect(screen.getByText("info")).toBeTruthy();
  });

  it("dir mode → mostra dir name + files section", async () => {
    const dir = {
      projectSlug: "marvisx",
      dirIdx: 0,
      kind: "plan" as const,
      name: "plans",
    };
    render(
      <GraphInspector selected="marvisx" selectedDir={dir} onClose={() => undefined} />,
    );
    await waitFor(() => {
      expect(screen.getByText("files")).toBeTruthy();
    });
  });

  it("dir mode brainstorms → matches doc con category frontmatter non-enum", async () => {
    // Regression: BE popola `category` dal frontmatter type:feat|fix|... → il
    // vecchio codice cast-ava qualsiasi stringa ad ActivityKind e poi
    // docMatchesKind("brainstorm") confrontava "feat" === "brainstorms" → no
    // match. Fix: validare category contro DOC_TAG_COLORS, fallback a
    // kindFromFilename → "brainstorms".
    const api = await import("@/lib/api");
    (api.getProjectDocs as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        filename: "docs/brainstorms/2026-04-22-foo.md",
        date: "2026-04-22",
        title: "Brainstorm foo",
        category: "feat", // non-enum → deve essere rifiutato
      },
    ]);
    const dir = {
      projectSlug: "marvisx",
      dirIdx: 0,
      kind: "brainstorm" as const,
      name: "brainstorms",
    };
    render(
      <GraphInspector selected="marvisx" selectedDir={dir} onClose={() => undefined} />,
    );
    await waitFor(() => {
      expect(screen.getByText("Brainstorm foo")).toBeTruthy();
    });
  });

  it("project mode → close button fires onClose", async () => {
    const onClose = vi.fn();
    render(<GraphInspector selected="marvisx" selectedDir={null} onClose={onClose} />);
    await waitFor(() => screen.getByText("project.marvisx"));
    const closeBtn = screen.getByTitle("Close (Esc)");
    closeBtn.click();
    expect(onClose).toHaveBeenCalled();
  });
});
