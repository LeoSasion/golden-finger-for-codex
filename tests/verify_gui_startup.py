"""Exercise a complete release's real Setup, stable launcher and GUI in isolation.

No authorization request or business command is submitted. Only a window whose
PID, creation time and executable path belong to this new installation receives
WM_CLOSE. Reports contain an explicit allowlist, never endpoint capabilities.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True


class CheckFailed(Exception):
    """Only fixed, non-sensitive reason codes may be reported."""


def require(condition, reason):
    if not condition:
        raise CheckFailed(reason)


def run_child(argv, environment, directory, children, *, timeout=10):
    child = subprocess.Popen(
        argv, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    children.append(child)
    try:
        output, error = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Do not force-kill even a test-owned process; retain its temporary tree.
        raise CheckFailed("child_deadline_exceeded") from None
    require(child.returncode == 0, "child_nonzero_exit")
    require(len(output) <= 65_536, "child_response_too_large")
    value = json.loads(output.decode("utf-8-sig"))
    require(isinstance(value, dict) and value.get("ok") is True, "child_response_invalid")
    return value, len(error)


def health(endpoint, expected):
    raw = endpoint.read_bytes()
    require(len(raw) <= 16_384, "endpoint_too_large")
    metadata = json.loads(raw.decode("ascii"))
    require(metadata.get("host") == "127.0.0.1", "endpoint_not_loopback")
    require(type(metadata.get("port")) is int and 1 <= metadata["port"] <= 65535,
            "endpoint_invalid_port")
    require(isinstance(metadata.get("token"), str) and 32 <= len(metadata["token"]) <= 256,
            "endpoint_invalid_capability")
    request = {
        "operation": "health", "protocol": expected["protocol"],
        "client_role": "launcher", "client_version": expected["version"],
        "client_build_id": expected["build_id"], "token": metadata["token"],
    }
    with socket.create_connection(("127.0.0.1", metadata["port"]), timeout=0.5) as connection:
        connection.settimeout(0.5)
        connection.sendall((json.dumps(request, ensure_ascii=True) + "\n").encode("ascii"))
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        deadline = time.monotonic() + 0.5
        while b"\n" not in response and len(response) <= 65_536:
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = connection.recv(65_537 - len(response))
            if not chunk:
                break
            response.extend(chunk)
            require(time.monotonic() <= deadline, "health_deadline_exceeded")
    require(len(response) <= 65_536, "health_response_too_large")
    value = json.loads(response.decode("ascii"))
    require(value.get("ok") is True, "health_not_ready")
    require(value.get("broker_version") == expected["version"] and
            value.get("broker_build_id") == expected["build_id"] and
            value.get("protocol") == expected["protocol"], "health_build_mismatch")
    require(type(value.get("pid")) is int and value["pid"] == metadata.get("pid"),
            "health_pid_mismatch")
    return value["pid"]


class OwnedGui:
    def __init__(self, pid, executable, install_root, started_ns):
        self.pid = pid
        self.handle = None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user.GetWindow.restype = wintypes.HWND
        self.user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        self.user.PostMessageW.restype = wintypes.BOOL
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = (self.callback_type, wintypes.LPARAM)
        self.user.EnumWindows.restype = wintypes.BOOL
        handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        require(bool(handle), "broker_process_unavailable")
        self.handle = handle
        try:
            path = ctypes.create_unicode_buffer(32_768)
            size = wintypes.DWORD(len(path))
            require(self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)),
                    "broker_image_unavailable")
            actual = Path(path.value).resolve()
            require(actual.is_relative_to(install_root) and actual == executable.resolve(),
                    "broker_image_outside_test_installation")
            times = [wintypes.FILETIME() for _ in range(4)]
            require(self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)),
                    "broker_creation_time_unavailable")
            created_ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created_ns = (created_ticks - 116444736000000000) * 100
            require(created_ns >= started_ns, "broker_predates_test")
        except BaseException:
            self.release()
            raise

    def visible_windows(self):
        windows = []

        @self.callback_type
        def collect(window, _):
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == self.pid and self.user.IsWindowVisible(window) and not self.user.GetWindow(window, 4):
                windows.append(window)
            return True

        require(self.user.EnumWindows(collect, 0), "window_enumeration_failed")
        return windows

    def wait_visible(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            require(self.kernel.WaitForSingleObject(self.handle, 0) == 258, "broker_exited_before_gui")
            windows = self.visible_windows()
            if windows:
                return len(windows)
            time.sleep(0.05)
        raise CheckFailed("gui_session_unavailable_or_window_deadline")

    def close_normally(self):
        if self.kernel.WaitForSingleObject(self.handle, 0) == 0:
            return True
        windows = self.visible_windows()
        require(bool(windows), "no_owned_window_for_normal_close")
        for window in windows:
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(owner))
            require(owner.value == self.pid, "window_owner_changed")
            require(self.user.PostMessageW(window, 0x0010, 0, 0), "normal_close_failed")
        return self.kernel.WaitForSingleObject(self.handle, 10_000) == 0

    def release(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path("dist/golden-finger-0.3.1"))
    parser.add_argument("--output", type=Path, default=Path("build/gui-startup-031-result.json"))
    options = parser.parse_args()
    require(os.name == "nt", "windows_required")
    workspace = Path(__file__).resolve().parents[1]
    build = (workspace / "build").resolve()
    build.mkdir(exist_ok=True)
    output = options.output.resolve()
    require(output.is_relative_to(build), "report_outside_build")
    package = options.package.resolve()
    manifest = json.loads((package / "build-manifest.json").read_text(encoding="utf-8"))
    expected = {name: manifest[name] for name in ("version", "build_id", "protocol")}
    started_ns = time.time_ns()
    started = time.monotonic()
    temporary = Path(tempfile.mkdtemp(prefix="gui-startup-", dir=build)).resolve()
    require(temporary.is_relative_to(build), "temporary_outside_build")
    root = temporary / "installation"
    profile = temporary / "profile"
    local = profile / "AppData" / "Local"
    roaming = profile / "AppData" / "Roaming"
    codex = profile / ".codex"
    temp = temporary / "temp"
    for directory in (local, roaming, codex, temp):
        directory.mkdir(parents=True)
    environment = dict(os.environ)
    for name in ("PSModulePath", "CODEX_APP_TOOLS_PIPE_PATH", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    environment.update({
        "LOCALAPPDATA": str(local), "APPDATA": str(roaming),
        "USERPROFILE": str(profile), "HOME": str(profile),
        "HOMEDRIVE": profile.drive, "HOMEPATH": str(profile)[len(profile.drive):],
        "CODEX_HOME": str(codex), "TEMP": str(temp), "TMP": str(temp),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    report = {
        "passed": False, "fixture": "real Setup EXE and stable dispatch to real GUI EXE",
        "started_at": datetime.now(timezone.utc).isoformat(), **expected,
        "checks": [], "authorization_requests": 0, "business_commands_executed": 0,
        "codex_configuration_changed": False,
        "powershell_module_path": "native_windows_powershell_defaults",
    }
    children = []
    gui = None
    broker_path = None
    broker_pid = None
    endpoint = local / "GoldenFinger" / "endpoint.json"
    try:
        require(not endpoint.exists() and not root.exists(), "fixture_not_fresh")
        installed, stderr_bytes = run_child(
            [str(package / "bin" / "GoldenFingerSetup.exe"), "install", str(package), "--root", str(root)],
            environment, temporary, children, timeout=30,
        )
        require(installed.get("version") == expected["version"] and
                installed.get("build_id") == expected["build_id"], "installed_build_mismatch")
        broker_path = Path(installed["broker_executable"]).resolve()
        require(broker_path.is_relative_to(root), "installed_broker_outside_fixture")
        require(not endpoint.exists(), "install_unexpectedly_started_broker")
        report["checks"].append("real_setup_installed_exact_build_without_starting_broker")
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        launched, launch_stderr = run_child(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(root / "dispatch.ps1"), "-Mode", "launch"],
            environment, temporary, children,
        )
        report["child_stderr_bytes"] = stderr_bytes + launch_stderr
        require(launched.get("state") == "ready", "launcher_did_not_start_new_broker")
        require(all(launched.get(name) == expected[name] for name in expected), "launcher_build_mismatch")
        broker_pid = health(endpoint, expected)
        require(broker_pid == launched.get("pid"), "launcher_health_pid_mismatch")
        startup = json.loads((root / "logs" / "last-startup.json").read_text(encoding="utf-8"))
        require(startup.get("state") == "ready" and startup.get("pid") == broker_pid and
                startup.get("build_id") == expected["build_id"], "startup_log_not_ready")
        report["checks"].append("stable_dispatch_and_independent_health_agree_on_exact_build_and_pid")
        gui = OwnedGui(broker_pid, broker_path, root, started_ns)
        report["visible_owned_window_count"] = gui.wait_visible()
        report["broker_pid"] = broker_pid
        report["checks"].append("new_broker_image_within_installation_has_visible_gui_window")
        require(not any(codex.rglob("*")), "isolated_codex_directory_changed")
        report["checks"].append("isolated_codex_configuration_untouched")
        report["passed"] = True
    except CheckFailed as exc:
        report["reason_code"] = str(exc)
    except Exception as exc:
        report["reason_code"] = "unexpected_" + type(exc).__name__
    finally:
        closed = False
        try:
            # A launcher failure may still have started a GUI. Only recover its
            # isolated endpoint PID and apply exactly the same ownership checks.
            if gui is None and broker_path is not None and endpoint.is_file():
                broker_pid = health(endpoint, expected)
                gui = OwnedGui(broker_pid, broker_path, root, started_ns)
            if gui is not None:
                closed = gui.close_normally()
                require(closed, "normal_close_deadline_exceeded")
                require(not endpoint.exists(), "endpoint_not_removed_on_normal_close")
                report["checks"].append("owned_gui_closed_with_wm_close_and_removed_isolated_endpoint")
            elif not endpoint.exists():
                closed = True
        except CheckFailed as exc:
            report["cleanup_reason_code"] = str(exc)
        except Exception as exc:
            report["cleanup_reason_code"] = "unexpected_" + type(exc).__name__
        finally:
            if gui is not None:
                gui.release()
        children_done = all(child.poll() is not None for child in children)
        if closed and children_done:
            try:
                require(temporary.resolve().is_relative_to(build) and temporary != build,
                        "cleanup_path_outside_fixture")
                shutil.rmtree(temporary)
                report["temporary_installation_removed"] = True
            except Exception as exc:
                report["cleanup_reason_code"] = "cleanup_" + type(exc).__name__
        if not report.get("temporary_installation_removed"):
            report["retained_fixture"] = temporary.relative_to(build).as_posix()
            report["passed"] = False
        if "cleanup_reason_code" in report:
            report["passed"] = False
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="ascii")
        print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
