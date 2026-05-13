#!/usr/bin/env node
// Postinstall: downloads the platform-specific claude-code-token-monitor binary
// from GitHub Releases. The asset name matches what release.yml uploads.
"use strict";

const fs = require("fs");
const path = require("path");
const https = require("https");
const { execSync } = require("child_process");

const pkg = require("../package.json");
const REPO = "emtyty/claude-token-monitor";
const VERSION = pkg.version;

if (process.env.CTM_SKIP_DOWNLOAD === "1") {
  console.log("claude-code-token-monitor: CTM_SKIP_DOWNLOAD=1, skipping binary download.");
  process.exit(0);
}

const PLATFORM_MAP = {
  "darwin-x64":   "claude-code-token-monitor-darwin-x64",
  "darwin-arm64": "claude-code-token-monitor-darwin-arm64",
  "linux-x64":    "claude-code-token-monitor-linux-x64",
  "linux-arm64":  "claude-code-token-monitor-linux-arm64",
  "win32-x64":    "claude-code-token-monitor-win32-x64.exe",
};

const key = `${process.platform}-${process.arch}`;
const asset = PLATFORM_MAP[key];
if (!asset) {
  console.error(
    `claude-code-token-monitor: unsupported platform ${key}.\n` +
    `Supported: ${Object.keys(PLATFORM_MAP).join(", ")}.\n` +
    `Open an issue at https://github.com/${REPO}/issues if you need this platform.`
  );
  process.exit(1);
}

const binDir = path.join(__dirname, "..", "bin");
const isWin = process.platform === "win32";
const targetName = isWin ? "claude-code-token-monitor.exe" : "claude-code-token-monitor";
const targetPath = path.join(binDir, targetName);
const url = `https://github.com/${REPO}/releases/download/v${VERSION}/${asset}`;

fs.mkdirSync(binDir, { recursive: true });

function download(srcUrl, dest, redirectsLeft = 5) {
  return new Promise((resolve, reject) => {
    const req = https.get(srcUrl, { headers: { "user-agent": "claude-code-token-monitor-installer" } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        if (redirectsLeft <= 0) return reject(new Error("too many redirects"));
        res.resume();
        return download(res.headers.location, dest, redirectsLeft - 1).then(resolve, reject);
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} fetching ${srcUrl}`));
      }
      const out = fs.createWriteStream(dest);
      res.pipe(out);
      out.on("finish", () => out.close(resolve));
      out.on("error", reject);
    });
    req.on("error", reject);
  });
}

(async () => {
  console.log(`claude-code-token-monitor: downloading ${asset} (v${VERSION})…`);
  try {
    await download(url, targetPath);
  } catch (err) {
    console.error(`claude-code-token-monitor: download failed — ${err.message}`);
    console.error(`URL: ${url}`);
    console.error(
      "If the release for this version doesn't exist yet, install a different version " +
      "or build from source: https://github.com/" + REPO
    );
    process.exit(1);
  }
  if (!isWin) {
    try { fs.chmodSync(targetPath, 0o755); } catch (_) { /* ignore */ }
  }
  console.log(`claude-code-token-monitor: installed to ${targetPath}`);
})();
