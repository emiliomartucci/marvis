// Mirrors core/wizard/state.py — Pydantic <-> Zod parity test enforced Wave 2.

import { z } from "zod";

export const StepId = z.enum([
  "welcome",
  "storage",
  "llm_provider",
  "first_project",
  "recap",
]);
export type StepId = z.infer<typeof StepId>;

export const LlmProvider = z.enum([
  "anthropic",
  "openai",
  "mac_gateway",
  "bedrock",
]);
export type LlmProvider = z.infer<typeof LlmProvider>;

export const DbBackend = z.enum(["sqlite", "postgres"]);
export type DbBackend = z.infer<typeof DbBackend>;

export const ProjectType = z.enum(["code", "work", "system"]);
export type ProjectType = z.infer<typeof ProjectType>;

export const WelcomePayload = z.object({
  bsl_accepted: z.boolean().default(false),
  accepted_at: z.string().datetime().optional().nullable(),
});
export type WelcomePayload = z.infer<typeof WelcomePayload>;

export const StoragePayload = z.object({
  projects_root: z.string(),
  db_backend: DbBackend.default("sqlite"),
  db_path: z.string().optional().nullable(),
  postgres_dsn: z.string().optional().nullable(),
});
export type StoragePayload = z.infer<typeof StoragePayload>;

export const LlmProviderPayload = z.object({
  provider: LlmProvider.optional().nullable(),
  api_key: z.string().optional().nullable(),
  base_url: z.string().optional().nullable(),
  test_passed: z.boolean().default(false),
});
export type LlmProviderPayload = z.infer<typeof LlmProviderPayload>;

export const FirstProjectPayload = z.object({
  name: z.string(),
  slug: z.string(),
  type: ProjectType.default("code"),
});
export type FirstProjectPayload = z.infer<typeof FirstProjectPayload>;

export const WizardState = z.object({
  version: z.string().default("1.0"),
  current_step: StepId.default("welcome"),
  completed_steps: z.array(StepId).default([]),
  skipped_steps: z.array(StepId).default([]),
  started_at: z.string().datetime().optional(),
  completed_at: z.string().datetime().optional().nullable(),
  welcome: WelcomePayload.default({ bsl_accepted: false }),
  storage: StoragePayload.optional().nullable(),
  llm_provider: LlmProviderPayload.optional().nullable(),
  first_project: FirstProjectPayload.optional().nullable(),
});
export type WizardState = z.infer<typeof WizardState>;

export const STEP_ORDER: readonly StepId[] = [
  "welcome",
  "storage",
  "llm_provider",
  "first_project",
  "recap",
] as const;

export const SKIPPABLE_STEPS: ReadonlySet<StepId> = new Set([
  "storage",
  "llm_provider",
  "first_project",
]);

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/;

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export interface ValidationError {
  field: string;
  message: string;
}

export function validateWelcome(p: WelcomePayload): ValidationError[] {
  const errors: ValidationError[] = [];
  if (!p.bsl_accepted) {
    errors.push({
      field: "bsl_accepted",
      message: "You must accept the BSL license to continue",
    });
  }
  return errors;
}

export function validateStorage(p: StoragePayload): ValidationError[] {
  const errors: ValidationError[] = [];
  if (!p.projects_root) {
    errors.push({ field: "projects_root", message: "Path cannot be empty" });
  } else if (!p.projects_root.startsWith("/") && !p.projects_root.startsWith("~")) {
    errors.push({
      field: "projects_root",
      message: "Must be an absolute path",
    });
  }
  if (p.db_backend === "sqlite") {
    if (!p.db_path) {
      errors.push({ field: "db_path", message: "SQLite path required" });
    }
  } else if (p.db_backend === "postgres") {
    if (!p.postgres_dsn) {
      errors.push({
        field: "postgres_dsn",
        message: "Postgres DSN required",
      });
    } else if (
      !p.postgres_dsn.startsWith("postgresql://") &&
      !p.postgres_dsn.startsWith("postgres://")
    ) {
      errors.push({
        field: "postgres_dsn",
        message: "DSN must start with postgresql://",
      });
    }
  }
  return errors;
}

export function validateLlmProvider(
  p: LlmProviderPayload,
  opts: { allowEmpty?: boolean } = {}
): ValidationError[] {
  if (opts.allowEmpty && !p.provider && !p.api_key) return [];
  const errors: ValidationError[] = [];
  if (!p.provider) {
    errors.push({ field: "provider", message: "Provider required" });
    return errors;
  }
  if (p.provider === "mac_gateway") {
    if (!p.base_url) {
      errors.push({
        field: "base_url",
        message: "Mac Gateway base_url required",
      });
    }
    if (!p.api_key) {
      errors.push({ field: "api_key", message: "Virtual key required" });
    }
  } else {
    if (!p.api_key) {
      errors.push({ field: "api_key", message: "API key required" });
    }
  }
  return errors;
}

export function validateFirstProject(
  p: FirstProjectPayload
): ValidationError[] {
  const errors: ValidationError[] = [];
  if (!p.name || !p.name.trim()) {
    errors.push({ field: "name", message: "Project name required" });
  }
  if (!p.slug) {
    errors.push({ field: "slug", message: "Slug required" });
  } else if (!SLUG_PATTERN.test(p.slug)) {
    errors.push({
      field: "slug",
      message: "Slug must match ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$",
    });
  }
  return errors;
}

export function maskApiKey(key: string | null | undefined): string {
  if (!key) return "";
  if (key.length <= 10) return "***";
  return `${key.slice(0, 6)}***${key.slice(-4)}`;
}
