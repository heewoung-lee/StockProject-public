param(
    [switch]$SkipInstall,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $ProjectRoot "apps\electron-dashboard"
$PackageJson = Join-Path $AppRoot "package.json"
$NodeModules = Join-Path $AppRoot "node_modules"
$ElectronBin = Join-Path $AppRoot "node_modules\electron\dist\electron.exe"
$ElectronShim = Join-Path $AppRoot "node_modules\.bin\electron.cmd"

if (!(Test-Path $PackageJson)) {
    throw "Electron dashboard package was not found: $PackageJson"
}

Set-Location $AppRoot

$NeedsInstall = !(Test-Path $NodeModules) -or !(Test-Path $ElectronBin) -or !(Test-Path $ElectronShim)
if (!$NeedsInstall) {
    & npm.cmd run verify:dependencies --silent *> $null
    $NeedsInstall = $LASTEXITCODE -ne 0
}

if ($SkipInstall -and $NeedsInstall) {
    throw "Electron dashboard dependencies are incomplete. Run tools\run_electron_dashboard.ps1 without -SkipInstall or run npm.cmd install in $AppRoot."
}

if (!$SkipInstall -and $NeedsInstall) {
    Write-Host "Installing Electron dashboard dependencies..."
    npm.cmd install
    if ($LASTEXITCODE -ne 0) {
        throw "Electron dashboard dependency installation failed."
    }
    npm.cmd rebuild electron
    if ($LASTEXITCODE -ne 0) {
        throw "Electron runtime installation failed."
    }
}

if (!(Test-Path $ElectronBin)) {
    throw "Electron executable was not found after install: $ElectronBin"
}

if (!$SkipBuild) {
    Write-Host "Building Electron dashboard..."
    npm.cmd run build
}

Write-Host "Starting StockProject Electron dashboard..."
npm.cmd run electron
