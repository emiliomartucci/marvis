import type { ProgramInfo, ProjectInfo } from "@/lib/types";

export function uploadableProjects(programs: ProgramInfo[]): ProjectInfo[] {
  return programs
    .flatMap((program) => program.projects)
    .filter((project) => project.on_server === true && project.path != null);
}

export function isVisibleUploadProject(
  slug: string | null | undefined,
  projects: ProjectInfo[]
): boolean {
  if (!slug) return false;
  return projects.some((project) => project.slug === slug);
}

export function resolveIngestUploadProject(
  cachedSlug: string | null | undefined,
  projects: ProjectInfo[]
): string {
  if (isVisibleUploadProject(cachedSlug, projects)) return cachedSlug ?? "";
  return projects[0]?.slug ?? "";
}
