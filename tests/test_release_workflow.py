"""Package and upgrade fixtures. No executable is launched or real plugin installed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_windows_release as builder
import install_release as installer
from release_metadata import MANIFEST_SCHEMA_VERSION, VERSION, sha256_file, source_build_id
from verify_windows_release import verify_package


def fixture_package(directory: Path, build: str = "a" * 24) -> Path:
    directory.mkdir(parents=True)
    contents = {
        "README.md": b"![Golden Finger](assets/readme-hero.png)\n\n# Golden Finger\n",
        "assets/readme-hero.png": b"user-provided-image-fixture",
        "BUILD-ID.txt": f"rel-{build}\n".encode(),
        ".codex-plugin/plugin.json": json.dumps({"name": "golden-finger", "version": VERSION}).encode(),
        ".mcp.json": json.dumps(builder.release_mcp_config()).encode(),
        "bin/GoldenFinger.exe": b"fixture-not-an-executable",
        "bin/golden-finger-mcp.exe": b"fixture-not-an-executable",
        "bin/GoldenFingerSetup.exe": b"fixture-not-an-executable",
        "bin/golden-finger.runtime/python.dll": b"fixture-runtime",
        "requirements-build.txt": b"fixture",
        "skills/golden-finger/SKILL.md": b"fixture",
        "docs/LOCAL-GUIDE.md": b"fixture guide",
    }
    for name in ("launch_relay.pyw", "relay_protocol.py", "relay_broker.py", "relay_mcp_server.py", "release_metadata.py", "install_release.py", "verify_windows_release.py"):
        contents["scripts/" + name] = f"# fixture source {build}\n".encode()
    contents["scripts/relay_protocol.py"] += b"PROTOCOL_VERSION = 7\n"
    for name, data in contents.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    build_id = source_build_id(directory / "scripts", prefix="rel-")
    contents["BUILD-ID.txt"] = (build_id + "\n").encode()
    (directory / "BUILD-ID.txt").write_bytes(contents["BUILD-ID.txt"])
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION, "name": "golden-finger", "version": VERSION,
        "build_id": build_id, "platform": "windows", "architecture": "x64",
        "layout": "pyinstaller-shared-onedir", "provenance": {},
        "files": {name: {"size": len(data), "sha256": sha256_file(directory / name)} for name, data in contents.items()},
    }
    (directory / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_config_preserves_source_transport_budgets_and_environment(self) -> None:
        source = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        release = builder.release_mcp_config()
        original = source["mcpServers"]["golden_finger"]
        packaged = release["mcpServers"]["golden_finger"]
        self.assertEqual(packaged["tool_timeout_sec"], 930)
        for key in original.keys() - {"command", "args", "cwd"}:
            self.assertEqual(packaged[key], original[key], key)
        self.assertEqual(packaged["command"], "./bin/golden-finger-mcp.exe")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="golden-finger-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "stable installation 中文"
        self.process_check = patch("install_release.ensure_not_running")
        self.process_check.start()
        self.addCleanup(self.process_check.stop)

    def test_integrity_rejects_corruption_and_unlisted_payload(self) -> None:
        package = fixture_package(self.base / "package")
        verify_package(package)
        (package / "bin" / "surprise.dll").write_bytes(b"unlisted")
        with self.assertRaisesRegex(ValueError, "inventory"):
            verify_package(package)
        (package / "bin" / "surprise.dll").unlink()
        (package / "bin" / "GoldenFinger.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            verify_package(package)

    def test_readme_hero_is_required_even_if_manifest_is_rewritten(self) -> None:
        package = fixture_package(self.base / "package")
        readme = package / "README.md"
        readme.write_text("# Golden Finger\n\n![hero](assets/missing.png)\n", encoding="utf-8")
        path = package / "build-manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["README.md"] = {"size": readme.stat().st_size, "sha256": sha256_file(readme)}
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "readme-hero"):
            verify_package(package)

    def test_build_copies_original_hero_bytes_and_version_pins(self) -> None:
        stage = self.base / "stage"
        (stage / "scripts").mkdir(parents=True)
        package = self.base / "dist" / "package"
        with patch.object(builder, "PACKAGE_ROOT", package), patch.object(builder, "BIN_DIR", package / "bin"), patch.object(builder, "DIST_ROOT", package.parent):
            builder.copy_plugin_payload(stage, "rel-" + "a" * 24)
        self.assertEqual(sha256_file(ROOT / "assets" / "readme-hero.png"), sha256_file(package / "assets" / "readme-hero.png"))
        self.assertEqual((ROOT / "README.md").read_bytes(), (package / "README.md").read_bytes())
        self.assertTrue((package / "requirements-build.txt").is_file())
        self.assertEqual((ROOT / "docs" / "LOCAL-GUIDE.md").read_bytes(), (package / "docs" / "LOCAL-GUIDE.md").read_bytes())

    def test_upgrade_rollback_keeps_both_verified_versions_and_stable_launchers(self) -> None:
        first = fixture_package(self.base / "first")
        second = fixture_package(self.base / "second", "b" * 24)
        old = installer.install(first, self.root)
        new = installer.install(second, self.root)
        self.assertNotEqual(old["current"], new["current"])
        self.assertEqual(new["previous"], old["current"])
        self.assertTrue(Path(old["broker_executable"]).is_file())
        rolled = installer.rollback(self.root)
        self.assertEqual(rolled["current"], old["current"])
        self.assertEqual(rolled["previous"], new["current"])
        launcher = Path(rolled["stable_broker_launcher"])
        self.assertTrue(launcher.read_bytes().startswith(b"@echo off"))
        self.assertIn("tools\\recovery", (self.root / "dispatch.ps1").read_text(encoding="utf-8-sig"))
        self.assertNotIn("active.json", (self.root / "dispatch.ps1").read_text(encoding="utf-8-sig"))

    def test_corrupt_upgrade_does_not_change_active_pointer(self) -> None:
        first = fixture_package(self.base / "first")
        installer.install(first, self.root)
        before = (self.root / "active.json").read_bytes()
        bad = fixture_package(self.base / "bad", "b" * 24)
        (bad / "assets" / "readme-hero.png").unlink()
        with self.assertRaises(ValueError):
            installer.install(bad, self.root)
        self.assertEqual(before, (self.root / "active.json").read_bytes())

    def test_copy_failure_preserves_active_and_removes_staging(self) -> None:
        installer.install(fixture_package(self.base / "first"), self.root)
        second = fixture_package(self.base / "second", "b" * 24)
        before = (self.root / "active.json").read_bytes()
        with patch("install_release.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                installer.install(second, self.root)
        self.assertEqual(before, (self.root / "active.json").read_bytes())
        self.assertFalse(list((self.root / "versions").glob(".staging-*")))

    def test_running_installation_blocks_upgrade_and_rollback(self) -> None:
        installer.install(fixture_package(self.base / "first"), self.root)
        before = (self.root / "active.json").read_bytes()
        with patch("install_release.ensure_not_running", side_effect=ValueError("running")):
            with self.assertRaisesRegex(ValueError, "running"):
                installer.install(fixture_package(self.base / "second", "b" * 24), self.root)
            with self.assertRaisesRegex(ValueError, "running"):
                installer.rollback(self.root)
        self.assertEqual(before, (self.root / "active.json").read_bytes())

    def test_export_is_complete_and_never_overwrites_destination(self) -> None:
        current = installer.install(fixture_package(self.base / "first"), self.root)
        target = self.base / "manual import"
        installer.export_active(self.root, target)
        self.assertEqual(verify_package(target), verify_package(Path(current["plugin_directory"])))
        with self.assertRaisesRegex(ValueError, "exists"):
            installer.export_active(self.root, target)

    def test_zip_checksum_and_traversal_are_rejected_before_install(self) -> None:
        package = fixture_package(self.base / "source" / "package")
        archive = Path(shutil.make_archive(str(self.base / "good"), "zip", root_dir=package.parent, base_dir=package.name))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            installer.install(archive, self.root, expected_sha256="0" * 64)
        self.assertFalse(self.root.exists())
        installer.install(archive, self.root, expected_sha256=sha256_file(archive))
        malicious = self.base / "traversal.zip"
        with zipfile.ZipFile(malicious, "w") as bundle:
            bundle.writestr("../escaped.txt", b"do not extract")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            installer.install(malicious, self.base / "other")
        self.assertFalse((self.base / "escaped.txt").exists())

    def test_root_and_pointer_cannot_target_unmanaged_files(self) -> None:
        package = fixture_package(self.base / "package")
        self.root.mkdir()
        keep = self.root / "user-file.txt"
        keep.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            installer.install(package, self.root)
        self.assertEqual(keep.read_text(), "preserve")
        with self.assertRaisesRegex(ValueError, "inside"):
            installer.install(package, package / "install")
        with self.assertRaisesRegex(ValueError, "reference"):
            installer.safe_version_path(self.root, "../../outside")

    def test_launch_and_connector_resolve_same_active_package_without_stdout_wrapping(self) -> None:
        current = installer.install(fixture_package(self.base / "package"), self.root)
        with patch("install_release.subprocess.Popen") as broker, patch("install_release.subprocess.call", return_value=7) as connector, patch("install_release.probe_broker") as probe:
            broker.return_value.pid = 1234
            broker.return_value.poll.return_value = None
            broker.return_value.stderr = None
            probe.side_effect = [None, {"pid": 1234, "version": VERSION, "build_id": current["build_id"], "protocol": 7}]
            self.assertEqual(installer.launch_active(self.root, connector=False), 0)
            self.assertEqual(installer.launch_active(self.root, connector=True), 7)
        self.assertEqual(broker.call_args.args[0], [current["broker_executable"]])
        self.assertEqual(connector.call_args.args[0], [str(Path(current["plugin_directory"]) / "bin" / "golden-finger-mcp.exe")])
        self.assertNotIn("stdout", connector.call_args.kwargs)

    def test_source_identity_covers_new_modules_and_dependency_changes(self) -> None:
        scripts = self.base / "source" / "scripts"
        scripts.mkdir(parents=True)
        module = scripts / "relay_protocol.py"
        module.write_bytes(b"x = 1\r\n")
        original = source_build_id(scripts)
        module.write_bytes(b"x = 1\n")
        self.assertEqual(original, source_build_id(scripts))
        (scripts / "codex_lifecycle.py").write_bytes(b"x = 1\n")
        added = source_build_id(scripts)
        self.assertNotEqual(original, added)
        (scripts.parent / "requirements-build.txt").write_text("dependency==1")
        self.assertNotEqual(added, source_build_id(scripts))

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher syntax is Windows-only")
    def test_stable_dispatcher_parses_in_windows_powershell(self) -> None:
        command = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors); if ($errors.Count) { $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }"
        checked = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            input=installer.DISPATCH_SCRIPT, text=True, encoding="utf-8",
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
