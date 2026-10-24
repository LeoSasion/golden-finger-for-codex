"""Focused recovery and startup tests; optional real release smoke stays isolated."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release as installer
from release_metadata import VERSION, sha256_file
from verify_windows_release import verify_package
from test_release_workflow import fixture_package


class RecoveryEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="golden-finger-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "安装 中文"
        self.package = fixture_package(self.base / "trusted")
        guard = patch("install_release.ensure_not_running")
        guard.start()
        self.addCleanup(guard.stop)
        self.current = installer.install(self.package, self.root)

    def test_recovery_tools_and_versions_survive_invalid_active_pointer(self):
        (self.root / "active.json").write_bytes(b"{broken")
        verify_package(installer.recovery_package(self.root))
        listing = installer.list_versions(self.root)
        self.assertEqual(listing["versions"][0]["id"], self.current["current"])
        self.assertTrue(listing["versions"][0]["valid"])
        restored = installer.recover(self.root, self.current["current"])
        self.assertEqual(restored["current"], self.current["current"])
        self.assertIsNone(restored["previous"])
        self.assertTrue(list((self.root / "quarantine").glob("active-*.json")))
        self.assertNotIn("active.json", (self.root / "dispatch.ps1").read_text(encoding="utf-8-sig"))

    def test_repair_restores_corrupt_version_without_replaying_or_switching(self):
        before = (self.root / "active.json").read_bytes()
        executable = Path(self.current["broker_executable"])
        executable.unlink()
        with self.assertRaises(ValueError):
            installer.status(self.root)
        result = installer.repair(self.package, self.root)
        self.assertFalse(result["active_pointer_changed"])
        self.assertEqual(before, (self.root / "active.json").read_bytes())
        self.assertTrue(executable.is_file())
        self.assertTrue(list((self.root / "quarantine").iterdir()))
        installer.status(self.root)

    def test_failed_repair_copy_preserves_existing_damaged_version_and_pointer(self):
        before = (self.root / "active.json").read_bytes()
        executable = Path(self.current["broker_executable"])
        executable.write_bytes(b"damaged evidence")
        with patch("install_release.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                installer.repair(self.package, self.root)
        self.assertEqual(executable.read_bytes(), b"damaged evidence")
        self.assertEqual(before, (self.root / "active.json").read_bytes())

    def test_explicit_external_recovery_tool_repair(self):
        recovery = installer.recovery_package(self.root)
        (recovery / "bin" / "GoldenFingerSetup.exe").unlink()
        result = installer.repair(self.package, self.root, recovery_tools=True)
        self.assertEqual(result["repaired"], "recovery-tools")
        installer.verify_recovery(self.root)

    def test_recover_rejects_invalid_target_without_touching_broken_pointer(self):
        active = self.root / "active.json"
        active.write_bytes(b"{broken")
        with self.assertRaises((ValueError, OSError)):
            installer.recover(self.root, "not-an-installed-version")
        self.assertEqual(active.read_bytes(), b"{broken")
        with self.assertRaises(ValueError):
            installer.recover(self.root, "../outside")

    def test_known_030_launcher_migrates_but_custom_launcher_is_preserved(self):
        dispatcher = self.root / "dispatch.ps1"
        dispatcher.write_text(installer.LEGACY_DISPATCH_SCRIPT, encoding="utf-8-sig")
        installer.install(self.package, self.root)
        self.assertEqual(dispatcher.read_text(encoding="utf-8-sig"), installer.DISPATCH_SCRIPT)
        dispatcher.write_text("user custom launcher", encoding="utf-8-sig")
        with self.assertRaisesRegex(ValueError, "modified"):
            installer.install(self.package, self.root)
        self.assertEqual(dispatcher.read_text(encoding="utf-8-sig"), "user custom launcher")

    def test_source_cli_can_list_and_recover_without_valid_active_json(self):
        (self.root / "active.json").write_bytes(b"not-json")
        script = Path(installer.__file__).resolve()
        listed = subprocess.run([sys.executable, "-B", str(script), "versions", "--root", str(self.root)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertTrue(json.loads(listed.stdout)["versions"][0]["valid"])
        recovered = subprocess.run([sys.executable, "-B", str(script), "recover", "--version", self.current["current"], "--root", str(self.root)], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell launcher")
    def test_source_dispatcher_checks_hashes_in_real_powershell(self):
        # Exercise the actual generated script through Python's inherited
        # environment, where Get-FileHash module auto-loading can be unavailable.
        # Stop before the fixture EXE: neither mismatch may reach native launch.
        package = installer.recovery_package(self.root)
        manifest = package / "build-manifest.json"
        original = manifest.read_bytes()
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(self.root / "dispatch.ps1"), "-Mode", "manage", "status"]
        for corrupted_file, expected in (
            (manifest, "Recovery integrity record mismatch."),
            (package / "assets" / "readme-hero.png", "Recovery file checksum mismatch."),
        ):
            with self.subTest(expected=expected):
                saved = corrupted_file.read_bytes()
                corrupted_file.write_bytes(bytes([saved[0] ^ 1]) + saved[1:])
                try:
                    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn(expected, result.stderr)
                    self.assertNotIn("Get-FileHash", result.stderr)
                finally:
                    corrupted_file.write_bytes(saved)
        self.assertEqual(manifest.read_bytes(), original)

    def test_same_build_already_running_does_not_spawn(self):
        ready = {"pid": 4321, "version": VERSION, "build_id": self.current["build_id"], "protocol": 7}
        with patch("install_release.probe_broker", return_value=ready), patch("install_release.subprocess.Popen") as spawn, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(installer.launch_active(self.root, connector=False), 0)
        spawn.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["state"], "already_running")

    def test_early_exit_is_failure_and_stderr_text_is_not_logged(self):
        process = Mock(pid=8888, stderr=io.BytesIO(b"token=secret-value\n"))
        process.poll.return_value = 4
        with patch("install_release.probe_broker", return_value=None), patch("install_release.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "exit code 4"):
                installer.launch_active(self.root, connector=False)
        log = (self.root / "logs" / "last-startup.json").read_text()
        self.assertNotIn("secret-value", log)
        self.assertNotIn("token=", log)
        self.assertEqual(json.loads(log)["reason_code"], "broker_exited_before_ready")
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_deadline_does_not_claim_success_or_kill_process(self):
        process = Mock(pid=8888, stderr=None)
        process.poll.return_value = None
        with patch("install_release.probe_broker", return_value=None), patch("install_release.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "handshake"):
                installer.launch_active(self.root, connector=False, deadline_seconds=0.1)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_health_probe_authenticates_loopback_and_rejects_other_build(self):
        token = "fixture-token-" + "a" * 32
        expected = {"version": VERSION, "build_id": self.current["build_id"]}
        seen = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                seen.append(request)
                result = {"ok": True, "pid": 7777, "protocol": 7, "broker_version": VERSION, "broker_build_id": expected["build_id"]}
                self.wfile.write((json.dumps(result) + "\n").encode("ascii"))
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = self.base / "local" / "GoldenFinger" / "endpoint.json"
        endpoint.parent.mkdir(parents=True)
        endpoint.write_text(json.dumps({"host": "127.0.0.1", "port": server.server_address[1], "pid": 7777, "protocol": 7, "broker_version": VERSION, "broker_build_id": expected["build_id"], "token": token}), encoding="ascii")
        try:
            with patch.dict(os.environ, {"LOCALAPPDATA": str(endpoint.parent.parent)}):
                self.assertEqual(installer.probe_broker(expected, 7)["pid"], 7777)
                with self.assertRaisesRegex(ValueError, "different"):
                    installer.probe_broker({"version": VERSION, "build_id": "rel-" + "f" * 24}, 7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(seen[0]["token"], token)
        self.assertEqual(seen[0]["operation"], "health")
        self.assertEqual(seen[0]["client_role"], "launcher")


@unittest.skipUnless(os.environ.get("GOLDEN_FINGER_RELEASE_PACKAGE"), "Set GOLDEN_FINGER_RELEASE_PACKAGE for isolated packaged-CLI smoke.")
class PackagedRecoverySmokeTests(unittest.TestCase):
    def test_packaged_recovery_survives_active_setup_and_pointer_damage(self):
        source = Path(os.environ["GOLDEN_FINGER_RELEASE_PACKAGE"]).resolve()
        verify_package(source)
        setup = source / "bin" / "GoldenFingerSetup.exe"
        with tempfile.TemporaryDirectory(prefix="golden-finger-cli-smoke-") as temporary:
            root = Path(temporary) / "安装恢复"
            def run(executable, *arguments):
                result = subprocess.run([str(executable), *arguments, "--root", str(root)], capture_output=True, text=True, encoding="utf-8", timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            current = run(setup, "install", str(source))
            installed = Path(current["plugin_directory"])
            (installed / "bin" / "GoldenFingerSetup.exe").unlink()
            (root / "active.json").write_bytes(b"{damaged")
            recovery = root / "tools" / "recovery" / "bin" / "GoldenFingerSetup.exe"
            listing = run(recovery, "versions")
            self.assertFalse(listing["versions"][0]["valid"])
            run(recovery, "repair", "--from", str(source))
            restored = run(recovery, "recover", "--version", current["current"])
            self.assertEqual(restored["current"], current["current"])
            dispatcher = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(root / "dispatch.ps1"), "-Mode", "manage", "status"], capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(dispatcher.returncode, 0, dispatcher.stderr)
            self.assertEqual(json.loads(dispatcher.stdout)["current"], current["current"])


if __name__ == "__main__":
    unittest.main()
