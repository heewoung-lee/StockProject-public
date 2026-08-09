Set-StrictMode -Version Latest

function ConvertTo-StockBotWindowsNativeArguments {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string[]]$Arguments
    )

    return @(
        foreach ($Argument in $Arguments) {
            if ($Argument.Length -eq 0) {
                '""'
                continue
            }
            if ($Argument -notmatch '[\s"]') {
                $Argument
                continue
            }

            $Builder = [System.Text.StringBuilder]::new()
            [void]$Builder.Append([char]34)
            $BackslashCount = 0
            foreach ($Character in $Argument.ToCharArray()) {
                if ($Character -eq [char]92) {
                    $BackslashCount++
                    continue
                }
                if ($Character -eq [char]34) {
                    [void]$Builder.Append(
                        [char]92,
                        (($BackslashCount * 2) + 1)
                    )
                    [void]$Builder.Append([char]34)
                    $BackslashCount = 0
                    continue
                }
                if ($BackslashCount -gt 0) {
                    [void]$Builder.Append([char]92, $BackslashCount)
                    $BackslashCount = 0
                }
                [void]$Builder.Append($Character)
            }
            if ($BackslashCount -gt 0) {
                [void]$Builder.Append([char]92, ($BackslashCount * 2))
            }
            [void]$Builder.Append([char]34)
            $Builder.ToString()
        }
    )
}

function Invoke-StockBotWindowsNativeProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string[]]$Arguments
    )

    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        throw "StockBot native process path is unavailable."
    }
    $NativeArguments = @(
        ConvertTo-StockBotWindowsNativeArguments -Arguments $Arguments
    )
    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $FilePath
    $StartInfo.Arguments = [string]::Join(" ", $NativeArguments)
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true

    $Process = $null
    try {
        $Process = [System.Diagnostics.Process]::Start($StartInfo)
        if ($null -eq $Process) {
            throw "StockBot native process did not start."
        }
        $StandardOutputTask = $Process.StandardOutput.ReadToEndAsync()
        $StandardErrorTask = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        $StandardOutput = $StandardOutputTask.GetAwaiter().GetResult()
        $StandardError = $StandardErrorTask.GetAwaiter().GetResult()
        $ExitCode = $Process.ExitCode
    }
    finally {
        if ($null -ne $Process) {
            $Process.Dispose()
        }
    }

    return [pscustomobject]@{
        ExitCode = $ExitCode
        StandardOutput = $StandardOutput
        StandardError = $StandardError
    }
}

function Test-StockBotPathWithinRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $FullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $FullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (
        $FullPath.Equals(
            $FullRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $true
    }
    return $FullPath.StartsWith(
        $FullRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-StockBotKnownFolderPath {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("ProgramData", "ProgramFiles")]
        [string]$Name
    )

    $SpecialFolder = if ($Name -eq "ProgramData") {
        [System.Environment+SpecialFolder]::CommonApplicationData
    }
    else {
        [System.Environment+SpecialFolder]::ProgramFiles
    }
    $KnownPath = [System.Environment]::GetFolderPath($SpecialFolder)
    if (
        [string]::IsNullOrWhiteSpace($KnownPath) -or
        ![System.IO.Path]::IsPathRooted($KnownPath)
    ) {
        throw "Windows known folder '$Name' is unavailable."
    }
    $FullPath = [System.IO.Path]::GetFullPath($KnownPath)
    $Item = Get-Item -LiteralPath $FullPath -Force -ErrorAction Stop
    if (!$Item.PSIsContainer) {
        throw "Windows known folder '$Name' is not a directory."
    }
    if (
        ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Windows known folder '$Name' cannot be a reparse point."
    }
    return $FullPath
}

function Get-StockBotServiceInstallLayout {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProgramFilesRoot
    )

    $ResolvedProgramFilesRoot = [System.IO.Path]::GetFullPath(
        $ProgramFilesRoot
    )
    return [pscustomobject]@{
        CurrentRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $ResolvedProgramFilesRoot "StockBotService")
        )
        LegacyRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $ResolvedProgramFilesRoot "StockBot\Service")
        )
    }
}

function Get-StockBotServiceCommandLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallRoot,
        [Parameter(Mandatory = $true)]
        [string]$ServiceConfigPath
    )

    $Executable = Join-Path `
        ([System.IO.Path]::GetFullPath($InstallRoot)) `
        "StockBotService.exe"
    $ResolvedServiceConfigPath = [System.IO.Path]::GetFullPath(
        $ServiceConfigPath
    )
    return (
        "`"$Executable`" run-service " +
        "--service-config `"$ResolvedServiceConfigPath`""
    )
}

function Get-StockBotServiceBundleState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InstallRoot,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $ResolvedInstallRoot = Assert-StockBotTrustedPath `
        -Path $InstallRoot `
        -TrustedRoot $TrustedRoot `
        -AllowMissing `
        -ExpectedType Directory
    $InstallItem = Get-StockBotPathItemIfPresent -Path $ResolvedInstallRoot
    if ($null -eq $InstallItem) {
        return "missing"
    }

    Assert-StockBotServiceBundleTreeAcl `
        -Path $ResolvedInstallRoot `
        -TrustedRoot $TrustedRoot | Out-Null
    $ManifestPath = Join-Path `
        $ResolvedInstallRoot `
        "stockbot-service-bundle-manifest.json"
    if (!(Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        try {
            Assert-StockBotLegacyServiceBundleStructure `
                -Path $ResolvedInstallRoot `
                -TrustedRoot $TrustedRoot | Out-Null
        }
        catch {
            return "partial"
        }
        return "legacy"
    }
    try {
        Assert-StockBotServiceBundleInventory `
            -Path $ResolvedInstallRoot `
            -TrustedRoot $TrustedRoot | Out-Null
    }
    catch {
        return "partial"
    }
    return "complete"
}

function Assert-StockBotLegacyServiceBundleStructure {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $BundlePath = Assert-StockBotDirectoryTreeSafe `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $RootEntries = @(
        Get-ChildItem `
            -LiteralPath $BundlePath `
            -Force `
            -ErrorAction Stop
    )
    if ($RootEntries.Count -ne 2) {
        throw "Legacy StockBot service bundle root inventory is invalid."
    }
    $Executable = Join-Path $BundlePath "StockBotService.exe"
    $InternalRoot = Join-Path $BundlePath "_internal"
    foreach ($Entry in $RootEntries) {
        if (
            (
                $Entry.Name -eq "StockBotService.exe" -and
                !$Entry.PSIsContainer
            ) -or
            (
                $Entry.Name -eq "_internal" -and
                $Entry.PSIsContainer
            )
        ) {
            continue
        }
        throw "Legacy StockBot service bundle contains an unexpected root entry."
    }
    foreach ($RequiredFile in @(
        $Executable,
        (Join-Path $InternalRoot "base_library.zip"),
        (Join-Path $InternalRoot "data\symbols.csv"),
        (Join-Path $InternalRoot "win32\win32service.pyd")
    )) {
        $ResolvedFile = Assert-StockBotTrustedPath `
            -Path $RequiredFile `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File
        if ((Get-Item -LiteralPath $ResolvedFile -Force).Length -le 0) {
            throw "Legacy StockBot service bundle contains an empty required file."
        }
    }
    $PythonRuntime = @(
        Get-ChildItem `
            -LiteralPath $InternalRoot `
            -File `
            -Filter "python3*.dll" `
            -Force `
            -ErrorAction Stop
    )
    $PyWinTypes = @(
        Get-ChildItem `
            -LiteralPath (Join-Path $InternalRoot "pywin32_system32") `
            -File `
            -Filter "pywintypes*.dll" `
            -Force `
            -ErrorAction Stop
    )
    if ($PythonRuntime.Count -ne 1 -or $PyWinTypes.Count -ne 1) {
        throw "Legacy StockBot service bundle runtime inventory is invalid."
    }
    foreach ($RuntimeFile in @($PythonRuntime + $PyWinTypes)) {
        Assert-StockBotTrustedPath `
            -Path $RuntimeFile.FullName `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
        if ($RuntimeFile.Length -le 0) {
            throw "Legacy StockBot service bundle contains an empty runtime file."
        }
    }
    return $BundlePath
}

function Assert-StockBotServiceBundleInventory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $BundlePath = Assert-StockBotDirectoryTreeSafe `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $ManifestName = "stockbot-service-bundle-manifest.json"
    $ManifestPath = Assert-StockBotTrustedPath `
        -Path (Join-Path $BundlePath $ManifestName) `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File
    $ManifestItem = Get-Item `
        -LiteralPath $ManifestPath `
        -Force `
        -ErrorAction Stop
    if ($ManifestItem.Length -gt 16777216) {
        throw "StockBot service bundle manifest is too large."
    }
    try {
        $Payload = [System.IO.File]::ReadAllText($ManifestPath) |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "StockBot service bundle manifest is not valid JSON."
    }
    if (
        $null -eq $Payload -or
        $Payload.schemaVersion -ne 1 -or
        $Payload.algorithm -ne "SHA256"
    ) {
        throw "StockBot service bundle manifest schema is invalid."
    }

    $ManifestFiles = @($Payload.files)
    if ($ManifestFiles.Count -lt 2 -or $ManifestFiles.Count -gt 20000) {
        throw "StockBot service bundle manifest file count is invalid."
    }
    $ManifestByPath = @{}
    foreach ($File in $ManifestFiles) {
        if ($null -eq $File) {
            throw "StockBot service bundle manifest contains an invalid entry."
        }
        $RelativePath = ([string]$File.path).Trim()
        $ExpectedHash = ([string]$File.sha256).Trim().ToLowerInvariant()
        $ExpectedSize = [long]0
        if (
            [string]::IsNullOrWhiteSpace($RelativePath) -or
            [System.IO.Path]::IsPathRooted($RelativePath) -or
            $RelativePath.Contains("\") -or
            $RelativePath.Contains(":") -or
            $RelativePath.StartsWith("/") -or
            $RelativePath.EndsWith("/") -or
            $RelativePath -match "(^|/)\.{1,2}(/|$)" -or
            $RelativePath -eq $ManifestName -or
            $ExpectedHash -notmatch "^[0-9a-f]{64}$" -or
            ![long]::TryParse([string]$File.size, [ref]$ExpectedSize) -or
            $ExpectedSize -lt 0 -or
            $ManifestByPath.ContainsKey($RelativePath)
        ) {
            throw "StockBot service bundle manifest contains an invalid entry."
        }
        foreach ($Component in @($RelativePath -split "/")) {
            if (
                [string]::IsNullOrWhiteSpace($Component) -or
                $Component.IndexOfAny(
                    [System.IO.Path]::GetInvalidFileNameChars()
                ) -ge 0
            ) {
                throw "StockBot service bundle manifest path is invalid."
            }
        }
        $ManifestByPath[$RelativePath] = [pscustomobject]@{
            Hash = $ExpectedHash
            Size = $ExpectedSize
        }
    }
    foreach ($RequiredPath in @(
        "StockBotService.exe",
        "_internal/data/symbols.csv"
    )) {
        if (!$ManifestByPath.ContainsKey($RequiredPath)) {
            throw "StockBot service bundle manifest is missing a required file."
        }
    }

    $ActualByPath = @{}
    foreach ($ActualFile in @(
        Get-ChildItem `
            -LiteralPath $BundlePath `
            -File `
            -Recurse `
            -Force `
            -ErrorAction Stop
    )) {
        $ResolvedFile = Assert-StockBotTrustedPath `
            -Path $ActualFile.FullName `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File
        $RelativePath = $ResolvedFile.Substring($BundlePath.Length).TrimStart(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        ).Replace("\", "/")
        if ($RelativePath -eq $ManifestName) {
            continue
        }
        if (
            $ActualByPath.ContainsKey($RelativePath) -or
            !$ManifestByPath.ContainsKey($RelativePath)
        ) {
            throw "StockBot service bundle file inventory is inconsistent."
        }
        $ActualByPath[$RelativePath] = $true
        $Expected = $ManifestByPath[$RelativePath]
        if (
            [long]$ActualFile.Length -ne [long]$Expected.Size -or
            (Get-StockBotFileSha256 `
                -Path $ResolvedFile `
                -TrustedRoot $TrustedRoot) -ne $Expected.Hash
        ) {
            throw "StockBot service bundle file verification failed."
        }
    }
    if ($ActualByPath.Count -ne $ManifestByPath.Count) {
        throw "StockBot service bundle file inventory is incomplete."
    }
    return $BundlePath
}

function Write-StockBotServiceBundleManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $BundlePath = Assert-StockBotLegacyServiceBundleStructure `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $ManifestPath = Join-Path `
        $BundlePath `
        "stockbot-service-bundle-manifest.json"
    $TemporaryManifestPath = $ManifestPath + ".tmp"
    foreach ($Candidate in @($ManifestPath, $TemporaryManifestPath)) {
        Assert-StockBotTrustedPath `
            -Path $Candidate `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
    }
    if (Test-Path -LiteralPath $ManifestPath) {
        throw "StockBot service bundle manifest already exists."
    }
    Remove-StockBotSafeFile `
        -Path $TemporaryManifestPath `
        -TrustedRoot $TrustedRoot

    $Files = @(
        foreach ($File in @(
            Get-ChildItem `
                -LiteralPath $BundlePath `
                -File `
                -Recurse `
                -Force `
                -ErrorAction Stop
        )) {
            $ResolvedFile = Assert-StockBotTrustedPath `
                -Path $File.FullName `
                -TrustedRoot $TrustedRoot `
                -ExpectedType File
            $RelativePath = $ResolvedFile.Substring(
                $BundlePath.Length
            ).TrimStart(
                [System.IO.Path]::DirectorySeparatorChar,
                [System.IO.Path]::AltDirectorySeparatorChar
            ).Replace("\", "/")
            [pscustomobject]@{
                FullName = $ResolvedFile
                RelativePath = $RelativePath
                Length = [long]$File.Length
            }
        }
    )
    $ManifestFiles = @(
        foreach ($File in @($Files | Sort-Object -Property RelativePath)) {
            [ordered]@{
                path = $File.RelativePath
                sha256 = Get-StockBotFileSha256 `
                    -Path $File.FullName `
                    -TrustedRoot $TrustedRoot
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
        $TemporaryManifestPath,
        (($Payload | ConvertTo-Json -Depth 4) + "`n"),
        $Utf8WithoutBom
    )
    [System.IO.File]::Move($TemporaryManifestPath, $ManifestPath)
    Assert-StockBotServiceBundleInventory `
        -Path $BundlePath `
        -TrustedRoot $TrustedRoot | Out-Null
    return $ManifestPath
}

function Get-StockBotPathItemIfPresent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    try {
        return Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
}

function Assert-StockBotTrustedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [switch]$AllowMissing,
        [ValidateSet("Any", "File", "Directory")]
        [string]$ExpectedType = "Any"
    )

    $FullRoot = [System.IO.Path]::GetFullPath($TrustedRoot).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $FullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (!(Test-StockBotPathWithinRoot -Path $FullPath -Root $FullRoot)) {
        throw "StockBot managed path escaped its trusted root."
    }

    $RootItem = Get-Item `
        -LiteralPath $FullRoot `
        -Force `
        -ErrorAction Stop
    if (!$RootItem.PSIsContainer) {
        throw "StockBot trusted root is not a directory."
    }
    if (
        ($RootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne
        0
    ) {
        throw "StockBot trusted root cannot be a reparse point."
    }

    $RelativePath = $FullPath.Substring($FullRoot.Length).TrimStart(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $CurrentPath = $FullRoot
    $FinalItem = $RootItem
    $MissingComponentFound = $false
    if ($RelativePath) {
        $Components = @(
            $RelativePath -split "[\\/]" |
                Where-Object { $_ -ne "" }
        )
        for ($Index = 0; $Index -lt $Components.Count; $Index++) {
            $Component = [string]$Components[$Index]
            if ($Component.Contains(":")) {
                throw "StockBot managed path cannot contain an alternate data stream."
            }
            $CurrentPath = Join-Path $CurrentPath $Component
            $CurrentItem = Get-StockBotPathItemIfPresent -Path $CurrentPath
            if ($null -eq $CurrentItem) {
                if (!$AllowMissing) {
                    throw "StockBot managed path does not exist."
                }
                $MissingComponentFound = $true
                $FinalItem = $null
                continue
            }
            if ($MissingComponentFound) {
                throw "StockBot managed path has an inconsistent missing component."
            }
            if (
                ($CurrentItem.Attributes -band
                    [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "StockBot managed path cannot contain a reparse point."
            }
            if (
                $Index -lt ($Components.Count - 1) -and
                !$CurrentItem.PSIsContainer
            ) {
                throw "StockBot managed path has a non-directory component."
            }
            $FinalItem = $CurrentItem
        }
    }

    if ($null -ne $FinalItem) {
        if ($ExpectedType -eq "File" -and $FinalItem.PSIsContainer) {
            throw "StockBot managed path must be a file."
        }
        if ($ExpectedType -eq "Directory" -and !$FinalItem.PSIsContainer) {
            throw "StockBot managed path must be a directory."
        }
    }
    return $FullPath
}

function Assert-StockBotDirectoryTreeSafe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory
    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($FullPath)
    while ($Pending.Count -gt 0) {
        $Current = $Pending.Pop()
        foreach ($Child in @(
            Get-ChildItem -LiteralPath $Current -Force -ErrorAction Stop
        )) {
            $ChildPath = Assert-StockBotTrustedPath `
                -Path $Child.FullName `
                -TrustedRoot $TrustedRoot
            if ($Child.PSIsContainer) {
                $Pending.Push($ChildPath)
            }
        }
    }
    return $FullPath
}

function New-StockBotSafeDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -AllowMissing `
        -ExpectedType Directory
    if (!(Test-Path -LiteralPath $FullPath -PathType Container)) {
        New-Item `
            -ItemType Directory `
            -Path $FullPath `
            -Force `
            -ErrorAction Stop | Out-Null
    }
    return Assert-StockBotTrustedPath `
        -Path $FullPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory
}

function Remove-StockBotSafeFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [int]$MaxAttempts = 5
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
        $FullPath = Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File
        $Item = Get-StockBotPathItemIfPresent -Path $FullPath
        if ($null -eq $Item) {
            return
        }
        $FullPath = Assert-StockBotTrustedPath `
            -Path $FullPath `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File
        try {
            Remove-Item `
                -LiteralPath $FullPath `
                -Force `
                -ErrorAction Stop
            return
        }
        catch {
            if ($Attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Remove-StockBotSafeTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [int]$MaxAttempts = 5
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
        $FullPath = Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory
        $Item = Get-StockBotPathItemIfPresent -Path $FullPath
        if ($null -eq $Item) {
            return
        }
        $FullPath = Assert-StockBotDirectoryTreeSafe `
            -Path $FullPath `
            -TrustedRoot $TrustedRoot
        try {
            Remove-Item `
                -LiteralPath $FullPath `
                -Recurse `
                -Force `
                -ErrorAction Stop
            return
        }
        catch {
            if ($Attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Move-StockBotSafeDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [int]$MaxAttempts = 5
    )

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
        $SourcePath = Assert-StockBotTrustedPath `
            -Path $Source `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory
        $DestinationPath = Assert-StockBotTrustedPath `
            -Path $Destination `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory
        $SourceItem = Get-StockBotPathItemIfPresent -Path $SourcePath
        $DestinationItem = Get-StockBotPathItemIfPresent -Path $DestinationPath
        if ($null -eq $SourceItem -and $null -ne $DestinationItem) {
            Assert-StockBotDirectoryTreeSafe `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
            return
        }
        if ($null -eq $SourceItem) {
            throw "StockBot directory move source is unavailable."
        }
        if ($null -ne $DestinationItem) {
            throw "StockBot directory move destination already exists."
        }
        Assert-StockBotDirectoryTreeSafe `
            -Path $SourcePath `
            -TrustedRoot $TrustedRoot | Out-Null
        try {
            Move-Item `
                -LiteralPath $SourcePath `
                -Destination $DestinationPath `
                -ErrorAction Stop
            Assert-StockBotDirectoryTreeSafe `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
            return
        }
        catch {
            if ($Attempt -eq $MaxAttempts) {
                throw
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Copy-StockBotDirectorySnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$SourceTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$Destination,
        [Parameter(Mandatory = $true)]
        [string]$DestinationTrustedRoot,
        [int]$MaxAttempts = 5
    )

    $SourcePath = Assert-StockBotDirectoryTreeSafe `
        -Path $Source `
        -TrustedRoot $SourceTrustedRoot
    $DestinationPath = Assert-StockBotTrustedPath `
        -Path $Destination `
        -TrustedRoot $DestinationTrustedRoot `
        -AllowMissing `
        -ExpectedType Directory
    if ($null -ne (
        Get-StockBotPathItemIfPresent -Path $DestinationPath
    )) {
        throw "StockBot snapshot destination already exists."
    }

    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
        New-StockBotSafeDirectory `
            -Path $DestinationPath `
            -TrustedRoot $DestinationTrustedRoot | Out-Null
        try {
            Assert-StockBotDirectoryTreeSafe `
                -Path $SourcePath `
                -TrustedRoot $SourceTrustedRoot | Out-Null
            foreach ($SourceItem in @(
                Get-ChildItem -LiteralPath $SourcePath -Force -ErrorAction Stop
            )) {
                if ($SourceItem.PSIsContainer) {
                    Assert-StockBotDirectoryTreeSafe `
                        -Path $SourceItem.FullName `
                        -TrustedRoot $SourceTrustedRoot | Out-Null
                }
                else {
                    Assert-StockBotTrustedPath `
                        -Path $SourceItem.FullName `
                        -TrustedRoot $SourceTrustedRoot `
                        -ExpectedType File | Out-Null
                }
                Assert-StockBotDirectoryTreeSafe `
                    -Path $DestinationPath `
                    -TrustedRoot $DestinationTrustedRoot | Out-Null
                Assert-StockBotTrustedPath `
                    -Path (Join-Path $DestinationPath $SourceItem.Name) `
                    -TrustedRoot $DestinationTrustedRoot `
                    -AllowMissing | Out-Null
                Copy-Item `
                    -LiteralPath $SourceItem.FullName `
                    -Destination $DestinationPath `
                    -Recurse `
                    -Force `
                    -ErrorAction Stop
            }
            Assert-StockBotDirectoryTreeSafe `
                -Path $DestinationPath `
                -TrustedRoot $DestinationTrustedRoot | Out-Null
            return
        }
        catch {
            $CopyFailure = $_.Exception
            try {
                Remove-StockBotSafeTree `
                    -Path $DestinationPath `
                    -TrustedRoot $DestinationTrustedRoot
            }
            catch {
                throw (
                    "StockBot snapshot copy failed and staging cleanup was " +
                    "incomplete: $($_.Exception.Message)"
                )
            }
            if ($Attempt -eq $MaxAttempts) {
                throw $CopyFailure
            }
            Start-Sleep -Seconds 1
        }
    }
}

function Install-StockBotDirectoryExactly {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,
        [Parameter(Mandatory = $true)]
        [string]$Destination,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$RequiredRelativeFile,
        [switch]$ValidateServiceBundleInventory
    )

    if (
        [System.IO.Path]::IsPathRooted($RequiredRelativeFile) -or
        $RequiredRelativeFile.Contains("..") -or
        $RequiredRelativeFile.Contains(":")
    ) {
        throw "Required bundle file must be a safe relative path."
    }
    $DestinationPath = Assert-StockBotTrustedPath `
        -Path $Destination `
        -TrustedRoot $TrustedRoot `
        -AllowMissing `
        -ExpectedType Directory
    $StagingPath = $DestinationPath + ".staging"
    $PreviousPath = $DestinationPath + ".previous"
    foreach ($TemporaryPath in @($StagingPath, $PreviousPath)) {
        Assert-StockBotTrustedPath `
            -Path $TemporaryPath `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory | Out-Null
    }
    $ExistingDestination = Get-StockBotPathItemIfPresent -Path $DestinationPath
    $ExistingPrevious = Get-StockBotPathItemIfPresent -Path $PreviousPath
    if ($null -ne $ExistingPrevious) {
        Assert-StockBotDirectoryTreeSafe `
            -Path $PreviousPath `
            -TrustedRoot $TrustedRoot | Out-Null
        Assert-StockBotTrustedPath `
            -Path (Join-Path $PreviousPath $RequiredRelativeFile) `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
        if ($null -ne $ExistingDestination) {
            Assert-StockBotDirectoryTreeSafe `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
            Remove-StockBotSafeTree `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot
        }
        Assert-StockBotTrustedPath `
            -Path $DestinationPath `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory | Out-Null
        Move-StockBotSafeDirectory `
            -Source $PreviousPath `
            -Destination $DestinationPath `
            -TrustedRoot $TrustedRoot
        Assert-StockBotDirectoryTreeSafe `
            -Path $DestinationPath `
            -TrustedRoot $TrustedRoot | Out-Null
        $ExistingDestination = Get-StockBotPathItemIfPresent `
            -Path $DestinationPath
        $ExistingPrevious = $null
    }
    Remove-StockBotSafeTree `
        -Path $StagingPath `
        -TrustedRoot $TrustedRoot
    if ($null -ne $ExistingPrevious) {
        Remove-StockBotSafeTree `
            -Path $PreviousPath `
            -TrustedRoot $TrustedRoot
    }

    $PreviousPrepared = $false
    $SwapCompleted = $false
    try {
        Copy-StockBotDirectorySnapshot `
            -Source $Source `
            -SourceTrustedRoot $Source `
            -Destination $StagingPath `
            -DestinationTrustedRoot $TrustedRoot
        $RequiredStagedFile = Assert-StockBotTrustedPath `
            -Path (Join-Path $StagingPath $RequiredRelativeFile) `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File
        if (!(Test-Path -LiteralPath $RequiredStagedFile -PathType Leaf)) {
            throw "Staged StockBot bundle is incomplete."
        }
        if ($ValidateServiceBundleInventory) {
            Set-StockBotServiceBundleTreeOwner `
                -Path $StagingPath `
                -TrustedRoot $TrustedRoot | Out-Null
            Assert-StockBotServiceBundleTreeAcl `
                -Path $StagingPath `
                -TrustedRoot $TrustedRoot | Out-Null
            Assert-StockBotServiceBundleInventory `
                -Path $StagingPath `
                -TrustedRoot $TrustedRoot | Out-Null
        }

        $ExistingDestination = Get-StockBotPathItemIfPresent `
            -Path $DestinationPath
        if ($null -ne $ExistingDestination) {
            Assert-StockBotDirectoryTreeSafe `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
            Assert-StockBotTrustedPath `
                -Path $PreviousPath `
                -TrustedRoot $TrustedRoot `
                -AllowMissing `
                -ExpectedType Directory | Out-Null
            Move-StockBotSafeDirectory `
                -Source $DestinationPath `
                -Destination $PreviousPath `
                -TrustedRoot $TrustedRoot
            $PreviousPrepared = $true
        }

        Assert-StockBotDirectoryTreeSafe `
            -Path $StagingPath `
            -TrustedRoot $TrustedRoot | Out-Null
        Assert-StockBotTrustedPath `
            -Path $DestinationPath `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType Directory | Out-Null
        Move-StockBotSafeDirectory `
            -Source $StagingPath `
            -Destination $DestinationPath `
            -TrustedRoot $TrustedRoot
        $SwapCompleted = $true
        Assert-StockBotDirectoryTreeSafe `
            -Path $DestinationPath `
            -TrustedRoot $TrustedRoot | Out-Null
        Assert-StockBotTrustedPath `
            -Path (Join-Path $DestinationPath $RequiredRelativeFile) `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
        if ($ValidateServiceBundleInventory) {
            Assert-StockBotServiceBundleTreeAcl `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
            Assert-StockBotServiceBundleInventory `
                -Path $DestinationPath `
                -TrustedRoot $TrustedRoot | Out-Null
        }
    }
    catch {
        $ReplacementFailure = $_.Exception
        $RestoreFailure = $null
        try {
            if (
                $SwapCompleted -and
                (Test-Path -LiteralPath $DestinationPath)
            ) {
                Remove-StockBotSafeTree `
                    -Path $DestinationPath `
                    -TrustedRoot $TrustedRoot
            }
            if (
                $PreviousPrepared -and
                (Test-Path -LiteralPath $PreviousPath -PathType Container)
            ) {
                Assert-StockBotDirectoryTreeSafe `
                    -Path $PreviousPath `
                    -TrustedRoot $TrustedRoot | Out-Null
                Assert-StockBotTrustedPath `
                    -Path $DestinationPath `
                    -TrustedRoot $TrustedRoot `
                    -AllowMissing `
                    -ExpectedType Directory | Out-Null
                Move-StockBotSafeDirectory `
                    -Source $PreviousPath `
                    -Destination $DestinationPath `
                    -TrustedRoot $TrustedRoot
                Assert-StockBotDirectoryTreeSafe `
                    -Path $DestinationPath `
                    -TrustedRoot $TrustedRoot | Out-Null
            }
        }
        catch {
            $RestoreFailure = $_.Exception
        }
        try {
            Remove-StockBotSafeTree `
                -Path $StagingPath `
                -TrustedRoot $TrustedRoot
        }
        catch {
            if ($null -eq $RestoreFailure) {
                $RestoreFailure = $_.Exception
            }
        }
        if ($null -ne $RestoreFailure) {
            throw (
                "StockBot bundle replacement failed and previous bundle " +
                "restoration was incomplete: $($RestoreFailure.Message)"
            )
        }
        throw $ReplacementFailure
    }

    if ($PreviousPrepared) {
        try {
            Remove-StockBotSafeTree `
                -Path $PreviousPath `
                -TrustedRoot $TrustedRoot
        }
        catch {
            $CleanupFailure = $_.Exception
            try {
                Remove-StockBotSafeTree `
                    -Path $DestinationPath `
                    -TrustedRoot $TrustedRoot
                Assert-StockBotDirectoryTreeSafe `
                    -Path $PreviousPath `
                    -TrustedRoot $TrustedRoot | Out-Null
                Move-StockBotSafeDirectory `
                    -Source $PreviousPath `
                    -Destination $DestinationPath `
                    -TrustedRoot $TrustedRoot
                Assert-StockBotDirectoryTreeSafe `
                    -Path $DestinationPath `
                    -TrustedRoot $TrustedRoot | Out-Null
            }
            catch {
                throw (
                    "StockBot bundle cleanup failed and previous bundle " +
                    "restoration was incomplete: $($_.Exception.Message)"
                )
            }
            throw $CleanupFailure
        }
    }
}

function Add-StockBotDirectoryAccessRule {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.DirectorySecurity]$Acl,
        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier]$Sid,
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemRights]$Rights
    )

    $Inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $Rights,
        $Inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $Acl.AddAccessRule($Rule)
}

function Set-StockBotRestrictedAccessRules {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.DirectorySecurity]$Acl,
        [switch]$GrantCurrentIdentityRead
    )

    $AdministratorsSid = [System.Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $Acl.SetOwner($AdministratorsSid)
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($Rule in @($Acl.Access)) {
        $Acl.RemoveAccessRuleAll($Rule)
    }
    Add-StockBotDirectoryAccessRule `
        -Acl $Acl `
        -Sid ([System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")) `
        -Rights ([System.Security.AccessControl.FileSystemRights]::FullControl)
    Add-StockBotDirectoryAccessRule `
        -Acl $Acl `
        -Sid $AdministratorsSid `
        -Rights ([System.Security.AccessControl.FileSystemRights]::FullControl)
    if ($GrantCurrentIdentityRead) {
        # Do not grant token access to broad local groups for proxy-UAC installs.
        Add-StockBotDirectoryAccessRule `
            -Acl $Acl `
            -Sid ([Security.Principal.WindowsIdentity]::GetCurrent().User) `
            -Rights ([System.Security.AccessControl.FileSystemRights]::ReadAndExecute)
    }
    return $Acl
}

function Set-StockBotRestrictedFileAccessRules {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSecurity]$Acl
    )

    $AdministratorsSid = [System.Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $Acl.SetOwner($AdministratorsSid)
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($Rule in @($Acl.Access)) {
        $Acl.RemoveAccessRuleAll($Rule)
    }
    foreach ($SidValue in @("S-1-5-18", "S-1-5-32-544")) {
        $Rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($SidValue),
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.AddAccessRule($Rule)
    }
    return $Acl
}

function New-StockBotRestrictedDirectorySecurity {
    param(
        [switch]$GrantCurrentIdentityRead
    )

    $Acl = [System.Security.AccessControl.DirectorySecurity]::new()
    return Set-StockBotRestrictedAccessRules `
        -Acl $Acl `
        -GrantCurrentIdentityRead:$GrantCurrentIdentityRead
}

function New-StockBotRestrictedFileSecurity {
    $Acl = [System.Security.AccessControl.FileSecurity]::new()
    return Set-StockBotRestrictedFileAccessRules -Acl $Acl
}

function Assert-StockBotRestrictedAccessControl {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemSecurity]$Acl
    )

    $SystemSid = "S-1-5-18"
    $AdministratorsSid = "S-1-5-32-544"
    $TrustedSids = @($SystemSid, $AdministratorsSid)
    $OwnerSid = $Acl.GetOwner(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($OwnerSid -notin $TrustedSids) {
        throw "StockBot protected state owner is not trusted."
    }
    if (!$Acl.AreAccessRulesProtected) {
        throw "StockBot protected state ACL inheritance is not disabled."
    }

    $RightsBySid = @{}
    foreach ($Rule in @(
        $Acl.GetAccessRules(
            $true,
            $true,
            [System.Security.Principal.SecurityIdentifier]
        )
    )) {
        $RuleSid = $Rule.IdentityReference.Value
        if (
            $Rule.IsInherited -or
            $Rule.AccessControlType -ne
                [System.Security.AccessControl.AccessControlType]::Allow -or
            $RuleSid -notin $TrustedSids
        ) {
            throw "StockBot protected state grants access to an untrusted identity."
        }
        $ExistingRights = [int64]0
        if ($RightsBySid.ContainsKey($RuleSid)) {
            $ExistingRights = [int64]$RightsBySid[$RuleSid]
        }
        $RightsBySid[$RuleSid] = (
            $ExistingRights -bor [int64]$Rule.FileSystemRights
        )
    }

    $FullControl = [int64](
        [System.Security.AccessControl.FileSystemRights]::FullControl
    )
    foreach ($TrustedSid in $TrustedSids) {
        if (
            !$RightsBySid.ContainsKey($TrustedSid) -or
            (
                [int64]$RightsBySid[$TrustedSid] -band $FullControl
            ) -ne $FullControl
        ) {
            throw "StockBot protected state ACL is incomplete."
        }
    }
}

function Assert-StockBotRestrictedPathAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [ValidateSet("File", "Directory")]
        [string]$ExpectedType
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType $ExpectedType
    $Acl = Get-Acl -LiteralPath $FullPath -ErrorAction Stop
    Assert-StockBotRestrictedAccessControl -Acl $Acl
    return $FullPath
}

function Set-StockBotRestrictedDirectoryAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [switch]$GrantCurrentIdentityRead
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory
    $Acl = Get-Acl -LiteralPath $FullPath -ErrorAction Stop
    $Acl = Set-StockBotRestrictedAccessRules `
        -Acl $Acl `
        -GrantCurrentIdentityRead:$GrantCurrentIdentityRead
    $FullPath = Assert-StockBotTrustedPath `
        -Path $FullPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory
    Set-Acl -LiteralPath $FullPath -AclObject $Acl -ErrorAction Stop
}

function New-StockBotRestrictedDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -AllowMissing `
        -ExpectedType Directory
    if ($null -ne (Get-StockBotPathItemIfPresent -Path $FullPath)) {
        throw "StockBot protected directory already exists."
    }
    $Acl = New-StockBotRestrictedDirectorySecurity
    [void][System.IO.Directory]::CreateDirectory($FullPath, $Acl)
    return Assert-StockBotRestrictedPathAcl `
        -Path $FullPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory
}

function New-StockBotRestrictedFileStream {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -AllowMissing `
        -ExpectedType File
    if ($null -ne (Get-StockBotPathItemIfPresent -Path $FullPath)) {
        throw "StockBot protected file already exists."
    }
    $Acl = New-StockBotRestrictedFileSecurity
    return [System.IO.FileStream]::new(
        $FullPath,
        [System.IO.FileMode]::CreateNew,
        [System.Security.AccessControl.FileSystemRights]::Write,
        [System.IO.FileShare]::None,
        4096,
        [System.IO.FileOptions]::WriteThrough,
        $Acl
    )
}

function Write-StockBotRestrictedFileBytes {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [Parameter(Mandatory = $true)]
        [byte[]]$Bytes
    )

    $Stream = New-StockBotRestrictedFileStream `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    try {
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
    Assert-StockBotRestrictedPathAcl `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File | Out-Null
}

function Copy-StockBotFileWithRestrictedAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$SourceTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationTrustedRoot
    )

    $ResolvedSource = Assert-StockBotTrustedPath `
        -Path $SourcePath `
        -TrustedRoot $SourceTrustedRoot `
        -ExpectedType File
    $SourceStream = [System.IO.File]::Open(
        $ResolvedSource,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $DestinationStream = $null
    try {
        $DestinationStream = New-StockBotRestrictedFileStream `
            -Path $DestinationPath `
            -TrustedRoot $DestinationTrustedRoot
        $SourceStream.CopyTo($DestinationStream)
        $DestinationStream.Flush($true)
    }
    finally {
        if ($null -ne $DestinationStream) {
            $DestinationStream.Dispose()
        }
        $SourceStream.Dispose()
    }
    Assert-StockBotRestrictedPathAcl `
        -Path $DestinationPath `
        -TrustedRoot $DestinationTrustedRoot `
        -ExpectedType File | Out-Null
}

function Set-StockBotRestrictedFileAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File
    $Acl = Get-Acl -LiteralPath $FullPath -ErrorAction Stop
    $Acl = Set-StockBotRestrictedFileAccessRules -Acl $Acl
    $FullPath = Assert-StockBotTrustedPath `
        -Path $FullPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File
    Set-Acl -LiteralPath $FullPath -AclObject $Acl -ErrorAction Stop
}

function Assert-StockBotRestrictedDirectoryTreeAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotDirectoryTreeSafe `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($FullPath)
    while ($Pending.Count -gt 0) {
        $CurrentPath = $Pending.Pop()
        $CurrentItem = Get-Item `
            -LiteralPath $CurrentPath `
            -Force `
            -ErrorAction Stop
        $ExpectedType = if ($CurrentItem.PSIsContainer) {
            "Directory"
        }
        else {
            "File"
        }
        $CurrentPath = Assert-StockBotRestrictedPathAcl `
            -Path $CurrentItem.FullName `
            -TrustedRoot $TrustedRoot `
            -ExpectedType $ExpectedType
        if ($CurrentItem.PSIsContainer) {
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
    return $FullPath
}

function Get-StockBotUnsafeWriteRightsMask {
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

function Set-StockBotServiceBundleTreeOwner {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotDirectoryTreeSafe `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $AdministratorsSid = [System.Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $Paths = @($FullPath) + @(
        Get-ChildItem `
            -LiteralPath $FullPath `
            -Recurse `
            -Force `
            -ErrorAction Stop |
            ForEach-Object { $_.FullName }
    )
    foreach ($CurrentPath in $Paths) {
        Assert-StockBotTrustedPath `
            -Path $CurrentPath `
            -TrustedRoot $TrustedRoot | Out-Null
        $Acl = Get-Acl -LiteralPath $CurrentPath -ErrorAction Stop
        $Acl.SetOwner($AdministratorsSid)
        Set-Acl `
            -LiteralPath $CurrentPath `
            -AclObject $Acl `
            -ErrorAction Stop
        $VerifiedOwner = (
            Get-Acl -LiteralPath $CurrentPath -ErrorAction Stop
        ).GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($VerifiedOwner -ne $AdministratorsSid.Value) {
            throw "StockBot service bundle owner normalization failed."
        }
    }
    return $FullPath
}

function Assert-StockBotServiceBundleTreeAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $FullPath = Assert-StockBotDirectoryTreeSafe `
        -Path $Path `
        -TrustedRoot $TrustedRoot
    $SystemSid = "S-1-5-18"
    $AdministratorsSid = "S-1-5-32-544"
    $TrustedInstallerSid = (
        [System.Security.Principal.NTAccount]::new(
            "NT SERVICE",
            "TrustedInstaller"
        ).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    )
    $TrustedOwnerSids = @(
        $SystemSid,
        $AdministratorsSid,
        $TrustedInstallerSid
    )
    $TrustedWriteSids = @(
        $SystemSid,
        $AdministratorsSid,
        $TrustedInstallerSid,
        "S-1-3-0",
        "S-1-3-4"
    )
    $FullControl = [int64](
        [System.Security.AccessControl.FileSystemRights]::FullControl
    )
    $WriteMask = [int64](Get-StockBotUnsafeWriteRightsMask)
    $GenericAll = [int64]0x10000000
    $Pending = [System.Collections.Generic.Stack[string]]::new()
    $Pending.Push($FullPath)
    while ($Pending.Count -gt 0) {
        $CurrentPath = $Pending.Pop()
        $CurrentItem = Get-Item `
            -LiteralPath $CurrentPath `
            -Force `
            -ErrorAction Stop
        $Acl = Get-Acl -LiteralPath $CurrentPath -ErrorAction Stop
        $OwnerSid = $Acl.GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($OwnerSid -notin $TrustedOwnerSids) {
            throw "StockBot service bundle owner is not trusted."
        }

        $RightsByTrustedSid = @{}
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
                throw "StockBot service bundle contains an unsupported access rule."
            }
            if (
                $Rule.AccessControlType -ne
                    [System.Security.AccessControl.AccessControlType]::Allow
            ) {
                continue
            }
            $RuleRights = [int64]$Rule.FileSystemRights
            if ($RuleSid -in @($SystemSid, $AdministratorsSid)) {
                $ExistingRights = [int64]0
                if ($RightsByTrustedSid.ContainsKey($RuleSid)) {
                    $ExistingRights = [int64]$RightsByTrustedSid[$RuleSid]
                }
                $RightsByTrustedSid[$RuleSid] = $ExistingRights -bor $RuleRights
            }
            elseif (
                $RuleSid -notin $TrustedWriteSids -and
                ($RuleRights -band $WriteMask) -ne 0
            ) {
                throw "StockBot service bundle grants write access to an untrusted identity."
            }
        }
        foreach ($TrustedSid in @($SystemSid, $AdministratorsSid)) {
            if (
                !$RightsByTrustedSid.ContainsKey($TrustedSid) -or
                (
                    (
                        [int64]$RightsByTrustedSid[$TrustedSid] -band $FullControl
                    ) -ne $FullControl -and
                    (
                        [int64]$RightsByTrustedSid[$TrustedSid] -band $GenericAll
                    ) -eq 0
                )
            ) {
                throw "StockBot service bundle ACL is incomplete."
            }
        }

        if ($CurrentItem.PSIsContainer) {
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
    return $FullPath
}

function Test-StockBotFilesEqual {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LeftPath,
        [Parameter(Mandatory = $true)]
        [string]$LeftTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$RightPath,
        [Parameter(Mandatory = $true)]
        [string]$RightTrustedRoot
    )

    $ResolvedLeft = Assert-StockBotTrustedPath `
        -Path $LeftPath `
        -TrustedRoot $LeftTrustedRoot `
        -ExpectedType File
    $ResolvedRight = Assert-StockBotTrustedPath `
        -Path $RightPath `
        -TrustedRoot $RightTrustedRoot `
        -ExpectedType File
    $LeftBytes = [System.IO.File]::ReadAllBytes($ResolvedLeft)
    $RightBytes = [System.IO.File]::ReadAllBytes($ResolvedRight)
    if ($LeftBytes.Length -ne $RightBytes.Length) {
        return $false
    }
    for ($Index = 0; $Index -lt $LeftBytes.Length; $Index++) {
        if ($LeftBytes[$Index] -ne $RightBytes[$Index]) {
            return $false
        }
    }
    return $true
}

function Assert-StockBotSafeAllowlistFileName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FileName
    )

    if (
        [string]::IsNullOrWhiteSpace($FileName) -or
        $FileName -in @(".", "..") -or
        $FileName.Contains(":") -or
        ![string]::Equals(
            [System.IO.Path]::GetFileName($FileName),
            $FileName,
            [System.StringComparison]::Ordinal
        )
    ) {
        throw "StockBot snapshot allowlist entries must be file names."
    }
}

function Copy-StockBotAllowlistedFilesToSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceRoot,
        [Parameter(Mandatory = $true)]
        [string]$SourceTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$SnapshotRoot,
        [Parameter(Mandatory = $true)]
        [string]$SnapshotTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$FileNames,
        [int]$MaxAttempts = 5
    )

    Assert-StockBotTrustedPath `
        -Path $SourceRoot `
        -TrustedRoot $SourceTrustedRoot `
        -AllowMissing `
        -ExpectedType Directory | Out-Null
    Assert-StockBotRestrictedDirectoryTreeAcl `
        -Path $SnapshotRoot `
        -TrustedRoot $SnapshotTrustedRoot | Out-Null
    foreach ($FileName in $FileNames) {
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
        $SourcePath = Join-Path $SourceRoot $FileName
        $SnapshotPath = Join-Path $SnapshotRoot $FileName
        Assert-StockBotTrustedPath `
            -Path $SourcePath `
            -TrustedRoot $SourceTrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        $SourceItem = Get-StockBotPathItemIfPresent -Path $SourcePath
        if ($null -eq $SourceItem) {
            continue
        }
        Assert-StockBotTrustedPath `
            -Path $SourcePath `
            -TrustedRoot $SourceTrustedRoot `
            -ExpectedType File | Out-Null
        Assert-StockBotTrustedPath `
            -Path $SnapshotPath `
            -TrustedRoot $SnapshotTrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        if ($null -ne (
            Get-StockBotPathItemIfPresent -Path $SnapshotPath
        )) {
            throw "StockBot snapshot file already exists."
        }

        for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
            try {
                Assert-StockBotTrustedPath `
                    -Path $SourcePath `
                    -TrustedRoot $SourceTrustedRoot `
                    -ExpectedType File | Out-Null
                Assert-StockBotTrustedPath `
                    -Path $SnapshotPath `
                    -TrustedRoot $SnapshotTrustedRoot `
                    -AllowMissing `
                    -ExpectedType File | Out-Null
                Copy-StockBotFileWithRestrictedAcl `
                    -SourcePath $SourcePath `
                    -SourceTrustedRoot $SourceTrustedRoot `
                    -DestinationPath $SnapshotPath `
                    -DestinationTrustedRoot $SnapshotTrustedRoot
                if (!(Test-StockBotFilesEqual `
                    -LeftPath $SourcePath `
                    -LeftTrustedRoot $SourceTrustedRoot `
                    -RightPath $SnapshotPath `
                    -RightTrustedRoot $SnapshotTrustedRoot
                )) {
                    throw "StockBot snapshot file verification failed."
                }
                Write-Output $FileName
                break
            }
            catch {
                $CopyFailure = $_.Exception
                Remove-StockBotSafeFile `
                    -Path $SnapshotPath `
                    -TrustedRoot $SnapshotTrustedRoot
                if ($Attempt -eq $MaxAttempts) {
                    throw $CopyFailure
                }
                Start-Sleep -Seconds 1
            }
        }
    }
}

function Restore-StockBotAllowlistedFilesFromSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SnapshotRoot,
        [Parameter(Mandatory = $true)]
        [string]$SnapshotTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string]$DestinationRoot,
        [Parameter(Mandatory = $true)]
        [string]$DestinationTrustedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$FileNames,
        [int]$MaxAttempts = 5
    )

    Assert-StockBotRestrictedDirectoryTreeAcl `
        -Path $SnapshotRoot `
        -TrustedRoot $SnapshotTrustedRoot | Out-Null
    Assert-StockBotTrustedPath `
        -Path $DestinationRoot `
        -TrustedRoot $DestinationTrustedRoot `
        -ExpectedType Directory | Out-Null
    foreach ($FileName in $FileNames) {
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
        $SnapshotPath = Join-Path $SnapshotRoot $FileName
        $DestinationPath = Join-Path $DestinationRoot $FileName
        Assert-StockBotTrustedPath `
            -Path $SnapshotPath `
            -TrustedRoot $SnapshotTrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        $SnapshotItem = Get-StockBotPathItemIfPresent -Path $SnapshotPath
        if ($null -eq $SnapshotItem) {
            continue
        }
        Assert-StockBotTrustedPath `
            -Path $SnapshotPath `
            -TrustedRoot $SnapshotTrustedRoot `
            -ExpectedType File | Out-Null
        Remove-StockBotSafeFile `
            -Path $DestinationPath `
            -TrustedRoot $DestinationTrustedRoot

        for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
            try {
                Assert-StockBotTrustedPath `
                    -Path $SnapshotPath `
                    -TrustedRoot $SnapshotTrustedRoot `
                    -ExpectedType File | Out-Null
                Assert-StockBotTrustedPath `
                    -Path $DestinationPath `
                    -TrustedRoot $DestinationTrustedRoot `
                    -AllowMissing `
                    -ExpectedType File | Out-Null
                [System.IO.File]::Copy(
                    $SnapshotPath,
                    $DestinationPath,
                    $false
                )
                if (!(Test-StockBotFilesEqual `
                    -LeftPath $SnapshotPath `
                    -LeftTrustedRoot $SnapshotTrustedRoot `
                    -RightPath $DestinationPath `
                    -RightTrustedRoot $DestinationTrustedRoot
                )) {
                    throw "StockBot restored file verification failed."
                }
                Write-Output $FileName
                break
            }
            catch {
                $RestoreFailure = $_.Exception
                Remove-StockBotSafeFile `
                    -Path $DestinationPath `
                    -TrustedRoot $DestinationTrustedRoot
                if ($Attempt -eq $MaxAttempts) {
                    throw $RestoreFailure
                }
                Start-Sleep -Seconds 1
            }
        }
    }
}

function Get-StockBotFileSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    $ResolvedPath = Assert-StockBotTrustedPath `
        -Path $Path `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File
    $Stream = [System.IO.File]::OpenRead($ResolvedPath)
    try {
        $Hasher = [System.Security.Cryptography.SHA256]::Create()
        try {
            $Hash = $Hasher.ComputeHash($Stream)
        }
        finally {
            $Hasher.Dispose()
        }
    }
    finally {
        $Stream.Dispose()
    }
    return (
        [System.BitConverter]::ToString($Hash).Replace("-", "").ToLowerInvariant()
    )
}

function Get-StockBotValidatedFreshRecoveryManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RecoveryRoot,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$FileNames
    )

    $RecoveryPath = Assert-StockBotDirectoryTreeSafe `
        -Path $RecoveryRoot `
        -TrustedRoot $TrustedRoot
    Assert-StockBotRestrictedPathAcl `
        -Path $RecoveryPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType Directory | Out-Null
    $ManifestName = "recovery-manifest.json"
    $TemporaryManifestName = ".recovery-manifest.json.tmp"
    $BackupManifestName = ".recovery-manifest.json.bak"
    $AllowedNames = @(
        $ManifestName,
        $TemporaryManifestName,
        $BackupManifestName
    )
    foreach ($FileName in $FileNames) {
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
        $AllowedNames += $FileName
    }
    $AllowedNameSet = @{}
    foreach ($AllowedName in $AllowedNames) {
        if ($AllowedNameSet.ContainsKey($AllowedName)) {
            throw "StockBot fresh recovery allowlist contains duplicate names."
        }
        $AllowedNameSet[$AllowedName] = $true
    }
    foreach ($Item in @(
        Get-ChildItem -LiteralPath $RecoveryPath -Force -ErrorAction Stop
    )) {
        if (
            $Item.PSIsContainer -or
            !$AllowedNameSet.ContainsKey($Item.Name)
        ) {
            throw "StockBot fresh recovery contains an unexpected entry."
        }
    }

    $ManifestPath = Join-Path $RecoveryPath $ManifestName
    $TemporaryManifestPath = Join-Path $RecoveryPath $TemporaryManifestName
    $BackupManifestPath = Join-Path $RecoveryPath $BackupManifestName
    $ManifestCandidate = if (
        Test-Path -LiteralPath $ManifestPath -PathType Leaf
    ) {
        $ManifestPath
    }
    elseif (Test-Path -LiteralPath $TemporaryManifestPath -PathType Leaf) {
        $TemporaryManifestPath
    }
    elseif (Test-Path -LiteralPath $BackupManifestPath -PathType Leaf) {
        $BackupManifestPath
    }
    else {
        throw "StockBot fresh recovery manifest is unavailable."
    }
    $ManifestCandidate = Assert-StockBotRestrictedPathAcl `
        -Path $ManifestCandidate `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File
    try {
        $Payload = [System.IO.File]::ReadAllText($ManifestCandidate) |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "StockBot fresh recovery manifest is not valid JSON."
    }
    if (
        $null -eq $Payload -or
        $Payload.schemaVersion -ne 1 -or
        $Payload.state -notin @("collecting", "prepared", "committed") -or
        $Payload.programDataRootExisted -isnot [bool] -or
        $Payload.installerRollbackExisted -isnot [bool]
    ) {
        throw "StockBot fresh recovery manifest schema is invalid."
    }

    $ManifestFiles = @($Payload.files)
    if ($Payload.state -eq "collecting") {
        if ($ManifestFiles.Count -ne 0) {
            throw "Collecting StockBot fresh recovery cannot list snapshot files."
        }
        return [pscustomobject]@{
            State = "collecting"
            ProgramDataRootExisted = [bool]$Payload.programDataRootExisted
            InstallerRollbackExisted = [bool]$Payload.installerRollbackExisted
            FileNames = [string[]]@()
        }
    }
    Assert-StockBotRestrictedDirectoryTreeAcl `
        -Path $RecoveryPath `
        -TrustedRoot $TrustedRoot | Out-Null
    $ManifestFileNames = @()
    $SeenFileNames = @{}
    foreach ($File in $ManifestFiles) {
        $FileName = [string]$File.name
        $FileHash = ([string]$File.sha256).Trim().ToLowerInvariant()
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
        if (
            $FileName -notin $FileNames -or
            $SeenFileNames.ContainsKey($FileName) -or
            $FileHash -notmatch "^[0-9a-f]{64}$"
        ) {
            throw "StockBot fresh recovery manifest file entry is invalid."
        }
        $BackupPath = Join-Path $RecoveryPath $FileName
        Assert-StockBotTrustedPath `
            -Path $BackupPath `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
        if (
            (Get-StockBotFileSha256 `
                -Path $BackupPath `
                -TrustedRoot $TrustedRoot) -ne $FileHash
        ) {
            throw "StockBot fresh recovery file hash verification failed."
        }
        $SeenFileNames[$FileName] = $true
        $ManifestFileNames += $FileName
    }
    foreach ($FileName in $FileNames) {
        $BackupPath = Join-Path $RecoveryPath $FileName
        $BackupExists = Test-Path -LiteralPath $BackupPath -PathType Leaf
        if ($BackupExists -ne $SeenFileNames.ContainsKey($FileName)) {
            throw "StockBot fresh recovery file inventory is inconsistent."
        }
    }

    return [pscustomobject]@{
        State = [string]$Payload.state
        ProgramDataRootExisted = [bool]$Payload.programDataRootExisted
        InstallerRollbackExisted = [bool]$Payload.installerRollbackExisted
        FileNames = [string[]]$ManifestFileNames
    }
}

function Test-StockBotFreshRecoveryContainsOnlyMetadata {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RecoveryRoot,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$FileNames
    )

    $RecoveryPath = Assert-StockBotDirectoryTreeSafe `
        -Path $RecoveryRoot `
        -TrustedRoot $TrustedRoot
    $MetadataNames = @(
        "recovery-manifest.json",
        ".recovery-manifest.json.tmp",
        ".recovery-manifest.json.bak"
    )
    foreach ($FileName in $FileNames) {
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
    }
    foreach ($Item in @(
        Get-ChildItem -LiteralPath $RecoveryPath -Force -ErrorAction Stop
    )) {
        if ($Item.PSIsContainer -or $Item.Name -notin $MetadataNames) {
            return $false
        }
    }
    return $true
}

function Write-StockBotFreshRecoveryManifest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RecoveryRoot,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot,
        [Parameter(Mandatory = $true)]
        [string[]]$FileNames,
        [Parameter(Mandatory = $true)]
        [ValidateSet("collecting", "prepared", "committed")]
        [string]$State,
        [Parameter(Mandatory = $true)]
        [bool]$ProgramDataRootExisted,
        [Parameter(Mandatory = $true)]
        [bool]$InstallerRollbackExisted
    )

    $RecoveryPath = Assert-StockBotRestrictedDirectoryTreeAcl `
        -Path $RecoveryRoot `
        -TrustedRoot $TrustedRoot
    $Files = @()
    foreach ($FileName in $FileNames) {
        Assert-StockBotSafeAllowlistFileName -FileName $FileName
        $BackupPath = Join-Path $RecoveryPath $FileName
        Assert-StockBotTrustedPath `
            -Path $BackupPath `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        if (Test-Path -LiteralPath $BackupPath -PathType Leaf) {
            $Files += [ordered]@{
                name = $FileName
                sha256 = Get-StockBotFileSha256 `
                    -Path $BackupPath `
                    -TrustedRoot $TrustedRoot
            }
        }
    }
    $Payload = [ordered]@{
        schemaVersion = 1
        state = $State
        programDataRootExisted = $ProgramDataRootExisted
        installerRollbackExisted = $InstallerRollbackExisted
        files = $Files
    }
    $ManifestPath = Join-Path $RecoveryPath "recovery-manifest.json"
    $TemporaryManifestPath = Join-Path `
        $RecoveryPath `
        ".recovery-manifest.json.tmp"
    $BackupManifestPath = Join-Path `
        $RecoveryPath `
        ".recovery-manifest.json.bak"
    foreach ($Path in @(
        $ManifestPath,
        $TemporaryManifestPath,
        $BackupManifestPath
    )) {
        Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
    }
    Remove-StockBotSafeFile `
        -Path $TemporaryManifestPath `
        -TrustedRoot $TrustedRoot
    Remove-StockBotSafeFile `
        -Path $BackupManifestPath `
        -TrustedRoot $TrustedRoot
    $Utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    $ManifestBytes = $Utf8WithoutBom.GetBytes(
        (($Payload | ConvertTo-Json -Depth 4) + "`n")
    )
    Write-StockBotRestrictedFileBytes `
        -Path $TemporaryManifestPath `
        -TrustedRoot $TrustedRoot `
        -Bytes $ManifestBytes
    Assert-StockBotTrustedPath `
        -Path $TemporaryManifestPath `
        -TrustedRoot $TrustedRoot `
        -ExpectedType File | Out-Null
    if (Test-Path -LiteralPath $ManifestPath -PathType Leaf) {
        [System.IO.File]::Replace(
            $TemporaryManifestPath,
            $ManifestPath,
            $BackupManifestPath,
            $true
        )
    }
    else {
        [System.IO.File]::Move($TemporaryManifestPath, $ManifestPath)
    }
    Set-StockBotRestrictedFileAcl `
        -Path $ManifestPath `
        -TrustedRoot $TrustedRoot
    Remove-StockBotSafeFile `
        -Path $BackupManifestPath `
        -TrustedRoot $TrustedRoot
    Get-StockBotValidatedFreshRecoveryManifest `
        -RecoveryRoot $RecoveryPath `
        -TrustedRoot $TrustedRoot `
        -FileNames $FileNames | Out-Null
}

function Resolve-StockBotPackagedCycleIntervalSeconds {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(5, 3600)]
        [double]$ExistingCycleIntervalSeconds,
        [Parameter(Mandatory = $true)]
        [ValidateRange(5, 3600)]
        [double]$CurrentCycleIntervalSeconds,
        [switch]$RestoreExisting
    )

    if ($RestoreExisting) {
        return $ExistingCycleIntervalSeconds
    }
    return $CurrentCycleIntervalSeconds
}

function Get-StockBotValidatedServiceConfigPaths {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ServiceConfigPath
    )

    if (!(Test-Path -LiteralPath $ServiceConfigPath -PathType Leaf)) {
        throw "StockBot service configuration was not found."
    }
    try {
        $Payload = [System.IO.File]::ReadAllText($ServiceConfigPath) |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "StockBot service configuration is not valid JSON."
    }
    if ($null -eq $Payload) {
        throw "StockBot service configuration schema is not supported."
    }
    $SchemaVersion = $Payload.schemaVersion
    if (
        $null -eq $SchemaVersion -or
        $SchemaVersion.GetType().Name -notin @("Int32", "Int64") -or
        [int]$SchemaVersion -notin @(1, 2)
    ) {
        throw "StockBot service configuration schema is not supported."
    }
    $AuthorizationProperty = $Payload.PSObject.Properties["liveOrdersAuthorized"]
    if (
        $null -eq $AuthorizationProperty -or
        $AuthorizationProperty.Value -isnot [bool] -or
        $AuthorizationProperty.Value -ne $true
    ) {
        throw "Existing StockBot service authorization is not valid."
    }
    $Fingerprint = ([string]$Payload.credentialScopeFingerprint).Trim()
    $CredentialBindingPending = $false
    if ([int]$SchemaVersion -eq 2) {
        $BindingProperty = $Payload.PSObject.Properties["credentialBindingPending"]
        if (
            $null -eq $BindingProperty -or
            $BindingProperty.Value -isnot [bool]
        ) {
            throw "Existing StockBot service credential binding state is invalid."
        }
        $CredentialBindingPending = [bool]$BindingProperty.Value
    }
    if ($CredentialBindingPending -and $Fingerprint) {
        throw "Pending StockBot service credential scope must be empty."
    }
    if (
        !$CredentialBindingPending -and
        $Fingerprint -notmatch "^[0-9a-f]{64}$"
    ) {
        throw "Existing StockBot service credential scope is not valid."
    }

    $ProjectRoot = ([string]$Payload.projectRoot).Trim()
    $ConfigPath = ([string]$Payload.configPath).Trim()
    $EnvFile = ([string]$Payload.envFile).Trim()
    foreach ($Candidate in @($ProjectRoot, $ConfigPath, $EnvFile)) {
        if (
            [string]::IsNullOrWhiteSpace($Candidate) -or
            ![System.IO.Path]::IsPathRooted($Candidate)
        ) {
            throw "Existing StockBot service paths must be absolute."
        }
    }
    $ResolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    $ResolvedConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
    $ResolvedEnvFile = [System.IO.Path]::GetFullPath($EnvFile)
    if (!(Test-Path -LiteralPath $ResolvedProjectRoot -PathType Container)) {
        throw "Existing StockBot project root is unavailable."
    }
    if (!(Test-Path -LiteralPath $ResolvedConfigPath -PathType Leaf)) {
        throw "Existing StockBot live configuration is unavailable."
    }
    if (!(Test-Path -LiteralPath $ResolvedEnvFile -PathType Leaf)) {
        throw "Existing StockBot credential file is unavailable."
    }

    $CycleIntervalSeconds = 0.0
    if (
        ![double]::TryParse(
            ([string]$Payload.cycleIntervalSeconds),
            [ref]$CycleIntervalSeconds
        ) -or
        [double]::IsNaN($CycleIntervalSeconds) -or
        [double]::IsInfinity($CycleIntervalSeconds) -or
        $CycleIntervalSeconds -lt 5 -or
        $CycleIntervalSeconds -gt 3600
    ) {
        throw "Existing StockBot service cycle interval is invalid."
    }

    return [pscustomobject]@{
        ProjectRoot = $ResolvedProjectRoot
        ConfigPath = $ResolvedConfigPath
        EnvFile = $ResolvedEnvFile
        CycleIntervalSeconds = $CycleIntervalSeconds
        CredentialBindingPending = $CredentialBindingPending
        CredentialBindingState = if ($CredentialBindingPending) {
            "pending"
        }
        else {
            "bound"
        }
        AllowCredentialBootstrap = $CredentialBindingPending
    }
}

function Test-StockBotServiceStopped {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Registration
    )

    return (
        ([string]$Registration.State).Equals(
            "Stopped",
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        [int]$Registration.ProcessId -eq 0
    )
}

function Wait-StockBotServiceStopped {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 90
    )

    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Registration = Get-CimInstance `
            -ClassName Win32_Service `
            -Filter "Name='$Name'" `
            -ErrorAction SilentlyContinue
        if ($null -eq $Registration) {
            throw "Windows service registration disappeared while waiting for stop."
        }
        if (Test-StockBotServiceStopped -Registration $Registration) {
            return $Registration
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)

    throw "StockBot service did not reach Stopped with PID 0 within $TimeoutSeconds seconds."
}

function Wait-StockBotServiceDeleted {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 30
    )

    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Registration = Get-CimInstance `
            -ClassName Win32_Service `
            -Filter "Name='$Name'" `
            -ErrorAction SilentlyContinue
        if ($null -eq $Registration) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)

    throw "StockBot service registration was not deleted within $TimeoutSeconds seconds."
}

function Remove-StockBotSessionAfterConfirmedStop {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Registration,
        [Parameter(Mandatory = $true)]
        [string]$SessionFile,
        [Parameter(Mandatory = $true)]
        [string]$TrustedRoot
    )

    if (!(Test-StockBotServiceStopped -Registration $Registration)) {
        return $false
    }
    Remove-StockBotSafeFile `
        -Path $SessionFile `
        -TrustedRoot $TrustedRoot
    return $true
}

Export-ModuleMember -Function @(
    "Assert-StockBotDirectoryTreeSafe",
    "Assert-StockBotLegacyServiceBundleStructure",
    "Assert-StockBotServiceBundleInventory",
    "Assert-StockBotServiceBundleTreeAcl",
    "Assert-StockBotTrustedPath",
    "Copy-StockBotAllowlistedFilesToSnapshot",
    "Copy-StockBotDirectorySnapshot",
    "ConvertTo-StockBotWindowsNativeArguments",
    "Get-StockBotValidatedFreshRecoveryManifest",
    "Get-StockBotValidatedServiceConfigPaths",
    "Get-StockBotKnownFolderPath",
    "Get-StockBotPathItemIfPresent",
    "Get-StockBotServiceBundleState",
    "Get-StockBotServiceCommandLine",
    "Get-StockBotServiceInstallLayout",
    "Install-StockBotDirectoryExactly",
    "Invoke-StockBotWindowsNativeProcess",
    "Move-StockBotSafeDirectory",
    "New-StockBotRestrictedDirectory",
    "New-StockBotSafeDirectory",
    "New-StockBotRestrictedDirectorySecurity",
    "Remove-StockBotSessionAfterConfirmedStop",
    "Remove-StockBotSafeFile",
    "Remove-StockBotSafeTree",
    "Resolve-StockBotPackagedCycleIntervalSeconds",
    "Restore-StockBotAllowlistedFilesFromSnapshot",
    "Set-StockBotServiceBundleTreeOwner",
    "Set-StockBotRestrictedDirectoryAcl",
    "Test-StockBotFilesEqual",
    "Test-StockBotFreshRecoveryContainsOnlyMetadata",
    "Test-StockBotPathWithinRoot",
    "Test-StockBotServiceStopped",
    "Wait-StockBotServiceDeleted",
    "Wait-StockBotServiceStopped",
    "Write-StockBotFreshRecoveryManifest",
    "Write-StockBotServiceBundleManifest"
)
