const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const dashboardRoot = path.resolve(__dirname, "..");
const packagedExecutable = path.resolve(
  dashboardRoot,
  "..",
  "..",
  "dist",
  "electron-dashboard",
  "win-unpacked",
  "StockBot.exe",
);
const smokeTest = path.join(dashboardRoot, "e2e", "electron-smoke.cjs");

if (!fs.existsSync(packagedExecutable)) {
  throw new Error("Packaged Electron executable is missing. Run npm run package:win:dir first.");
}

const result = spawnSync(process.execPath, ["--test", smokeTest], {
  cwd: dashboardRoot,
  env: {
    ...process.env,
    STOCKBOT_E2E_EXECUTABLE: packagedExecutable,
  },
  stdio: "inherit",
});

if (result.error) {
  throw result.error;
}
process.exitCode = result.status ?? 1;
