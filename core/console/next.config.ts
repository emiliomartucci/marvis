import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone",
  trailingSlash: true,
  eslint: {
    ignoreDuringBuilds: true,
  },
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Service-Worker-Allowed", value: "/" },
        ],
      },
      {
        source: "/inbox/triage/files/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            // Extra frame-src origins (e.g. a self-hosted automations editor)
            // are injected per-deployment via NEXT_PUBLIC_EXTRA_FRAME_SRC; the
            // OSS default ships none.
            value:
              `default-src 'self'; connect-src 'self' https://api.justaskmarvis.com wss://api.justaskmarvis.com https://term.justaskmarvis.com:8443 wss://term.justaskmarvis.com:8443; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://api.justaskmarvis.com blob:; frame-src 'self'${process.env.NEXT_PUBLIC_EXTRA_FRAME_SRC ? " " + process.env.NEXT_PUBLIC_EXTRA_FRAME_SRC : ""} https://api.justaskmarvis.com blob:; frame-ancestors 'self'`,
          },
        ],
      },
    ];
  },
};

export default config;
