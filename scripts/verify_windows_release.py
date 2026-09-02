"""Verify a built Golden Finger Windows release without installing it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    package = args.package.resolve(strict=True)
    manifest_path = package / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        path = package / relative
        if not path.is_file():
            raise RuntimeError(f"Missing release file: {relative}")
        if path.stat().st_size != expected["size"]:
            raise RuntimeError(f"Size mismatch: {relative}")
        if sha256_file(path) != expected["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch: {relative}")

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
                "broker_self_test": broker_probe,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
