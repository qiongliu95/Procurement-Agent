"""Windows 一键启动器的安全契约与实际 PowerShell 行为检查。"""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent
CONTROL = ROOT / "scripts" / "procurement_agent_control.ps1"


class WindowsLauncherContractTest(unittest.TestCase):
    def test_entry_points_exist(self):
        for name in ("启动采购Agent.bat", "关闭采购Agent.bat", "配置采购Agent模型.bat"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_start_bat_has_no_bom_so_echo_off_is_recognized(self):
        self.assertTrue((ROOT / "启动采购Agent.bat").read_bytes().startswith(b"@echo off"))

    def test_powershell_preserves_bom_for_windows_chinese_messages(self):
        self.assertTrue(CONTROL.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_config_stays_outside_project_and_key_is_protected(self):
        text = CONTROL.read_text(encoding="utf-8-sig")
        self.assertIn("$env:LOCALAPPDATA", text)
        self.assertIn("ConvertFrom-SecureString", text)
        self.assertIn("ConvertTo-SecureString", text)
        self.assertNotIn("sk-", text)

    def test_start_reuses_database_and_waits_for_both_services(self):
        text = CONTROL.read_text(encoding="utf-8-sig")
        self.assertIn("@('-u', 'backend.py', 'demo_test.sqlite')", text)
        self.assertIn("@('-u', 'agent_server.py')", text)
        self.assertNotIn("reset_demo.py", text)
        self.assertIn("Wait-Ready ${function:Test-BackendReady}", text)
        self.assertIn("Wait-Ready ${function:Test-AgentReady}", text)
        self.assertIn("Start-Process $AgentUrl", text)
        self.assertIn("Start-Process $BackendUrl", text)

    def test_background_processes_are_hidden_and_stop_is_pid_scoped(self):
        text = CONTROL.read_text(encoding="utf-8-sig")
        self.assertGreaterEqual(text.count("-WindowStyle Hidden"), 2)
        self.assertIn("BackendStartedAt", text)
        self.assertIn("AgentStartedAt", text)
        self.assertIn("Stop-Process -Id ([int]$PidValue)", text)
        self.assertNotIn("taskkill", text.lower())


@unittest.skipUnless(os.name == "nt", "需要 Windows PowerShell")
class WindowsLauncherBehaviorTest(unittest.TestCase):
    def run_control(self, body, *, no_browser=False):
        with tempfile.TemporaryDirectory() as directory:
            escaped_control = str(CONTROL).replace("'", "''")
            escaped_directory = directory.replace("'", "''")
            script = (
                "$ErrorActionPreference='Stop'; "
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                f". '{escaped_control}' " + ("-NoBrowser; " if no_browser else "; ")
                + f"$LogDirectory='{escaped_directory}'; "
                + f"$StatePath=Join-Path '{escaped_directory}' 'state.json'; "
                + body
            )
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                capture_output=True, timeout=20,
            )
            output = result.stdout.decode("utf-8-sig", errors="replace")
            self.assertEqual(result.returncode, 0, output + result.stderr.decode(errors="replace"))
            return output

    def test_dead_child_is_reported_immediately_and_log_is_not_empty(self):
        python = sys.executable.replace("'", "''")
        output = self.run_control(
            f"$child=Start-Process -FilePath '{python}' -ArgumentList @('-c','exit(7)') "
            "-WindowStyle Hidden -PassThru; $child.WaitForExit(); "
            "$timer=[Diagnostics.Stopwatch]::StartNew(); "
            "$ready=Wait-Ready { $false } 'test-child' -Seconds 30 -ChildProcess $child; "
            "if ($ready -or $timer.Elapsed.TotalSeconds -gt 5) { throw 'did not fail fast' }; "
            "$log=Get-Content -Raw (Join-Path $LogDirectory 'launcher.log'); "
            "if ($log -notmatch '7') { throw 'missing diagnostic' }; 'EXIT_DIAGNOSTIC_OK'"
        )
        self.assertIn("EXIT_DIAGNOSTIC_OK", output)

    def test_slow_initialization_can_become_ready_without_changing_default(self):
        output = self.run_control(
            "if ($AgentStartupTimeoutSeconds -ne 180) { throw 'wrong cold-start timeout' }; "
            "$script:polls=0; "
            "$ready=Wait-Ready { $script:polls++; $script:polls -ge 3 } 'slow-child' -Seconds 3; "
            "if (-not $ready) { throw 'late readiness rejected' }; 'DELAYED_READY_OK'"
        )
        self.assertIn("DELAYED_READY_OK", output)

    def test_timeout_is_logged(self):
        output = self.run_control(
            "$ready=Wait-Ready { $false } 'timeout-child' -Seconds 1; "
            "if ($ready -or -not (Test-Path (Join-Path $LogDirectory 'launcher.log'))) "
            "{ throw 'timeout not diagnosed' }; 'TIMEOUT_OK'"
        )
        self.assertIn("TIMEOUT_OK", output)

    def test_config_roundtrip_uses_powershell_own_security_module(self):
        output = self.run_control(
            "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1'); "
            "$ConfigPath=Join-Path $LogDirectory 'test-config.json'; "
            "$key=ConvertTo-SecureString 'test-only-placeholder' -AsPlainText -Force; "
            "@{BaseUrl='http://127.0.0.1:1'; ModelName='test'; ProtectedApiKey=(ConvertFrom-SecureString $key)} "
            "| ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8; "
            "Remove-Module Microsoft.PowerShell.Security; "
            "$env:PSModulePath='C:\\missing-modules'; "
            "$env:AGENT_MODEL_API_KEY=''; $env:AGENT_MODEL_BASE_URL=''; $env:AGENT_MODEL_NAME=''; "
            "Import-ModelConfig; if ($env:AGENT_MODEL_API_KEY -ne 'test-only-placeholder') "
            "{ throw 'config import failed' }; 'CONFIG_ROUNDTRIP_OK'"
        )
        self.assertIn("CONFIG_ROUNDTRIP_OK", output)

    def browser_body(self, expected):
        # 仅模拟服务就绪与浏览器打开，不触碰数据库、配置和真实服务。
        return (
            "function Resolve-Python311 { 'unused-python' }; "
            "function Test-Dependency {}; function Import-ModelConfig {}; "
            "function Read-State { $null }; function Save-State {}; "
            "$script:checks=0; $script:opened=@(); "
            "function Test-BackendReady { $script:checks++; $true }; "
            "function Test-AgentReady { $script:checks++; $true }; "
            "function Start-Process { param($FilePath); "
            "if ($script:checks -ne 2) { throw 'browser opened before readiness' }; "
            "$script:opened+= $FilePath }; Start-Services; "
            f"if ($script:opened.Count -ne {expected}) {{ throw 'wrong browser count' }}; "
            + ("if ($script:opened[0] -ne $AgentUrl -or $script:opened[1] -ne $BackendUrl) "
               "{ throw 'wrong browser urls' }; " if expected else "")
            + "'BROWSER_CONTRACT_OK'"
        )

    def test_both_pages_open_after_both_services_are_ready(self):
        self.assertIn("BROWSER_CONTRACT_OK", self.run_control(self.browser_body(2)))

    def test_no_browser_suppresses_both_pages(self):
        self.assertIn("BROWSER_CONTRACT_OK", self.run_control(self.browser_body(0), no_browser=True))


class AgentStartupLogTest(unittest.TestCase):
    def test_warmup_failure_still_has_flushed_progress_without_secrets(self):
        from agent_server import build_runtime_from_env

        index = Mock()
        index.warmup.side_effect = RuntimeError("controlled warmup failure")
        with patch("agent_server.OpenAICompatibleModel.from_env"), \
                patch("agent_server.build_default_index", return_value=(index, {})), \
                patch.dict(os.environ, {"AGENT_RAG_WARMUP": "1", "AGENT_MODEL_API_KEY": "secret-marker"}), \
                patch("builtins.print") as printed:
            with self.assertRaisesRegex(RuntimeError, "controlled warmup failure"):
                build_runtime_from_env()
        self.assertEqual(len(printed.call_args_list), 3)
        self.assertTrue(all(call.kwargs.get("flush") for call in printed.call_args_list))
        self.assertIn("warming up", printed.call_args.args[0])
        self.assertNotIn("secret-marker", str(printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
