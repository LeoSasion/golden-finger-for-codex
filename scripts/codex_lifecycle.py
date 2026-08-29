"""Fail-closed Codex Desktop task inventory and graceful restart support.

This module is deliberately separate from the authorization transport.  It
uses the read-only ``list_threads`` tool already exposed by a running Codex
Desktop host, and it never reads Codex databases, rollouts, or session files.
If the host bridge or its response contract cannot be verified, inventory is
reported as unavailable and restart remains disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
import struct
import threading
import time
from typing import Any, Callable, Iterable
import unicodedata


APP_TOOLS_PIPE_PREFIX = "codex-browser-use-"
APP_TOOLS_SCHEMA_VERSION = 4
MAX_APP_TOOL_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_PIPE_TIMEOUT_SECONDS = 1.5
DEFAULT_INVENTORY_REFRESH_SECONDS = 2.0
INVENTORY_STALE_SECONDS = 8.0
MAX_VISIBLE_TASKS = 50
_THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PIPE_PREFERENCE_LOCK = threading.Lock()
_preferred_pipe_path: str | None = None


class CodexLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_codex_thread_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _THREAD_ID_PATTERN.fullmatch(candidate) else None


def extract_codex_thread_id(metadata: object) -> str | None:
    """Extract host-provided thread metadata without trusting caller labels."""

    if not isinstance(metadata, dict):
        return None
    for key in (
        "openai/threadId",
        "openai/thread_id",
        "codexThreadId",
        "codex_thread_id",
        "threadId",
        "thread_id",
    ):
        normalized = normalize_codex_thread_id(metadata.get(key))
        if normalized is not None:
            return normalized

    turn_metadata: object = metadata.get("x-codex-turn-metadata")
    if isinstance(turn_metadata, str):
        try:
            turn_metadata = json.loads(turn_metadata)
        except json.JSONDecodeError:
            turn_metadata = None
    if isinstance(turn_metadata, dict):
        normalized = normalize_codex_thread_id(
            turn_metadata.get("thread_id") or turn_metadata.get("threadId")
        )
        if normalized is not None:
            return normalized

    nested = metadata.get("thread")
    if isinstance(nested, dict):
        return normalize_codex_thread_id(nested.get("id"))
    return None


def _display_text(value: object, *, fallback: str, limit: int) -> str:
    if not isinstance(value, str):
        return fallback
    filtered = "".join(
        " "
        if character.isspace() or unicodedata.category(character) == "Cc"
        else character
        for character in value
        if not unicodedata.category(character).startswith("C")
        or unicodedata.category(character) == "Cc"
    )
    cleaned = " ".join(filtered.split())
    if not cleaned:
        return fallback
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


@dataclass(frozen=True)
class CodexTask:
    task_id: str
    title: str
    status: str
    cwd: str
    kind: str
    updated_at: float

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class CodexTaskInventory:
    available: bool
    tasks: tuple[CodexTask, ...]
    checked_monotonic: float
    error_code: str | None = None
    source_count: int = 0

    @property
    def active_tasks(self) -> tuple[CodexTask, ...]:
        return tuple(task for task in self.tasks if task.active)

    def is_fresh(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return self.available and current - self.checked_monotonic <= INVENTORY_STALE_SECONDS


def unavailable_inventory(code: str) -> CodexTaskInventory:
    return CodexTaskInventory(
        available=False,
        tasks=(),
        checked_monotonic=time.monotonic(),
        error_code=code,
        source_count=0,
    )


def parse_list_threads_payload(payload: object) -> tuple[CodexTask, ...]:
    if not isinstance(payload, dict):
        raise CodexLifecycleError(
            "invalid_inventory",
            "Codex returned a non-object task inventory.",
        )
    if payload.get("schemaVersion") != APP_TOOLS_SCHEMA_VERSION:
        raise CodexLifecycleError(
            "inventory_schema_mismatch",
            "The Codex task inventory schema is not supported by this build.",
        )
    unavailable_hosts = payload.get("unavailableHosts")
    unavailable_sources = payload.get("unavailableSources")
    if unavailable_hosts != [] or unavailable_sources != []:
        raise CodexLifecycleError(
            "inventory_partial",
            "Codex reported an incomplete task inventory.",
        )
    raw_threads = payload.get("threads")
    pinned_threads = payload.get("pinnedThreads")
    if not isinstance(raw_threads, list) or not isinstance(pinned_threads, list):
        raise CodexLifecycleError(
            "invalid_inventory",
            "Codex did not return a task list.",
        )
    if len(raw_threads) >= MAX_VISIBLE_TASKS or len(pinned_threads) > MAX_VISIBLE_TASKS:
        raise CodexLifecycleError(
            "inventory_truncated",
            "Codex returned a task list that may be truncated.",
        )

    tasks: list[CodexTask] = []
    seen_ids: set[str] = set()
    for raw in [*pinned_threads, *raw_threads]:
        if not isinstance(raw, dict):
            raise CodexLifecycleError(
                "invalid_inventory",
                "Codex returned an invalid task entry.",
            )
        task_id = _display_text(raw.get("id"), fallback="", limit=200)
        status = _display_text(raw.get("status"), fallback="unknown", limit=32)
        normalized_task_id = normalize_codex_thread_id(task_id)
        if normalized_task_id is None:
            raise CodexLifecycleError(
                "invalid_inventory",
                "Codex returned an invalid task identifier.",
            )
        if status not in {"active", "idle", "notLoaded", "systemError"}:
            raise CodexLifecycleError(
                "inventory_status_unsupported",
                "Codex returned an unsupported task status.",
            )
        if normalized_task_id in seen_ids:
            continue
        seen_ids.add(normalized_task_id)
        updated_at = raw.get("updatedAt")
        if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
            updated_at = 0.0
        tasks.append(
            CodexTask(
                task_id=normalized_task_id,
                title=_display_text(raw.get("title"), fallback="未命名任务", limit=160),
                status=status,
                cwd=_display_text(raw.get("cwd"), fallback="(projectless)", limit=260),
                kind=_display_text(raw.get("kind"), fallback="codex", limit=32),
                updated_at=float(updated_at),
            )
        )
    tasks.sort(key=lambda task: (not task.active, -task.updated_at, task.title.casefold()))
    return tuple(tasks)


class _NativePipeJsonRpc:
    def __init__(self, path: str, *, timeout_seconds: float) -> None:
        if os.name != "nt":
            raise CodexLifecycleError("platform_unsupported", "Windows is required.")
        self.path = path
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._next_id = 1
        self._handle: int | None = None

    def __enter__(self) -> _NativePipeJsonRpc:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        timeout_ms = max(1, round(self.timeout_seconds * 1000))
        if not kernel32.WaitNamedPipeW(self.path, timeout_ms):
            raise CodexLifecycleError(
                "app_bridge_unavailable",
                "The Codex app tools bridge did not become available.",
            )
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            self.path,
            0x80000000 | 0x40000000,
            0,
            None,
            3,
            0,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, invalid_handle):
            raise CodexLifecycleError(
                "app_bridge_unavailable",
                "The Codex app tools bridge could not be opened.",
            )
        self._handle = int(handle)
        server_process_id = wintypes.ULONG()
        kernel32.GetNamedPipeServerProcessId.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        )
        kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        if not kernel32.GetNamedPipeServerProcessId(
            wintypes.HANDLE(self._handle),
            ctypes.byref(server_process_id),
        ):
            self.__exit__()
            raise CodexLifecycleError(
                "app_bridge_identity_unverified",
                "The Codex app tools bridge identity could not be verified.",
            )
        server_executable = _process_executable(int(server_process_id.value))
        if not is_codex_desktop_executable(server_executable):
            self.__exit__()
            raise CodexLifecycleError(
                "app_bridge_identity_unverified",
                "The task inventory pipe is not owned by Codex Desktop.",
            )
        return self

    def __exit__(self, *_exc: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                wintypes.HANDLE(handle)
            )

    def _write_all(self, payload: bytes) -> None:
        handle = self._handle
        if handle is None:
            raise CodexLifecycleError("app_bridge_closed", "Codex bridge is closed.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WriteFile.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        kernel32.WriteFile.restype = wintypes.BOOL
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(payload)
        if not kernel32.WriteFile(
            wintypes.HANDLE(handle),
            buffer,
            len(payload),
            ctypes.byref(written),
            None,
        ) or written.value != len(payload):
            raise CodexLifecycleError(
                "app_bridge_write_failed",
                "The Codex task inventory request could not be written.",
            )

    def _available_bytes(self) -> int:
        handle = self._handle
        if handle is None:
            return 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        available = wintypes.DWORD()
        if not kernel32.PeekNamedPipe(
            wintypes.HANDLE(handle),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            raise CodexLifecycleError(
                "app_bridge_read_failed",
                "The Codex task inventory bridge disconnected.",
            )
        return int(available.value)

    def _read_exact(self, size: int) -> bytes:
        handle = self._handle
        if handle is None:
            raise CodexLifecycleError("app_bridge_closed", "Codex bridge is closed.")
        deadline = time.monotonic() + self.timeout_seconds
        chunks = bytearray()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        while len(chunks) < size:
            available = self._available_bytes()
            if available <= 0:
                if time.monotonic() >= deadline:
                    raise CodexLifecycleError(
                        "app_bridge_timeout",
                        "Codex did not return its task inventory in time.",
                    )
                time.sleep(0.01)
                continue
            count = min(size - len(chunks), available)
            buffer = ctypes.create_string_buffer(count)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(
                wintypes.HANDLE(handle),
                buffer,
                count,
                ctypes.byref(read),
                None,
            ) or read.value <= 0:
                raise CodexLifecycleError(
                    "app_bridge_read_failed",
                    "Codex closed the task inventory bridge.",
                )
            chunks.extend(buffer.raw[: read.value])
        return bytes(chunks)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message = {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        payload = json.dumps(
            message,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_APP_TOOL_FRAME_BYTES:
            raise CodexLifecycleError("request_too_large", "Codex request is too large.")
        self._write_all(struct.pack("<I", len(payload)) + payload)
        frame_size = struct.unpack("<I", self._read_exact(4))[0]
        if frame_size <= 0 or frame_size > MAX_APP_TOOL_FRAME_BYTES:
            raise CodexLifecycleError(
                "invalid_app_bridge_frame",
                "Codex returned an invalid task inventory frame.",
            )
        try:
            response = json.loads(self._read_exact(frame_size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexLifecycleError(
                "invalid_app_bridge_response",
                "Codex returned invalid task inventory JSON.",
            ) from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise CodexLifecycleError(
                "invalid_app_bridge_response",
                "Codex returned a mismatched task inventory response.",
            )
        error = response.get("error")
        if isinstance(error, dict):
            raise CodexLifecycleError(
                "app_bridge_request_failed",
                _display_text(error.get("message"), fallback="Codex rejected the request.", limit=300),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CodexLifecycleError(
                "invalid_app_bridge_response",
                "Codex returned no task inventory result.",
            )
        return result


def _app_tool_pipe_paths() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    try:
        names = os.listdir(r"\\.\pipe")
    except OSError:
        return ()
    matches = sorted(
        name
        for name in names
        if name.startswith(APP_TOOLS_PIPE_PREFIX)
        and len(name) <= len(APP_TOOLS_PIPE_PREFIX) + 64
    )
    discovered = tuple("\\\\.\\pipe\\" + name for name in matches[:32])
    preferred: list[str] = []
    configured = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    if isinstance(configured, str) and configured in discovered:
        preferred.append(configured)
    with _PIPE_PREFERENCE_LOCK:
        if _preferred_pipe_path in discovered:
            preferred.append(str(_preferred_pipe_path))
    return tuple(dict.fromkeys([*preferred, *discovered]))


def _remember_pipe_path(path: str) -> None:
    global _preferred_pipe_path
    with _PIPE_PREFERENCE_LOCK:
        _preferred_pipe_path = path


def _tool_from_catalog(result: dict[str, Any]) -> tuple[str, str]:
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise CodexLifecycleError("tool_catalog_invalid", "Codex tool catalog is invalid.")
    for tool in tools:
        if (
            isinstance(tool, dict)
            and tool.get("name") == "list_threads"
            and isinstance(tool.get("namespace"), str)
        ):
            return "list_threads", str(tool["namespace"])
    raise CodexLifecycleError(
        "task_inventory_tool_missing",
        "The running Codex app does not expose a task inventory tool.",
    )


def _payload_from_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("success") is not True:
        raise CodexLifecycleError(
            "task_inventory_call_failed",
            "Codex could not provide a complete task inventory.",
        )
    content_items = result.get("contentItems")
    if not isinstance(content_items, list):
        raise CodexLifecycleError(
            "task_inventory_call_failed",
            "Codex returned no task inventory content.",
        )
    for item in content_items:
        if isinstance(item, dict) and item.get("type") == "inputText":
            text = item.get("text")
            if isinstance(text, str):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise CodexLifecycleError(
                        "invalid_inventory",
                        "Codex returned invalid task inventory content.",
                    ) from exc
                if isinstance(payload, dict):
                    return payload
    raise CodexLifecycleError(
        "invalid_inventory",
        "Codex returned no readable task inventory.",
    )


def probe_codex_task_inventory(
    thread_ids: Iterable[str],
    *,
    pipe_paths: Iterable[str] | None = None,
    timeout_seconds: float = DEFAULT_PIPE_TIMEOUT_SECONDS,
) -> CodexTaskInventory:
    normalized_ids = tuple(
        dict.fromkeys(
            normalized
            for value in thread_ids
            if (normalized := normalize_codex_thread_id(value)) is not None
        )
    )
    if not normalized_ids:
        return unavailable_inventory("awaiting_thread_metadata")
    candidates = tuple(pipe_paths) if pipe_paths is not None else _app_tool_pipe_paths()
    if not candidates:
        return unavailable_inventory("app_bridge_unavailable")

    last_error = "app_bridge_unavailable"
    for path in candidates:
        try:
            with _NativePipeJsonRpc(path, timeout_seconds=timeout_seconds) as rpc:
                tool_name, namespace = _tool_from_catalog(
                    rpc.request("tools/list", {"threadStartKind": "all"})
                )
                for thread_id in normalized_ids:
                    try:
                        result = rpc.request(
                            "tools/call",
                            {
                                "arguments": {"limit": MAX_VISIBLE_TASKS},
                                "callId": "golden-finger-inventory-" + secrets.token_hex(8),
                                "namespace": namespace,
                                "threadId": thread_id,
                                "tool": tool_name,
                                "turnId": "golden-finger-inventory",
                            },
                        )
                        tasks = parse_list_threads_payload(
                            _payload_from_tool_result(result)
                        )
                        _remember_pipe_path(path)
                        return CodexTaskInventory(
                            available=True,
                            tasks=tasks,
                            checked_monotonic=time.monotonic(),
                            source_count=1,
                        )
                    except CodexLifecycleError as exc:
                        last_error = exc.code
        except (CodexLifecycleError, OSError) as exc:
            last_error = (
                exc.code if isinstance(exc, CodexLifecycleError) else "app_bridge_unavailable"
            )
    return unavailable_inventory(last_error)


class CodexTaskMonitor:
    def __init__(
        self,
        *,
        probe: Callable[[Iterable[str]], CodexTaskInventory] = probe_codex_task_inventory,
        refresh_seconds: float = DEFAULT_INVENTORY_REFRESH_SECONDS,
    ) -> None:
        self._probe = probe
        self._refresh_seconds = max(0.5, float(refresh_seconds))
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._thread_ids: tuple[str, ...] = ()
        self._snapshot = unavailable_inventory("awaiting_thread_metadata")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="golden-finger-codex-inventory",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.5)

    def register_thread_ids(self, values: Iterable[str]) -> None:
        normalized = tuple(
            dict.fromkeys(
                candidate
                for value in values
                if (candidate := normalize_codex_thread_id(value)) is not None
            )
        )
        with self._lock:
            changed = normalized != self._thread_ids
            self._thread_ids = normalized
        if changed:
            self._wake.set()

    def snapshot(self) -> CodexTaskInventory:
        with self._lock:
            return self._snapshot

    def probe_now(self) -> CodexTaskInventory:
        with self._lock:
            thread_ids = self._thread_ids
        try:
            with self._probe_lock:
                snapshot = self._probe(thread_ids)
        except Exception:
            snapshot = unavailable_inventory("inventory_internal_error")
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            self.probe_now()
            self._wake.wait(self._refresh_seconds)
            self._wake.clear()


@dataclass(frozen=True)
class CodexDesktopTarget:
    hwnd: int
    process_id: int
    executable: str


@dataclass(frozen=True)
class RestartResult:
    ok: bool
    code: str
    message: str


def is_codex_desktop_executable(path: object) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    normalized = str(Path(path)).replace("/", "\\").casefold()
    packaged = (
        "\\windowsapps\\openai.codex_" in normalized
        and normalized.endswith("\\app\\chatgpt.exe")
    )
    local = (
        "\\openai\\codex\\" in normalized
        and normalized.endswith(("\\chatgpt.exe", "\\codex desktop.exe"))
    )
    return packaged or local


def _process_executable(process_id: int) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        size = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def find_codex_desktop_targets() -> tuple[CodexDesktopTarget, ...]:
    if os.name != "nt":
        return ()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    targets: list[CodexDesktopTarget] = []
    seen: set[tuple[int, int]] = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        executable = _process_executable(int(process_id.value))
        if executable is None or not is_codex_desktop_executable(executable):
            return True
        key = (int(hwnd), int(process_id.value))
        if key not in seen:
            seen.add(key)
            targets.append(
                CodexDesktopTarget(
                    hwnd=int(hwnd),
                    process_id=int(process_id.value),
                    executable=executable,
                )
            )
        return True

    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    callback = callback_type(collect)
    if not user32.EnumWindows(callback, 0):
        return ()
    return tuple(targets)


def _process_has_exited(process_id: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, False, process_id)
    if not handle:
        return ctypes.get_last_error() == 87
    try:
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel32.CloseHandle(handle)


def graceful_restart_codex(*, timeout_seconds: float = 15.0) -> RestartResult:
    """Ask Codex to close normally, then relaunch; never force-terminate it."""

    if os.name != "nt":
        return RestartResult(False, "platform_unsupported", "仅支持 Windows Codex 客户端。")
    targets = find_codex_desktop_targets()
    if not targets:
        return RestartResult(False, "codex_window_not_found", "未找到可安全重启的 Codex 窗口。")
    executables = {str(Path(target.executable)) for target in targets}
    if len(executables) != 1:
        return RestartResult(False, "ambiguous_codex_runtime", "检测到多个不同的 Codex 运行版本。")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL
    for target in targets:
        if not user32.PostMessageW(target.hwnd, 0x0010, 0, 0):
            return RestartResult(False, "codex_close_failed", "Codex 未接受正常关闭请求。")

    process_ids = {target.process_id for target in targets}
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if all(_process_has_exited(process_id) for process_id in process_ids):
            break
        time.sleep(0.1)
    else:
        return RestartResult(
            False,
            "codex_close_timeout",
            "Codex 未在时限内正常退出；已放弃重启，未强制结束进程。",
        )

    executable = next(iter(executables))
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    )
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(
        None,
        "open",
        executable,
        None,
        str(Path(executable).parent),
        1,
    )
    result_value = int(result or 0)
    if result_value <= 32:
        return RestartResult(False, "codex_launch_failed", "Codex 已关闭，但自动重新打开失败。")
    return RestartResult(True, "restarted", "Codex 已正常关闭并重新打开。")
