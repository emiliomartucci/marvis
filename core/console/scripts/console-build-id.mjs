const FULL_GIT_SHA = /^[0-9a-f]{40}$/;

/**
 * Keep Next.js static exports byte-reproducible for an exact source revision.
 * Release and CI builds must provide the checked-out Git SHA explicitly;
 * interactive local builds use a stable, non-release identifier.
 *
 * @param {NodeJS.ProcessEnv | Record<string, string | undefined>} env
 * @returns {string}
 */
export function resolveConsoleBuildId(env = process.env) {
  const configured = env.MARVIS_CONSOLE_BUILD_ID;

  if (configured !== undefined) {
    if (!FULL_GIT_SHA.test(configured)) {
      throw new Error(
        "MARVIS_CONSOLE_BUILD_ID must be the exact 40-character lowercase Git SHA",
      );
    }
    return configured;
  }

  if (env.CI) {
    throw new Error(
      "MARVIS_CONSOLE_BUILD_ID is required for reproducible CI builds",
    );
  }

  return "local";
}
