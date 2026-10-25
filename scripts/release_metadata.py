"""Single package version and reproducible source identity (no runtime side effects)."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
from pathlib import Path
import platform
import re
import subprocess
import sys

VERSION = "0.5.1"
MANIFEST_SCHEMA_VERSION = 2


def read_protocol_version(scripts_dir: Path) -> int:
    """Read the verified package's protocol without importing or executing it."""
    tree = ast.parse((scripts_dir / "relay_protocol.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PROTOCOL_VERSION" for target in node.targets):
            if isinstance(node.value, ast.Constant) and type(node.value.value) is int:
                return node.value.value
    raise ValueError("Package protocol version is not a literal integer.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_build_id(scripts_dir: Path, *, prefix: str = "src-") -> str:
    """Include every shipped Python module, including codex_lifecycle.py, and pins."""
    files = sorted(
        (path for path in scripts_dir.iterdir() if path.suffix in {".py", ".pyw"}),
        key=lambda path: path.name,
    )
    requirements = scripts_dir.parent / "requirements-build.txt"
    if requirements.is_file():
        files.append(requirements)
    if not files:
        raise OSError("No source files found for the build identity.")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8") + b"\0")
        # Git checkout line-ending policy must not change the runtime identity.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if path.name == "relay_protocol.py":
            # The release process changes only this stamp in its copied sources.
            data = re.sub(rb'^PACKAGE_BUILD_ID = "rel-[a-f0-9]{24}"$', b'PACKAGE_BUILD_ID = "source"', data, flags=re.MULTILINE)
        digest.update(data + b"\0")
    return prefix + digest.hexdigest()[:24]


def build_provenance(root: Path) -> dict[str, object]:
    def git(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    requirements = root / "requirements-build.txt"
    dependencies: dict[str, str] = {}
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8").splitlines():
            package = line.split("==", 1)[0].strip()
            if not package or package.startswith("#"):
                continue
            try:
                dependencies[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                dependencies[package] = "not-installed"
    status = git("status", "--porcelain", "--untracked-files=normal")
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "dependencies": dependencies,
        "requirements_sha256": sha256_file(requirements) if requirements.is_file() else None,
    }
