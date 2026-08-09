const fs = require("node:fs");
const path = require("node:path");

const repositoryRoot = path.resolve(__dirname, "..", "..", "..");
const distributionRoot = path.join(repositoryRoot, "dist");
const legacyDashboardOutput = path.join(distributionRoot, "StockBot");

if (path.dirname(legacyDashboardOutput) !== distributionRoot) {
  throw new Error("Legacy dashboard output resolved outside the distribution directory.");
}

fs.rmSync(legacyDashboardOutput, { recursive: true, force: true });
