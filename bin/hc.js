#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const root = path.resolve(__dirname, "..");
const existing = process.env.PYTHONPATH;
const env = {
  ...process.env,
  PYTHONPATH: existing ? `${root}${path.delimiter}${existing}` : root,
};

function run(python) {
  return spawnSync(python, ["-m", "hconv.cli", ...process.argv.slice(2)], {
    stdio: "inherit",
    env,
  });
}

let result = run("python3");
if (result.error && result.error.code === "ENOENT") {
  result = run("python");
}

if (result.error) {
  console.error(`hc: failed to launch Python: ${result.error.message}`);
  process.exit(127);
}

process.exit(result.status ?? 1);
