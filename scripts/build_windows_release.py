"""Build a self-contained Windows Golden Finger plugin release.

The script writes only under the repository's build/ and dist/ directories.
It never installs the plugin, edits a marketplace, or creates shortcuts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.0"
STAGE = ROOT / "build" / "golden-finger-release-stage"
PYINSTALLER_WORK = ROOT / "build" / "pyinstaller"
DIST_ROOT = ROOT / "dist"
PACKAGE_ROOT = DIST_ROOT / f"golden-finger-{VERSION}"
BIN_DIR = PACKAGE_ROOT / "bin"
RUNTIME_FILES = (
    ROOT / "scripts" / "relay_protocol.py",
    ROOT / "scripts" / "codex_lifecycle.py",
    ROOT / "scripts" / "relay_mcp_server.py",
    ROOT / "scripts" / "relay_broker.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_build_id() -> str:
    digest = hashlib.sha256()
    for path in RUNTIME_FILES:
        digest.update(path.name.encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "rel-" + digest.hexdigest()[:24]


def checked_directory(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(parent.resolve())
    return resolved


def reset_directory(path: Path, parent: Path) -> None:
    checked = checked_directory(path, parent)
    if checked.exists():
        shutil.rmtree(checked)
    checked.mkdir(parents=True, exist_ok=True)


def prepare_stage(build_id: str) -> Path:
    reset_directory(STAGE, ROOT / "build")
    shutil.copytree(ROOT / "scripts", STAGE / "scripts", dirs_exist_ok=True)
    protocol_path = STAGE / "scripts" / "relay_protocol.py"
    source = protocol_path.read_text(encoding="utf-8")
    updated, replacements = re.subn(
        r'^PACKAGE_BUILD_ID = "source"$',
        f'PACKAGE_BUILD_ID = "{build_id}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise RuntimeError("Could not stamp the staged runtime build ID.")
    protocol_path.write_text(updated, encoding="utf-8", newline="\n")
    return STAGE


def run_pyinstaller(script: Path, name: str, *, windowed: bool) -> Path:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        name,
        "--distpath",
        str(BIN_DIR),
        "--workpath",
        str(PYINSTALLER_WORK / name),
        "--specpath",
        str(PYINSTALLER_WORK / "spec"),
        "--paths",
        str(script.parent),
        "--windowed" if windowed else "--console",
        str(script),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    executable = BIN_DIR / f"{name}.exe"
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce {executable}.")
    return executable


def release_mcp_config() -> dict[str, object]:
    return {
        "mcpServers": {
            "golden_finger": {
                "title": "Codex金手指",
                "description": (
                    "Golden Finger for Codex: connect Codex to the separately launched "
                    "human-authorization relay window for the Yes工程师 workflow."
                ),
                "command": "./bin/golden-finger-mcp.exe",
                "args": [],
                "cwd": ".",
                "env_vars": ["LOCALAPPDATA", "PATH", "USERPROFILE"],
                "startup_timeout_sec": 10,
                "tool_timeout_sec": 65,
                "default_tools_approval_mode": "auto",
            }
        }
    }


def copy_plugin_payload(stage: Path, build_id: str) -> None:
    reset_directory(PACKAGE_ROOT, DIST_ROOT)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for directory in (".codex-plugin", "skills"):
        shutil.copytree(ROOT / directory, PACKAGE_ROOT / directory)
    shutil.copytree(stage / "scripts", PACKAGE_ROOT / "scripts")
    for name in ("README.md", "EXTERNAL-TESTING.md"):
        shutil.copy2(ROOT / name, PACKAGE_ROOT / name)
    (PACKAGE_ROOT / ".mcp.json").write_text(
        json.dumps(release_mcp_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (PACKAGE_ROOT / "BUILD-ID.txt").write_text(
        build_id + "\n",
        encoding="ascii",
        newline="\n",
    )


def write_manifest(build_id: str) -> Path:
    files: dict[str, dict[str, object]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*")):
        if path.is_file() and path.name != "build-manifest.json":
            relative = path.relative_to(PACKAGE_ROOT).as_posix()
            files[relative] = {
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    manifest = {
        "name": "golden-finger",
        "version": VERSION,
        "build_id": build_id,
        "platform": "windows",
        "architecture": "x64",
        "files": files,
    }
    destination = PACKAGE_ROOT / "build-manifest.json"
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination


def make_zip() -> Path:
    archive_base = DIST_ROOT / f"golden-finger-{VERSION}-windows-x64"
    archive = Path(
        shutil.make_archive(
            str(archive_base),
            "zip",
            root_dir=PACKAGE_ROOT.parent,
            base_dir=PACKAGE_ROOT.name,
        )
    )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare a stamped package tree without invoking PyInstaller.",
    )
    args = parser.parse_args()
    build_id = source_build_id()
    stage = prepare_stage(build_id)
    copy_plugin_payload(stage, build_id)
    if not args.prepare_only:
        run_pyinstaller(stage / "scripts" / "relay_broker.py", "GoldenFinger", windowed=True)
        run_pyinstaller(
            stage / "scripts" / "relay_mcp_server.py",
            "golden-finger-mcp",
            windowed=False,
        )
    write_manifest(build_id)
    archive = make_zip()
    print(
        json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "build_id": build_id,
                "package": str(PACKAGE_ROOT),
                "archive": str(archive),
                "executables_built": not args.prepare_only,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
