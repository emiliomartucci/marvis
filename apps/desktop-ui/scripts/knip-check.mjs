#!/usr/bin/env node
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";

const CATEGORIES = [
  "files", "dependencies", "devDependencies", "unlisted",
  "unresolved", "exports", "types"
];

// --- Load baseline ---
let baseline;
try {
  baseline = JSON.parse(readFileSync(".knip-baseline.json", "utf8"));
} catch (err) {
  console.error("Failed to read .knip-baseline.json:", err.message);
  console.error("Run: npm run knip:baseline");
  process.exit(2);
}

if (!baseline.counts) {
  console.error(".knip-baseline.json missing 'counts' field. Regenerate with: npm run knip:baseline");
  process.exit(2);
}

// --- Run knip ---
let raw;
try {
  raw = execSync("node_modules/.bin/knip --reporter json --cache", {
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    stdio: ["pipe", "pipe", "pipe"],
  });
} catch (e) {
  if (e.status === 2) {
    console.error("Knip crashed (exit 2). Failing check.");
    if (e.stderr) console.error(e.stderr);
    process.exit(1);
  }
  // exit 1 = issues found, normal
  raw = e.stdout || "";
  if (e.stderr) {
    const warnings = e.stderr.trim();
    if (warnings) console.warn("Knip warnings:", warnings);
  }
  if (!raw) {
    console.error("Knip produced no output. stderr:", e.stderr || e.message);
    process.exit(2);
  }
}

let current;
try {
  current = JSON.parse(raw);
} catch (parseErr) {
  console.error("Failed to parse Knip JSON:", parseErr.message);
  console.error("Raw output (first 500 chars):", raw.slice(0, 500));
  process.exit(2);
}

// --- Count from v6 format: { issues: [{ file, exports: [], types: [], ... }] } ---
const countFromIssues = (data) => {
  const issues = data.issues || [];
  const counts = {};
  for (const cat of CATEGORIES) {
    if (cat === "files") {
      counts.files = issues.reduce((acc, i) => acc + (i.files?.length ?? 0), 0);
    } else {
      counts[cat] = issues.reduce((acc, i) => acc + (i[cat]?.length ?? 0), 0);
    }
  }
  counts.total = Object.values(counts).reduce((a, b) => a + b, 0);
  return counts;
};

const c = countFromIssues(current);
const b = baseline.counts;

// --- Compare per-category ---
const regressions = {};
let totalRegression = 0;

for (const cat of CATEGORIES) {
  const baseVal = b[cat] ?? 0;
  const currVal = c[cat] ?? 0;
  const delta = currVal - baseVal;
  if (delta > 0) {
    regressions[cat] = { baseline: baseVal, current: currVal, delta };
    totalRegression += delta;
  }
}

if (totalRegression > 0) {
  console.error("Knip baseline regression detected\n");
  for (const [cat, { baseline: bv, current: cv, delta }] of Object.entries(regressions)) {
    console.error(`  ${cat}: ${bv} -> ${cv}  (+${delta})`);
  }
  console.error("\nFix the new issues, or if legitimate:");
  console.error("  - Add /** @public */ or /** @lintignore */ JSDoc tag on the export");
  console.error("  - Add to ignoreDependencies/ignoreFiles in knip.json");
  console.error("  - Update baseline: npm run knip:baseline (requires justification in commit msg)");
  process.exit(1);
}

// --- Success ---
const improvements = {};
for (const cat of CATEGORIES) {
  const delta = (c[cat] ?? 0) - (b[cat] ?? 0);
  if (delta < 0) improvements[cat] = delta;
}

console.log("Knip OK -- no regressions");
console.log(`  Total: ${c.total} issues (baseline: ${baseline.total ?? b.total ?? "?"})`);
if (Object.keys(improvements).length > 0) {
  console.log("  Improvements:", JSON.stringify(improvements));
}
