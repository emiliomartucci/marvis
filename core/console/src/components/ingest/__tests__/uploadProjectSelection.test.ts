import { describe, expect, it } from "vitest";
import type { ProgramInfo, ProjectInfo } from "@/lib/types";
import {
  isVisibleUploadProject,
  resolveIngestUploadProject,
  uploadableProjects,
} from "../uploadProjectSelection";

function project(
  slug: string,
  overrides: Partial<ProjectInfo> = {}
): ProjectInfo {
  return {
    slug,
    name: slug,
    program: null,
    language: null,
    lifecycle: null,
    phase: null,
    scope: null,
    description: null,
    type: "work",
    repo_path: null,
    metadata_path: `/data/projects/${slug}`,
    status: null,
    task_counts: {
      pending: 0,
      approved: 0,
      in_progress: 0,
      review: 0,
      completed: 0,
      rejected: 0,
      failed: 0,
    },
    last_handoff: null,
    last_status_update: null,
    on_server: true,
    path: `/data/projects/${slug}`,
    ...overrides,
  };
}

describe("upload project selection", () => {
  it("only exposes projects that are visible and uploadable", () => {
    const programs: ProgramInfo[] = [
      {
        name: "demo",
        description: "",
        projects: [
          project("datacenter-eu-strategy"),
          project("marvisx", { on_server: false }),
          project("site-frankfurt-buildout", { path: null }),
        ],
      },
    ];

    expect(uploadableProjects(programs).map((item) => item.slug)).toEqual([
      "datacenter-eu-strategy",
    ]);
  });

  it("rejects cached projects outside the current visible set", () => {
    const visible = [
      project("datacenter-eu-strategy"),
      project("site-frankfurt-buildout"),
    ];

    expect(isVisibleUploadProject("marvisx", visible)).toBe(false);
    expect(resolveIngestUploadProject("marvisx", visible)).toBe(
      "datacenter-eu-strategy"
    );
    expect(resolveIngestUploadProject("site-frankfurt-buildout", visible)).toBe(
      "site-frankfurt-buildout"
    );
  });
});
