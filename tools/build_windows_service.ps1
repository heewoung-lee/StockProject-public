param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [switch]$SkipInstallBuildDependencies
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SpecPath = Join-Path $ProjectRoot "packaging\stockbot-service-windows.spec"
$OutputRoot = Join-Path $ProjectRoot "dist\StockBotService"
$OutputExe = Join-Path $OutputRoot "StockBotService.exe"
$ManifestName = "stockbot-service-bundle-manifest.json"
$ManifestPath = Join-Path $OutputRoot $ManifestName
$HelperModulePath = Join-Path `
    $ProjectRoot `
    "tools\stockbot_service_installer_helpers.psm1"

function InstallBuildDependencies {
    if ($SkipInstallBuildDependencies) {
        return
    }
    & $Python -m pip install -e ".[windows-service-build]"
    if ($LASTEXITCODE -ne 0) {
        throw "Windows service build dependency installation failed."
    }
}

if (!(Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable was not found: $Python"
}
if (!(Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
    throw "Windows service PyInstaller spec was not found: $SpecPath"
}

Set-Location $ProjectRoot
InstallBuildDependencies
& $Python -m PyInstaller --clean --noconfirm $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "Windows service PyInstaller build failed."
}

if (!(Test-Path -LiteralPath $OutputExe -PathType Leaf)) {
    throw "StockBotService.exe was not created: $OutputExe"
}
$SymbolsPath = Join-Path $OutputRoot "_internal\data\symbols.csv"
if (!(Test-Path -LiteralPath $SymbolsPath -PathType Leaf)) {
    throw "StockBot service symbol data was not created: $SymbolsPath"
}

$Files = @(
    Get-ChildItem `
        -LiteralPath $OutputRoot `
        -File `
        -Recurse `
        -Force `
        -ErrorAction Stop |
        Where-Object { $_.FullName -ne $ManifestPath } |
        ForEach-Object {
            $RelativePath = $_.FullName.Substring(
                $OutputRoot.Length
            ).TrimStart(
                [System.IO.Path]::DirectorySeparatorChar,
                [System.IO.Path]::AltDirectorySeparatorChar
            ).Replace("\", "/")
            [pscustomobject]@{
                FullName = $_.FullName
                RelativePath = $RelativePath
                Length = [long]$_.Length
            }
        } |
        Sort-Object -Property RelativePath
)
$ManifestFiles = @(
    foreach ($File in $Files) {
        [ordered]@{
            path = $File.RelativePath
            sha256 = (
                Get-FileHash `
                    -LiteralPath $File.FullName `
                    -Algorithm SHA256 `
                    -ErrorAction Stop
            ).Hash.ToLowerInvariant()
            size = $File.Length
        }
    }
)
$Payload = [ordered]@{
    schemaVersion = 1
    algorithm = "SHA256"
    files = $ManifestFiles
}
$Utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText(
    $ManifestPath,
    (($Payload | ConvertTo-Json -Depth 4) + "`n"),
    $Utf8WithoutBom
)
Import-Module $HelperModulePath -Force
Assert-StockBotServiceBundleInventory `
    -Path $OutputRoot `
    -TrustedRoot $OutputRoot | Out-Null

Write-Host "Created $OutputExe"
