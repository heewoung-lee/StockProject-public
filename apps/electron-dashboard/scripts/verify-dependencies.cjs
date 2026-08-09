const { spawnSync } = require("node:child_process");

const packageConfig = require("../package.json");
const expectedElectronVersion = String(packageConfig.devDependencies.electron || "").trim();
const installedElectronVersion = String(require("electron/package.json").version || "").trim();

if (!expectedElectronVersion || installedElectronVersion !== expectedElectronVersion) {
  throw new Error(
    `Electron package mismatch: expected ${expectedElectronVersion || "unset"}, ` +
      `installed ${installedElectronVersion || "missing"}. Run npm install.`,
  );
}

const electronExecutable = require("electron");
const result = spawnSync(electronExecutable, ["--version"], {
  encoding: "utf8",
  windowsHide: true,
  timeout: 15000,
});
const runtimeVersion = String(result.stdout || "").trim().replace(/^v/, "");

if (result.error || result.status !== 0 || runtimeVersion !== expectedElectronVersion) {
  throw new Error(
    `Electron runtime mismatch: expected ${expectedElectronVersion}, ` +
      `found ${runtimeVersion || "unavailable"}. Run npm rebuild electron.`,
  );
}
