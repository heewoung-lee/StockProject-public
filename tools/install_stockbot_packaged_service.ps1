param(
    [Parameter(Mandatory = $true)]
    [string]$PackageResourcesRoot,
    [switch]$AuthorizeLiveOrders
)

$ErrorActionPreference = "Stop"
$script:StockBotPackagedInstallExitCode = 90
trap {
    [Console]::Out.Write(
        "SBPSI1:{0:D2}" -f $script:StockBotPackagedInstallExitCode
    )
    exit $script:StockBotPackagedInstallExitCode
}
$ServiceName = "StockBotLive"
$PackagedCycleIntervalSeconds = 15
$InstallerRoot = Join-Path $PackageResourcesRoot "installer"
$InstallerScript = Join-Path $InstallerRoot "install_stockbot_service.ps1"
$UninstallerScript = Join-Path $InstallerRoot "uninstall_stockbot_service.ps1"
$InstallerHelpers = Join-Path $InstallerRoot "stockbot_service_installer_helpers.psm1"
$SourceBundlePath = Join-Path $PackageResourcesRoot "bundle"
$ConfigTemplatePath = Join-Path $PackageResourcesRoot "config.live.example.yaml"

function Assert-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    if (!$Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "StockBot packaged service installation requires administrator privileges."
    }
}

$script:StockBotPackagedInstallExitCode = 40
if (!$AuthorizeLiveOrders) {
    throw "The Windows installer did not receive explicit live service consent."
}

foreach ($RequiredPath in @(
    $InstallerScript,
    $UninstallerScript,
    $InstallerHelpers,
    $ConfigTemplatePath
)) {
    if (!(Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "The StockBot installer resource bundle is incomplete."
    }
}
if (!(Test-Path -LiteralPath $SourceBundlePath -PathType Container)) {
    throw "The StockBot service bundle is unavailable."
}

Import-Module -Name $InstallerHelpers -Force -ErrorAction Stop
Assert-Administrator
Assert-StockBotServiceBundleInventory `
    -Path $SourceBundlePath `
    -TrustedRoot $SourceBundlePath | Out-Null

$ProgramDataKnownRoot = Get-StockBotKnownFolderPath -Name ProgramData
$ProgramFilesKnownRoot = Get-StockBotKnownFolderPath -Name ProgramFiles
$ServiceRoots = Get-StockBotServiceInstallLayout `
    -ProgramFilesRoot $ProgramFilesKnownRoot
$ProgramDataRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $ProgramDataKnownRoot "StockBot")
)
$ServiceConfigPath = Join-Path $ProgramDataRoot "service-config.json"
$ServiceInstallRoot = $ServiceRoots.CurrentRoot
$LegacyServiceInstallRoot = $ServiceRoots.LegacyRoot
$RollbackRoot = Join-Path $ProgramDataRoot "installer-rollback"
$BackupBundleRoot = Join-Path $RollbackRoot "bundle"
$BackupServiceConfigPath = Join-Path $RollbackRoot "service-config.json"
$FreshRecoveryRoot = Join-Path $ProgramDataKnownRoot "StockBotInstallerRecovery"
$FreshStateFileAllowlist = @(
    "credentials.env",
    "config.live.yaml",
    "service-config.json",
    "bridge-session.json"
)
$FreshBackupPrepared = $false
$FreshProgramDataRootExisted = $false
$FreshInstallerRollbackExisted = $false

foreach ($ManagedDirectory in @(
    $ProgramDataRoot,
    $ServiceInstallRoot,
    $LegacyServiceInstallRoot,
    $RollbackRoot,
    $BackupBundleRoot,
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

function Get-StockBotExistingServiceInstallation {
    $Registration = Get-CimInstance `
        -ClassName Win32_Service `
        -Filter "Name='$ServiceName'" `
        -ErrorAction Stop
    if ($null -eq $Registration) {
        throw "StockBot service registration was not found."
    }
    if (
        ![string]::Equals(
            ([string]$Registration.StartName).Trim(),
            "LocalSystem",
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Existing StockBot service does not run as LocalSystem."
    }

    $CurrentCommand = Get-StockBotServiceCommandLine `
        -InstallRoot $ServiceInstallRoot `
        -ServiceConfigPath $ServiceConfigPath
    $LegacyCommand = Get-StockBotServiceCommandLine `
        -InstallRoot $LegacyServiceInstallRoot `
        -ServiceConfigPath $ServiceConfigPath
    $RegisteredCommand = ([string]$Registration.PathName).Trim()
    if (
        [string]::Equals(
            $RegisteredCommand,
            $CurrentCommand,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        $Layout = "Current"
        $InstallRoot = $ServiceInstallRoot
    }
    elseif (
        [string]::Equals(
            $RegisteredCommand,
            $LegacyCommand,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        $Layout = "Legacy"
        $InstallRoot = $LegacyServiceInstallRoot
    }
    else {
        throw "Existing StockBot service registration has an unexpected command."
    }

    $script:StockBotPackagedInstallExitCode = 43
    $BundleState = Get-StockBotServiceBundleState `
        -InstallRoot $InstallRoot `
        -TrustedRoot $ProgramFilesKnownRoot
    $BundleSourceRoot = $InstallRoot
    $RecoveredFromPrevious = $false
    $PreviousRoot = $InstallRoot + ".previous"
    $PreviousItem = Get-StockBotPathItemIfPresent -Path $PreviousRoot
    if ($null -ne $PreviousItem) {
        $PreviousState = Get-StockBotServiceBundleState `
            -InstallRoot $PreviousRoot `
            -TrustedRoot $ProgramFilesKnownRoot
        if ($PreviousState -notin @("complete", "legacy")) {
            throw "Existing StockBot previous service bundle is incomplete."
        }
        $BundleState = $PreviousState
        $BundleSourceRoot = $PreviousRoot
        $RecoveredFromPrevious = $true
    }
    elseif ($BundleState -eq "partial") {
        throw "Existing StockBot service bundle is incomplete."
    }
    return [pscustomobject]@{
        BundleState = $BundleState
        BundleSourceRoot = $BundleSourceRoot
        InstallRoot = $InstallRoot
        Layout = $Layout
        RecoveredFromPrevious = $RecoveredFromPrevious
        Registration = $Registration
    }
}

function Invoke-StockBotServiceControl {
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
        throw "StockBot service control failed."
    }
}
foreach ($ManagedFile in @(
    $ServiceConfigPath,
    $BackupServiceConfigPath
)) {
    Assert-StockBotTrustedPath `
        -Path $ManagedFile `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
}

function Remove-StockBotInstallerRollback {
    Assert-StockBotTrustedPath `
        -Path $RollbackRoot `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
    Remove-StockBotSafeTree `
        -Path $RollbackRoot `
        -TrustedRoot $ProgramDataKnownRoot
}

function New-StockBotInstallerRollback {
    param(
        [string]$SourceServiceRoot = "",
        [switch]$CreateBundleManifest,
        [switch]$RepairOnly
    )

    if (!$RepairOnly) {
        Assert-StockBotDirectoryTreeSafe `
            -Path $SourceServiceRoot `
            -TrustedRoot $ProgramFilesKnownRoot | Out-Null
    }
    Assert-StockBotTrustedPath `
        -Path $ServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null

    Remove-StockBotInstallerRollback
    New-StockBotSafeDirectory `
        -Path $RollbackRoot `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
    Set-StockBotRestrictedDirectoryAcl `
        -Path $RollbackRoot `
        -TrustedRoot $ProgramDataKnownRoot
    if (!$RepairOnly) {
        Copy-StockBotDirectorySnapshot `
            -Source $SourceServiceRoot `
            -SourceTrustedRoot $ProgramFilesKnownRoot `
            -Destination $BackupBundleRoot `
            -DestinationTrustedRoot $ProgramDataKnownRoot
        if ($CreateBundleManifest) {
            Write-StockBotServiceBundleManifest `
                -Path $BackupBundleRoot `
                -TrustedRoot $ProgramDataKnownRoot | Out-Null
        }
        Set-StockBotServiceBundleTreeOwner `
            -Path $BackupBundleRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
        Assert-StockBotServiceBundleTreeAcl `
            -Path $BackupBundleRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
        Assert-StockBotServiceBundleInventory `
            -Path $BackupBundleRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
    }

    Assert-StockBotTrustedPath `
        -Path $ServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    Assert-StockBotTrustedPath `
        -Path $BackupServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
    [System.IO.File]::Copy(
        $ServiceConfigPath,
        $BackupServiceConfigPath,
        $false
    )
    Assert-StockBotTrustedPath `
        -Path $BackupServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    if (!(Test-StockBotFilesEqual `
        -LeftPath $ServiceConfigPath `
        -LeftTrustedRoot $ProgramDataKnownRoot `
        -RightPath $BackupServiceConfigPath `
        -RightTrustedRoot $ProgramDataKnownRoot
    )) {
        throw "StockBot service rollback configuration verification failed."
    }
    if (!$RepairOnly) {
        Assert-StockBotTrustedPath `
            -Path (Join-Path $BackupBundleRoot "StockBotService.exe") `
            -TrustedRoot $ProgramDataKnownRoot `
            -ExpectedType File | Out-Null
    }
}

function Restore-StockBotExistingInstallation {
    param(
        [Parameter(Mandatory = $true)]
        [object]$ExistingPaths,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Current", "Legacy")]
        [string]$ServiceInstallLayout
    )

    Assert-StockBotDirectoryTreeSafe `
        -Path $BackupBundleRoot `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
    Assert-StockBotServiceBundleTreeAcl `
        -Path $BackupBundleRoot `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
    Assert-StockBotServiceBundleInventory `
        -Path $BackupBundleRoot `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
    Assert-StockBotTrustedPath `
        -Path $BackupServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    $RollbackArguments = @{
        AuthorizeLiveOrders = $true
        RegisterOnly = $true
        SourceBundlePath = $BackupBundleRoot
        StopExistingService = $true
        AllowServiceRootMigration = $true
        ServiceInstallLayout = $ServiceInstallLayout
        ProjectRoot = $ExistingPaths.ProjectRoot
        ConfigPath = $ExistingPaths.ConfigPath
        EnvFile = $ExistingPaths.EnvFile
        CycleIntervalSeconds = Resolve-StockBotPackagedCycleIntervalSeconds `
            -ExistingCycleIntervalSeconds $ExistingPaths.CycleIntervalSeconds `
            -CurrentCycleIntervalSeconds $PackagedCycleIntervalSeconds `
            -RestoreExisting
    }
    if ($ExistingPaths.AllowCredentialBootstrap) {
        $RollbackArguments.AllowCredentialBootstrap = $true
    }
    & $InstallerScript @RollbackArguments

    Assert-StockBotTrustedPath `
        -Path $BackupServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    Assert-StockBotTrustedPath `
        -Path $ServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    [System.IO.File]::Copy(
        $BackupServiceConfigPath,
        $ServiceConfigPath,
        $true
    )
    if (!(Test-StockBotFilesEqual `
        -LeftPath $BackupServiceConfigPath `
        -LeftTrustedRoot $ProgramDataKnownRoot `
        -RightPath $ServiceConfigPath `
        -RightTrustedRoot $ProgramDataKnownRoot
    )) {
        throw "Restored StockBot service configuration does not match its backup."
    }

    Set-Service -Name $ServiceName -StartupType Manual
    $StoppedRegistration = Wait-StockBotServiceStopped -Name $ServiceName
    $RestoredService = Get-Service -Name $ServiceName -ErrorAction Stop
    try {
        if (
            $RestoredService.Status -ne "Stopped" -or
            $RestoredService.StartType -ne "Manual" -or
            !(Test-StockBotServiceStopped -Registration $StoppedRegistration)
        ) {
            throw "Restored StockBot service did not remain Manual and Stopped with PID 0."
        }
    }
    finally {
        $RestoredService.Dispose()
    }
}

function Restore-StockBotMissingBundleRepair {
    param(
        [Parameter(Mandatory = $true)]
        [object]$ExistingInstallation
    )

    Assert-StockBotTrustedPath `
        -Path $BackupServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null

    Invoke-StockBotServiceControl -Arguments @(
        "failure",
        $ServiceName,
        "reset=", "0",
        "actions=", ""
    )
    Invoke-StockBotServiceControl -Arguments @(
        "failureflag",
        $ServiceName,
        "0"
    )
    Set-Service -Name $ServiceName -StartupType Manual
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
            -SessionFile (Join-Path $ProgramDataRoot "bridge-session.json") `
            -TrustedRoot $ProgramDataKnownRoot)
    ) {
        throw "StockBot repair could not confirm a stopped service."
    }

    $OriginalBinaryPath = Get-StockBotServiceCommandLine `
        -InstallRoot $ExistingInstallation.InstallRoot `
        -ServiceConfigPath $ServiceConfigPath
    Invoke-StockBotServiceControl -Arguments @(
        "config",
        $ServiceName,
        "binPath=", $OriginalBinaryPath
    )
    foreach ($RepairPath in @(
        $ServiceInstallRoot,
        ($ServiceInstallRoot + ".staging"),
        ($ServiceInstallRoot + ".previous")
    )) {
        Remove-StockBotSafeTree `
            -Path $RepairPath `
            -TrustedRoot $ProgramFilesKnownRoot
    }

    Assert-StockBotTrustedPath `
        -Path $ServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType File | Out-Null
    [System.IO.File]::Copy(
        $BackupServiceConfigPath,
        $ServiceConfigPath,
        $true
    )
    if (!(Test-StockBotFilesEqual `
        -LeftPath $BackupServiceConfigPath `
        -LeftTrustedRoot $ProgramDataKnownRoot `
        -RightPath $ServiceConfigPath `
        -RightTrustedRoot $ProgramDataKnownRoot
    )) {
        throw "StockBot repair configuration restore verification failed."
    }
}

function Remove-StockBotFreshStateBackup {
    Assert-StockBotTrustedPath `
        -Path $FreshRecoveryRoot `
        -TrustedRoot $ProgramDataKnownRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
    Remove-StockBotSafeTree `
        -Path $FreshRecoveryRoot `
        -TrustedRoot $ProgramDataKnownRoot
}

function New-StockBotFreshStateBackup {
    foreach ($OrphanedServicePath in @(
        $ServiceInstallRoot,
        ($ServiceInstallRoot + ".staging"),
        ($ServiceInstallRoot + ".previous"),
        $LegacyServiceInstallRoot,
        ($LegacyServiceInstallRoot + ".staging"),
        ($LegacyServiceInstallRoot + ".previous")
    )) {
        $OrphanedServiceItem = Get-StockBotPathItemIfPresent `
            -Path $OrphanedServicePath
        if ($null -ne $OrphanedServiceItem) {
            Assert-StockBotDirectoryTreeSafe `
                -Path $OrphanedServicePath `
                -TrustedRoot $ProgramFilesKnownRoot | Out-Null
            throw (
                "Refusing fresh installation over an unregistered StockBot " +
                "service bundle."
            )
        }
    }
    $ExistingProgramData = Get-StockBotPathItemIfPresent `
        -Path $ProgramDataRoot
    if ($null -ne $ExistingProgramData) {
        Assert-StockBotTrustedPath `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -ExpectedType Directory | Out-Null
        $script:FreshProgramDataRootExisted = $true
        $ExistingRollback = Get-StockBotPathItemIfPresent `
            -Path $RollbackRoot
        if ($null -ne $ExistingRollback) {
            Assert-StockBotDirectoryTreeSafe `
                -Path $RollbackRoot `
                -TrustedRoot $ProgramDataKnownRoot | Out-Null
            $script:FreshInstallerRollbackExisted = $true
        }
    }

    New-StockBotRestrictedDirectory `
        -Path $FreshRecoveryRoot `
        -TrustedRoot $ProgramDataKnownRoot | Out-Null
    try {
        Write-StockBotFreshRecoveryManifest `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist `
            -State "collecting" `
            -ProgramDataRootExisted $FreshProgramDataRootExisted `
            -InstallerRollbackExisted $FreshInstallerRollbackExisted
        Copy-StockBotAllowlistedFilesToSnapshot `
            -SourceRoot $ProgramDataRoot `
            -SourceTrustedRoot $ProgramDataKnownRoot `
            -SnapshotRoot $FreshRecoveryRoot `
            -SnapshotTrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist | Out-Null
        Write-StockBotFreshRecoveryManifest `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist `
            -State "prepared" `
            -ProgramDataRootExisted $FreshProgramDataRootExisted `
            -InstallerRollbackExisted $FreshInstallerRollbackExisted
        Assert-StockBotDirectoryTreeSafe `
            -Path $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
        $script:FreshBackupPrepared = $true
    }
    catch {
        Remove-StockBotFreshStateBackup
        throw
    }
}

function Restore-StockBotFreshStateBackup {
    $Recovery = Get-StockBotValidatedFreshRecoveryManifest `
        -RecoveryRoot $FreshRecoveryRoot `
        -TrustedRoot $ProgramDataKnownRoot `
        -FileNames $FreshStateFileAllowlist
    if ($Recovery.State -ne "prepared") {
        throw "StockBot fresh recovery is not prepared for restoration."
    }
    $BackupFiles = @($Recovery.FileNames)
    if ($Recovery.ProgramDataRootExisted -or $BackupFiles.Count -gt 0) {
        New-StockBotSafeDirectory `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
        Set-StockBotRestrictedDirectoryAcl `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -GrantCurrentIdentityRead
    }
    $RestoredFiles = @()
    if ($BackupFiles.Count -gt 0) {
        $RestoredFiles = @(
            Restore-StockBotAllowlistedFilesFromSnapshot `
                -SnapshotRoot $FreshRecoveryRoot `
                -SnapshotTrustedRoot $ProgramDataKnownRoot `
                -DestinationRoot $ProgramDataRoot `
                -DestinationTrustedRoot $ProgramDataKnownRoot `
                -FileNames $FreshStateFileAllowlist
        )
    }
    if ($RestoredFiles.Count -ne $BackupFiles.Count) {
        throw "StockBot fresh-install file restoration was incomplete."
    }
    Remove-StockBotFreshStateBackup
}

function Recover-StockBotInterruptedFreshInstall {
    $ExistingRecovery = Get-StockBotPathItemIfPresent `
        -Path $FreshRecoveryRoot
    if ($null -eq $ExistingRecovery) {
        return
    }
    try {
        $Recovery = Get-StockBotValidatedFreshRecoveryManifest `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist
    }
    catch {
        if (!(Test-StockBotFreshRecoveryContainsOnlyMetadata `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist
        )) {
            throw
        }
        Remove-StockBotFreshStateBackup
        return
    }
    if ($Recovery.State -in @("collecting", "committed")) {
        Remove-StockBotFreshStateBackup
        return
    }

    $UninstallArguments = @{
        PreserveFreshRecovery = $true
    }
    if ($Recovery.InstallerRollbackExisted) {
        $UninstallArguments.PreserveInstallerRollback = $true
    }
    & $UninstallerScript @UninstallArguments
    Restore-StockBotFreshStateBackup
}

Recover-StockBotInterruptedFreshInstall

$script:StockBotPackagedInstallExitCode = 41
$ExistingServiceController = Get-Service `
    -Name $ServiceName `
    -ErrorAction SilentlyContinue
$HadExistingService = $null -ne $ExistingServiceController
if ($null -ne $ExistingServiceController) {
    $ExistingServiceController.Dispose()
}
$InstallArguments = @{
    AuthorizeLiveOrders = $true
    AllowServiceRootMigration = $true
    ServiceInstallLayout = "Current"
    SourceBundlePath = [System.IO.Path]::GetFullPath($SourceBundlePath)
    StopExistingService = $true
}
$ExistingPaths = $null
$ExistingInstallation = $null

if ($HadExistingService) {
    $ExistingInstallation = Get-StockBotExistingServiceInstallation
    $script:StockBotPackagedInstallExitCode = 42
    Assert-StockBotTrustedPath `
        -Path $ServiceConfigPath `
        -TrustedRoot $ProgramDataKnownRoot `
        -ExpectedType File | Out-Null
    $ExistingPaths = Get-StockBotValidatedServiceConfigPaths `
        -ServiceConfigPath $ServiceConfigPath
    $InstallArguments.ProjectRoot = $ExistingPaths.ProjectRoot
    $InstallArguments.ConfigPath = $ExistingPaths.ConfigPath
    $InstallArguments.EnvFile = $ExistingPaths.EnvFile
    $InstallArguments.CycleIntervalSeconds = (
        Resolve-StockBotPackagedCycleIntervalSeconds `
            -ExistingCycleIntervalSeconds $ExistingPaths.CycleIntervalSeconds `
            -CurrentCycleIntervalSeconds $PackagedCycleIntervalSeconds
    )
    $CredentialBindingState = $ExistingPaths.CredentialBindingState
    if (
        $ExistingPaths.AllowCredentialBootstrap -and
        $CredentialBindingState -eq "pending"
    ) {
        $InstallArguments.AllowCredentialBootstrap = $true
    }
}
else {
    $ConfigPath = Join-Path $ProgramDataRoot "config.live.yaml"
    $EnvFile = Join-Path $ProgramDataRoot "credentials.env"
}

$BackupMode = "none"
try {
    if ($HadExistingService) {
        $script:StockBotPackagedInstallExitCode = 44
        if ($ExistingInstallation.BundleState -eq "complete") {
            New-StockBotInstallerRollback `
                -SourceServiceRoot $ExistingInstallation.BundleSourceRoot
            $BackupMode = "rollback"
        }
        elseif ($ExistingInstallation.BundleState -eq "legacy") {
            New-StockBotInstallerRollback `
                -SourceServiceRoot $ExistingInstallation.BundleSourceRoot `
                -CreateBundleManifest
            $BackupMode = "rollback"
        }
        elseif ($ExistingInstallation.BundleState -eq "missing") {
            New-StockBotInstallerRollback -RepairOnly
            $BackupMode = "repair"
        }
        else {
            throw "Existing StockBot service bundle state is unsupported."
        }
    }
    else {
        New-StockBotFreshStateBackup
        New-StockBotSafeDirectory `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot | Out-Null
        # Proxy-UAC cannot safely identify the unelevated caller here. Broad token
        # read ACLs remain intentionally forbidden, so that case fails closed.
        Set-StockBotRestrictedDirectoryAcl `
            -Path $ProgramDataRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -GrantCurrentIdentityRead
        Assert-StockBotTrustedPath `
            -Path $ConfigPath `
            -TrustedRoot $ProgramDataKnownRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        if (!(Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            Assert-StockBotTrustedPath `
                -Path $ConfigPath `
                -TrustedRoot $ProgramDataKnownRoot `
                -AllowMissing `
                -ExpectedType File | Out-Null
            [System.IO.File]::Copy($ConfigTemplatePath, $ConfigPath, $false)
        }
        Assert-StockBotTrustedPath `
            -Path $EnvFile `
            -TrustedRoot $ProgramDataKnownRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        if (!(Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
            $Utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
            Assert-StockBotTrustedPath `
                -Path $EnvFile `
                -TrustedRoot $ProgramDataKnownRoot `
                -AllowMissing `
                -ExpectedType File | Out-Null
            [System.IO.File]::WriteAllText($EnvFile, "", $Utf8WithoutBom)
        }
        $InstallArguments.ProjectRoot = $ProgramDataRoot
        $InstallArguments.ConfigPath = $ConfigPath
        $InstallArguments.EnvFile = $EnvFile
        $InstallArguments.CycleIntervalSeconds = $PackagedCycleIntervalSeconds
        $InstallArguments.AllowCredentialBootstrap = $true
    }
    $script:StockBotPackagedInstallExitCode = 45
    & $InstallerScript @InstallArguments
    if (!$HadExistingService) {
        Write-StockBotFreshRecoveryManifest `
            -RecoveryRoot $FreshRecoveryRoot `
            -TrustedRoot $ProgramDataKnownRoot `
            -FileNames $FreshStateFileAllowlist `
            -State "committed" `
            -ProgramDataRootExisted $FreshProgramDataRootExisted `
            -InstallerRollbackExisted $FreshInstallerRollbackExisted
        Remove-StockBotFreshStateBackup
        $FreshBackupPrepared = $false
    }
}
catch {
    if ($HadExistingService) {
        if ($BackupMode -eq "none") {
            throw "StockBot service update failed before a rollback backup was ready."
        }
        $script:StockBotPackagedInstallExitCode = 46
        if ($BackupMode -eq "rollback") {
            try {
                Restore-StockBotExistingInstallation `
                    -ExistingPaths $ExistingPaths `
                    -ServiceInstallLayout $ExistingInstallation.Layout
            }
            catch {
                throw "StockBot service update failed and previous-version rollback was incomplete."
            }
            try {
                Remove-StockBotInstallerRollback
            }
            catch {
                # The service remains Manual and Stopped; protected backup may remain.
            }
            throw "StockBot service update failed; the previous service was restored but left stopped."
        }
        try {
            Restore-StockBotMissingBundleRepair `
                -ExistingInstallation $ExistingInstallation
        }
        catch {
            throw "StockBot service repair failed and fail-closed cleanup was incomplete."
        }
        try {
            Remove-StockBotInstallerRollback
        }
        catch {
            # The service remains Manual and Stopped; protected backup may remain.
        }
        throw "StockBot service repair failed; it was left stopped for a safe retry."
    }
    if (!$FreshBackupPrepared) {
        throw "StockBot fresh installation failed before managed state was changed."
    }
    try {
        $UninstallArguments = @{
            PreserveFreshRecovery = $true
        }
        if ($FreshInstallerRollbackExisted) {
            $UninstallArguments.PreserveInstallerRollback = $true
        }
        & $UninstallerScript @UninstallArguments
    }
    catch {
        throw "StockBot fresh installation failed and fail-closed cleanup was incomplete."
    }
    try {
        Restore-StockBotFreshStateBackup
    }
    catch {
        throw "StockBot fresh installation failed and original private files could not be restored."
    }
    throw "StockBot fresh installation failed; partial state was removed and original files restored."
}

if ($BackupMode -ne "none") {
    try {
        Remove-StockBotInstallerRollback
    }
    catch {
        # The running service is healthy; the protected backup is replaced next update.
    }
}

if ($HadExistingService -and $ExistingInstallation.Layout -eq "Legacy") {
    foreach ($LegacyPath in @(
        $LegacyServiceInstallRoot,
        ($LegacyServiceInstallRoot + ".staging"),
        ($LegacyServiceInstallRoot + ".previous")
    )) {
        try {
            Remove-StockBotSafeTree `
                -Path $LegacyPath `
                -TrustedRoot $ProgramFilesKnownRoot
        }
        catch {
            # The current service is healthy and no longer references this path.
        }
    }
}
[Console]::Out.Write("SBPSI1:00")
