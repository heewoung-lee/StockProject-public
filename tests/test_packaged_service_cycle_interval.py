import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagedServiceCycleIntervalTest(unittest.TestCase):
    def test_update_interval_policy_applies_current_value_and_restores_rollback_value(
        self,
    ):
        module_path = (
            ROOT / "tools" / "stockbot_service_installer_helpers.psm1"
        ).resolve()
        quoted_module_path = str(module_path).replace("'", "''")
        powershell = f"""
Import-Module '{quoted_module_path}' -Force
$updated = Resolve-StockBotPackagedCycleIntervalSeconds `
    -ExistingCycleIntervalSeconds 60 `
    -CurrentCycleIntervalSeconds 15
if ($updated -ne 15) {{ exit 11 }}
$rollback = Resolve-StockBotPackagedCycleIntervalSeconds `
    -ExistingCycleIntervalSeconds 60 `
    -CurrentCycleIntervalSeconds 15 `
    -RestoreExisting
if ($rollback -ne 60) {{ exit 12 }}
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_existing_and_fresh_installs_use_current_packaged_interval(self):
        script = (
            ROOT / "tools" / "install_stockbot_packaged_service.ps1"
        ).read_text(encoding="utf-8")
        install_arguments_start = script.index("$InstallArguments = @{")
        install_flow = script[install_arguments_start:]

        self.assertIn("$PackagedCycleIntervalSeconds = 15", script)
        self.assertEqual(
            1,
            install_flow.count(
                "$InstallArguments.CycleIntervalSeconds = "
                "$PackagedCycleIntervalSeconds"
            ),
        )
        self.assertNotIn(
            "$InstallArguments.CycleIntervalSeconds = "
            "$ExistingPaths.CycleIntervalSeconds",
            install_flow,
        )
        self.assertIn(
            "Resolve-StockBotPackagedCycleIntervalSeconds",
            install_flow,
        )
        self.assertIn(
            "-RestoreExisting",
            script[:install_arguments_start],
        )


if __name__ == "__main__":
    unittest.main()
