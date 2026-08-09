param(
    [switch]$PreserveInstallerRollback,
    [switch]$PreserveFreshRecovery
)

$ErrorActionPreference = "Stop"
$ServiceName = "StockBotLive"
$InstallerHelpers = Join-Path $PSScriptRoot "stockbot_service_installer_helpers.psm1"
Import-Module -Name $InstallerHelpers -Force -ErrorAction Stop

function Assert-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    if (!$Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "StockBot service removal requires administrator privileges."
    }
}

function Invoke-ServiceControl {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string[]]$Arguments
    )

    $ScExe = Join-Path $env:SystemRoot "System32\sc.exe"
    $Result = Invoke-StockBotWindowsNativeProcess `
        -FilePath $ScExe `
        -Arguments $Arguments
    if ($Result.ExitCode -ne 0) {
        throw (
            "Service Control Manager command failed: $($Arguments[0]) " +
            "(exit code $($Result.ExitCode))"
        )
    }
}

function Get-ValidatedStockBotServiceRegistration {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string[]]$AllowedBinaryPaths
    )

    $Registration = Get-CimInstance `
        -ClassName Win32_Service `
        -Filter "Name='$Name'" `
        -ErrorAction SilentlyContinue
    if ($null -eq $Registration) {
        return $null
    }
    $RegisteredBinaryPath = ([string]$Registration.PathName).Trim()
    $BinaryPathAllowed = $false
    foreach ($AllowedBinaryPath in $AllowedBinaryPaths) {
        if (
            [string]::Equals(
                $RegisteredBinaryPath,
                $AllowedBinaryPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            $BinaryPathAllowed = $true
            break
        }
    }
    if (!$BinaryPathAllowed) {
        throw "Refusing to remove a StockBotLive registration with an unexpected command."
    }
    if (
        ![string]::Equals(
            ([string]$Registration.StartName).Trim(),
            "LocalSystem",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Refusing to remove a StockBotLive registration with an unexpected account."
    }
    return $Registration
}

function Assert-NoUnregisteredStockBotServiceProcess {
    $UnexpectedProcess = Get-CimInstance Win32_Process |
        Where-Object {
            $ProcessNameMatches = ([string]$_.Name).Equals(
                "StockBotService.exe",
                [System.StringComparison]::OrdinalIgnoreCase
            )
            $CommandLine = [string]$_.CommandLine
            (
                $ProcessNameMatches -and
                ![string]::IsNullOrWhiteSpace($CommandLine) -and
                $CommandLine.Contains("run-service")
            )
        } |
        Select-Object -First 1
    if ($null -ne $UnexpectedProcess) {
        throw "Refusing cleanup while an unregistered StockBot service process is running."
    }
}

Assert-Administrator

$ProgramFilesKnownRoot = Get-StockBotKnownFolderPath -Name ProgramFiles
$ProgramDataKnownRoot = Get-StockBotKnownFolderPath -Name ProgramData
$StockBotProgramFilesRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $ProgramFilesKnownRoot "StockBot")
)
$ServiceRoots = Get-StockBotServiceInstallLayout `
    -ProgramFilesRoot $ProgramFilesKnownRoot
$ServiceInstallRoot = $ServiceRoots.CurrentRoot
$LegacyServiceInstallRoot = $ServiceRoots.LegacyRoot
$InstalledExecutable = Join-Path $ServiceInstallRoot "StockBotService.exe"
$LegacyInstalledExecutable = Join-Path `
    $LegacyServiceInstallRoot `
    "StockBotService.exe"
$ProgramDataRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $ProgramDataKnownRoot "StockBot")
)
$ServiceConfigPath = Join-Path $ProgramDataRoot "service-config.json"
$SessionFilePath = Join-Path $ProgramDataRoot "bridge-session.json"
$FreshRecoveryRoot = Join-Path $ProgramDataKnownRoot "StockBotInstallerRecovery"
$ProgramDataRemovalAllowlist = @(
    "credentials.env",
    "config.live.yaml",
    "service-config.json",
    "bridge-session.json"
)
$ExpectedBinaryPaths = @(
    (Get-StockBotServiceCommandLine `
        -InstallRoot $ServiceInstallRoot `
        -ServiceConfigPath $ServiceConfigPath),
    (Get-StockBotServiceCommandLine `
        -InstallRoot $LegacyServiceInstallRoot `
        -ServiceConfigPath $ServiceConfigPath)
)

foreach ($ManagedDirectory in @(
    $StockBotProgramFilesRoot,
    $ServiceInstallRoot,
    ($ServiceInstallRoot + ".staging"),
    ($ServiceInstallRoot + ".previous"),
    $LegacyServiceInstallRoot,
    ($LegacyServiceInstallRoot + ".staging"),
    ($LegacyServiceInstallRoot + ".previous"),
    $ProgramDataRoot,
    (Join-Path $ProgramDataRoot "installer-rollback"),
    $FreshRecoveryRoot
)) {
    $ManagedRoot = if (
        Test-StockBotPathWithinRoot `
            -Path $ManagedDirectory `
            -Root $ProgramFilesKnownRoot
    ) {
        $ProgramFilesKnownRoot
    }
    else {
        $ProgramDataKnownRoot
    }
    Assert-StockBotTrustedPath `
        -Path $ManagedDirectory `
        -TrustedRoot $ManagedRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
}
foreach ($ManagedFile in @(
    $InstalledExecutable,
    $LegacyInstalledExecutable,
    $ServiceConfigPath,
    $SessionFilePath
)) {
    $ManagedRoot = if (
        Test-StockBotPathWithinRoot `
            -Path $ManagedFile `
            -Root $ProgramFilesKnownRoot
    ) {
        $ProgramFilesKnownRoot
    }
    else {
        $ProgramDataKnownRoot
    }
    Assert-StockBotTrustedPath `
        -Path $ManagedFile `
        -TrustedRoot $ManagedRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
}

$ValidatedFreshRecovery = $null
$DiscardableFreshRecovery = $false
if (
    !$PreserveFreshRecovery -and
    (Test-Path -LiteralPath $FreshRecoveryRoot -PathType Container)
) {
    try {
        $ValidatedFreshRecovery = Get-StockBotValidatedFreshRecoveryManifest `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $ProgramDataRemovalAllowlist
    }
    catch {
        $DiscardableFreshRecovery = (
            Test-StockBotFreshRecoveryContainsOnlyMetadata `
                -RecoveryRoot $FreshRecoveryRoot `
                -TrustedRoot $ProgramDataKnownRoot `
                -FileNames $ProgramDataRemovalAllowlist
        )
        if (!$DiscardableFreshRecovery) {
            throw
        }
    }
}

$Registration = Get-ValidatedStockBotServiceRegistration `
    -Name $ServiceName `
    -AllowedBinaryPaths $ExpectedBinaryPaths
if ($null -ne $Registration) {
    Set-Service -Name $ServiceName -StartupType Manual
    Invoke-ServiceControl -Arguments @(
        "failure",
        $ServiceName,
        "reset=", "0",
        "actions=", ""
    )
    Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "0")

    $Service = Get-Service -Name $ServiceName -ErrorAction Stop
    try {
        if ($Service.Status -ne "Stopped") {
            Stop-Service -Name $ServiceName -ErrorAction Stop
        }
    }
    finally {
        $Service.Dispose()
    }
    $StoppedRegistration = Wait-StockBotServiceStopped -Name $ServiceName
    if (
        !(Remove-StockBotSessionAfterConfirmedStop `
            -Registration $StoppedRegistration `
            -SessionFile $SessionFilePath `
            -TrustedRoot $ProgramDataKnownRoot)
    ) {
        throw "StockBot session cleanup was refused because stop was not confirmed."
    }
    Invoke-ServiceControl -Arguments @("delete", $ServiceName)
    Wait-StockBotServiceDeleted -Name $ServiceName
}
else {
    Assert-NoUnregisteredStockBotServiceProcess
    $StoppedRegistration = [pscustomobject]@{
        State = "Stopped"
        ProcessId = 0
    }
    Remove-StockBotSessionAfterConfirmedStop `
        -Registration $StoppedRegistration `
        -SessionFile $SessionFilePath `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
}

$ProgramFilesDirectoryRemovalAllowlist = @(
    $ServiceInstallRoot,
    ($ServiceInstallRoot + ".staging"),
    ($ServiceInstallRoot + ".previous"),
    $LegacyServiceInstallRoot,
    ($LegacyServiceInstallRoot + ".staging"),
    ($LegacyServiceInstallRoot + ".previous")
)
foreach ($Candidate in $ProgramFilesDirectoryRemovalAllowlist) {
    Assert-StockBotTrustedPath `
        -Path $Candidate `
        -TrustedRoot $ProgramFilesKnownRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
    Remove-StockBotSafeTree `
        -Path $Candidate `
        -TrustedRoot $ProgramFilesKnownRoot
}

foreach ($FileName in $ProgramDataRemovalAllowlist) {
    $Candidate = [System.IO.Path]::GetFullPath(
        (Join-Path $ProgramDataRoot $FileName)
    )
    Assert-StockBotTrustedPath `
        -Path $Candidate `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
    Remove-StockBotSafeFile `
        -Path $Candidate `
        -TrustedRoot $ProgramDataKnownRoot
}

$ProgramDataDirectoryRemovalAllowlist = @()
if (!$PreserveInstallerRollback) {
    $ProgramDataDirectoryRemovalAllowlist += "installer-rollback"
}
foreach ($DirectoryName in $ProgramDataDirectoryRemovalAllowlist) {
    $Candidate = [System.IO.Path]::GetFullPath(
        (Join-Path $ProgramDataRoot $DirectoryName)
    )
    Assert-StockBotTrustedPath `
        -Path $Candidate `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
    Remove-StockBotSafeTree `
        -Path $Candidate `
        -TrustedRoot $ProgramDataKnownRoot
}

if ($null -ne $ValidatedFreshRecovery -or $DiscardableFreshRecovery) {
    Remove-StockBotSafeTree `
        -Path $FreshRecoveryRoot `
        -TrustedRoot $ProgramDataKnownRoot
}

# Trade and audit records are intentionally preserved under ProgramData.
if (Test-Path -LiteralPath $ProgramDataRoot -PathType Container) {
    Assert-StockBotTrustedPath `
        -Path $ProgramDataRoot `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType Directory | Out-Null
    $RemainingData = Get-ChildItem -LiteralPath $ProgramDataRoot -Force |
        Select-Object -First 1
    if ($null -eq $RemainingData) {
        Remove-StockBotSafeTree `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot
    }
}

if (Test-Path -LiteralPath $StockBotProgramFilesRoot -PathType Container) {
    Assert-StockBotDirectoryTreeSafe `
        -Path $StockBotProgramFilesRoot `
        -TrustedRoot $ProgramFilesKnownRoot | Out-Null
    $RemainingProgramFiles = Get-ChildItem `
        -LiteralPath $StockBotProgramFilesRoot `
        -Force |
        Select-Object -First 1
    if ($null -eq $RemainingProgramFiles) {
        Remove-StockBotSafeTree `
            -Path $StockBotProgramFilesRoot `
            -TrustedRoot $ProgramFilesKnownRoot
    }
}
