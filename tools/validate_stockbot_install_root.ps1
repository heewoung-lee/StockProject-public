param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = "Stop"
$script:StockBotValidationExitCode = 90

function Assert-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    if (!$Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "StockBot installation requires administrator privileges."
    }
}

function Assert-NoReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($Path)
    while ($Pending.Count -gt 0) {
        $CurrentPath = $Pending.Pop()
        $CurrentItem = Get-Item `
            -LiteralPath $CurrentPath `
            -Force `
            -ErrorAction Stop
        if (
            ($CurrentItem.Attributes -band
                [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "StockBot install path cannot contain a reparse point."
        }
        if (!$CurrentItem.PSIsContainer) {
            continue
        }
        foreach ($Child in @(
            Get-ChildItem `
                -LiteralPath $CurrentPath `
                -Force `
                -ErrorAction Stop
        )) {
            if (
                ($Child.Attributes -band
                    [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "StockBot install path cannot contain a reparse point."
            }
            if ($Child.PSIsContainer) {
                $Pending.Push($Child.FullName)
            }
        }
    }
}

function Invoke-Icacls {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $Icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    & $Icacls @Arguments 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "StockBot install path ACL configuration failed."
    }
}

function Set-StockBotDirectoryInheritance {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($Path)
    while ($Pending.Count -gt 0) {
        $CurrentPath = $Pending.Pop()
        $CurrentItem = Get-Item `
            -LiteralPath $CurrentPath `
            -Force `
            -ErrorAction Stop
        if (
            !$CurrentItem.PSIsContainer -or
            ($CurrentItem.Attributes -band
                [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "StockBot install path contains an invalid directory."
        }
        Invoke-Icacls -Arguments @(
            $CurrentPath,
            "/grant:r",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
            "*S-1-5-32-545:(OI)(CI)RX",
            "/Q"
        )
        foreach ($Child in @(
            Get-ChildItem `
                -LiteralPath $CurrentPath `
                -Force `
                -Directory `
                -ErrorAction Stop
        )) {
            if (
                ($Child.Attributes -band
                    [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "StockBot install path cannot contain a reparse point."
            }
            $Pending.Push($Child.FullName)
        }
    }
}

function Get-StockBotWriteRightsMask {
    $SpecificWriteRights = [int64](
        [System.Security.AccessControl.FileSystemRights]::WriteData -bor
        [System.Security.AccessControl.FileSystemRights]::AppendData -bor
        [System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [System.Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [System.Security.AccessControl.FileSystemRights]::Delete -bor
        [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [System.Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    return (
        $SpecificWriteRights -bor
        [int64]0x40000000 -bor
        [int64]0x10000000
    )
}

function Test-StockBotRightsIncludeWrite {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemRights]$Rights
    )

    return (
        (
            [int64]$Rights -band (Get-StockBotWriteRightsMask)
        ) -ne 0
    )
}

function Assert-RestrictedInstallAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $AdministratorsSid = "S-1-5-32-544"
    $SystemSid = "S-1-5-18"
    $UsersSid = "S-1-5-32-545"
    $FullControl = [int64](
        [System.Security.AccessControl.FileSystemRights]::FullControl
    )
    $ReadAndExecute = [int64](
        [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
    )
    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($Path)
    while ($Pending.Count -gt 0) {
        $script:StockBotValidationExitCode = 23
        $CurrentPath = $Pending.Pop()
        $CurrentItem = Get-Item `
            -LiteralPath $CurrentPath `
            -Force `
            -ErrorAction Stop
        $Acl = Get-Acl -LiteralPath $CurrentPath -ErrorAction Stop
        $Owner = $Acl.GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        $script:StockBotValidationExitCode = 29
        if ($Owner -notin @($AdministratorsSid, $SystemSid)) {
            throw "StockBot install path owner is not trusted."
        }
        $script:StockBotValidationExitCode = 30
        if (!$Acl.AreAccessRulesProtected) {
            throw "StockBot install path is missing required access rules."
        }
        $HasAdministratorsFullControl = $false
        $HasSystemFullControl = $false
        $HasUsersReadAndExecute = $false
        foreach ($Rule in @(
            $Acl.GetAccessRules(
                $true,
                $true,
                [System.Security.Principal.SecurityIdentifier]
            )
        )) {
            $RuleSid = $Rule.IdentityReference.Value
            if (
                $Rule.AccessControlType -eq
                    [System.Security.AccessControl.AccessControlType]::Deny
            ) {
                throw "StockBot install path is missing required access rules."
            }
            if (
                $Rule.AccessControlType -ne
                    [System.Security.AccessControl.AccessControlType]::Allow
            ) {
                continue
            }
            $RuleRights = [int64]$Rule.FileSystemRights
            if (
                $RuleSid -eq $AdministratorsSid -and
                ($RuleRights -band $FullControl) -eq $FullControl
            ) {
                $HasAdministratorsFullControl = $true
            }
            if (
                $RuleSid -eq $SystemSid -and
                ($RuleRights -band $FullControl) -eq $FullControl
            ) {
                $HasSystemFullControl = $true
            }
            if (
                $RuleSid -eq $UsersSid -and
                ($RuleRights -band $ReadAndExecute) -eq $ReadAndExecute
            ) {
                $HasUsersReadAndExecute = $true
            }
            if (
                $RuleSid -notin @($AdministratorsSid, $SystemSid) -and
                (
                    Test-StockBotRightsIncludeWrite `
                        -Rights $Rule.FileSystemRights
                )
            ) {
                throw "StockBot install path grants write access to an untrusted identity."
            }
        }
        if (
            !$HasAdministratorsFullControl -or
            !$HasSystemFullControl -or
            !$HasUsersReadAndExecute
        ) {
            throw "StockBot install path is missing required access rules."
        }
        if ($CurrentItem.PSIsContainer) {
            $script:StockBotValidationExitCode = 23
            foreach ($Child in @(
                Get-ChildItem `
                    -LiteralPath $CurrentPath `
                    -Force `
                    -ErrorAction Stop
            )) {
                $Pending.Push($Child.FullName)
            }
        }
    }
}

try {
    $script:StockBotValidationExitCode = 20
    Assert-Administrator

    $script:StockBotValidationExitCode = 21
    $ProgramFilesRoot = [System.Environment]::GetFolderPath(
        [System.Environment+SpecialFolder]::ProgramFiles
    )
    $ExpectedRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $ProgramFilesRoot "StockBot")
    ).TrimEnd("\")
    $ResolvedInstallRoot = [System.IO.Path]::GetFullPath(
        $InstallRoot
    ).TrimEnd("\")
    if (
        ![string]::Equals(
            $ResolvedInstallRoot,
            $ExpectedRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "StockBot must be installed in the fixed Program Files location."
    }

    $script:StockBotValidationExitCode = 22
    $ProgramFilesItem = Get-Item `
        -LiteralPath $ProgramFilesRoot `
        -Force `
        -ErrorAction Stop
    if (
        !$ProgramFilesItem.PSIsContainer -or
        ($ProgramFilesItem.Attributes -band
            [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "The Windows Program Files root is not trusted."
    }

    $script:StockBotValidationExitCode = 23
    if (!(Test-Path -LiteralPath $ResolvedInstallRoot -PathType Container)) {
        New-Item `
            -ItemType Directory `
            -Path $ResolvedInstallRoot `
            -ErrorAction Stop | Out-Null
    }

    $script:StockBotValidationExitCode = 24
    Assert-NoReparsePoint -Path $ResolvedInstallRoot

    $script:StockBotValidationExitCode = 25
    Invoke-Icacls -Arguments @(
        $ResolvedInstallRoot,
        "/setowner", "*S-1-5-32-544",
        "/T", "/C", "/Q"
    )

    $script:StockBotValidationExitCode = 26
    Invoke-Icacls -Arguments @(
        $ResolvedInstallRoot,
        "/reset",
        "/T", "/C", "/Q"
    )

    $script:StockBotValidationExitCode = 27
    Invoke-Icacls -Arguments @(
        $ResolvedInstallRoot,
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
        "*S-1-5-32-545:RX",
        "/T", "/C", "/Q"
    )
    Set-StockBotDirectoryInheritance -Path $ResolvedInstallRoot

    $script:StockBotValidationExitCode = 28
    Assert-NoReparsePoint -Path $ResolvedInstallRoot

    $script:StockBotValidationExitCode = 29
    Assert-RestrictedInstallAcl -Path $ResolvedInstallRoot

    [Console]::Out.Write("SBIRV1:00")
    exit 0
} catch {
    exit $script:StockBotValidationExitCode
}
