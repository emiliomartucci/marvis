import { describe, expect, it } from "vitest";

import {
  computeProjectPulse,
  parseAdrDisplay,
  patchProgramProjectColor,
} from "@/lib/projectsLocal";
import type { DocEntry, ProgramInfo, TaskResponse } from "@/lib/types";

function task(status: TaskResponse["status"]): TaskResponse {
  return { status } as TaskResponse;
}

describe("projectsLocal", () => {
  it("computes project pulse excluding rejected tasks", () => {
    const pulse = computeProjectPulse([
      task("completed"),
      task("review"),
      task("in_progress"),
      task("rejected"),
    ]);

    expect(pulse).toEqual({
      completed: 1,
      total: 3,
      open: 2,
      review: 1,
      percent: 33,
      fraction: "1/3",
    });
  });

  it("parses ADR frontmatter and displays supersede chains", () => {
    const doc: DocEntry = {
      filename: "docs/decisions/2026-06-10-old-choice.md",
      date: null,
      title: null,
      category: null,
    };

    const adr = parseAdrDisplay(
      doc,
      [
        "---",
        "title: Old architecture",
        "date: 2026-06-10",
        "status: current",
        "superseded_by: docs/decisions/2026-06-12-new-choice.md",
        "---",
        "# Body title",
        "",
        "First useful sentence.",
      ].join("\n"),
    );

    expect(adr).toMatchObject({
      filename: doc.filename,
      title: "Old architecture",
      date: "2026-06-10",
      status: "superseded",
      supersededBy: "docs/decisions/2026-06-12-new-choice.md",
      excerpt: "Body title First useful sentence.",
    });
  });

  it("patches one project color without changing sibling programs", () => {
    const programs: ProgramInfo[] = [
      {
        name: "marvis",
        description: "",
        projects: [
          {
            slug: "marvisx",
            name: "MarvisX",
            program: "marvis",
            language: null,
            lifecycle: "active",
            phase: null,
            scope: "work",
            description: null,
            type: "code",
            repo_path: null,
            metadata_path: null,
            status: null,
            task_counts: { pending: 0, approved: 0, in_progress: 0, review: 0, completed: 0, rejected: 0, failed: 0 },
            last_handoff: null,
            last_status_update: null,
            on_server: true,
            color: null,
          },
        ],
      },
      {
        name: "personal",
        description: "",
        projects: [
          {
            slug: "site",
            name: "Site",
            program: "personal",
            language: null,
            lifecycle: "active",
            phase: null,
            scope: "personal",
            description: null,
            type: "work",
            repo_path: null,
            metadata_path: null,
            status: null,
            task_counts: { pending: 0, approved: 0, in_progress: 0, review: 0, completed: 0, rejected: 0, failed: 0 },
            last_handoff: null,
            last_status_update: null,
            on_server: true,
            color: "#009e73",
          },
        ],
      },
    ];

    const patched = patchProgramProjectColor(programs, "marvisx", "#56b4e9");

    expect(patched[0].projects[0].color).toBe("#56b4e9");
    expect(patched[1].projects[0].color).toBe("#009e73");
  });
});
