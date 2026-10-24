"""Verify a built Golden Finger Windows release without installing it."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
from release_metadata import MANIFEST_SCHEMA_VERSION, sha256_file, source_build_id, read_protocol_version


def checked_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"Invalid package path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(
        part in {".", ".."} or part.endswith((".", " "))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        for part in path.parts
    ):
        raise ValueError(f"Unsafe package path: {value!r}")
    return path


def is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def package_files(package: Path) -> set[str]:
    if is_link(package):
        raise ValueError("Package root must not be a symbolic link or junction.")
    found: set[str] = set()
    folded: set[str] = set()
    for parent, directories, names in os.walk(package, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            relative = path.relative_to(package).as_posix()
            checked_relative_path(relative)
            if is_link(path):
                raise ValueError(f"Links are not allowed in a release: {relative}")
            if relative.casefold() in folded:
                raise ValueError(f"Case-colliding release path: {relative}")
            folded.add(relative.casefold())
            if path.is_file():
                found.add(relative)
    return found


def validate_readme_assets(package: Path) -> None:
    readme = (package / "README.md").read_text(encoding="utf-8")
    images = list(re.finditer(r"!\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", readme))
    hero = next((match for match in images if match.group(1) == "assets/readme-hero.png"), None)
    title = re.search(r"^#\s+", readme, re.MULTILINE)
    if hero is None or (title is not None and hero.start() > title.start()):
        raise ValueError("README must retain assets/readme-hero.png before its project title.")
    for match in images:
        target = urlsplit(match.group(1))
        if target.scheme or target.netloc:
            continue
        relative = checked_relative_path(unquote(target.path))
        if not (package / relative).is_file():
            raise ValueError(f"README image is missing from the release: {relative}")


def verify_package(package: Path) -> dict[str, object]:
    """Verify complete inventory and metadata without executing package contents.

    Manifest hashes detect corruption, not an untrusted publisher. Obtain releases
    from the project's trusted release page; optionally compare its ZIP checksum.
    """
    actual_files = package_files(package)
    manifest_path = package / "build-manifest.json"
    if manifest_path.stat().st_size > 16_777_216:
        raise ValueError("Release manifest is too large.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported release manifest; use a complete current release package.")
    if manifest.get("name") != "golden-finger" or manifest.get("platform") != "windows" or manifest.get("architecture") != "x64":
        raise ValueError("This is not a Golden Finger Windows x64 package.")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[+-][A-Za-z0-9.-]+)?", str(manifest.get("version", ""))):
        raise ValueError("Invalid package version.")
    if not re.fullmatch(r"rel-[a-f0-9]{24}", str(manifest.get("build_id", ""))):
        raise ValueError("Invalid release build ID.")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Release inventory is missing.")
    for relative, expected in files.items():
        path = package / checked_relative_path(relative)
        if not isinstance(expected, dict) or not path.is_file():
            raise ValueError(f"Missing release file: {relative}")
        if path.stat().st_size != expected.get("size") or sha256_file(path) != expected.get("sha256"):
            raise ValueError(f"Release size/SHA-256 mismatch: {relative}")
    if actual_files != set(files) | {"build-manifest.json"}:
        raise ValueError("Release inventory differs from manifest (missing or unexpected files).")
    required_sources = {
        "requirements-build.txt", "skills/golden-finger/SKILL.md", "docs/LOCAL-GUIDE.md",
        "scripts/launch_relay.pyw", "scripts/relay_protocol.py",
        "scripts/relay_broker.py", "scripts/relay_mcp_server.py",
        "scripts/release_metadata.py", "scripts/install_release.py",
        "scripts/verify_windows_release.py",
    }
    if not required_sources.issubset(files):
        raise ValueError("The complete plugin Skill and source scripts must accompany the binaries.")
    if source_build_id(package / "scripts", prefix="rel-") != manifest["build_id"]:
        raise ValueError("Packaged source fingerprint differs from the declared release build ID.")
    if "protocol" in manifest and manifest["protocol"] != read_protocol_version(package / "scripts"):
        raise ValueError("Package protocol differs from the manifest.")
    plugin = json.loads((package / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    if plugin.get("name") != "golden-finger" or plugin.get("version") != manifest["version"]:
        raise ValueError("Plugin version differs from release manifest.")
    if (package / "BUILD-ID.txt").read_text(encoding="ascii").strip() != manifest["build_id"]:
        raise ValueError("BUILD-ID.txt differs from release manifest.")
    config = json.loads((package / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["golden_finger"]
    if server.get("command") != "./bin/golden-finger-mcp.exe" or server.get("args") != [] or server.get("cwd") != ".":
        raise ValueError("Release MCP launch configuration is invalid.")
    timeout = server.get("tool_timeout_sec")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 930:
        raise ValueError("Release MCP tool timeout must cover a 900-second poll plus transport margin (930 seconds).")
    if manifest.get("layout") != "pyinstaller-shared-onedir":
        raise ValueError("Release must use the shared onedir layout.")
    for name in ("GoldenFinger.exe", "golden-finger-mcp.exe", "GoldenFingerSetup.exe"):
        if f"bin/{name}" not in files:
            raise ValueError(f"Self-contained executable is missing: {name}")
    if not any(path.startswith("bin/golden-finger.runtime/") for path in files):
        raise ValueError("Shared onedir runtime is empty.")
    validate_readme_assets(package)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--integrity-only", action="store_true", help="Do not execute EXEs.")
    parser.add_argument("--expected-commit")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    package = args.package.absolute()
    manifest = verify_package(package)
    provenance = manifest.get("provenance", {})
    if args.expected_commit and provenance.get("git_commit") != args.expected_commit:
        raise ValueError("Release commit differs from the checked-out source.")
    if args.require_clean and provenance.get("git_dirty") is not False:
        raise ValueError("Official release requires a clean source checkout.")
    if args.integrity_only:
        print(json.dumps({"ok": True, "version": manifest["version"], "build_id": manifest["build_id"], "integrity_only": True}))
        return 0

    config = json.loads((package / ".mcp.json").read_text(encoding="utf-8"))
    executable = package / config["mcpServers"]["golden_finger"]["command"]
    broker = package / "bin" / "GoldenFinger.exe"
    if not executable.is_file() or not broker.is_file():
        raise RuntimeError("Self-contained executables are missing.")
    if manifest.get("layout") != "pyinstaller-shared-onedir":
        raise RuntimeError("Release manifest does not declare the shared PyInstaller onedir layout.")
    runtime_directory = package / "bin" / "golden-finger.runtime"
    if not runtime_directory.is_dir() or not any(
        path.is_file() for path in runtime_directory.rglob("*")
    ):
        raise RuntimeError(f"Shared onedir runtime is missing or empty: {runtime_directory.name}")

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }
    completed = subprocess.run(
        [str(executable)],
        input=json.dumps(initialize, ensure_ascii=True) + "\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=True,
    )
    response = json.loads(completed.stdout.splitlines()[0])
    server_info = response["result"]["serverInfo"]
    if server_info["version"] != manifest["version"]:
        raise RuntimeError("MCP executable version does not match the release manifest.")
    with tempfile.TemporaryDirectory(prefix="golden-finger-verify-") as temp_root:
        mcp_test_path = Path(temp_root) / "mcp-self-test.json"
        subprocess.run(
            [str(executable), "--self-test-output", str(mcp_test_path)],
            capture_output=True, timeout=15, check=True,
        )
        mcp_probe = json.loads(mcp_test_path.read_text(encoding="utf-8"))
        if mcp_probe.get("mcp_version") != manifest["version"] or mcp_probe.get("mcp_build_id") != manifest["build_id"]:
            raise RuntimeError("MCP executable version/build ID differs from the release manifest.")
        if mcp_probe.get("runtime_mode") != "self-contained-exe":
            raise RuntimeError("MCP executable did not report self-contained runtime mode.")
        self_test_path = Path(temp_root) / "broker-self-test.json"
        subprocess.run(
            [str(broker), "--self-test-output", str(self_test_path)],
            capture_output=True,
            timeout=15,
            check=True,
        )
        broker_probe = json.loads(self_test_path.read_text(encoding="utf-8"))
    if broker_probe.get("broker_version") != manifest["version"]:
        raise RuntimeError("Broker executable version does not match the release manifest.")
    if broker_probe.get("broker_build_id") != manifest["build_id"]:
        raise RuntimeError("Broker executable build ID does not match the release manifest.")
    if broker_probe.get("runtime_mode") != "self-contained-exe":
        raise RuntimeError("Broker executable did not report self-contained runtime mode.")
    if broker_probe.get("job_object_active") is not True:
        raise RuntimeError("Broker executable could not create its Windows Job Object.")
    print(
        json.dumps(
            {
                "ok": True,
                "version": manifest["version"],
                "build_id": manifest["build_id"],
                "layout": manifest["layout"],
                "mcp_server": server_info,
                "mcp_self_test": mcp_probe,
                "broker_self_test": broker_probe,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
