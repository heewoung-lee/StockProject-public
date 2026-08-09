import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class WindowsPackagingTest(unittest.TestCase):
    def test_legacy_tkinter_dashboard_runtime_and_packaging_are_removed(self):
        legacy_paths = (
            ROOT / "src" / "stockbot" / "app.py",
            ROOT / "tests" / "test_app.py",
            ROOT / "packaging" / "stockbot_app_entry.py",
            ROOT / "packaging" / "stockbot-windows.spec",
            ROOT / "tools" / "build_windows_app.ps1",
            ROOT / "tools" / "create_windows_icon.py",
            ROOT / "docs" / "operations" / "run-korean-dashboard-app.md",
        )

        for path in legacy_paths:
            self.assertFalse(path.exists(), str(path.relative_to(ROOT)))

        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertNotIn("stockbot-app", config["project"]["scripts"])
        self.assertNotIn("stockbot-advisor", config["project"]["scripts"])
        self.assertNotIn("app-build", config["project"]["optional-dependencies"])

    def test_pyproject_declares_isolated_windows_service_build_extra_and_entrypoint(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        dependencies = config["project"]["optional-dependencies"]["windows-service-build"]
        self.assertTrue(any(dependency.lower().startswith("pyinstaller") for dependency in dependencies))
        self.assertTrue(any(dependency.lower().startswith("pywin32") for dependency in dependencies))
        self.assertEqual(
            "stockbot.windows_service:main",
            config["project"]["scripts"]["stockbot-windows-service"],
        )

    def test_windows_service_entrypoint_delegates_to_service_core_cli(self):
        entrypoint = (ROOT / "packaging" / "stockbot_service_entry.py").read_text(encoding="utf-8")

        self.assertIn("from stockbot.windows_service import main", entrypoint)
        self.assertIn("raise SystemExit(main())", entrypoint)

    def test_windows_service_spec_builds_waitable_cli_bundle_without_runtime_secrets(self):
        spec = (ROOT / "packaging" / "stockbot-service-windows.spec").read_text(encoding="utf-8")
        compact_spec = spec.replace(" ", "")
        lowered_spec = spec.lower()

        self.assertIn("stockbot_service_entry.py", spec)
        self.assertIn('name="StockBotService"', compact_spec)
        self.assertIn("console=True", compact_spec)
        self.assertIn('(str(project_root / "data" / "symbols.csv"), "data")', spec)
        self.assertNotIn("stockbot_app_entry.py", spec)
        for hidden_import in (
            "servicemanager",
            "win32event",
            "win32service",
            "win32serviceutil",
            "win32timezone",
        ):
            self.assertIn(hidden_import, spec)
        for forbidden in (
            ".env",
            "logs",
            "journal",
            "pending_live_orders",
            "config.example",
            "config.live",
            "sample_bars",
            "c:/users",
            "c:\\users",
        ):
            self.assertNotIn(forbidden, lowered_spec)

    def test_windows_service_build_script_uses_dedicated_extra_and_spec(self):
        script = (ROOT / "tools" / "build_windows_service.ps1").read_text(encoding="utf-8")

        self.assertIn(".[windows-service-build]", script)
        self.assertIn("-m PyInstaller", script)
        self.assertIn("stockbot-service-windows.spec", script)
        self.assertIn('"dist\\StockBotService"', script)
        self.assertIn(
            '$OutputExe = Join-Path $OutputRoot "StockBotService.exe"',
            script,
        )

    def test_windows_service_installer_requires_admin_and_restricts_programdata_acl(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")
        helpers = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).read_text(encoding="utf-8")
        compact_script = script.replace(" ", "")
        compact_helpers = helpers.replace(" ", "")

        self.assertIn('$ServiceName = "StockBotLive"', script)
        self.assertNotIn("[string]$ServiceName", script)
        self.assertIn("WindowsBuiltInRole]::Administrator", script)
        self.assertIn("Get-StockBotKnownFolderPath", script)
        self.assertNotIn("$env:ProgramData", script)
        self.assertNotIn("$env:ProgramFiles", script)
        self.assertIn("SetAccessRuleProtection($true,$false)", compact_helpers)
        self.assertIn("Set-Acl", helpers)
        self.assertIn("S-1-5-18", helpers)
        self.assertIn("S-1-5-32-544", helpers)
        self.assertIn("WindowsIdentity]::GetCurrent().User", compact_helpers)
        self.assertNotIn("S-1-5-32-545", helpers)
        self.assertIn("Resolve-Path", script)
        self.assertIn("([string]$_.Name).Equals(", script)
        self.assertIn('"StockBot.exe",', script)
        self.assertIn(
            "Install-StockBotDirectoryExactly `",
            script,
        )
        existing_service_branch = script.index("if ($null -ne $ExistingService)")
        existing_validation = script.index(
            "Get-ValidatedServiceRegistration `",
            existing_service_branch,
        )
        bundle_copy = script.index("Install-StockBotDirectoryExactly `")
        existing_recovery_disable = script.index(
            '"actions=", ""',
            existing_service_branch,
        )
        self.assertLess(existing_validation, bundle_copy)
        self.assertLess(existing_recovery_disable, bundle_copy)

    def test_electron_windows_package_builds_and_bundles_service_without_secrets(self):
        package = json.loads(
            (ROOT / "apps" / "electron-dashboard" / "package.json").read_text(
                encoding="utf-8"
            )
        )

        for script_name in ("package:win", "package:win:dir"):
            package_script = package["scripts"][script_name]
            self.assertIn("npm run build:windows-service", package_script)
            self.assertLess(
                package_script.index("npm run build:windows-service"),
                package_script.index("electron-builder"),
            )
        self.assertIn(
            "../../tools/build_windows_service.ps1",
            package["scripts"]["build:windows-service"],
        )

        resources = {
            item["to"]: item["from"] for item in package["build"]["extraResources"]
        }
        self.assertEqual(
            "../../dist/StockBotService",
            resources["stockbot-service/bundle"],
        )
        self.assertEqual(
            "../../config.live.example.yaml",
            resources["stockbot-service/config.live.example.yaml"],
        )
        for script_name in (
            "install_stockbot_service.ps1",
            "install_stockbot_packaged_service.ps1",
            "uninstall_stockbot_service.ps1",
            "stockbot_service_installer_helpers.psm1",
        ):
            self.assertEqual(
                f"../../tools/{script_name}",
                resources[f"stockbot-service/installer/{script_name}"],
            )

        serialized_resources = json.dumps(
            package["build"]["extraResources"]
        ).lower()
        for forbidden in (
            '".env"',
            "credentials.env",
            "logs/",
            "journal/",
            "bridge-session.json",
            "service-config.json",
            "stockbot-diagnostics",
        ):
            self.assertNotIn(forbidden, serialized_resources)

        nsis = package["build"]["nsis"]
        self.assertFalse(nsis["perMachine"])
        self.assertTrue(nsis["allowElevation"])
        self.assertFalse(nsis["allowToChangeInstallationDirectory"])
        self.assertEqual("build/installer.nsh", nsis["include"])

    def test_nsis_requires_live_service_consent_and_owns_service_lifecycle(self):
        script = (
            ROOT / "apps" / "electron-dashboard" / "build" / "installer.nsh"
        ).read_text(encoding="utf-8")

        self.assertIn("!macro customWelcomePage", script)
        welcome_page = script[
            script.index("!macro customWelcomePage") :
            script.index("!macroend", script.index("!macro customWelcomePage"))
        ]
        self.assertIn("MUI_PAGE_WELCOME", welcome_page)
        self.assertNotIn("StockBotServiceConsentPageCreate", welcome_page)
        self.assertIn("!macro customInstallMode", script)
        install_mode = script[
            script.index("!macro customInstallMode") :
            script.index("!macroend", script.index("!macro customInstallMode"))
        ]
        self.assertIn('StrCpy $isForceMachineInstall "1"', install_mode)
        self.assertIn("!macro customPageAfterChangeDir", script)
        preinstall_pages = script[
            script.index("!macro customPageAfterChangeDir") :
            script.index(
                "!macroend",
                script.index("!macro customPageAfterChangeDir"),
            )
        ]
        self.assertIn(
            "Page custom StockBotServiceConsentPageCreate",
            preinstall_pages,
        )
        self.assertIn(
            "Page custom StockBotPreflightPageCreate",
            preinstall_pages,
        )
        self.assertIn("${NSD_CreateCheckbox}", script)
        self.assertIn("${NSD_GetState}", script)
        self.assertIn("${BST_CHECKED}", script)
        self.assertIn(
            'ReadRegStr $StockBotExistingServicePath HKLM '
            '"SYSTEM\\CurrentControlSet\\Services\\StockBotLive" "ImagePath"',
            script,
        )
        self.assertIn("${Silent}", script)
        self.assertIn("/ALLOWLIVEORDERS", script)
        self.assertIn("Abort", script)
        self.assertIn('StrCpy $INSTDIR "$PROGRAMFILES64\\StockBot"', script)
        self.assertIn("Function StockBotValidateInstallRoot", script)
        self.assertIn("validate_stockbot_install_root.ps1", script)
        validator_function = script[
            script.index("Function StockBotValidateInstallRoot") :
            script.index("FunctionEnd", script.index("Function StockBotValidateInstallRoot"))
        ]
        self.assertEqual(
            2,
            validator_function.count('"$SYSDIR\\icacls.exe" "$PLUGINSDIR"'),
        )
        self.assertIn('/remove:g "*S-1-1-0"', validator_function)
        self.assertIn(
            '/grant:r "*S-1-5-18:F" "*S-1-5-32-544:F"',
            validator_function,
        )
        self.assertIn(
            '/grant:r "*S-1-5-18:(OI)(CI)F" '
            '"*S-1-5-32-544:(OI)(CI)F"',
            validator_function,
        )
        self.assertLess(
            validator_function.index('/remove:g "*S-1-1-0"'),
            validator_function.index(
                "File /oname=$PLUGINSDIR\\validate-stockbot-install-root.ps1"
            ),
        )
        self.assertIn('"SBIRV1:00"', validator_function)
        for diagnostic_code in (
            "20",
            "21",
            "22",
            "23",
            "24",
            "25",
            "26",
            "27",
            "28",
            "29",
            "30",
            "90",
            "91",
            "92",
            "93",
            "99",
        ):
            self.assertIn(f"SBIRV1-{diagnostic_code}", validator_function)
        for line in validator_function.splitlines():
            if "MessageBox" in line or "DetailPrint" in line:
                self.assertNotIn("$StockBotPowerShellOutput", line)
        self.assertIn("Call StockBotValidateInstallRoot", script)
        custom_init = script[
            script.index("!macro customInit") :
            script.index("!macroend", script.index("!macro customInit"))
        ]
        self.assertIn("${If} ${Silent}", custom_init)
        self.assertIn("!insertmacro setInstallModePerAllUsers", custom_init)
        self.assertIn("${If} ${UAC_IsAdmin}", custom_init)
        self.assertIn(
            '${ElseIf} $hasPerMachineInstallation != "1"',
            custom_init,
        )
        self.assertIn(
            "Silent StockBot installation must be run as administrator.",
            custom_init,
        )
        self.assertIn("Call StockBotValidateInstallRoot", custom_init)
        self.assertGreater(
            custom_init.index("Call StockBotValidateInstallRoot"),
            custom_init.index("${If} ${UAC_IsAdmin}"),
        )
        preflight_function = script[
            script.index("Function StockBotPreflightPageCreate") :
            script.index(
                "FunctionEnd",
                script.index("Function StockBotPreflightPageCreate"),
            )
        ]
        self.assertIn("Call StockBotValidateInstallRoot", preflight_function)
        self.assertIn("Abort", preflight_function)
        self.assertIn("Function StockBotFailInstallRootValidation", script)
        validation_function = script[
            script.index("Function StockBotValidateInstallRoot") :
            script.index(
                "FunctionEnd",
                script.index("Function StockBotValidateInstallRoot"),
            )
        ]
        self.assertNotIn('Abort "StockBot', validation_function)
        self.assertIn("Call StockBotFailInstallRootValidation", validation_function)
        self.assertLess(
            script.index(
                "Call StockBotValidateInstallRoot",
                script.index("!macro customInstall\n"),
            ),
            script.index("install_stockbot_packaged_service.ps1"),
        )
        installer_only_guard = script.index("!ifndef BUILD_UNINSTALLER")
        self.assertGreater(
            script.index("Var StockBotExistingServicePath"),
            installer_only_guard,
        )
        self.assertGreater(
            script.index("Var StockBotLiveServiceAuthorized"),
            installer_only_guard,
        )
        self.assertGreater(
            script.index("Var StockBotServiceConsentCheckbox"),
            installer_only_guard,
        )

        custom_install = script[
            script.index("!macro customInstall\n") :
            script.index("!macroend", script.index("!macro customInstall\n"))
        ]
        self.assertIn("install_stockbot_packaged_service.ps1", custom_install)
        self.assertIn("-AuthorizeLiveOrders", custom_install)
        self.assertIn("nsExec::ExecToStack", custom_install)
        self.assertIn("/TIMEOUT=600000", custom_install)
        self.assertNotIn("/TIMEOUT=180000", custom_install)
        self.assertIn("SetDetailsPrint none", custom_install)
        self.assertNotIn(
            'Abort "StockBot Windows service installation failed."',
            custom_install,
        )
        self.assertIn("Call StockBotFailServiceInstall", custom_install)
        for diagnostic_code in ("40", "41", "42", "43", "44", "45", "46", "90"):
            self.assertIn(f"SBPSI1-{diagnostic_code}", custom_install)
        for line in custom_install.splitlines():
            if "MessageBox" in line or "DetailPrint" in line:
                self.assertNotIn("$StockBotPowerShellOutput", line)

        custom_uninstall = script[
            script.index("!macro customUnInstall") :
            script.index("!macroend", script.index("!macro customUnInstall"))
        ]
        self.assertIn("${isUpdated}", custom_uninstall)
        self.assertIn("uninstall_stockbot_service.ps1", custom_uninstall)
        self.assertIn("nsExec::ExecToStack", custom_uninstall)
        self.assertIn("/TIMEOUT=600000", custom_uninstall)
        self.assertNotIn("/TIMEOUT=180000", custom_uninstall)
        self.assertIn("Abort", custom_uninstall)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_install_root_validator_does_not_treat_read_execute_as_write(self):
        script_path = ROOT / "tools" / "validate_stockbot_install_root.ps1"
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("$Acl.GetOwner(", script)
        self.assertIn("$Acl.GetAccessRules(", script)
        self.assertNotIn(".Translate(", script)

        quoted_script_path = str(script_path).replace("'", "''")
        powershell = (
            "$tokens = $null; $errors = $null; "
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{quoted_script_path}', [ref]$tokens, [ref]$errors); "
            "$maskFunction = $ast.Find({ "
            "param($node) "
            "$node -is "
            "[System.Management.Automation.Language.FunctionDefinitionAst] "
            "-and $node.Name -eq 'Get-StockBotWriteRightsMask' "
            "}, $true); "
            "$testFunction = $ast.Find({ "
            "param($node) "
            "$node -is "
            "[System.Management.Automation.Language.FunctionDefinitionAst] "
            "-and $node.Name -eq 'Test-StockBotRightsIncludeWrite' "
            "}, $true); "
            "if ($null -eq $maskFunction -or $null -eq $testFunction) { exit 11 }; "
            "Invoke-Expression $maskFunction.Extent.Text; "
            "Invoke-Expression $testFunction.Extent.Text; "
            "$mask = [int64](Get-StockBotWriteRightsMask); "
            "$readExecute = [int64]"
            "[System.Security.AccessControl.FileSystemRights]::ReadAndExecute; "
            "if (($mask -band $readExecute) -ne 0) { exit 12 }; "
            "$requiredWrite = [int64]("
            "[System.Security.AccessControl.FileSystemRights]::WriteData -bor "
            "[System.Security.AccessControl.FileSystemRights]::AppendData -bor "
            "[System.Security.AccessControl.FileSystemRights]::"
            "WriteExtendedAttributes -bor "
            "[System.Security.AccessControl.FileSystemRights]::WriteAttributes -bor "
            "[System.Security.AccessControl.FileSystemRights]::"
            "DeleteSubdirectoriesAndFiles -bor "
            "[System.Security.AccessControl.FileSystemRights]::Delete -bor "
            "[System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor "
            "[System.Security.AccessControl.FileSystemRights]::TakeOwnership"
            "); "
            "$requiredWrite = $requiredWrite -bor [int64]0x40000000 -bor "
            "[int64]0x10000000; "
            "if (($mask -band $requiredWrite) -ne $requiredWrite) { exit 13 }; "
            "if (Test-StockBotRightsIncludeWrite "
            "-Rights ([System.Security.AccessControl.FileSystemRights]::"
            "ReadAndExecute)) { exit 14 }; "
            "$writeCases = @("
            "[System.Security.AccessControl.FileSystemRights]::Write, "
            "[System.Security.AccessControl.FileSystemRights]::Modify, "
            "[System.Security.AccessControl.FileSystemRights]::FullControl, "
            "[System.Enum]::ToObject("
            "[System.Security.AccessControl.FileSystemRights], "
            "[int32]0x40000000), "
            "[System.Enum]::ToObject("
            "[System.Security.AccessControl.FileSystemRights], "
            "[int32]0x10000000)"
            "); "
            "foreach ($rights in $writeCases) { "
            "if (!(Test-StockBotRightsIncludeWrite -Rights $rights)) { exit 15 } "
            "}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                powershell,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_install_root_validator_grants_direct_and_inheritable_access(self):
        script = (
            ROOT / "tools" / "validate_stockbot_install_root.ps1"
        ).read_text(encoding="utf-8")

        direct_grants = (
            '"*S-1-5-18:F"',
            '"*S-1-5-32-544:F"',
            '"*S-1-5-32-545:RX"',
        )
        inheritable_grants = (
            '"*S-1-5-18:(OI)(CI)F"',
            '"*S-1-5-32-544:(OI)(CI)F"',
            '"*S-1-5-32-545:(OI)(CI)RX"',
        )
        for expected_grant in direct_grants + inheritable_grants:
            self.assertIn(expected_grant, script)
        self.assertIn("function Set-StockBotDirectoryInheritance", script)
        self.assertLess(
            script.rindex(direct_grants[0]),
            script.rindex("Set-StockBotDirectoryInheritance -Path"),
        )

    def test_install_root_validator_requires_protected_complete_acl(self):
        script = (
            ROOT / "tools" / "validate_stockbot_install_root.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("$Acl.AreAccessRulesProtected", script)
        self.assertIn('$UsersSid = "S-1-5-32-545"', script)
        self.assertIn("$HasAdministratorsFullControl", script)
        self.assertIn("$HasSystemFullControl", script)
        self.assertIn("$HasUsersReadAndExecute", script)
        self.assertIn(
            "StockBot install path is missing required access rules.",
            script,
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_workspace_removes_explicit_everyone_grant(self):
        with TemporaryDirectory() as temporary_directory:
            plugin_directory = Path(temporary_directory) / "plugin"
            plugin_directory.mkdir()
            validator_path = plugin_directory / "validator.ps1"
            validator_path.write_text("exit 0", encoding="ascii")
            future_path = plugin_directory / "future.ps1"
            quoted_plugin_directory = str(plugin_directory).replace("'", "''")
            quoted_validator_path = str(validator_path).replace("'", "''")
            quoted_future_path = str(future_path).replace("'", "''")
            powershell = (
                "$pluginDirectory = '"
                f"{quoted_plugin_directory}"
                "'; "
                "$validatorPath = '"
                f"{quoted_validator_path}"
                "'; "
                "$futurePath = '"
                f"{quoted_future_path}"
                "'; "
                "$currentSid = "
                "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                "& icacls.exe $pluginDirectory /inheritance:r "
                '/grant:r "*$($currentSid):(OI)(CI)F" '
                '"*S-1-1-0:(OI)(CI)(D,RC,RD,DC)" /T /C /Q | Out-Null; '
                "if ($LASTEXITCODE -ne 0) { exit 11 }; "
                "& icacls.exe $pluginDirectory /inheritance:r "
                '/remove:g "*S-1-1-0" '
                '/grant:r "*$($currentSid):F" /T /C /Q | Out-Null; '
                "if ($LASTEXITCODE -ne 0) { exit 12 }; "
                "& icacls.exe $pluginDirectory "
                '/grant:r "*$($currentSid):(OI)(CI)F" /Q | Out-Null; '
                "if ($LASTEXITCODE -ne 0) { exit 13 }; "
                "Set-Content -LiteralPath $futurePath -Value 'exit 0' "
                "-Encoding Ascii -ErrorAction Stop; "
                "$items = @("
                "Get-Item -LiteralPath "
                "$pluginDirectory, $validatorPath, $futurePath"
                "); "
                "foreach ($item in $items) { "
                "$rules = (Get-Acl -LiteralPath $item.FullName).GetAccessRules("
                "$true, $true, "
                "[Security.Principal.SecurityIdentifier]"
                "); "
                "if ($rules | Where-Object { "
                "$_.IdentityReference.Value -eq 'S-1-1-0' "
                "}) { exit 14 } "
                "}; "
                "Get-Content -LiteralPath $validatorPath -ErrorAction Stop "
                "| Out-Null; "
                "Get-Content -LiteralPath $futurePath -ErrorAction Stop "
                "| Out-Null"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_install_root_validator_returns_a_redacted_diagnostic_code(self):
        script_path = ROOT / "tools" / "validate_stockbot_install_root.ps1"
        admin_check = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$identity = "
                "[Security.Principal.WindowsIdentity]::GetCurrent(); "
                "$principal = "
                "[Security.Principal.WindowsPrincipal]::new($identity); "
                "if ($principal.IsInRole("
                "[Security.Principal.WindowsBuiltInRole]::Administrator"
                ")) { exit 0 }; exit 1",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        expected_code = 21 if admin_check.returncode == 0 else 20
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-InstallRoot",
                str(ROOT),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(expected_code, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def test_nsis_uses_native_windows_powershell_for_lifecycle_scripts(self):
        script = (
            ROOT / "apps" / "electron-dashboard" / "build" / "installer.nsh"
        ).read_text(encoding="utf-8")
        lines = script.splitlines()
        powershell_calls = [
            index
            for index, line in enumerate(lines)
            if '"$SYSDIR\\WindowsPowerShell\\v1.0\\powershell.exe"' in line
        ]

        self.assertIn('!include "x64.nsh"', script)
        self.assertEqual(3, len(powershell_calls))
        for index in powershell_calls:
            self.assertEqual("${DisableX64FSRedirection}", lines[index - 1].strip())
            self.assertEqual("${EnableX64FSRedirection}", lines[index + 1].strip())

    def test_packaged_service_wrapper_preserves_existing_paths_or_bootstraps_programdata(self):
        script = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Get-StockBotValidatedServiceConfigPaths", script)
        self.assertIn("$ExistingPaths.ProjectRoot", script)
        self.assertIn("$ExistingPaths.ConfigPath", script)
        self.assertIn("$ExistingPaths.EnvFile", script)
        self.assertIn("$ExistingPaths.CycleIntervalSeconds", script)
        self.assertIn("$ExistingPaths.AllowCredentialBootstrap", script)
        self.assertIn("$ExistingPaths.CredentialBindingState", script)
        self.assertIn("Get-StockBotKnownFolderPath", script)
        self.assertNotIn("$env:ProgramData", script)
        self.assertNotIn("$env:ProgramFiles", script)
        self.assertIn('"config.live.yaml"', script)
        self.assertIn('"credentials.env"', script)
        self.assertIn("[System.IO.File]::WriteAllText", script)
        self.assertIn("config.live.example.yaml", script)
        self.assertIn("StopExistingService = $true", script)
        self.assertIn("AllowCredentialBootstrap = $true", script)
        self.assertIn("AuthorizeLiveOrders = $true", script)
        self.assertIn('"installer-rollback"', script)
        self.assertIn("$BackupBundleRoot", script)
        self.assertIn("$BackupServiceConfigPath", script)
        self.assertIn("$RollbackArguments", script)
        self.assertIn("RegisterOnly = $true", script)
        self.assertIn("Wait-StockBotServiceStopped", script)
        self.assertIn('$RestoredService.StartType -ne "Manual"', script)
        self.assertIn("$UninstallerScript", script)
        self.assertIn("Remove-StockBotInstallerRollback", script)
        self.assertIn("New-StockBotFreshStateBackup", script)
        self.assertIn("Restore-StockBotFreshStateBackup", script)
        self.assertIn("Copy-StockBotAllowlistedFilesToSnapshot", script)
        self.assertIn("Restore-StockBotAllowlistedFilesFromSnapshot", script)
        self.assertIn("$FreshStateFileAllowlist", script)
        self.assertIn('"StockBotInstallerRecovery"', script)
        transaction_start = script.index("try {", script.index("$BackupMode"))
        fresh_backup = script.index("New-StockBotFreshStateBackup", transaction_start)
        fresh_provisioning = script.index("[System.IO.File]::Copy($ConfigTemplatePath")
        self.assertLess(transaction_start, fresh_provisioning)
        self.assertLess(fresh_backup, fresh_provisioning)
        self.assertNotIn("Get-Content", script)

    def test_service_uninstaller_deletes_only_fixed_service_and_private_metadata(self):
        script = (
            ROOT / "tools" / "uninstall_stockbot_service.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Assert-Administrator", script)
        self.assertIn("Get-StockBotKnownFolderPath", script)
        self.assertNotIn("$env:ProgramData", script)
        self.assertNotIn("$env:ProgramFiles", script)
        self.assertIn("Get-ValidatedStockBotServiceRegistration", script)
        self.assertIn('"actions=", ""', script)
        self.assertIn("Stop-Service", script)
        self.assertIn("Wait-StockBotServiceStopped", script)
        self.assertIn("Remove-StockBotSessionAfterConfirmedStop", script)
        self.assertIn('Invoke-ServiceControl -Arguments @("delete", $ServiceName)', script)
        self.assertIn("Wait-StockBotServiceDeleted", script)
        self.assertIn("Assert-StockBotTrustedPath", script)
        self.assertIn("Remove-StockBotSafeTree", script)
        self.assertIn("Remove-StockBotSafeFile", script)
        self.assertIn("$ProgramDataRemovalAllowlist", script)
        for allowed_name in (
            "credentials.env",
            "config.live.yaml",
            "service-config.json",
            "bridge-session.json",
        ):
            self.assertIn(f'"{allowed_name}"', script)
        self.assertNotIn(
            "Remove-Item -LiteralPath $ProgramDataRoot -Recurse",
            script,
        )
        self.assertIn("$ProgramDataDirectoryRemovalAllowlist", script)
        self.assertIn('"installer-rollback"', script)
        self.assertIn("[switch]$PreserveInstallerRollback", script)
        self.assertIn('$ServiceInstallRoot + ".staging"', script)
        self.assertIn('$ServiceInstallRoot + ".previous"', script)
        self.assertIn('$LegacyServiceInstallRoot + ".staging"', script)
        self.assertIn('$LegacyServiceInstallRoot + ".previous"', script)
        self.assertIn("Trade and audit records are intentionally preserved", script)
        preserved_data_check = script[
            script.index(
                "# Trade and audit records are intentionally preserved"
            ) :
            script.index(
                "if (Test-Path -LiteralPath $StockBotProgramFilesRoot"
            )
        ]
        self.assertIn("Assert-StockBotTrustedPath", preserved_data_check)
        self.assertNotIn(
            "Assert-StockBotDirectoryTreeSafe",
            preserved_data_check,
        )
        self.assertIn("$Service.Dispose()", script)
        self.assertLess(
            script.index("$Service.Dispose()"),
            script.index('Invoke-ServiceControl -Arguments @("delete", $ServiceName)'),
        )
        self.assertIn("[string]::IsNullOrWhiteSpace($CommandLine)", script)

    def test_windows_service_installer_has_opt_in_packaged_update_path(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[switch]$StopExistingService", script)
        self.assertIn("[switch]$AllowCredentialBootstrap", script)
        self.assertIn('"--allow-credential-bootstrap"', script)
        self.assertIn("Wait-StockBotServiceStopped", script)
        existing_service_branch = script.index("if ($null -ne $ExistingService)")
        recovery_disable = script.index('"actions=", ""', existing_service_branch)
        stop_service = script.index("Stop-Service", existing_service_branch)
        stopped_verification = script.index(
            "Wait-StockBotServiceStopped",
            stop_service,
        )
        bundle_copy = script.index("Install-StockBotDirectoryExactly `")
        self.assertLess(recovery_disable, stop_service)
        self.assertLess(stop_service, stopped_verification)
        self.assertLess(stopped_verification, bundle_copy)
        self.assertIn(
            'if ($ExistingService.Status -ne "Stopped" -and !$StopExistingService)',
            script,
        )
        self.assertIn(
            'throw "Stop $ServiceName before updating its service bundle."',
            script,
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_service_packaging_powershell_scripts_parse_under_windows_powershell(self):
        script_paths = (
            ROOT / "tools" / "install_stockbot_service.ps1",
            ROOT / "tools" / "install_stockbot_packaged_service.ps1",
            ROOT / "tools" / "uninstall_stockbot_service.ps1",
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1",
            ROOT / "tools" / "validate_stockbot_install_root.ps1",
        )
        for script_path in script_paths:
            quoted_script_path = str(script_path).replace("'", "''")
            powershell = (
                "$tokens = $null; $errors = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile("
                f"'{quoted_script_path}', "
                "[ref]$tokens, [ref]$errors); "
                "if ($errors.Count -ne 0) { "
                "$errors | ForEach-Object { Write-Error $_.Message }; exit 10 }"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                0,
                result.returncode,
                f"{script_path.name}\nstdout={result.stdout}\nstderr={result.stderr}",
            )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_validates_preserved_paths_and_root_boundaries(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            project.mkdir()
            config = root / "config.live.yaml"
            config.write_text("trading_mode: live\n", encoding="utf-8")
            env_file = root / "credentials.env"
            env_file.write_text("", encoding="utf-8")
            service_config = root / "service-config.json"
            service_config.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 15,
                        "credentialScopeFingerprint": "a" * 64,
                        "liveOrdersAuthorized": True,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_bound = root / "service-schema-two-bound.json"
            schema_two_bound.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 20,
                        "credentialScopeFingerprint": "b" * 64,
                        "liveOrdersAuthorized": True,
                        "credentialBindingPending": False,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_pending = root / "service-schema-two-pending.json"
            schema_two_pending.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 25,
                        "credentialScopeFingerprint": "",
                        "liveOrdersAuthorized": True,
                        "credentialBindingPending": True,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_invalid = root / "service-schema-two-invalid.json"
            schema_two_invalid.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 15,
                        "credentialScopeFingerprint": "c" * 64,
                        "liveOrdersAuthorized": True,
                        "credentialBindingPending": True,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_bound_without_fingerprint = (
                root / "service-schema-two-bound-without-fingerprint.json"
            )
            schema_two_bound_without_fingerprint.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 15,
                        "credentialScopeFingerprint": "",
                        "liveOrdersAuthorized": True,
                        "credentialBindingPending": False,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_missing_binding = root / "service-schema-two-missing-binding.json"
            schema_two_missing_binding.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 15,
                        "credentialScopeFingerprint": "d" * 64,
                        "liveOrdersAuthorized": True,
                    }
                ),
                encoding="utf-8",
            )
            schema_two_string_authorization = (
                root / "service-schema-two-string-authorization.json"
            )
            schema_two_string_authorization.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "projectRoot": str(project.resolve()),
                        "configPath": str(config.resolve()),
                        "envFile": str(env_file.resolve()),
                        "sessionFile": str((root / "bridge-session.json").resolve()),
                        "cycleIntervalSeconds": 15,
                        "credentialScopeFingerprint": "e" * 64,
                        "liveOrdersAuthorized": "true",
                        "credentialBindingPending": False,
                    }
                ),
                encoding="utf-8",
            )
            child = root / "service" / "bundle"
            sibling = root / "service-old"
            powershell = f"""
Import-Module {quote(module_path)} -Force
$paths = Get-StockBotValidatedServiceConfigPaths `
    -ServiceConfigPath {quote(service_config)}
if ($paths.ProjectRoot -ne {quote(project.resolve())}) {{ exit 11 }}
if ($paths.ConfigPath -ne {quote(config.resolve())}) {{ exit 12 }}
if ($paths.EnvFile -ne {quote(env_file.resolve())}) {{ exit 13 }}
if ($paths.CycleIntervalSeconds -ne 15) {{ exit 14 }}
if ($paths.AllowCredentialBootstrap -or $paths.CredentialBindingState -ne 'bound') {{
    exit 17
}}
$bound = Get-StockBotValidatedServiceConfigPaths `
    -ServiceConfigPath {quote(schema_two_bound)}
if ($bound.AllowCredentialBootstrap -or $bound.CredentialBindingState -ne 'bound') {{
    exit 18
}}
if ($bound.CycleIntervalSeconds -ne 20) {{ exit 19 }}
$pending = Get-StockBotValidatedServiceConfigPaths `
    -ServiceConfigPath {quote(schema_two_pending)}
if (!$pending.AllowCredentialBootstrap -or $pending.CredentialBindingState -ne 'pending') {{
    exit 20
}}
if (!$pending.CredentialBindingPending -or $pending.CycleIntervalSeconds -ne 25) {{
    exit 21
}}
$invalidRejected = $false
try {{
    Get-StockBotValidatedServiceConfigPaths `
        -ServiceConfigPath {quote(schema_two_invalid)} | Out-Null
}}
catch {{
    $invalidRejected = $true
}}
if (!$invalidRejected) {{ exit 22 }}
$invalidConfigs = @(
    {quote(schema_two_bound_without_fingerprint)},
    {quote(schema_two_missing_binding)},
    {quote(schema_two_string_authorization)}
)
foreach ($invalidConfig in $invalidConfigs) {{
    $rejected = $false
    try {{
        Get-StockBotValidatedServiceConfigPaths `
            -ServiceConfigPath $invalidConfig | Out-Null
    }}
    catch {{
        $rejected = $true
    }}
    if (!$rejected) {{ exit 23 }}
}}
if (!(Test-StockBotPathWithinRoot -Path {quote(child)} -Root {quote(root / "service")})) {{
    exit 15
}}
if (Test-StockBotPathWithinRoot -Path {quote(sibling)} -Root {quote(root / "service")}) {{
    exit 16
}}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_rejects_reparse_components_and_trees(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed = root / "managed"
            managed.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_bytes(b"keep")
            junction = managed / "junction"
            powershell = f"""
Import-Module {quote(module_path)} -Force
Assert-StockBotTrustedPath `
    -Path {quote(managed / "new" / "file.txt")} `
    -TrustedRoot {quote(managed)} `
    -AllowMissing | Out-Null
New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(outside)} | Out-Null
$componentRejected = $false
try {{
    Assert-StockBotTrustedPath `
        -Path {quote(junction / "keep.txt")} `
        -TrustedRoot {quote(managed)} | Out-Null
}}
catch {{
    $componentRejected = $true
}}
if (!$componentRejected) {{ exit 10 }}
$treeRejected = $false
try {{
    Assert-StockBotDirectoryTreeSafe `
        -Path {quote(managed)} `
        -TrustedRoot {quote(root)} | Out-Null
}}
catch {{
    $treeRejected = $true
}}
if (!$treeRejected) {{ exit 11 }}
$removeRejected = $false
try {{
    Remove-StockBotSafeTree `
        -Path {quote(managed)} `
        -TrustedRoot {quote(root)}
}}
catch {{
    $removeRejected = $true
}}
if (!$removeRejected) {{ exit 12 }}
if (!(Test-Path -LiteralPath {quote(sentinel)} -PathType Leaf)) {{ exit 13 }}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_exact_bundle_swap_removes_stale_files(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            managed = root / "managed"
            destination = managed / "Service"
            destination.mkdir(parents=True)
            (destination / "StockBotService.exe").write_bytes(b"old")
            (destination / "stale.dll").write_bytes(b"stale")
            source = root / "source"
            source.mkdir()
            (source / "StockBotService.exe").write_bytes(b"new")
            (source / "current.dll").write_bytes(b"current")
            invalid_source = root / "invalid-source"
            invalid_source.mkdir()
            (invalid_source / "not-service.txt").write_bytes(b"invalid")
            interrupted_destination = managed / "RecoveredService"
            interrupted_previous = Path(str(interrupted_destination) + ".previous")
            interrupted_previous.mkdir()
            (interrupted_previous / "StockBotService.exe").write_bytes(b"recovered")
            interrupted_staging = Path(str(interrupted_destination) + ".staging")
            interrupted_staging.mkdir()
            (interrupted_staging / "partial.dll").write_bytes(b"partial")
            double_destination = managed / "DoubleStateService"
            double_destination.mkdir()
            (double_destination / "StockBotService.exe").write_bytes(b"newer")
            double_previous = Path(str(double_destination) + ".previous")
            double_previous.mkdir()
            (double_previous / "StockBotService.exe").write_bytes(b"older")
            powershell = f"""
Import-Module {quote(module_path)} -Force
Install-StockBotDirectoryExactly `
    -Source {quote(source)} `
    -Destination {quote(destination)} `
    -TrustedRoot {quote(managed)} `
    -RequiredRelativeFile 'StockBotService.exe'
if (Test-Path -LiteralPath {quote(destination / "stale.dll")}) {{ exit 10 }}
if (!(Test-Path -LiteralPath {quote(destination / "current.dll")} -PathType Leaf)) {{
    exit 11
}}
if ([System.IO.File]::ReadAllText({quote(destination / "StockBotService.exe")}) -ne 'new') {{
    exit 12
}}
$beforeFailure = [System.IO.File]::ReadAllBytes(
    {quote(destination / "StockBotService.exe")}
)
$rejected = $false
try {{
    Install-StockBotDirectoryExactly `
        -Source {quote(invalid_source)} `
        -Destination {quote(destination)} `
        -TrustedRoot {quote(managed)} `
        -RequiredRelativeFile 'StockBotService.exe'
}}
catch {{
    $rejected = $true
}}
if (!$rejected) {{ exit 13 }}
$afterFailure = [System.IO.File]::ReadAllBytes(
    {quote(destination / "StockBotService.exe")}
)
if (
    $beforeFailure.Length -ne $afterFailure.Length -or
    [System.BitConverter]::ToString($beforeFailure) -ne
        [System.BitConverter]::ToString($afterFailure)
) {{
    exit 14
}}
if (
    (Test-Path -LiteralPath {quote(Path(str(destination) + ".staging"))}) -or
    (Test-Path -LiteralPath {quote(Path(str(destination) + ".previous"))})
) {{
    exit 15
}}
$interruptedRejected = $false
try {{
    Install-StockBotDirectoryExactly `
        -Source {quote(invalid_source)} `
        -Destination {quote(interrupted_destination)} `
        -TrustedRoot {quote(managed)} `
        -RequiredRelativeFile 'StockBotService.exe'
}}
catch {{
    $interruptedRejected = $true
}}
if (!$interruptedRejected) {{ exit 16 }}
if (
    [System.IO.File]::ReadAllText(
        {quote(interrupted_destination / "StockBotService.exe")}
    ) -ne 'recovered'
) {{
    exit 17
}}
if (
    (Test-Path -LiteralPath {quote(interrupted_staging)}) -or
    (Test-Path -LiteralPath {quote(interrupted_previous)})
) {{
    exit 18
}}
$doubleStateRejected = $false
try {{
    Install-StockBotDirectoryExactly `
        -Source {quote(invalid_source)} `
        -Destination {quote(double_destination)} `
        -TrustedRoot {quote(managed)} `
        -RequiredRelativeFile 'StockBotService.exe'
}}
catch {{
    $doubleStateRejected = $true
}}
if (!$doubleStateRejected) {{ exit 19 }}
if (
    [System.IO.File]::ReadAllText(
        {quote(double_destination / "StockBotService.exe")}
    ) -ne 'older'
) {{
    exit 20
}}
if (Test-Path -LiteralPath {quote(double_previous)}) {{ exit 21 }}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_restores_allowlisted_snapshot_bytes(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            snapshot = root / "snapshot"
            source.mkdir()
            snapshot.mkdir()
            originals = {
                "credentials.env": b"\x00KIS\xff\n",
                "config.live.yaml": b"trading_mode: live\r\n",
                "service-config.json": b'{"schemaVersion":2}\n',
                "bridge-session.json": b'{"token":"old"}\x00',
            }
            for name, content in originals.items():
                (source / name).write_bytes(content)
            missing = "not-created.txt"
            allowlist = list(originals) + [missing]
            ps_allowlist = ", ".join(quote(name) for name in allowlist)
            powershell = f"""
$module = Import-Module {quote(module_path)} -Force -PassThru
& $module {{
    function script:Assert-StockBotRestrictedDirectoryTreeAcl {{
        param([string]$Path, [string]$TrustedRoot)
        return Assert-StockBotDirectoryTreeSafe `
            -Path $Path `
            -TrustedRoot $TrustedRoot
    }}
    function script:Assert-StockBotRestrictedPathAcl {{
        param(
            [string]$Path,
            [string]$TrustedRoot,
            [string]$ExpectedType
        )
        return Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -ExpectedType $ExpectedType
    }}
    function script:Set-StockBotRestrictedFileAcl {{
        param([string]$Path, [string]$TrustedRoot)
        Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
    }}
    function script:New-StockBotRestrictedFileStream {{
        param([string]$Path, [string]$TrustedRoot)
        Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        return [System.IO.FileStream]::new(
            $Path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    }}
}}
$allowlist = @({ps_allowlist})
$captured = @(
    Copy-StockBotAllowlistedFilesToSnapshot `
        -SourceRoot {quote(source)} `
        -SourceTrustedRoot {quote(root)} `
        -SnapshotRoot {quote(snapshot)} `
        -SnapshotTrustedRoot {quote(root)} `
        -FileNames $allowlist
)
if ($captured.Count -ne 4) {{ exit 10 }}
foreach ($name in $allowlist) {{
    Remove-StockBotSafeFile `
        -Path (Join-Path {quote(source)} $name) `
        -TrustedRoot {quote(root)}
}}
[System.IO.File]::WriteAllBytes(
    (Join-Path {quote(source)} 'credentials.env'),
    [byte[]](1, 2, 3)
)
$restored = @(
    Restore-StockBotAllowlistedFilesFromSnapshot `
        -SnapshotRoot {quote(snapshot)} `
        -SnapshotTrustedRoot {quote(root)} `
        -DestinationRoot {quote(source)} `
        -DestinationTrustedRoot {quote(root)} `
        -FileNames $allowlist
)
if ($restored.Count -ne 4) {{ exit 11 }}
foreach ($name in $captured) {{
    if (!(Test-StockBotFilesEqual `
        -LeftPath (Join-Path {quote(snapshot)} $name) `
        -LeftTrustedRoot {quote(root)} `
        -RightPath (Join-Path {quote(source)} $name) `
        -RightTrustedRoot {quote(root)}
    )) {{
        exit 12
    }}
}}
if (Test-Path -LiteralPath (Join-Path {quote(source)} {quote(missing)})) {{
    exit 13
}}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_builds_restricted_acl_without_broad_user_read(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        powershell = f"""
Import-Module {quote(module_path)} -Force
$acl = New-StockBotRestrictedDirectorySecurity -GrantCurrentIdentityRead
if (!$acl.AreAccessRulesProtected) {{ exit 10 }}
$rightsBySid = @{{}}
foreach ($rule in $acl.Access) {{
    $sid = $rule.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($rule.AccessControlType -ne
        [System.Security.AccessControl.AccessControlType]::Allow) {{
        exit 11
    }}
    $rights = 0
    if ($rightsBySid.ContainsKey($sid)) {{
        $rights = [int]$rightsBySid[$sid]
    }}
    $rightsBySid[$sid] = $rights -bor [int]$rule.FileSystemRights
}}
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$fullControl = [int][System.Security.AccessControl.FileSystemRights]::FullControl
$readAndExecute = [int][System.Security.AccessControl.FileSystemRights]::ReadAndExecute
if (!$rightsBySid.ContainsKey('S-1-5-18')) {{ exit 12 }}
if (([int]$rightsBySid['S-1-5-18'] -band $fullControl) -ne $fullControl) {{
    exit 13
}}
if (!$rightsBySid.ContainsKey('S-1-5-32-544')) {{ exit 14 }}
if (([int]$rightsBySid['S-1-5-32-544'] -band $fullControl) -ne $fullControl) {{
    exit 15
}}
if (!$rightsBySid.ContainsKey($currentSid)) {{ exit 16 }}
if (([int]$rightsBySid[$currentSid] -band $readAndExecute) -ne
    $readAndExecute) {{
    exit 17
}}
foreach ($broadSid in @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')) {{
    if ($rightsBySid.ContainsKey($broadSid)) {{ exit 18 }}
}}
"""
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                powershell,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_installer_helper_rejects_untrusted_recovery_acl(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        powershell = f"""
$module = Import-Module {quote(module_path)} -Force -PassThru
& $module {{
    $ErrorActionPreference = 'Stop'
    $trustedAcl = New-StockBotRestrictedDirectorySecurity
    Assert-StockBotRestrictedAccessControl -Acl $trustedAcl
    $trustedFileAcl = New-StockBotRestrictedFileSecurity
    Assert-StockBotRestrictedAccessControl -Acl $trustedFileAcl

    $usersSid = [System.Security.Principal.SecurityIdentifier]::new(
        'S-1-5-32-545'
    )
    $writeRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $usersSid,
        [System.Security.AccessControl.FileSystemRights]::Modify,
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $trustedAcl.AddAccessRule($writeRule)
    $broadWriteRejected = $false
    try {{
        Assert-StockBotRestrictedAccessControl -Acl $trustedAcl
    }}
    catch {{
        $broadWriteRejected = $true
    }}
    if (!$broadWriteRejected) {{ exit 10 }}

    $userOwnedAcl = New-StockBotRestrictedDirectorySecurity
    $userOwnedAcl.SetOwner(
        [Security.Principal.WindowsIdentity]::GetCurrent().User
    )
    $userOwnerRejected = $false
    try {{
        Assert-StockBotRestrictedAccessControl -Acl $userOwnedAcl
    }}
    catch {{
        $userOwnerRejected = $true
    }}
    if (!$userOwnerRejected) {{ exit 11 }}
}}
"""
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                powershell,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_gitignore_tracks_only_the_nsis_build_resource_exception(self):
        ignore_path = ROOT / ".gitignore"
        ignore_rules = ignore_path.read_text(encoding="utf-8").splitlines()

        self.assertIn("build/", ignore_rules)
        self.assertIn("!apps/electron-dashboard/build/", ignore_rules)
        self.assertIn(
            "!apps/electron-dashboard/build/installer.nsh",
            ignore_rules,
        )
        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "apps/electron-dashboard/build/installer.nsh",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            1,
            result.returncode,
            f"installer.nsh is still ignored: {result.stdout}{result.stderr}",
        )

    def test_windows_service_installer_passes_absolute_paths_and_configures_scm(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")
        lowered_script = script.lower()

        self.assertIn('"configure"', script)
        self.assertIn('"--service-config"', script)
        self.assertIn('"--authorize-live-orders"', script)
        self.assertIn("[switch]$AuthorizeLiveOrders", script)
        self.assertIn("if (!$AuthorizeLiveOrders)", script)
        for argument in ("--project-root", "--config", "--env-file", "--session-file"):
            self.assertIn(f'"{argument}"', script)
        self.assertIn("Get-StockBotServiceCommandLine", script)
        self.assertIn("sc.exe", lowered_script)
        self.assertIn('Invoke-ServiceControl -Arguments @(', script)
        self.assertIn("Invoke-StockBotWindowsNativeProcess", script)
        self.assertIn("$Result.ExitCode", script)
        self.assertIn("exit code $($Result.ExitCode)", script)
        self.assertIn("New-Service", script)
        self.assertIn("-BinaryPathName $BinaryPath", script)
        self.assertIn("-DisplayName $DisplayName", script)
        self.assertIn("-StartupType $InitialStartupType", script)
        self.assertIn('$InitialStartupType = "Manual"', script)
        self.assertIn('"start=", "delayed-auto"', script)
        self.assertIn('"start=", "demand"', script)
        self.assertNotIn('"DelayedAutoStart"', script)
        self.assertNotIn('"create",\n        $ServiceName', script)
        self.assertIn('"binPath=", $BinaryPath', script)
        self.assertIn('"failure"', script)
        self.assertIn('"actions=", "restart/60000/restart/60000/restart/60000"', script)
        self.assertIn('"actions=", ""', script)
        self.assertIn('("failureflag", $ServiceName, "0")', script)
        self.assertIn('("failureflag", $ServiceName, "1")', script)
        self.assertIn("Start-Service -Name $ServiceName", script)
        self.assertNotIn("ConvertTo-Json", script)
        self.assertNotIn("Write-Host $Session", script)
        self.assertNotIn("Write-Output $Session", script)
        self.assertNotIn("Get-Content", script)

    def test_windows_service_installer_passes_bounded_cycle_interval(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")
        configure_arguments = script[
            script.index("$ConfigureArguments = @(") :
            script.index("& $InstalledExecutable @ConfigureArguments")
        ]

        self.assertIn("[double]$CycleIntervalSeconds = 15", script)
        self.assertIn("[double]::IsNaN($CycleIntervalSeconds)", script)
        self.assertIn("[double]::IsInfinity($CycleIntervalSeconds)", script)
        self.assertIn("$CycleIntervalSeconds -lt 5", script)
        self.assertIn("$CycleIntervalSeconds -gt 3600", script)
        self.assertIn('"--cycle-interval-seconds"', configure_arguments)
        self.assertIn("$CycleIntervalSeconds", configure_arguments)
        self.assertIn("& $InstalledExecutable @ConfigureArguments", script)

    def test_windows_service_control_accepts_empty_scm_action_argument(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")
        helper = script[
            script.index("function Invoke-ServiceControl") :
            script.index("function Get-ValidatedServiceRegistration")
        ]

        self.assertIn("[AllowEmptyString()]", helper)
        self.assertIn(
            "Invoke-StockBotWindowsNativeProcess",
            helper,
        )
        self.assertNotIn("& $ScExe", helper)
        self.assertIn('"actions=", ""', script)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_windows_service_control_preserves_empty_and_quoted_native_arguments(self):
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        helper_module = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        command_line = (
            '"C:\\Program Files\\StockBotService\\StockBotService.exe" '
            'run-service --service-config '
            '"C:\\ProgramData\\StockBot\\service-config.json"'
        )
        with TemporaryDirectory() as temp_dir:
            probe_path = Path(temp_dir) / "native_argv_probe.py"
            probe_path.write_text(
                "import json\nimport sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            logical_arguments = [
                "failure",
                "StockBotLive",
                "reset=",
                "0",
                "actions=",
                "",
                "config",
                "StockBotLive",
                "binPath=",
                command_line,
            ]
            powershell_arguments = ",\n        ".join(
                quote(argument) for argument in [probe_path, *logical_arguments]
            )
            powershell = f"""
Import-Module -Name {quote(helper_module)} -Force -ErrorAction Stop
$result = Invoke-StockBotWindowsNativeProcess `
    -FilePath {quote(sys.executable)} `
    -Arguments @(
        {powershell_arguments}
    )
if ($result.ExitCode -ne 0) {{
    [Console]::Error.Write($result.StandardError)
    exit 20
}}
$result.StandardOutput
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )
        self.assertEqual(logical_arguments, json.loads(result.stdout.strip()))

    def test_windows_service_installer_waits_for_fresh_running_session(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")

        self.assertIn("function Wait-ServiceReady", script)
        self.assertIn("function Test-ServiceSessionReady", script)
        self.assertIn("[System.IO.File]::ReadAllText($SessionFile)", script)
        self.assertIn("ConvertFrom-Json", script)
        self.assertIn('$Session.schemaVersion -ne 1', script)
        self.assertIn("$SessionToken.Length -lt 32", script)
        self.assertIn("$Registration.ProcessId -ne $SessionProcessId", script)
        self.assertIn("Invoke-RestMethod", script)
        self.assertIn('"/api/health"', script)
        self.assertIn("Test-ServiceSessionReady", script)
        self.assertIn("Remove-StockBotSessionAfterConfirmedStop", script)
        self.assertIn(
            "Refusing to remove the bridge session because the service is not Stopped with PID 0.",
            script,
        )
        self.assertNotIn(
            "if (Test-Path -LiteralPath $SessionFilePath) {\n"
            "    Remove-Item -LiteralPath $SessionFilePath -Force\n"
            "}",
            script,
        )
        recovery_disable = script.index('"actions=", ""')
        fresh_registration = script.index(
            "$RegisteredConfiguration = Get-ValidatedServiceRegistration",
            recovery_disable,
        )
        session_cleanup = script.index(
            "Remove-StockBotSessionAfterConfirmedStop",
            fresh_registration,
        )
        self.assertLess(recovery_disable, fresh_registration)
        self.assertLess(fresh_registration, session_cleanup)
        self.assertIn('$Service.Status -eq "Running"', script)
        self.assertIn("Test-Path -LiteralPath $SessionFile", script)
        self.assertIn("Wait-ServiceReady", script)

    def test_windows_service_installer_can_register_without_starting_live_runtime(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$RegisterOnly", script)
        self.assertIn('$InitialStartupType = "Manual"', script)
        register_only_guard = script.rindex("if ($RegisterOnly)")
        service_start = script.index("Start-Service -Name $ServiceName")
        readiness_wait = script.rindex("Wait-ServiceReady")
        self.assertLess(register_only_guard, service_start)
        self.assertLess(service_start, readiness_wait)
        self.assertIn("No live runtime was started", script)

    def test_windows_service_installer_rolls_back_failed_live_activation(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")

        activation = script[script.rindex("if ($RegisterOnly)") :]
        self.assertIn("try {", activation)
        self.assertIn("Start-Service -Name $ServiceName", activation)
        self.assertIn("Wait-ServiceReady", activation)
        self.assertIn(
            'Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "delayed-auto")',
            activation,
        )
        service_start = activation.index("Start-Service -Name $ServiceName")
        readiness_wait = activation.index("Wait-ServiceReady")
        delayed_auto = activation.index(
            'Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "delayed-auto")'
        )
        failure_flag = activation.index(
            'Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "1")'
        )
        recovery_actions = activation.index(
            '"actions=", "restart/60000/restart/60000/restart/60000"'
        )
        self.assertLess(service_start, readiness_wait)
        self.assertLess(readiness_wait, delayed_auto)
        self.assertLess(delayed_auto, failure_flag)
        self.assertLess(failure_flag, recovery_actions)
        final_readiness = activation.rindex("Wait-ServiceReady", 0, activation.index("catch {"))
        self.assertGreater(final_readiness, recovery_actions)
        self.assertEqual(2, activation[: activation.index("catch {")].count("Wait-ServiceReady"))
        self.assertIn("catch {", activation)
        self.assertIn("Stop-Service -Name $ServiceName", activation)
        self.assertIn('Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "demand")', activation)
        rollback = activation[activation.index("catch {") :]
        self.assertIn('"actions=", ""', rollback)
        self.assertIn(
            'Invoke-ServiceControl -Arguments @("failureflag", $ServiceName, "0")',
            rollback,
        )
        manual_restore = rollback.index(
            'Invoke-ServiceControl -Arguments @("config", $ServiceName, "start=", "demand")'
        )
        rollback_registration = rollback.index(
            "$RollbackRegistration = Get-CimInstance"
        )
        rollback_cleanup = rollback.index(
            "Remove-StockBotSessionAfterConfirmedStop",
            rollback_registration,
        )
        self.assertLess(manual_restore, rollback_registration)
        self.assertLess(rollback_registration, rollback_cleanup)
        self.assertIn("$ServiceStopped = $false", activation)
        self.assertIn("Test-StockBotServiceStopped", activation)
        self.assertIn("Remove-StockBotSessionAfterConfirmedStop", activation)
        self.assertIn("throw $ActivationException", activation)
        helpers = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).read_text(encoding="utf-8")
        self.assertIn('"Stopped"', helpers)
        self.assertIn("[int]$Registration.ProcessId -eq 0", helpers)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_windows_service_session_cleanup_requires_confirmed_stopped_process(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        self.assertTrue(module_path.is_file())

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = root / "bridge-session.json"
            session_path.write_text("{}", encoding="utf-8")
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            powershell = f"""
Import-Module {quote(module_path)} -Force
$running = [pscustomobject]@{{ State = 'Running'; ProcessId = 4321 }}
$cleanup = Remove-StockBotSessionAfterConfirmedStop `
    -Registration $running `
    -SessionFile {quote(session_path)} `
    -TrustedRoot {quote(root)}
if ($cleanup -or !(Test-Path -LiteralPath {quote(session_path)})) {{ exit 10 }}
$stoppedWithProcess = [pscustomobject]@{{ State = 'Stopped'; ProcessId = 4321 }}
$cleanup = Remove-StockBotSessionAfterConfirmedStop `
    -Registration $stoppedWithProcess `
    -SessionFile {quote(session_path)} `
    -TrustedRoot {quote(root)}
if ($cleanup -or !(Test-Path -LiteralPath {quote(session_path)})) {{ exit 11 }}
$runningWithoutProcess = [pscustomobject]@{{ State = 'Running'; ProcessId = 0 }}
$cleanup = Remove-StockBotSessionAfterConfirmedStop `
    -Registration $runningWithoutProcess `
    -SessionFile {quote(session_path)} `
    -TrustedRoot {quote(root)}
if ($cleanup -or !(Test-Path -LiteralPath {quote(session_path)})) {{ exit 12 }}
$stopped = [pscustomobject]@{{ State = 'Stopped'; ProcessId = 0 }}
$cleanup = Remove-StockBotSessionAfterConfirmedStop `
    -Registration $stopped `
    -SessionFile {quote(session_path)} `
    -TrustedRoot {quote(root)}
if (!$cleanup -or (Test-Path -LiteralPath {quote(session_path)})) {{ exit 13 }}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_admin_managed_paths_reject_reparse_points_and_use_known_folders(self):
        helpers = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).read_text(encoding="utf-8")
        scripts = [
            (ROOT / "tools" / name).read_text(encoding="utf-8")
            for name in (
                "install_stockbot_service.ps1",
                "install_stockbot_packaged_service.ps1",
                "uninstall_stockbot_service.ps1",
            )
        ]

        self.assertIn("CommonApplicationData", helpers)
        self.assertIn("SpecialFolder]::ProgramFiles", helpers)
        self.assertIn("FileAttributes]::ReparsePoint", helpers)
        self.assertIn("function Assert-StockBotTrustedPath", helpers)
        self.assertIn("function Assert-StockBotDirectoryTreeSafe", helpers)
        self.assertIn("function Remove-StockBotSafeFile", helpers)
        self.assertIn("function Remove-StockBotSafeTree", helpers)
        self.assertIn("function New-StockBotSafeDirectory", helpers)
        for script in scripts:
            self.assertIn("Get-StockBotKnownFolderPath", script)
            self.assertNotIn("$env:ProgramData", script)
            self.assertNotIn("$env:ProgramFiles", script)
        combined = helpers + "\n".join(scripts)
        for broad_principal in (
            "S-1-5-32-545",
            "Authenticated Users",
            "Everyone",
        ):
            self.assertNotIn(broad_principal, combined)

    def test_bundle_update_uses_exact_staging_swap_and_stopped_rollback(self):
        helpers = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).read_text(encoding="utf-8")
        installer = (
            ROOT / "tools" / "install_stockbot_service.ps1"
        ).read_text(encoding="utf-8")
        wrapper = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("function Install-StockBotDirectoryExactly", helpers)
        self.assertIn('".staging"', helpers)
        self.assertIn('".previous"', helpers)
        self.assertIn("Move-Item `", helpers)
        self.assertIn("function Move-StockBotSafeDirectory", helpers)
        self.assertIn("-Source $StagingPath", helpers)
        self.assertIn("-Source $PreviousPath", helpers)
        self.assertIn("[int]$MaxAttempts = 5", helpers)
        self.assertIn("Start-Sleep -Seconds 1", helpers)
        self.assertIn("Remove-StockBotSafeTree", helpers)
        self.assertIn("Install-StockBotDirectoryExactly `", installer)
        self.assertNotIn("function Copy-ServiceBundle", installer)
        self.assertNotIn(
            "Copy-Item `\n                -Path (Join-Path $Source \"*\")",
            installer,
        )
        rollback = wrapper[
            wrapper.index("function Restore-StockBotExistingInstallation") :
            wrapper.index("function Remove-StockBotFreshStateBackup")
        ]
        self.assertIn("RegisterOnly = $true", rollback)
        self.assertIn("Wait-StockBotServiceStopped", rollback)
        self.assertIn('$RestoredService.StartType -ne "Manual"', rollback)
        self.assertNotIn("Start-Service", rollback)

    def test_fresh_install_failure_restores_preexisting_private_files_by_bytes(self):
        script = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")

        for file_name in (
            "credentials.env",
            "config.live.yaml",
            "service-config.json",
            "bridge-session.json",
        ):
            self.assertIn(f'"{file_name}"', script)
        self.assertIn("New-StockBotFreshStateBackup", script)
        self.assertIn("Restore-StockBotFreshStateBackup", script)
        self.assertIn("[System.IO.File]::Copy", script)
        self.assertIn("Test-StockBotFilesEqual", script)
        self.assertIn('"StockBotInstallerRecovery"', script)
        self.assertIn("$FreshInstallerRollbackExisted", script)
        self.assertIn("PreserveInstallerRollback = $true", script)
        self.assertIn("$OrphanedServicePath", script)
        existing_program_data_check = script[
            script.index("$ExistingProgramData =") :
            script.index("$script:FreshProgramDataRootExisted = $true")
        ]
        self.assertIn("Assert-StockBotTrustedPath", existing_program_data_check)
        self.assertNotIn(
            "Assert-StockBotDirectoryTreeSafe",
            existing_program_data_check,
        )
        self.assertIn(
            "Refusing fresh installation over an unregistered StockBot",
            script,
        )
        self.assertNotIn("S-1-5-32-545", script)
        catch_block = script[script.index("catch {", script.index("$BackupMode")) :]
        self.assertLess(
            catch_block.index("& $UninstallerScript"),
            catch_block.index("Restore-StockBotFreshStateBackup"),
        )

    def test_interrupted_fresh_install_recovery_is_resumable_and_uninstallable(self):
        wrapper = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")
        uninstaller = (
            ROOT / "tools" / "uninstall_stockbot_service.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Get-StockBotValidatedFreshRecoveryManifest", wrapper)
        self.assertIn('State "collecting"', wrapper)
        self.assertIn('State "prepared"', wrapper)
        self.assertIn('State "committed"', wrapper)
        self.assertIn("function Recover-StockBotInterruptedFreshInstall", wrapper)
        self.assertLess(
            wrapper.index("Recover-StockBotInterruptedFreshInstall\n\n"),
            wrapper.index("$ExistingServiceController ="),
        )
        self.assertIn("PreserveFreshRecovery = $true", wrapper)
        self.assertIn(
            "Test-StockBotFreshRecoveryContainsOnlyMetadata",
            wrapper,
        )

        self.assertIn("[switch]$PreserveFreshRecovery", uninstaller)
        self.assertIn(
            "Test-StockBotFreshRecoveryContainsOnlyMetadata",
            uninstaller,
        )
        validation = uninstaller.index(
            "Get-StockBotValidatedFreshRecoveryManifest"
        )
        service_mutation = uninstaller.index(
            "$Registration = Get-ValidatedStockBotServiceRegistration"
        )
        self.assertLess(validation, service_mutation)
        self.assertIn(
            "Remove-StockBotSafeTree `\n"
            "        -Path $FreshRecoveryRoot",
            uninstaller,
        )

    def test_fresh_recovery_objects_are_created_with_restricted_acl_atomically(self):
        helper = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).read_text(encoding="utf-8")
        wrapper = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("function New-StockBotRestrictedDirectory", helper)
        self.assertIn("[System.IO.Directory]::CreateDirectory(", helper)
        self.assertIn("function New-StockBotRestrictedFileStream", helper)
        self.assertIn("[System.IO.FileStream]::new(", helper)
        self.assertIn(
            "New-StockBotRestrictedDirectory `\n"
            "        -Path $FreshRecoveryRoot",
            wrapper,
        )
        self.assertNotIn(
            "New-StockBotSafeDirectory `\n"
            "        -Path $FreshRecoveryRoot",
            wrapper,
        )
        manifest_validation = helper[
            helper.index("function Get-StockBotValidatedFreshRecoveryManifest") :
            helper.index("function Test-StockBotFreshRecoveryContainsOnlyMetadata")
        ]
        self.assertLess(
            manifest_validation.index('$Payload.state -eq "collecting"'),
            manifest_validation.index(
                "Assert-StockBotRestrictedDirectoryTreeAcl",
                manifest_validation.index('$Payload.state -eq "collecting"'),
            ),
        )
        recovery = wrapper[
            wrapper.index("function Recover-StockBotInterruptedFreshInstall") :
            wrapper.index("Recover-StockBotInterruptedFreshInstall\n\n")
        ]
        metadata_catch = recovery[
            recovery.index("catch {") :
            recovery.index('if ($Recovery.State -in @("collecting", "committed"))')
        ]
        self.assertIn(
            "Test-StockBotFreshRecoveryContainsOnlyMetadata",
            metadata_catch,
        )
        self.assertNotIn("& $UninstallerScript", metadata_catch)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_fresh_recovery_manifest_accepts_interrupted_collection_and_rejects_tampering(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recovery = root / "recovery"
            recovery.mkdir()
            early_recovery = root / "early-recovery"
            early_recovery.mkdir()
            early_manifest = early_recovery / ".recovery-manifest.json.tmp"
            credential_file = recovery / "credentials.env"
            powershell = f"""
$module = Import-Module {quote(module_path)} -Force -PassThru
& $module {{
    function script:Assert-StockBotRestrictedDirectoryTreeAcl {{
        param([string]$Path, [string]$TrustedRoot)
        return Assert-StockBotDirectoryTreeSafe `
            -Path $Path `
            -TrustedRoot $TrustedRoot
    }}
    function script:Assert-StockBotRestrictedPathAcl {{
        param(
            [string]$Path,
            [string]$TrustedRoot,
            [string]$ExpectedType
        )
        return Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -ExpectedType $ExpectedType
    }}
    function script:Set-StockBotRestrictedFileAcl {{
        param([string]$Path, [string]$TrustedRoot)
        Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -ExpectedType File | Out-Null
    }}
    function script:New-StockBotRestrictedFileStream {{
        param([string]$Path, [string]$TrustedRoot)
        Assert-StockBotTrustedPath `
            -Path $Path `
            -TrustedRoot $TrustedRoot `
            -AllowMissing `
            -ExpectedType File | Out-Null
        return [System.IO.FileStream]::new(
            $Path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
    }}
}}
$files = @('credentials.env', 'config.live.yaml')
[System.IO.File]::WriteAllText(
    {quote(early_manifest)},
    '{{',
    [System.Text.UTF8Encoding]::new($false)
)
if (!(Test-StockBotFreshRecoveryContainsOnlyMetadata `
    -RecoveryRoot {quote(early_recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files
)) {{ exit 10 }}
[System.IO.File]::WriteAllText(
    {quote(early_recovery / "credentials.env")},
    'partial-copy',
    [System.Text.UTF8Encoding]::new($false)
)
if (Test-StockBotFreshRecoveryContainsOnlyMetadata `
    -RecoveryRoot {quote(early_recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files
) {{ exit 15 }}
Write-StockBotFreshRecoveryManifest `
    -RecoveryRoot {quote(recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files `
    -State 'collecting' `
    -ProgramDataRootExisted $true `
    -InstallerRollbackExisted $false
[System.IO.File]::WriteAllText(
    {quote(credential_file)},
    'placeholder-value',
    [System.Text.UTF8Encoding]::new($false)
)
$collecting = Get-StockBotValidatedFreshRecoveryManifest `
    -RecoveryRoot {quote(recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files
if ($collecting.State -ne 'collecting') {{ exit 11 }}
Write-StockBotFreshRecoveryManifest `
    -RecoveryRoot {quote(recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files `
    -State 'prepared' `
    -ProgramDataRootExisted $true `
    -InstallerRollbackExisted $false
$prepared = Get-StockBotValidatedFreshRecoveryManifest `
    -RecoveryRoot {quote(recovery)} `
    -TrustedRoot {quote(root)} `
    -FileNames $files
if ($prepared.State -ne 'prepared') {{ exit 12 }}
if ($prepared.FileNames.Count -ne 1) {{ exit 13 }}
[System.IO.File]::AppendAllText({quote(credential_file)}, '-tampered')
$rejected = $false
try {{
    Get-StockBotValidatedFreshRecoveryManifest `
        -RecoveryRoot {quote(recovery)} `
        -TrustedRoot {quote(root)} `
        -FileNames $files | Out-Null
}}
catch {{
    $rejected = $true
}}
if (!$rejected) {{ exit 14 }}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "PowerShell behavior test requires Windows",
    )
    def test_fresh_recovery_rejects_coherent_manifest_under_untrusted_root(self):
        module_path = ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recovery = root / "recovery"
            recovery.mkdir()
            credential_file = recovery / "credentials.env"
            credential_bytes = b"attacker-controlled-placeholder"
            credential_file.write_bytes(credential_bytes)
            manifest = {
                "schemaVersion": 1,
                "state": "prepared",
                "programDataRootExisted": True,
                "installerRollbackExisted": False,
                "files": [
                    {
                        "name": credential_file.name,
                        "sha256": hashlib.sha256(credential_bytes).hexdigest(),
                    }
                ],
            }
            (recovery / "recovery-manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            powershell = f"""
Import-Module {quote(module_path)} -Force
$accepted = $true
try {{
    Get-StockBotValidatedFreshRecoveryManifest `
        -RecoveryRoot {quote(recovery)} `
        -TrustedRoot {quote(root)} `
        -FileNames @('credentials.env', 'config.live.yaml') | Out-Null
}}
catch {{
    $accepted = $false
}}
if ($accepted) {{ exit 10 }}
"""
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_windows_service_installer_rejects_a_standalone_live_bridge(self):
        script = (ROOT / "tools" / "install_stockbot_service.ps1").read_text(encoding="utf-8")

        self.assertIn("function Assert-NoStandaloneLiveBridge", script)
        self.assertIn("Get-CimInstance Win32_Process", script)
        self.assertIn("stockbot.electron_bridge", script)
        self.assertNotIn('$CommandLine.Contains("--persistent-live")', script)

    def test_electron_prefers_installed_service_before_environment_bridge(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "apps" / "electron-dashboard" / "electron" / "bridge_lifecycle.cjs"
        ).read_text(encoding="utf-8")
        launch_policy = lifecycle[
            lifecycle.index("function bridgeLaunchPolicy(") :
            lifecycle.index("function rendererOwnedBridgeArgs(")
        ]

        self.assertIn("bridgeLaunchPolicy", main)
        self.assertLess(
            launch_policy.index("if (serviceInstalled)"),
            launch_policy.index("if (environmentBridgeConfigured)"),
        )

    def test_gitignore_keeps_pyinstaller_outputs_untracked(self):
        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("build/", ignore_rules)
        self.assertIn("dist/", ignore_rules)

    def test_gitignore_keeps_local_diagnostic_exports_untracked(self):
        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("stockbot-diagnostics-*.json", ignore_rules)

    def test_pyproject_declares_electron_bridge_entrypoint(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('stockbot-electron-bridge = "stockbot.electron_bridge:main"', text)
        self.assertTrue(callable(importlib.import_module("stockbot.electron_bridge").main))

    def test_electron_dashboard_declares_windows_installer_and_safe_smoke(self):
        package = json.loads(
            (ROOT / "apps" / "electron-dashboard" / "package.json").read_text(encoding="utf-8")
        )
        ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual("electron/main.cjs", package["main"])
        self.assertEqual(">=22.12.0", package["engines"]["node"])
        self.assertEqual("tsc -b && vite build", package["scripts"]["build"])
        self.assertIn("npm ls --depth=0", package["scripts"]["verify:dependencies"])
        self.assertIn("verify-dependencies.cjs", package["scripts"]["verify:dependencies"])
        self.assertIn("npm run verify:dependencies", package["scripts"]["package:win"])
        self.assertIn("npm run verify:dependencies", package["scripts"]["package:win:dir"])
        self.assertIn("npm run prepare:package", package["scripts"]["package:win"])
        self.assertIn("npm run prepare:package", package["scripts"]["package:win:dir"])
        self.assertIn("electron-builder --win nsis --x64", package["scripts"]["package:win"])
        self.assertIn("test:e2e:packaged", package["scripts"]["verify:package:win"])
        self.assertIn("electron-builder", package["devDependencies"])
        self.assertIn("playwright-core", package["devDependencies"])
        self.assertEqual("../../dist/electron-dashboard", package["build"]["directories"]["output"])
        self.assertEqual("com.heewoung.stockbot", package["build"]["appId"])
        self.assertEqual("StockBot", package["build"]["executableName"])
        self.assertEqual(
            package["devDependencies"]["electron"],
            package["build"]["electronVersion"],
        )
        self.assertTrue(package["build"]["asar"])
        self.assertIn("node_modules/", ignore_rules)

        prepare_script = (
            ROOT / "apps" / "electron-dashboard" / "scripts" / "prepare-package.cjs"
        ).read_text(encoding="utf-8")
        self.assertIn('path.join(distributionRoot, "StockBot")', prepare_script)
        self.assertIn("fs.rmSync", prepare_script)

    def test_electron_vite_build_uses_relative_asset_base_for_load_file(self):
        config = (ROOT / "apps" / "electron-dashboard" / "vite.config.ts").read_text(encoding="utf-8")

        self.assertIn('base: "./"', config)

    def test_electron_renderer_uses_base_relative_public_icon(self):
        public_icon = ROOT / "apps" / "electron-dashboard" / "public" / "stockbot-donghak-ant-icon.png"
        app_source = (ROOT / "apps" / "electron-dashboard" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertTrue(public_icon.exists())
        self.assertIn("import.meta.env.BASE_URL", app_source)
        self.assertIn("stockbot-donghak-ant-icon.png", app_source)
        self.assertNotIn('src="/stockbot-donghak-ant-icon.png"', app_source)

    def test_built_electron_html_uses_relative_asset_paths_when_present(self):
        dist_index = ROOT / "apps" / "electron-dashboard" / "dist" / "index.html"
        if not dist_index.exists():
            self.skipTest("Electron dist has not been built")
        html = dist_index.read_text(encoding="utf-8")

        self.assertNotIn('src="/assets/', html)
        self.assertNotIn('href="/assets/', html)
        self.assertIn('src="./assets/', html)
        self.assertIn('href="./assets/', html)

    def test_electron_main_starts_bridge_on_ephemeral_port_and_ipc_only(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "apps" / "electron-dashboard" / "electron" / "bridge_lifecycle.cjs"
        ).read_text(encoding="utf-8")
        preload = (ROOT / "apps" / "electron-dashboard" / "electron" / "preload.cjs").read_text(encoding="utf-8")
        dashboard_actions = (ROOT / "apps" / "electron-dashboard" / "electron" / "dashboard_actions.cjs").read_text(encoding="utf-8")

        self.assertIn('"--port",', lifecycle)
        self.assertIn('    "0",', lifecycle)
        self.assertNotIn("--persistent-live", lifecycle)
        self.assertIn("waitForBridgeReady", main)
        self.assertIn("ipcMain.handle", main)
        self.assertIn("stockbot:load-state", main)
        self.assertIn("stockbot:run-action", main)
        self.assertIn("DASHBOARD_ACTIONS", main)
        self.assertIn('"kis-credentials"', dashboard_actions)
        self.assertIn("ipcRenderer.invoke", preload)
        self.assertNotIn("token:", preload)
        self.assertNotIn("STOCKBOT_BRIDGE_TOKEN", preload)

    def test_electron_main_passes_live_config_to_python_bridge(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "apps" / "electron-dashboard" / "electron" / "bridge_lifecycle.cjs"
        ).read_text(encoding="utf-8")

        self.assertIn("dashboardConfigPath", main)
        self.assertIn("STOCKBOT_CONFIG_PATH", main)
        self.assertIn("config.live.example.yaml", main)
        self.assertIn('"--config"', lifecycle)

    def test_electron_main_allows_kis_data_source_switch_action(self):
        dashboard_actions = (ROOT / "apps" / "electron-dashboard" / "electron" / "dashboard_actions.cjs").read_text(encoding="utf-8")

        self.assertIn('"data-source"', dashboard_actions)
        self.assertIn('"clear-manual-reconciliation"', dashboard_actions)

    def test_electron_main_falls_back_across_python_candidates_until_bridge_ready(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")

        self.assertIn("for (const python of pythonCandidates())", main)
        self.assertIn("launchBridgeWithPython", main)
        self.assertIn("waitForBridgeReady", main)
        self.assertIn('child.once("error", onError)', main)

    def test_electron_main_does_not_reclassify_owned_bridge_as_external(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")

        self.assertNotIn("process.env.STOCKBOT_BRIDGE_URL = session.url", main)
        self.assertIn("bridgeSessionFromEnvironment", main)
        self.assertIn("createActiveBridgeProcessRegistry", main)
        self.assertLess(
            main.index('requestBridgeJson(session, "/api/health"'),
            main.index("bridgeProcesses.adopt(child)"),
        )
        self.assertNotIn("bridgeProcess.kill()", main)

    def test_electron_main_tags_both_renderer_ipc_paths_with_bridge_generation(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")

        register_ipc = main[main.index("function registerBridgeIpc") : main.index("function startBridge")]
        load_state_marker = 'ipcMain.handle("stockbot:load-state"'
        run_action_marker = 'ipcMain.handle("stockbot:run-action"'
        load_state_handler = register_ipc.split(load_state_marker, maxsplit=1)[1].split(run_action_marker, maxsplit=1)[0]
        run_action_handler = register_ipc.split(run_action_marker, maxsplit=1)[1]
        renderer_request = main[
            main.index("async function requestBridgeStateForRenderer") : main.index("function startBridge")
        ]

        self.assertIn("createBridgeSessionSequencer()", main)
        self.assertIn("requestBridgeStateForRenderer", load_state_handler)
        self.assertIn("requestBridgeStateForRenderer", run_action_handler)
        self.assertIn("bridgePayloadForRenderer", renderer_request)
        self.assertIn("bridgeFailurePayloadForRenderer", renderer_request)
        self.assertIn("currentGeneration()", renderer_request)

    def test_electron_bridge_console_paths_redact_raw_diagnostics(self):
        main = (ROOT / "apps" / "electron-dashboard" / "electron" / "main.cjs").read_text(encoding="utf-8")

        self.assertIn("redactBridgeDiagnosticText", main)
        self.assertNotIn("console.error(`[stockbot-bridge] ${chunk}`)", main)
        self.assertNotIn("errors.push(`${python}: ${error && error.message ? error.message : String(error)}`)", main)

    def test_root_electron_dashboard_launcher_points_to_option_c_app(self):
        cmd = (ROOT / "run-electron-dashboard.cmd").read_text(encoding="utf-8")
        script = (ROOT / "tools" / "run_electron_dashboard.ps1").read_text(encoding="utf-8")

        self.assertIn("tools\\run_electron_dashboard.ps1", cmd)
        self.assertIn("apps\\electron-dashboard", script)
        self.assertIn("npm.cmd run verify:dependencies --silent", script)
        self.assertIn("npm.cmd install", script)
        self.assertIn("npm.cmd rebuild electron", script)
        self.assertIn("npm.cmd run build", script)
        self.assertIn("npm.cmd run electron", script)


if __name__ == "__main__":
    unittest.main()
