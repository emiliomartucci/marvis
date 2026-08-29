import type { NextConfig } from "next";
import { resolveConsoleBuildId } from "./scripts/console-build-id.mjs";

const config: NextConfig = {
  output: "export",
  generateBuildId: async () => resolveConsoleBuildId(),
  trailingSlash: true,
  basePath: "/ui",
  images: {
    unoptimized: true,
  },
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default config;
