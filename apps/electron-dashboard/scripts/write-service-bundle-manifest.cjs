const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const manifestName = "stockbot-service-bundle-manifest.json";
const requiredPaths = new Set([
  "StockBotService.exe",
  "_internal/data/symbols.csv",
]);

function collectFiles(root, current = root) {
  const files = [];
  const entries = fs
    .readdirSync(current, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name, "en"));

  for (const entry of entries) {
    const fullPath = path.join(current, entry.name);
    const relativePath = path
      .relative(root, fullPath)
      .split(path.sep)
      .join("/");
    if (relativePath === manifestName) {
      continue;
    }
    if (entry.isSymbolicLink()) {
      throw new Error("StockBot service bundle cannot contain links.");
    }
    if (entry.isDirectory()) {
      files.push(...collectFiles(root, fullPath));
      continue;
    }
    if (!entry.isFile()) {
      throw new Error("StockBot service bundle contains an unsupported entry.");
    }
    const stat = fs.lstatSync(fullPath);
    const sha256 = crypto
      .createHash("sha256")
      .update(fs.readFileSync(fullPath))
      .digest("hex");
    files.push({
      path: relativePath,
      sha256,
      size: stat.size,
    });
  }
  return files;
}

function writeServiceBundleManifest(bundleRoot) {
  const resolvedRoot = path.resolve(bundleRoot);
  const rootStat = fs.lstatSync(resolvedRoot);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error("StockBot service bundle root is not a trusted directory.");
  }

  const manifestPath = path.join(resolvedRoot, manifestName);
  const temporaryPath = `${manifestPath}.tmp`;
  fs.rmSync(temporaryPath, { force: true });
  const files = collectFiles(resolvedRoot).sort((left, right) =>
    left.path.localeCompare(right.path, "en"),
  );
  const paths = new Set(files.map((file) => file.path));
  for (const requiredPath of requiredPaths) {
    if (!paths.has(requiredPath)) {
      throw new Error(
        `StockBot service bundle is missing ${requiredPath}.`,
      );
    }
  }

  const payload = {
    schemaVersion: 1,
    algorithm: "SHA256",
    files,
  };
  fs.writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    flag: "w",
  });
  fs.renameSync(temporaryPath, manifestPath);
}

async function writePackagedServiceBundleManifest(context) {
  const bundleRoot = path.join(
    context.appOutDir,
    "resources",
    "stockbot-service",
    "bundle",
  );
  writeServiceBundleManifest(bundleRoot);
}

module.exports = writePackagedServiceBundleManifest;
module.exports.writeServiceBundleManifest = writeServiceBundleManifest;
