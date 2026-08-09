param(
    [string]$DisplayName = "StockBot Live Trading Service",
    [string]$ProjectRoot = "",
    [string]$SourceBundlePath = "",
    [string]$ConfigPath = "",
    [string]$EnvFile = "",
    [double]$CycleIntervalSeconds = 15,
    [switch]$AuthorizeLiveOrders,
    [switch]$RegisterOnly,
    [switch]$StopExistingService,
    [switch]$AllowCredentialBootstrap,
    [ValidateSet("Current", "Legacy")]
    [string]$ServiceInstallLayout = "Current",
    [switch]$AllowServiceRootMigration
)

$ErrorActionPreference = "Stop"
$ServiceName = "StockBotLive"
$InstallerHelpers = Join-Path $PSScriptRoot "stockbot_service_installer_helpers.psm1"
Import-Module -Name $InstallerHelpers -Force -ErrorAction Stop

function Assert-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    if (!$Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this installer from an elevated PowerShell session."
    }
}

function Assert-NoStandaloneLiveBridge {
    $StandaloneBridge = Get-CimInstance Win32_Process |
        Where-Object {
            $CommandLine = [string]$_.CommandLine
            ([string]$_.Name).Equals(
                "StockBot.exe",
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
                $CommandLine.Contains("stockbot.electron_bridge") -or
                ($CommandLine.Contains("StockBotService.exe") -and
                    $CommandLine.Contains("run-console"))
        } |
        Select-Object -First 1
    if ($null -ne $StandaloneBridge) {
        throw "Close the StockBot desktop app before installing or updating the Windows service."
    }
}

function Resolve-RequiredPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [switch]$Directory
    )

    $Resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    $Item = Get-Item -LiteralPath $Resolved.Path
    if ($Directory -and !$Item.PSIsContainer) {
        throw "$Label must be a directory: $($Resolved.Path)"
    }
    if (!$Directory -and $Item.PSIsContainer) {
        throw "$Label must be a file: $($Resolved.Path)"
    }
    return [System.IO.Path]::GetFullPath($Resolved.Path)
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
    if (![string]::IsNullOrWhiteSpace($Result.StandardOutput)) {
        $Result.StandardOutput.TrimEnd() | Out-Host
    }
    if ($Result.ExitCode -ne 0) {
        throw (
            "Service Control Manager command failed: $($Arguments[0]) " +
            "(exit code $($Result.ExitCode))"
        )
    }
}

function Get-ValidatedServiceRegistration {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string[]]$AllowedBinaryPaths
    )

    $Registration = Get-CimInstance `
        -ClassName Win32_Service `
        -Filter "Name='$Name'" `
        -ErrorAction Stop
    if ($null -eq $Registration) {
        throw "Windows service registration was not found: $Name"
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
        throw "Existing $Name registration has an unexpected executable command."
    }
    if (
        ![string]::Equals(
            ([string]$Registration.StartName).Trim(),
            "LocalSystem",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Existing $Name registration does not run as LocalSystem."
    }
    return $Registration
}

function Test-ServiceSessionReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$SessionFile,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    try {
        $SessionFile = Assert-StockBotTrustedPath `
            -Path $SessionFile `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File
        if (!(Test-Path -LiteralPath $SessionFile -PathType Leaf)) {
            return $false
        }
        $SessionFile = Assert-StockBotTrustedPath `
            -Path $SessionFile `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File
        $Session = [System.IO.File]::ReadAllText($SessionFile) | ConvertFrom-Json
        if ($null -eq $Session -or $Session.schemaVersion -ne 1) {
            return $false
        }
        $SessionToken = ([string]$Session.token).Trim()
        if ($SessionToken.Length -lt 32) {
            return $false
        }
        $SessionProcessId = 0
        if (
            ![int]::TryParse(
                ([string]$Session.processId),
                [ref]$SessionProcessId
            ) -or
            $SessionProcessId -le 0
        ) {
            return $false
        }
        $CreatedAt = [DateTimeOffset]::MinValue
        if (
            ![DateTimeOffset]::TryParse(
                ([string]$Session.createdAt),
                [ref]$CreatedAt
            )
        ) {
            return $false
        }
        $SessionUrl = ([string]$Session.url).Trim()
        $SessionUri = $null
        if (
            $SessionUrl -notmatch "^http://127\.0\.0\.1:[1-9]\d{0,4}/?$" -or
            ![Uri]::TryCreate($SessionUrl, [UriKind]::Absolute, [ref]$SessionUri) -or
            $SessionUri.Port -gt 65535 -or
            $SessionUri.AbsolutePath -ne "/" -or
            $SessionUri.Query -or
            $SessionUri.Fragment -or
            $SessionUri.UserInfo
        ) {
            return $false
        }
        $Registration = Get-CimInstance `
            -ClassName Win32_Service `
            -Filter "Name='$Name'" `
            -ErrorAction Stop
        if (
            $Registration.State -ne "Running" -or
            [int]$Registration.ProcessId -ne $SessionProcessId
        ) {
            return $false
        }
        $Health = Invoke-RestMethod `
            -Uri ($SessionUri.AbsoluteUri.TrimEnd("/") + "/api/health") `
            -Method Get `
            -TimeoutSec 3 `
            -ErrorAction Stop
        return $Health.ok -eq $true
    }
    catch {
        return $false
    }
}

function Wait-ServiceReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$SessionFile,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [int]$TimeoutSeconds = 30
    )

    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $Service) {
            throw "Windows service registration disappeared while waiting for startup."
        }
        if (
            $Service.Status -eq "Running" -and
            (Test-ServiceSessionReady `
                -Name $Name `
                -SessionFile $SessionFile `
                -TrustedRoot $TrustedRoot)
        ) {
            return
        }
        if ($Service.Status -eq "Stopped") {
            throw "StockBot service stopped before its local bridge became ready."
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $Deadline)

    throw "StockBot service did not become ready within $TimeoutSeconds seconds."
}

Assert-Administrator
if (!$AuthorizeLiveOrders) {
    throw "Pass -AuthorizeLiveOrders to explicitly authorize persistent live order attempts for the saved KIS account scope."
}
if (
    [double]::IsNaN($CycleIntervalSeconds) -or
    [double]::IsInfinity($CycleIntervalSeconds) -or
    $CycleIntervalSeconds -lt 5 -or
    $CycleIntervalSeconds -gt 3600
) {
    throw "CycleIntervalSeconds must be between 5 and 3600 seconds."
}
Assert-NoStandaloneLiveBridge

$ScriptProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = $ScriptProjectRoot
}
$ResolvedProjectRoot = Resolve-RequiredPath -Path $ProjectRoot -Label "Project root" -Directory

if ([string]::IsNullOrWhiteSpace($SourceBundlePath)) {
    $SourceBundlePath = Join-Path $ResolvedProjectRoot "dist\StockBotService"
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $ResolvedProjectRoot "config.live.example.yaml"
}
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $ResolvedProjectRoot ".env"
}

$ResolvedSourceBundlePath = Resolve-RequiredPath -Path $SourceBundlePath -Label "Service bundle" -Directory
Assert-StockBotServiceBundleInventory `
    -Path $ResolvedSourceBundlePath `
    -TrustedRoot $ResolvedSourceBundlePath | Out-Null
$ResolvedConfigPath = Resolve-RequiredPath -Path $ConfigPath -Label "Live config"
$ResolvedEnvFile = Resolve-RequiredPath -Path $EnvFile -Label "Environment file"
$SourceExecutable = Join-Path $ResolvedSourceBundlePath "StockBotService.exe"
if (!(Test-Path -LiteralPath $SourceExecutable -PathType Leaf)) {
    throw "Service executable was not found in the bundle: $SourceExecutable"
}

$ExistingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$ProgramFilesKnownRoot = Get-StockBotKnownFolderPath -Name ProgramFiles
$ProgramDataKnownRoot = Get-StockBotKnownFolderPath -Name ProgramData
$ServiceRoots = Get-StockBotServiceInstallLayout `
    -ProgramFilesRoot $ProgramFilesKnownRoot
$InstallRoot = if ($ServiceInstallLayout -eq "Legacy") {
    $ServiceRoots.LegacyRoot
}
else {
    $ServiceRoots.CurrentRoot
}
$AlternateInstallRoot = if ($ServiceInstallLayout -eq "Legacy") {
    $ServiceRoots.CurrentRoot
}
else {
    $ServiceRoots.LegacyRoot
}
$InstalledExecutable = Join-Path $InstallRoot "StockBotService.exe"
$ProgramDataRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $ProgramDataKnownRoot "StockBot")
)
$ServiceConfigPath = Join-Path $ProgramDataRoot "service-config.json"
$SessionFilePath = Join-Path $ProgramDataRoot "bridge-session.json"
$BinaryPath = Get-StockBotServiceCommandLine `
    -InstallRoot $InstallRoot `
    -ServiceConfigPath $ServiceConfigPath
$AlternateBinaryPath = Get-StockBotServiceCommandLine `
    -InstallRoot $AlternateInstallRoot `
    -ServiceConfigPath $ServiceConfigPath
$ServiceDescription = "Runs the StockBot live controller and market-hours cycle scheduler."
$InitialStartupType = "Manual"

Assert-StockBotTrustedPath `
    -Path $InstallRoot `
    -TrustedRoot $ProgramFilesKnownRoot `
    -AllowMissing `
    -ExpectedType Directory | Out-Null
Assert-StockBotTrustedPath `
    -Path $AlternateInstallRoot `
    -TrustedRoot $ProgramFilesKnownRoot `
    -AllowMissing `
    -ExpectedType Directory | Out-Null
Assert-StockBotTrustedPath `
    -Path $ProgramDataRoot `
    -TrustedRoot $ProgramDataKnownRoot `
    -AllowMissing `
    -ExpectedType Directory | Out-Null
foreach ($ManagedFile in @(
    $InstalledExecutable,
    $ServiceConfigPath,
    $SessionFilePath
)) {
    $ManagedRoot = if (
        Test-StockBotPathWithinRoot `
            -Path $ManagedFile `
            -Root $InstallRoot
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

$ExistingRegistrationUsesAlternateRoot = $false
if ($null -ne $ExistingService) {
    if ($ExistingService.Status -ne "Stopped" -and !$StopExistingService) {
        throw "Stop $ServiceName before updating its service bundle."
    }
    $AllowedExistingBinaryPaths = @($BinaryPath)
    if ($AllowServiceRootMigration) {
        $AllowedExistingBinaryPaths += $AlternateBinaryPath
    }
    $ExistingRegistration = Get-ValidatedServiceRegistration `
        -Name $ServiceName `
        -AllowedBinaryPaths $AllowedExistingBinaryPaths
    $ExistingRegistrationUsesAlternateRoot = [string]::Equals(
        ([string]$ExistingRegistration.PathName).Trim(),
        $AlternateBinaryPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    Set-Service -Name $ServiceName -StartupType $InitialStartupType
    Invoke-ServiceControl -Arguments @(
        "failure",
        $ServiceName,
        "reset=", "0",
        "actions=", ""
    )
    Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "0")
    if ($ExistingService.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -ErrorAction Stop
    }
    $StoppedRegistration = Wait-StockBotServiceStopped -Name $ServiceName
    if (!(Test-StockBotServiceStopped -Registration $StoppedRegistration)) {
        throw "Refusing to update the service bundle before Stopped with PID 0."
    }
}

Install-StockBotDirectoryExactly `
    -Source $ResolvedSourceBundlePath `
    -Destination $InstallRoot `
    -TrustedRoot $ProgramFilesKnownRoot `
    -RequiredRelativeFile "StockBotService.exe" `
    -ValidateServiceBundleInventory
Assert-StockBotTrustedPath `
    -Path $InstalledExecutable `
    -TrustedRoot $ProgramFilesKnownRoot `
    -ExpectedType File | Out-Null
if (!(Test-Path -LiteralPath $InstalledExecutable -PathType Leaf)) {
    throw "Installed service executable was not found: $InstalledExecutable"
}

New-StockBotSafeDirectory `
    -Path $ProgramDataRoot `
    -TrustedRoot $ProgramDataKnownRoot | Out-Null
Set-StockBotRestrictedDirectoryAcl `
    -Path $ProgramDataRoot `
    -TrustedRoot $ProgramDataKnownRoot `
    -GrantCurrentIdentityRead

$ConfigureArguments = @(
    "configure",
    "--service-config", $ServiceConfigPath,
    "--project-root", $ResolvedProjectRoot,
    "--config", $ResolvedConfigPath,
    "--env-file", $ResolvedEnvFile,
    "--session-file", $SessionFilePath,
    "--cycle-interval-seconds", $CycleIntervalSeconds,
    "--authorize-live-orders"
)
if ($AllowCredentialBootstrap) {
    $ConfigureArguments += "--allow-credential-bootstrap"
}
Assert-StockBotTrustedPath `
    -Path $InstalledExecutable `
    -TrustedRoot $ProgramFilesKnownRoot `
    -ExpectedType File | Out-Null
foreach ($ManagedFile in @($ServiceConfigPath, $SessionFilePath)) {
    Assert-StockBotTrustedPath `
        -Path $ManagedFile `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
}
& $InstalledExecutable @ConfigureArguments
if ($LASTEXITCODE -ne 0) {
    throw "StockBot service configuration failed."
}

if ($null -eq $ExistingService) {
    New-Service `
        -Name $ServiceName `
        -BinaryPathName $BinaryPath `
        -DisplayName $DisplayName `
        -Description $ServiceDescription `
        -StartupType $InitialStartupType | Out-Null
}
else {
    Set-Service `
        -Name $ServiceName `
        -DisplayName $DisplayName `
        -Description $ServiceDescription `
        -StartupType $InitialStartupType
}

if ($null -ne $ExistingService -and $ExistingRegistrationUsesAlternateRoot) {
    Invoke-ServiceControl -Arguments @(
        "config",
        $ServiceName,
        "binPath=", $BinaryPath
    )
}
Get-ValidatedServiceRegistration `
    -Name $ServiceName `
    -AllowedBinaryPaths @($BinaryPath) | Out-Null
Invoke-ServiceControl -Arguments @(
    "failure",
    $ServiceName,
    "reset=", "0",
    "actions=", ""
)
Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "0")
$RegisteredConfiguration = Get-ValidatedServiceRegistration `
    -Name $ServiceName `
    -AllowedBinaryPaths @($BinaryPath)
if (
    !(Remove-StockBotSessionAfterConfirmedStop `
        -Registration $RegisteredConfiguration `
        -SessionFile $SessionFilePath `
        -TrustedRoot $ProgramDataKnownRoot)
) {
    throw "Refusing to remove the bridge session because the service is not Stopped with PID 0."
}
if ($RegisterOnly) {
    $RegisteredService = Get-Service -Name $ServiceName -ErrorAction Stop
    $RegisteredConfiguration = Get-ValidatedServiceRegistration `
        -Name $ServiceName `
        -AllowedBinaryPaths @($BinaryPath)
    if (
        $RegisteredService.Status -ne "Stopped" -or
        $RegisteredService.StartType -ne "Manual" -or
        [int]$RegisteredConfiguration.ProcessId -ne 0 -or
        (Test-Path -LiteralPath $SessionFilePath)
    ) {
        throw "Register-only postconditions were not satisfied."
    }
    Write-Host "$DisplayName is registered with manual startup. No live runtime was started."
    return
}
try {
    Start-Service -Name $ServiceName
    Wait-ServiceReady `
        -Name $ServiceName `
        -SessionFile $SessionFilePath `
        -TrustedRoot $ProgramDataKnownRoot
    Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "delayed-auto")
    Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "1")
    Invoke-ServiceControl -Arguments @(
        "failure",
        $ServiceName,
        "reset=", "86400",
        "actions=", "restart/60000/restart/60000/restart/60000"
    )
    Wait-ServiceReady `
        -Name $ServiceName `
        -SessionFile $SessionFilePath `
        -TrustedRoot $ProgramDataKnownRoot
}
catch {
    $ActivationException = $_.Exception
    $RollbackErrors = [System.Collections.Generic.List[string]]::new()
    $ServiceStopped = $false
    $StopFailureMessage = ""
    $RollbackRegistration = $null
    try {
        Invoke-ServiceControl -Arguments @(
            "failure",
            $ServiceName,
            "reset=", "0",
            "actions=", ""
        )
    }
    catch {
        $RollbackErrors.Add("failure action removal failed: $($_.Exception.Message)")
    }
    try {
        Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "0")
    }
    catch {
        $RollbackErrors.Add("failure flag reset failed: $($_.Exception.Message)")
    }
    try {
        $ServiceAfterFailure = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($null -ne $ServiceAfterFailure -and $ServiceAfterFailure.Status -ne "Stopped") {
            Stop-Service -Name $ServiceName -ErrorAction Stop
            $ServiceAfterFailure = Get-Service -Name $ServiceName -ErrorAction Stop
            $ServiceAfterFailure.WaitForStatus(
                [System.ServiceProcess.ServiceControllerStatus]::Stopped,
                [TimeSpan]::FromSeconds(30)
            )
        }
    }
    catch {
        $StopFailureMessage = $_.Exception.Message
    }
    try {
        Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "demand")
    }
    catch {
        $RollbackErrors.Add("manual startup restore failed: $($_.Exception.Message)")
    }
    try {
        $RollbackRegistration = Get-CimInstance `
            -ClassName Win32_Service `
            -Filter "Name='$ServiceName'" `
            -ErrorAction Stop
        if (Test-StockBotServiceStopped -Registration $RollbackRegistration) {
            $ServiceStopped = $true
        }
        else {
            $StopDetail = if ($StopFailureMessage) {
                ": $StopFailureMessage"
            }
            else {
                ""
            }
            $RollbackErrors.Add("service did not reach Stopped with PID 0$StopDetail")
        }
    }
    catch {
        $StopDetail = if ($StopFailureMessage) {
            "; stop request failure: $StopFailureMessage"
        }
        else {
            ""
        }
        $RollbackErrors.Add(
            "service stop verification failed: $($_.Exception.Message)$StopDetail"
        )
    }
    if ($ServiceStopped) {
        try {
            $SessionCleanupConfirmed = Remove-StockBotSessionAfterConfirmedStop `
                -Registration $RollbackRegistration `
                -SessionFile $SessionFilePath `
                -TrustedRoot $ProgramDataKnownRoot
            if (!$SessionCleanupConfirmed) {
                $RollbackErrors.Add(
                    "session cleanup was refused because the service stop was not confirmed"
                )
            }
        }
        catch {
            $RollbackErrors.Add("session cleanup failed: $($_.Exception.Message)")
        }
    }
    if ($RollbackErrors.Count -gt 0) {
        throw (
            "Live service activation failed and fail-closed rollback was incomplete. " +
            "Original failure: $($ActivationException.Message). " +
            "Rollback errors: $($RollbackErrors -join '; ')"
        )
    }
    throw $ActivationException
}
Write-Host "$DisplayName is running."
