import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
INSTALLER_NSH = (
    ROOT / "apps" / "electron-dashboard" / "build" / "installer.nsh"
)
LIFECYCLE_SCRIPTS = (
    TOOLS / "install_stockbot_service.ps1",
    TOOLS / "install_stockbot_packaged_service.ps1",
    TOOLS / "uninstall_stockbot_service.ps1",
)


def read_script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def braced_block(source: str, marker: str) -> str:
    try:
        marker_index = source.index(marker)
    except ValueError as error:
        raise AssertionError(
            f"Missing PowerShell block marker {marker!r}"
        ) from error
    block_start = source.index("{", marker_index)
    depth = 0
    for index in range(block_start, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[block_start : index + 1]
    raise AssertionError(f"Unclosed PowerShell block after {marker!r}")


def nsis_macro(source: str, name: str) -> str:
    match = re.search(rf"(?m)^!macro {re.escape(name)}\s*$", source)
    if match is None:
        raise AssertionError(f"Missing NSIS macro {name!r}")
    start = match.start()
    end = source.index("!macroend", start)
    return source[start:end]


class InstallerUpgradeRecoveryTest(unittest.TestCase):
    def test_lifecycle_scripts_preserve_native_service_control_arguments(self):
        helpers = read_script(
            TOOLS / "stockbot_service_installer_helpers.psm1"
        )
        self.assertIn(
            "function ConvertTo-StockBotWindowsNativeArguments",
            helpers,
        )
        self.assertIn(
            "function Invoke-StockBotWindowsNativeProcess",
            helpers,
        )
        self.assertIn(
            "[System.Diagnostics.ProcessStartInfo]::new()",
            helpers,
        )
        self.assertIn(
            '$StartInfo.Arguments = [string]::Join(" ", $NativeArguments)',
            helpers,
        )
        self.assertEqual(
            2,
            helpers.count("ReadToEndAsync()"),
            "stdout and stderr must be drained concurrently",
        )
        self.assertIn(
            '"Invoke-StockBotWindowsNativeProcess"',
            helpers,
        )

        for script_path in LIFECYCLE_SCRIPTS:
            with self.subTest(script=script_path.name):
                script = read_script(script_path)
                self.assertIn(
                    "Invoke-StockBotWindowsNativeProcess",
                    script,
                )
                self.assertNotIn("& $ScExe @NativeArguments", script)

    def test_lifecycle_scripts_handle_dedicated_and_legacy_service_roots(self):
        helpers = read_script(
            TOOLS / "stockbot_service_installer_helpers.psm1"
        )
        self.assertIn(
            'Join-Path $ResolvedProgramFilesRoot "StockBotService"',
            helpers,
        )
        self.assertIn(
            'Join-Path $ResolvedProgramFilesRoot "StockBot\\Service"',
            helpers,
        )

        for script_path in LIFECYCLE_SCRIPTS:
            with self.subTest(script=script_path.name):
                script = read_script(script_path)
                self.assertIn(
                    "Get-StockBotServiceInstallLayout",
                    script,
                    f"{script_path.name} must resolve both fixed roots",
                )
                self.assertIn(
                    ".CurrentRoot",
                    script,
                    f"{script_path.name} must manage the dedicated root",
                )
                self.assertIn(
                    ".LegacyRoot",
                    script,
                    f"{script_path.name} must explicitly handle the legacy root",
                )

    def test_packaged_wrapper_distinguishes_bundle_states_and_repairs_missing(
        self,
    ):
        wrapper = read_script(
            TOOLS / "install_stockbot_packaged_service.ps1"
        )
        helpers = read_script(
            TOOLS / "stockbot_service_installer_helpers.psm1"
        )

        self.assertIn("Get-StockBotServiceBundleState", wrapper)
        for state in ("complete", "legacy", "missing", "partial"):
            self.assertRegex(
                wrapper,
                re.compile(
                    rf'(?:BundleState|\$BundleState).*["\']{state}["\']',
                    re.IGNORECASE,
                ),
                f"packaged update must explicitly handle {state!r}",
            )

        missing_branch = braced_block(
            wrapper,
            'elseif ($ExistingInstallation.BundleState -eq "missing")',
        )
        self.assertIn("-RepairOnly", missing_branch)
        self.assertIn('$BackupMode = "repair"', missing_branch)

        partial_branch = braced_block(
            wrapper,
            'if ($BundleState -eq "partial")',
        )
        self.assertIn("throw", partial_branch.lower())
        bundle_state_helper = braced_block(
            helpers,
            "function Get-StockBotServiceBundleState",
        )
        self.assertIn("Assert-StockBotTrustedPath", bundle_state_helper)
        self.assertIn("Assert-StockBotServiceBundleTreeAcl", bundle_state_helper)
        self.assertIn(
            "Assert-StockBotServiceBundleInventory",
            bundle_state_helper,
        )
        self.assertIn(
            "Assert-StockBotLegacyServiceBundleStructure",
            bundle_state_helper,
        )

    def test_service_bundle_uses_hash_inventory_before_service_stop(self):
        helpers = read_script(
            TOOLS / "stockbot_service_installer_helpers.psm1"
        )
        builder = read_script(TOOLS / "build_windows_service.ps1")
        installer = read_script(TOOLS / "install_stockbot_service.ps1")

        self.assertIn(
            "function Assert-StockBotServiceBundleInventory",
            helpers,
        )
        self.assertIn("stockbot-service-bundle-manifest.json", helpers)
        self.assertIn("Get-StockBotFileSha256", helpers)
        self.assertIn("stockbot-service-bundle-manifest.json", builder)
        self.assertIn("Get-FileHash", builder)
        inventory_check = installer.index(
            "Assert-StockBotServiceBundleInventory"
        )
        service_lookup = installer.index(
            "Get-Service -Name $ServiceName",
            inventory_check,
        )
        self.assertLess(inventory_check, service_lookup)
        self.assertIn("-ValidateServiceBundleInventory", installer)
        exact_install = braced_block(
            helpers,
            "function Install-StockBotDirectoryExactly",
        )
        self.assertIn(
            "Assert-StockBotServiceBundleInventory",
            exact_install,
        )
        self.assertIn(
            "Assert-StockBotServiceBundleTreeAcl",
            exact_install,
        )
        self.assertIn(
            "Set-StockBotServiceBundleTreeOwner",
            exact_install,
        )

    def test_electron_package_refreshes_inventory_after_pack_and_sign(self):
        package = json.loads(
            read_script(
                ROOT / "apps" / "electron-dashboard" / "package.json"
            )
        )
        build = package["build"]
        expected_hook = "scripts/write-service-bundle-manifest.cjs"
        self.assertEqual(expected_hook, build["afterPack"])
        self.assertEqual(expected_hook, build["afterSign"])

        hook = read_script(
            ROOT
            / "apps"
            / "electron-dashboard"
            / "scripts"
            / "write-service-bundle-manifest.cjs"
        )
        self.assertIn("stockbot-service-bundle-manifest.json", hook)
        self.assertIn('createHash("sha256")', hook)
        self.assertIn("isSymbolicLink", hook)
        self.assertIn("StockBotService.exe", hook)
        self.assertIn("_internal/data/symbols.csv", hook)

    def test_service_bundle_copy_relies_on_program_files_acl_validation(self):
        helpers = read_script(
            TOOLS / "stockbot_service_installer_helpers.psm1"
        )
        installer = read_script(TOOLS / "install_stockbot_service.ps1")

        self.assertNotIn("RestrictDestinationAcl", helpers)
        self.assertNotIn("RestrictDestinationAcl", installer)
        bundle_state_helper = braced_block(
            helpers,
            "function Get-StockBotServiceBundleState",
        )
        self.assertIn(
            "Assert-StockBotServiceBundleTreeAcl",
            bundle_state_helper,
        )
        self.assertNotIn(
            "Assert-StockBotRestrictedDirectoryTreeAcl",
            bundle_state_helper,
        )

    def test_missing_bundle_repair_never_uses_normal_rollback(self):
        wrapper = read_script(
            TOOLS / "install_stockbot_packaged_service.ps1"
        )
        missing_branch = braced_block(
            wrapper,
            'elseif ($ExistingInstallation.BundleState -eq "missing")',
        )
        repair_cleanup = braced_block(
            wrapper,
            "function Restore-StockBotMissingBundleRepair",
        )

        for forbidden_call in (
            "Restore-StockBotExistingInstallation",
            "Copy-StockBotDirectorySnapshot",
        ):
            self.assertNotIn(forbidden_call, missing_branch)
            self.assertNotIn(forbidden_call, repair_cleanup)

        self.assertIn(
            "New-StockBotInstallerRollback -RepairOnly",
            missing_branch,
        )
        self.assertNotIn(
            "New-StockBotInstallerRollback `",
            missing_branch,
        )
        self.assertIn("Stop-Service", repair_cleanup)
        self.assertRegex(
            repair_cleanup,
            re.compile(r"Manual", re.IGNORECASE),
        )
        self.assertIn(
            "-InstallRoot $ExistingInstallation.InstallRoot",
            repair_cleanup,
        )
        self.assertIn('"binPath=", $OriginalBinaryPath', repair_cleanup)
        self.assertIn("Remove-StockBotSafeTree", repair_cleanup)
        self.assertIn("$ServiceInstallRoot", repair_cleanup)

    def test_manifestless_and_previous_bundles_use_normal_rollback(self):
        wrapper = read_script(
            TOOLS / "install_stockbot_packaged_service.ps1"
        )
        existing_probe = braced_block(
            wrapper,
            "function Get-StockBotExistingServiceInstallation",
        )
        rollback = braced_block(
            wrapper,
            "function New-StockBotInstallerRollback",
        )

        self.assertIn('$InstallRoot + ".previous"', existing_probe)
        self.assertIn("BundleSourceRoot", existing_probe)
        self.assertIn("RecoveredFromPrevious", existing_probe)
        self.assertIn("Write-StockBotServiceBundleManifest", rollback)
        self.assertIn(
            "Assert-StockBotServiceBundleInventory",
            rollback,
        )
        self.assertIn(
            "Assert-StockBotServiceBundleTreeAcl",
            rollback,
        )
        self.assertIn(
            "Set-StockBotServiceBundleTreeOwner",
            rollback,
        )
        restore = braced_block(
            wrapper,
            "function Restore-StockBotExistingInstallation",
        )
        self.assertIn(
            "Assert-StockBotServiceBundleInventory",
            restore,
        )
        self.assertIn(
            "Assert-StockBotServiceBundleTreeAcl",
            restore,
        )
        legacy_branch = braced_block(
            wrapper,
            'elseif ($ExistingInstallation.BundleState -eq "legacy")',
        )
        self.assertIn("-CreateBundleManifest", legacy_branch)
        self.assertIn(
            "$ExistingInstallation.BundleSourceRoot",
            legacy_branch,
        )
        committed_cleanup = braced_block(
            wrapper,
            "if ($HadExistingService -and $ExistingInstallation.Layout -eq",
        )
        self.assertIn("try {", committed_cleanup)
        self.assertIn("catch {", committed_cleanup)
        self.assertNotIn("throw", committed_cleanup)

    def test_packaged_wrapper_validates_source_before_recovery_or_service_probe(
        self,
    ):
        wrapper = read_script(
            TOOLS / "install_stockbot_packaged_service.ps1"
        )
        inventory_check = wrapper.index(
            "Assert-StockBotServiceBundleInventory"
        )
        recovery = wrapper.index("Recover-StockBotInterruptedFreshInstall")
        service_probe = wrapper.index(
            "Get-Service `\n    -Name $ServiceName",
            recovery,
        )
        self.assertLess(inventory_check, recovery)
        self.assertLess(inventory_check, service_probe)

    def test_nsis_shows_stable_redacted_service_install_failure(self):
        script = read_script(INSTALLER_NSH)
        custom_install = nsis_macro(script, "customInstall")
        result_handling = custom_install[
            custom_install.index("Pop $StockBotPowerShellOutput") :
        ]

        self.assertIn(
            "Call StockBotFailServiceInstall",
            result_handling,
        )
        self.assertNotIn("Abort", result_handling)
        self.assertIn("SBPSI1-", result_handling)

        fail_function_start = script.index(
            "Function StockBotFailServiceInstall"
        )
        fail_function_end = script.index(
            "FunctionEnd",
            fail_function_start,
        )
        fail_function = script[fail_function_start:fail_function_end]
        self.assertIn("MessageBox", fail_function)
        self.assertIn("DetailPrint", fail_function)
        self.assertIn("SetErrorLevel", fail_function)
        self.assertIn("Quit", fail_function)
        self.assertNotIn("$StockBotPowerShellOutput", fail_function)

    @unittest.skipUnless(
        shutil.which("powershell.exe"),
        "PowerShell is required for installer helper behavior tests",
    )
    def test_bundle_state_helper_classifies_legacy_and_verified_inventory(self):
        helper_path = (
            TOOLS / "stockbot_service_installer_helpers.psm1"
        ).resolve()

        with TemporaryDirectory() as temporary_directory:
            bundle_root = Path(temporary_directory) / "service"
            quoted_helper = str(helper_path).replace("'", "''")
            quoted_bundle = str(bundle_root).replace("'", "''")
            quoted_trusted_root = str(Path(temporary_directory)).replace(
                "'",
                "''",
            )
            powershell = f"""
$ErrorActionPreference = 'Stop'
$module = Import-Module '{quoted_helper}' -Force -PassThru
& $module {{
    param([string]$BundleRoot, [string]$TrustedRoot)
    function script:Assert-StockBotTrustedPath {{
        param(
            [string]$Path,
            [string]$TrustedRoot,
            [switch]$AllowMissing,
            [string]$ExpectedType
        )
        return [System.IO.Path]::GetFullPath($Path)
    }}
    function script:Assert-StockBotDirectoryTreeSafe {{
        param([string]$Path, [string]$TrustedRoot)
        return [System.IO.Path]::GetFullPath($Path)
    }}
    function script:Assert-StockBotServiceBundleTreeAcl {{
        param([string]$Path, [string]$TrustedRoot)
        return [System.IO.Path]::GetFullPath($Path)
    }}

    $missing = Get-StockBotServiceBundleState `
        -InstallRoot $BundleRoot `
        -TrustedRoot $TrustedRoot
    [System.IO.Directory]::CreateDirectory($BundleRoot) | Out-Null
    $partial = Get-StockBotServiceBundleState `
        -InstallRoot $BundleRoot `
        -TrustedRoot $TrustedRoot
    [System.IO.Directory]::CreateDirectory(
        (Join-Path $BundleRoot '_internal\\data')
    ) | Out-Null
    [System.IO.Directory]::CreateDirectory(
        (Join-Path $BundleRoot '_internal\\pywin32_system32')
    ) | Out-Null
    [System.IO.Directory]::CreateDirectory(
        (Join-Path $BundleRoot '_internal\\win32')
    ) | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot 'StockBotService.exe'),
        'service'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot '_internal\\data\\symbols.csv'),
        'symbols'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot '_internal\\base_library.zip'),
        'stdlib'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot '_internal\\python312.dll'),
        'python'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot '_internal\\pywin32_system32\\pywintypes312.dll'),
        'pywintypes'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $BundleRoot '_internal\\win32\\win32service.pyd'),
        'win32service'
    )
    $legacy = Get-StockBotServiceBundleState `
        -InstallRoot $BundleRoot `
        -TrustedRoot $TrustedRoot
    Write-StockBotServiceBundleManifest `
        -Path $BundleRoot `
        -TrustedRoot $TrustedRoot | Out-Null
    $complete = Get-StockBotServiceBundleState `
        -InstallRoot $BundleRoot `
        -TrustedRoot $TrustedRoot
    [System.IO.File]::AppendAllText(
        (Join-Path $BundleRoot '_internal\\data\\symbols.csv'),
        '-tampered'
    )
    $tampered = Get-StockBotServiceBundleState `
        -InstallRoot $BundleRoot `
        -TrustedRoot $TrustedRoot
    @($missing, $partial, $legacy, $complete, $tampered) |
        ConvertTo-Json -Compress
}} '{quoted_bundle}' '{quoted_trusted_root}'
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=ROOT,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            result.stderr or result.stdout,
        )
        self.assertEqual(
            ["missing", "partial", "legacy", "complete", "partial"],
            json.loads(result.stdout.strip()),
        )


if __name__ == "__main__":
    unittest.main()
