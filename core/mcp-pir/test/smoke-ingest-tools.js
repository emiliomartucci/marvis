#!/usr/bin/env node
/**
 * P1.5.E7 — smoke test for the 8 new ingest triage MCP tools.
 *
 * Spawns mcp-pir/index.mjs over stdio, sends an MCP JSON-RPC `tools/list`
 * request, and asserts that the response includes every E7 tool name and
 * that the JSON-Schema serialization of their input shapes did not crash
 * (regression guard for learning f2663d51 — Zod v4 z.record 1-arg crash).
 *
 * Usage:
 *   PIR_API_URL=http://127.0.0.1:8100 TASKS_API_TOKEN=... \
 *     node mcp-pir/test/smoke-ingest-tools.js
 *
 * Exits 0 on success, 1 with a descriptive message on failure.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.resolve(__dirname, "..", "index.mjs");

const EXPECTED = [
  "list_ingest_pending",
  "approve_ingest_pending",
  "reject_ingest_pending",
  "patch_ingest_pending",
  "upload_ingest",
  "classify_ingest",
  "write_haiku_frontmatter",
  "reparse_ingest",
];

function send(child, payload) {
  child.stdin.write(JSON.stringify(payload) + "\n");
}

async function listTools() {
  const child = spawn("node", [SERVER], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, PIR_API_URL: process.env.PIR_API_URL || "http://127.0.0.1:8100" },
  });

  let buf = "";
  const seen = new Promise((resolve, reject) => {
    child.stdout.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if (!line.trim()) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id === 1 && msg.result) {
            resolve(msg.result);
          }
        } catch (_) {
          // ignore non-JSON lines (debug output)
        }
      }
    });
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`mcp-pir exited ${code}`));
    });
    setTimeout(() => reject(new Error("tools/list timeout (5s)")), 5000);
  });

  // MCP handshake: initialize first, then tools/list.
  send(child, {
    jsonrpc: "2.0", id: 0, method: "initialize",
    params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "smoke", version: "0.1" } },
  });
  send(child, { jsonrpc: "2.0", method: "notifications/initialized" });
  send(child, { jsonrpc: "2.0", id: 1, method: "tools/list" });

  const result = await seen;
  child.kill();
  return result.tools || [];
}

(async () => {
  const tools = await listTools();
  const names = new Set(tools.map((t) => t.name));
  const missing = EXPECTED.filter((n) => !names.has(n));
  if (missing.length) {
    console.error(`FAIL: missing tools: ${missing.join(", ")}`);
    process.exit(1);
  }
  // Sanity: ensure the schemas survived JSON Schema serialization (Zod v4 trap).
  for (const name of EXPECTED) {
    const tool = tools.find((t) => t.name === name);
    if (!tool || !tool.inputSchema) {
      console.error(`FAIL: ${name} has no inputSchema`);
      process.exit(1);
    }
  }
  console.log(`OK: ${EXPECTED.length}/${EXPECTED.length} ingest tools present (total ${tools.length}).`);
})().catch((err) => {
  console.error("FAIL:", err.message);
  process.exit(1);
});
