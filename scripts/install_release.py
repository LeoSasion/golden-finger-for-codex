"""Explicit local install, upgrade, rollback and export; never changes Codex data.

Run from source with Python or use bin/GoldenFingerSetup.exe in a complete release.
Only a trusted, complete release directory or ZIP is accepted. Verification checks
integrity, not publisher authenticity. Installation never executes imported files.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

sys.dont_write_bytecode = True
from release_metadata import sha256_file, read_protocol_version
from verify_windows_release import checked_relative_path, is_link, verify_package

MARKER = ".golden-finger-install.json"
ACTIVE = "active.json"
MAX_ZIP_BYTES = 1_073_741_824
MAX_ZIP_ENTRIES = 20_000

LEGACY_DISPATCH_SCRIPT = r'''param(
    [ValidateSet('launch', 'connector', 'manage')][string]$Mode,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
try {
    $installRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $state = Get-Content -LiteralPath (Join-Path $installRoot 'active.json') -Raw | ConvertFrom-Json
    if ($state.schema_version -ne 1 -or $state.current -notmatch '^[A-Za-z0-9][A-Za-z0-9.+-]{0,160}$') {
        throw 'Invalid active version pointer.'
    }
    $package = Join-Path (Join-Path $installRoot 'versions') $state.current
    $runner = Join-Path $package 'bin\GoldenFingerSetup.exe'
    foreach ($path in @($installRoot, (Join-Path $installRoot 'active.json'), (Join-Path $installRoot 'versions'), $package, (Join-Path $package 'bin'), $runner)) {
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installation links are not supported.' }
    }
    if ($Mode -eq 'manage') {
        if (-not $Arguments) { $Arguments = @('status') }
        & $runner @Arguments --root $installRoot
    } else {
        & $runner $Mode --root $installRoot
    }
    exit $LASTEXITCODE
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 2 }
'''

DISPATCH_SCRIPT = r'''param(
    [ValidateSet('launch', 'connector', 'manage')][string]$Mode,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
)
$ErrorActionPreference = 'Stop'
function Get-GoldenFingerSha256([string]$LiteralPath) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($LiteralPath)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}
try {
    $installRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $package = Join-Path $installRoot 'tools\recovery'
    $manifestPath = Join-Path $package 'build-manifest.json'
    $record = Get-Content -LiteralPath (Join-Path $installRoot 'tools\recovery.json') -Raw | ConvertFrom-Json
    if ($record.schema_version -ne 1 -or (Get-GoldenFingerSha256 $manifestPath) -ne $record.manifest_sha256) { throw 'Recovery integrity record mismatch. Use a fresh trusted release package.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    foreach ($file in $manifest.files.PSObject.Properties) {
        $relative = $file.Name
        if ($relative -match '(^/|\\|:|(^|/)\.\.(/|$))') { throw 'Unsafe recovery manifest path.' }
        $path = Join-Path $package $relative
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or $item.Length -ne $file.Value.size -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Recovery file is missing or changed.' }
        if ((Get-GoldenFingerSha256 $path) -ne $file.Value.sha256) { throw 'Recovery file checksum mismatch. Use a fresh trusted release package.' }
    }
    $runner = Join-Path $package 'bin\GoldenFingerSetup.exe'
    foreach ($path in @($installRoot, (Join-Path $installRoot 'tools'), $package, (Join-Path $package 'bin'), $runner)) {
        if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installation links are not supported.' }
    }
    if ($Mode -eq 'manage') {
        if (-not $Arguments) { $Arguments = @('status') }
        & $runner @Arguments --root $installRoot
    } else { & $runner $Mode --root $installRoot }
    exit $LASTEXITCODE
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 2 }
'''


def atomic_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    reject_link_ancestors(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding=encoding, newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_launchers(root: Path) -> None:
    # Constant script contents, not interpolated shell commands from paths or metadata.
    files = {"dispatch.ps1": DISPATCH_SCRIPT}
    for name, mode in (("GoldenFinger.cmd", "launch"), ("golden-finger-mcp.cmd", "connector"), ("GoldenFingerSetup.cmd", "manage")):
        arguments = " %*" if mode == "manage" else ""
        files[name] = '@echo off\npowershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0dispatch.ps1" -Mode ' + mode + arguments + '\nexit /b %errorlevel%\n'
    for name, contents in files.items():
        path = root / name
        reject_link_ancestors(path)
        if path.exists():
            existing = path.read_text(encoding="utf-8-sig")
            if existing == contents:
                continue
            if not (name == "dispatch.ps1" and existing == LEGACY_DISPATCH_SCRIPT):
                raise ValueError(f"Stable launcher was modified; review it before upgrading: {path}")
        atomic_text(path, contents, encoding="ascii" if name.endswith(".cmd") else "utf-8-sig")


def recovery_package(root: Path) -> Path:
    path = root / "tools" / "recovery"
    reject_link_ancestors(path)
    return path


def verify_recovery(root: Path) -> dict[str, object]:
    package = recovery_package(root)
    record_path = root / "tools" / "recovery.json"
    reject_link_ancestors(record_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("manifest_sha256") != sha256_file(package / "build-manifest.json"):
        raise ValueError("Recovery tools differ from their integrity record. Repair them from a trusted external release.")
    return verify_package(package)


def stage_package(package: Path, destination: Path, *, backup_root: Path | None = None) -> None:
    """Validate a copied tree before publishing it; retain replaced files as evidence."""
    reject_link_ancestors(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = sha256_file(package / "build-manifest.json")
    staging = destination.with_name(".staging-" + uuid.uuid4().hex)
    backup = None
    try:
        shutil.copytree(package, staging)
        verify_package(staging)
        if sha256_file(staging / "build-manifest.json") != expected:
            raise ValueError("Source package changed while copying.")
        if destination.exists():
            if backup_root is None:
                raise ValueError("Destination already exists.")
            reject_link_ancestors(backup_root)
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (destination.name + "-" + uuid.uuid4().hex)
            destination.rename(backup)
        try:
            staging.rename(destination)
        except BaseException:
            if backup is not None and not destination.exists():
                backup.rename(destination)
            raise
    finally:
        if staging.exists():
            staging.resolve().relative_to(destination.parent.resolve())
            shutil.rmtree(staging)


def ensure_recovery(root: Path, package: Path) -> None:
    target = recovery_package(root)
    record = root / "tools" / "recovery.json"
    if target.exists():
        if record.exists():
            verify_recovery(root)
            return
        # Resume an interrupted first bootstrap only from the exact trusted source.
        verify_package(target)
        if sha256_file(target / "build-manifest.json") != sha256_file(package / "build-manifest.json"):
            raise ValueError("Unrecorded recovery package differs; use explicit recovery-tool repair.")
    else:
        source_manifest = verify_package(package)
        if tuple(map(int, re.match(r"^(\d+)\.(\d+)\.(\d+)", str(source_manifest["version"])).groups())) < (0, 3, 1):
            if not getattr(sys, "frozen", False):
                raise ValueError("First bootstrap requires a 0.3.1+ recovery package; install that release before selecting an older version.")
            package = Path(sys.executable).resolve().parents[1]
            verify_package(package)
        stage_package(package, target)
    atomic_json(record, {"schema_version": 1, "manifest_sha256": sha256_file(target / "build-manifest.json")})
    atomic_text(root / "tools" / "RECOVERY.txt", "Independent recovery package: this complete, immutable copy survives damage to active versions.\nIts hashes verify integrity, not publisher authenticity. Keep the entire directory.\nIf these recovery tools are damaged, run a fresh trusted release's GoldenFingerSetup.exe repair --from <package> --recovery-tools --root <install-root>.\n")


def ensure_not_running(root: Path) -> None:
    if os.name != "nt":
        return
    script = r'''$ErrorActionPreference = 'Stop'
$prefix = [IO.Path]::GetFullPath($env:GOLDEN_FINGER_INSTALL_ROOT).TrimEnd('\') + '\'
$matches = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne [int]$env:GOLDEN_FINGER_INSTALL_PID -and $_.ExecutablePath -and
    $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
} | ForEach-Object { $_.ProcessId })
ConvertTo-Json -InputObject $matches -Compress
'''
    environment = dict(os.environ, GOLDEN_FINGER_INSTALL_ROOT=str(root), GOLDEN_FINGER_INSTALL_PID=str(os.getpid()))
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=True, env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if json.loads(completed.stdout):
        raise ValueError("Close this installation's Golden Finger window and disconnect its MCP before upgrading or rolling back. No running process was stopped.")


def default_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "GoldenFinger" / "install"


def reject_link_ancestors(path: Path) -> None:
    for component in (path, *path.parents):
        if is_link(component):
            raise ValueError(f"Install/export paths must not cross symbolic links or junctions: {component}")


def atomic_json(path: Path, value: dict[str, object]) -> None:
    reject_link_ancestors(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_root(root: Path, *, create: bool = False) -> Path:
    root = root.absolute()
    reject_link_ancestors(root)
    marker = root / MARKER
    reject_link_ancestors(marker)
    if not marker.is_file():
        if not create:
            raise ValueError(f"No managed installation at {root}")
        if root.exists() and any(root.iterdir()):
            raise ValueError("Refusing to adopt a nonempty directory without an installation marker.")
        root.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents concurrent first-run installers replacing ownership.
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"product": "golden-finger", "schema_version": 1}, stream)
    if json.loads(marker.read_text(encoding="utf-8")) != {"product": "golden-finger", "schema_version": 1}:
        raise ValueError("Invalid installation ownership marker.")
    return root


@contextmanager
def installation_lock(root: Path):
    lock_path = root / ".install.lock"
    reject_link_ancestors(lock_path)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another installation operation is running; retry after it finishes.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def safe_version_path(root: Path, identity: str) -> Path:
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,160}", identity):
        raise ValueError("Invalid installation version reference.")
    path = root / "versions" / identity
    reject_link_ancestors(path)
    return path


def read_active(root: Path) -> dict[str, object]:
    active = root / ACTIVE
    reject_link_ancestors(active)
    if not active.exists():
        return {"schema_version": 1, "current": None, "previous": None}
    data = json.loads(active.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not data.get("current"):
        raise ValueError("Invalid active version pointer.")
    for key in ("current", "previous"):
        if data.get(key) is not None:
            safe_version_path(root, data[key])
    return data


def extract_zip(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_ZIP_ENTRIES or sum(item.file_size for item in entries) > MAX_ZIP_BYTES:
            raise ValueError("Release archive exceeds extraction limits.")
        names: set[str] = set()
        for entry in entries:
            name = entry.filename.removesuffix("/")
            relative = checked_relative_path(name)
            if name.casefold() in names:
                raise ValueError(f"Duplicate archive path: {name}")
            names.add(name.casefold())
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"Archive links/special files are not supported: {name}")
            target = destination / relative
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1_048_576)
    if (destination / "build-manifest.json").is_file():
        return destination
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir() or not (children[0] / "build-manifest.json").is_file():
        raise ValueError("Archive must contain exactly one complete release directory.")
    return children[0]


@contextmanager
def package_source(source: Path, expected_sha256: str | None = None):
    source = source.absolute()
    reject_link_ancestors(source)
    if source.is_dir():
        if expected_sha256:
            raise ValueError("--sha256 applies to ZIP files, not directories.")
        yield source
        return
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ValueError("Select a complete release directory or ZIP file.")
    if expected_sha256 and sha256_file(source) != expected_sha256.lower():
        raise ValueError("Release ZIP SHA-256 does not match the expected checksum.")
    with tempfile.TemporaryDirectory(prefix="golden-finger-import-") as temporary:
        yield extract_zip(source, Path(temporary))


def install(source: Path, root: Path, *, expected_sha256: str | None = None) -> dict[str, object]:
    with package_source(source, expected_sha256) as package:
        manifest = verify_package(package)
        if root.absolute().is_relative_to(package.absolute()):
            raise ValueError("Install root must not be inside the source package.")
        manifest_hash = sha256_file(package / "build-manifest.json")
        identity = f"{manifest['version']}-{manifest['build_id']}-{manifest_hash[:12]}"
        root = check_root(root, create=True)
        with installation_lock(root):
            ensure_not_running(root)
            previous_state = read_active(root)
            destination = safe_version_path(root, identity)
            destination.parent.mkdir(exist_ok=True)
            if destination.exists():
                verify_package(destination)
                if sha256_file(destination / "build-manifest.json") != manifest_hash:
                    raise ValueError("Existing version identity contains a different manifest.")
            else:
                stage_package(package, destination)
            ensure_recovery(root, package)
            write_launchers(root)
            if previous_state.get("current") != identity:
                atomic_json(root / ACTIVE, {"schema_version": 1, "current": identity, "previous": previous_state.get("current")})
        return status(root)


def rollback(root: Path) -> dict[str, object]:
    root = check_root(root)
    with installation_lock(root):
        ensure_not_running(root)
        state = read_active(root)
        previous = state.get("previous")
        if not previous:
            raise ValueError("No previous installed version is available.")
        verify_package(safe_version_path(root, previous))
        atomic_json(root / ACTIVE, {"schema_version": 1, "current": previous, "previous": state.get("current")})
    return status(root)


def recover(root: Path, identity: str) -> dict[str, object]:
    """Explicitly select a verified retained version even when active.json is broken."""
    root = check_root(root)
    with installation_lock(root):
        ensure_not_running(root)
        selected = safe_version_path(root, identity)
        verify_package(selected)
        verify_recovery(root)
        try:
            previous = read_active(root).get("current")
            if previous:
                verify_package(safe_version_path(root, previous))
        except (OSError, ValueError, KeyError, TypeError):
            previous = None
        if previous == identity:
            previous = None
        active_path = root / ACTIVE
        reject_link_ancestors(active_path)
        if active_path.exists():
            # Preserve malformed evidence without trusting it as a path or printing it.
            backup = root / "quarantine"
            reject_link_ancestors(backup)
            backup.mkdir(exist_ok=True)
            shutil.copy2(active_path, backup / ("active-" + uuid.uuid4().hex + ".json"))
        write_launchers(root)
        atomic_json(active_path, {"schema_version": 1, "current": identity, "previous": previous})
    return status(root)


def repair(source: Path, root: Path, *, expected_sha256: str | None = None, recovery_tools: bool = False) -> dict[str, object]:
    with package_source(source, expected_sha256) as package:
        manifest = verify_package(package)
        root = check_root(root)
        if root.is_relative_to(package.absolute()):
            raise ValueError("Install root must not be inside the repair source package.")
        identity = f"{manifest['version']}-{manifest['build_id']}-{sha256_file(package / 'build-manifest.json')[:12]}"
        with installation_lock(root):
            ensure_not_running(root)
            destination = recovery_package(root) if recovery_tools else safe_version_path(root, identity)
            if package.absolute().is_relative_to(destination.absolute()):
                raise ValueError("Repair must use an independent trusted package copy.")
            if recovery_tools and getattr(sys, "frozen", False) and Path(sys.executable).resolve().is_relative_to(destination.resolve()):
                raise ValueError("Repair recovery tools using Setup from an external trusted release, not the running recovery copy.")
            if recovery_tools and tuple(map(int, re.match(r"^(\d+)\.(\d+)\.(\d+)", str(manifest["version"])).groups())) < (0, 3, 1):
                raise ValueError("Independent recovery tools require a 0.3.1 or newer package.")
            if not recovery_tools and not destination.exists():
                try:
                    state = read_active(root)
                except (OSError, ValueError, KeyError, TypeError):
                    state = None
                if state is not None and identity not in {state.get("current"), state.get("previous")}:
                    raise ValueError("Repair package does not match a retained or selected version. Use install for upgrades, or recover after explicitly repairing a missing selected version.")
            stage_package(package, destination, backup_root=root / "quarantine")
            if recovery_tools:
                atomic_json(root / "tools" / "recovery.json", {"schema_version": 1, "manifest_sha256": sha256_file(destination / "build-manifest.json")})
            else:
                ensure_recovery(root, package)
            write_launchers(root)
        return {"ok": True, "root": str(root), "repaired": "recovery-tools" if recovery_tools else identity,
                "active_pointer_changed": False, "next_step": "Run status; if active.json is damaged, run recover --version <complete retained directory ID>. No requests were replayed."}


def list_versions(root: Path) -> dict[str, object]:
    root = check_root(root)
    directory = root / "versions"
    reject_link_ancestors(directory)
    entries = []
    for path in sorted(directory.iterdir()) if directory.is_dir() else []:
        if path.name.startswith("."):
            continue
        try:
            manifest = verify_package(safe_version_path(root, path.name))
            entries.append({"id": path.name, "valid": True, "version": manifest["version"], "build_id": manifest["build_id"]})
        except (OSError, ValueError, KeyError, TypeError):
            entries.append({"id": path.name, "valid": False})
    return {"ok": True, "root": str(root), "versions": entries}


def status(root: Path) -> dict[str, object]:
    root = check_root(root)
    state = read_active(root)
    if not state.get("current"):
        return {"ok": True, "root": str(root), "current": None, "previous": None}
    package = safe_version_path(root, state["current"])
    manifest = verify_package(package)
    return {
        "ok": True, "root": str(root), **state,
        "version": manifest["version"], "build_id": manifest["build_id"],
        "plugin_directory": str(package), "broker_executable": str(package / "bin" / "GoldenFinger.exe"),
        "stable_broker_launcher": str(root / "GoldenFinger.cmd"),
        "stable_connector_launcher": str(root / "golden-finger-mcp.cmd"),
        "stable_manager": str(root / "GoldenFingerSetup.cmd"),
        "recovery_directory": str(recovery_package(root)),
        "codex_registration": "manual", "running_processes_changed": False,
        "next_steps": [
            "Close the old Golden Finger window after pending/running work has finished.",
            "Launch stable_broker_launcher; it resolves active.json and verifies the complete active package. Do not copy an EXE alone.",
            "Use Codex's supported local plugin import/installation UI with plugin_directory. If unavailable, keep the exported directory and follow the supported marketplace workflow; do not edit private databases.",
            "Enable the imported plugin and reload its MCP connection. Confirm the window's diagnostic build ID matches the connector.",
            "Rollback only changes the local active pointer. Repeat the manual Codex import/reconnection step after any version switch.",
        ],
    }


def export_active(root: Path, destination: Path) -> dict[str, object]:
    current = status(root)
    if not current.get("current"):
        raise ValueError("No active version to export.")
    destination = destination.absolute()
    reject_link_ancestors(destination)
    if destination.is_relative_to(Path(current["plugin_directory"])):
        raise ValueError("Export destination must not be inside the active package.")
    if destination.exists():
        raise ValueError("Export destination already exists; choose a new directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".golden-finger-export-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "plugin"
        shutil.copytree(current["plugin_directory"], staged)
        verify_package(staged)
        # Do not overwrite a directory created concurrently by another process.
        if destination.exists():
            raise ValueError("Export destination was created by another process.")
        staged.rename(destination)
    return {"ok": True, "plugin_directory": str(destination), "next_steps": current["next_steps"]}


def probe_broker(expected: dict[str, object], protocol: int, *, timeout: float = 0.5) -> dict[str, object] | None:
    """Authenticate a loopback health request; never return or log endpoint secrets."""
    endpoint = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "GoldenFinger" / "endpoint.json"
    try:
        with endpoint.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            return None
        metadata = json.loads(raw.decode("ascii"))
        if not isinstance(metadata, dict) or metadata.get("host") != "127.0.0.1":
            return None
        if type(metadata.get("port")) is not int or not 1 <= metadata["port"] <= 65535:
            return None
        if not isinstance(metadata.get("token"), str) or not 32 <= len(metadata["token"]) <= 256:
            return None
        request = {"operation": "health", "protocol": metadata.get("protocol"),
                   "client_role": "launcher",
                   "client_version": metadata.get("broker_version"), "client_build_id": metadata.get("broker_build_id"), "token": metadata["token"]}
        with socket.create_connection(("127.0.0.1", metadata["port"]), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall((json.dumps(request, ensure_ascii=True) + "\n").encode("ascii"))
            connection.shutdown(socket.SHUT_WR)
            response = bytearray()
            deadline = time.monotonic() + timeout
            while len(response) <= 65_536 and time.monotonic() < deadline:
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                chunk = connection.recv(65_537 - len(response))
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in response:
                    break
            if len(response) > 65_536:
                return None
        health = json.loads(response.decode("ascii"))
        if not isinstance(health, dict) or health.get("ok") is not True or type(health.get("pid")) is not int:
            return None
        if health.get("broker_version") != expected["version"] or health.get("broker_build_id") != expected["build_id"] or health.get("protocol") != protocol:
            raise ValueError("A different Golden Finger build is already running. Close it normally before launching this version; no process was stopped.")
        if health["pid"] != metadata.get("pid"):
            return None
        return {"pid": health["pid"], "version": health["broker_version"], "build_id": health["broker_build_id"], "protocol": health["protocol"]}
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        return None


class StartupErrorCapture:
    """Drain stderr without retaining or writing secret-bearing text."""
    def __init__(self, stream):
        self.stream = stream
        self.byte_count = 0
        self.truncated = False
        if stream is not None:
            threading.Thread(target=self._drain, name="golden-finger-startup-stderr", daemon=True).start()

    def _drain(self):
        try:
            while True:
                data = self.stream.read(4096)
                if not data:
                    break
                self.byte_count = min(1_048_576, self.byte_count + len(data))
                if self.byte_count == 1_048_576:
                    self.truncated = True
        except (OSError, ValueError):
            pass
        finally:
            self.stream.close()


def record_startup(root: Path, result: dict[str, object], capture: StartupErrorCapture | None = None) -> None:
    log = root / "logs"
    reject_link_ancestors(log)
    log.mkdir(exist_ok=True)
    # Deliberate allowlist: no raw stderr, exception messages, endpoint or tokens.
    safe = {key: value for key, value in result.items() if key in {"state", "pid", "version", "build_id", "exit_code", "reason_code"}}
    safe["recorded_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    safe["stderr_bytes"] = capture.byte_count if capture else 0
    safe["stderr_truncated"] = capture.truncated if capture else False
    atomic_json(log / "last-startup.json", safe)


def launch_active(root: Path, *, connector: bool, deadline_seconds: float = 10.0) -> int:
    current = status(root)
    if not current.get("current"):
        raise ValueError("No active package installed.")
    package = Path(current["plugin_directory"])
    if connector:
        # Inherit standard streams exactly; stdout must remain MCP JSON-RPC only.
        return subprocess.call([str(package / "bin" / "golden-finger-mcp.exe")], cwd=package)
    protocol = read_protocol_version(package / "scripts")
    already = probe_broker(current, protocol)
    if already is not None:
        result = {"state": "already_running", **already}
        record_startup(root, result)
        print(json.dumps({"ok": True, **result}, ensure_ascii=True))
        return 0
    process = subprocess.Popen([current["broker_executable"]], cwd=package, close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    capture = StartupErrorCapture(process.stderr)
    deadline = time.monotonic() + max(0.1, min(deadline_seconds, 30.0))
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            # A simultaneous launcher may have created the exact same broker.
            ready = probe_broker(current, protocol)
            if ready is not None:
                record_startup(root, {"state": "already_running", **ready}, capture)
                print(json.dumps({"ok": True, "state": "already_running", **ready}, ensure_ascii=True))
                return 0
            record_startup(root, {"state": "failed", "pid": process.pid, "exit_code": code, "reason_code": "broker_exited_before_ready"}, capture)
            raise ValueError(f"Golden Finger exited before becoming ready (exit code {code}). See logs/last-startup.json; raw stderr is intentionally not stored.")
        ready = probe_broker(current, protocol, timeout=min(0.5, max(0.01, deadline - time.monotonic())))
        if ready is not None and ready["pid"] == process.pid and process.poll() is None:
            record_startup(root, {"state": "ready", **ready}, capture)
            print(json.dumps({"ok": True, "state": "ready", **ready}, ensure_ascii=True))
            return 0
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    record_startup(root, {"state": "failed", "pid": process.pid, "reason_code": "startup_deadline_exceeded"}, capture)
    raise ValueError("Golden Finger did not complete its exact-build health handshake in time. The process was not stopped. Check the window and logs/last-startup.json before retrying.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="Install/upgrade from a trusted release directory or ZIP.")
    install_parser.add_argument("source", type=Path)
    install_parser.add_argument("--sha256", help="Expected ZIP SHA-256 from a trusted release listing.")
    repair_parser = commands.add_parser("repair", help="Restore damaged files from an independent trusted complete package.")
    repair_parser.add_argument("--from", dest="source", type=Path, required=True)
    repair_parser.add_argument("--sha256")
    repair_parser.add_argument("--recovery-tools", action="store_true", help="Repair the independent tools using an external release's Setup.")
    recover_parser = commands.add_parser("recover", help="Select a verified retained version even if active.json is damaged.")
    recover_parser.add_argument("--version", dest="identity", required=True, help="Complete directory ID from the versions command.")
    for command in (install_parser, repair_parser, recover_parser, commands.add_parser("versions"), commands.add_parser("status"), commands.add_parser("rollback"), commands.add_parser("launch"), commands.add_parser("connector")):
        command.add_argument("--root", type=Path, default=default_root())
    export_parser = commands.add_parser("export", help="Export the complete active plugin for manual import.")
    export_parser.add_argument("destination", type=Path)
    export_parser.add_argument("--root", type=Path, default=default_root())
    args = parser.parse_args()
    try:
        if getattr(sys, "frozen", False):
            verify_package(Path(sys.executable).resolve().parents[1])
        if args.command in {"launch", "connector"}:
            return launch_active(args.root, connector=args.command == "connector")
        if args.command == "install":
            result = install(args.source, args.root, expected_sha256=args.sha256)
        elif args.command == "rollback":
            result = rollback(args.root)
        elif args.command == "export":
            result = export_active(args.root, args.destination)
        elif args.command == "repair":
            result = repair(args.source, args.root, expected_sha256=args.sha256, recovery_tools=args.recovery_tools)
        elif args.command == "recover":
            result = recover(args.root, args.identity)
        elif args.command == "versions":
            result = list_versions(args.root)
        else:
            result = status(args.root)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
