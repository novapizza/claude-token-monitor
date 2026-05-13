#!/usr/bin/env node
// Entry point invoked when a user runs `claude-code-token-monitor` (or via npx).
// Spawns the platform-specific binary that postinstall placed under bin/.
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const isWin = process.platform === "win32";
const binName = isWin ? "claude-code-token-monitor.exe" : "claude-code-token-monitor";
const binPath = path.join(__dirname, "..", "bin", binName);

if (!fs.existsSync(binPath)) {
  console.error(
    "claude-code-token-monitor: binary not found at " + binPath + ".\n" +
    "The postinstall step may have failed. Try reinstalling:\n" +
    "  npm install -g claude-code-token-monitor\n" +
    "Or run the installer manually:\n" +
    "  node " + path.join(__dirname, "install.js")
  );
  process.exit(1);
}

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });
if (result.error) {
  console.error("claude-code-token-monitor: failed to start binary —", result.error.message);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
