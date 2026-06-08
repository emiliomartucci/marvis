import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: [
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        pir: {
          base: "hsl(var(--pir-base) / <alpha-value>)",
          "surface-0": "hsl(var(--pir-surface-0) / <alpha-value>)",
          "surface-1": "hsl(var(--pir-surface-1) / <alpha-value>)",
          "surface-2": "hsl(var(--pir-surface-2) / <alpha-value>)",
          "surface-3": "hsl(var(--pir-surface-3) / <alpha-value>)",
          accent: "hsl(var(--pir-accent) / <alpha-value>)",
          info: "hsl(var(--pir-info) / <alpha-value>)",
          purple: "hsl(var(--pir-purple) / <alpha-value>)",
          success: "hsl(var(--pir-success) / <alpha-value>)",
          warning: "hsl(var(--pir-warning) / <alpha-value>)",
          error: "hsl(var(--pir-error) / <alpha-value>)",
          "text-primary": "var(--pir-text-primary)",
          "text-secondary": "var(--pir-text-secondary)",
          "text-tertiary": "var(--pir-text-tertiary)",
          "text-muted": "var(--pir-text-muted)",
        },
        // Okabe-Ito CVD-safe palette (Wong 2011) for Knowledge Graph node/edge types
        "pir-kg-node-function":   "hsl(36 100% 45%)",   // #E69F00 orange
        "pir-kg-node-file":       "hsl(202 75% 63%)",   // #56B4E9 sky blue
        "pir-kg-node-task":       "hsl(204 100% 35%)",  // #0072B2 blue
        "pir-kg-node-handoff":    "hsl(326 35% 56%)",   // #CC79A7 reddish purple
        "pir-kg-node-learning":   "hsl(18 100% 42%)",   // #D55E00 vermillion
        "pir-kg-node-pr":         "hsl(162 100% 31%)",  // #009E73 green
        "pir-kg-node-commit":     "hsl(54 82% 62%)",    // #F0E442 yellow
        "pir-kg-node-project":    "hsl(0 0% 10%)",      // near-black
        "pir-kg-node-module":     "hsl(202 60% 75%)",   // sky lighter
        // Orphan sub-cluster colors
        "pir-kg-cluster-docs":    "hsl(30 70% 50%)",
        "pir-kg-cluster-memory":  "hsl(275 60% 55%)",
        "pir-kg-cluster-scripts": "hsl(180 55% 48%)",
        "pir-kg-cluster-kb":      "hsl(60 40% 40%)",
        "pir-kg-cluster-tests":   "hsl(340 50% 60%)",
        "pir-kg-cluster-output":  "hsl(0 0% 55%)",
        "pir-kg-cluster-data":    "hsl(160 45% 60%)",
        // Edge types
        "pir-kg-edge-calls":      "hsl(0 0% 45%)",
        "pir-kg-edge-mentions":   "hsl(36 80% 50%)",
        "pir-kg-edge-depends":    "hsl(275 50% 55%)",
        "pir-kg-edge-describes":  "hsl(204 80% 50%)",
      },
      borderColor: {
        pir: {
          DEFAULT: "var(--pir-border)",
          strong: "var(--pir-border-strong)",
          accent: "var(--pir-border-accent)",
        },
      },
      fontFamily: {
        sans: ["var(--pir-font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--pir-font-mono)", "ui-monospace", "Menlo", "Consolas", "monospace"],
        display: ["var(--pir-font-display)", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      borderRadius: {
        sm: "2px",
        DEFAULT: "4px",
        md: "4px",
        lg: "6px",
      },
      fontSize: {
        "display": ["24px", { lineHeight: "1.2", fontWeight: "600", letterSpacing: "-0.02em" }],
        "heading": ["16px", { lineHeight: "1.4", fontWeight: "600", letterSpacing: "-0.01em" }],
        "body": ["13px", { lineHeight: "1.5", fontWeight: "400" }],
        "label": ["12px", { lineHeight: "1.4", fontWeight: "500", letterSpacing: "0.01em" }],
        "caption": ["11px", { lineHeight: "1.4", fontWeight: "400", letterSpacing: "0.02em" }],
      },
    },
  },
  plugins: [require("@tailwindcss/typography")],
};

export default config;
