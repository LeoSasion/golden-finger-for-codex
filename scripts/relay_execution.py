"""Process creation primitives independent of the approval window.

Windows children remain suspended until their Job Object has been assigned.
Only then is the initial thread resumed, so user code cannot create an
uncontained descendant during startup.
"""

from __future__ import annotations

import ctypes
import codecs
from ctypes import wintypes
import locale
import os
import subprocess
import sys
import threading
from typing import Any, Callable

from relay_protocol import MAX_OUTPUT_BYTES, RelayError, merge_execution_environment


CREATE_SUSPENDED = 0x00000004
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def resume_initial_thread(process: subprocess.Popen[bytes]) -> None:
    """Resume the sole thread of a freshly created suspended process.

    Popen closes CreateProcess's thread handle. Toolhelp lets us reopen that
    thread using documented Windows APIs, without relying on private ntdll APIs
    or altering subprocess's global CreateProcess binding.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RelayError("process_resume_failed", "Cannot inspect the suspended process.")
    thread_ids: list[int] = []
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == process.pid:
                thread_ids.append(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise RelayError(
            "process_resume_failed", "The suspended process has an unexpected thread count."
        )
    thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_ids[0])
    if not thread:
        raise RelayError("process_resume_failed", "Cannot open the suspended initial thread.")
    try:
        if kernel32.ResumeThread(thread) != 1:
            raise RelayError("process_resume_failed", "Cannot resume the initial thread safely.")
    finally:
        kernel32.CloseHandle(thread)


def start_contained_process(
    command: list[str], *, working_directory: str, job: WindowsJob,
    env: dict[str, str] | None = None,
    before_resume: Callable[[subprocess.Popen[bytes]], bool] | None = None,
) -> subprocess.Popen[bytes]:
    """Create, contain, then run exact argv; fail closed if containment fails."""
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
            | CREATE_SUSPENDED
        )
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=flags,
        env=merge_execution_environment(env),
    )
    try:
        job.assign(process)
        if before_resume is not None and not before_resume(process):
            raise RelayError("execution_stopped_before_start", "Execution was stopped before the process resumed.")
        if os.name == "nt":
            resume_initial_thread(process)
        return process
    except BaseException:
        # A suspended process must never be abandoned on a failed startup.
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise


def output_encoding(data: bytes, requested: str = "auto", *, truncated: bool = False) -> str:
    if requested != "auto":
        name = codecs.lookup(requested).name
        if name in {"utf-16", "utf-32"}:
            big_bom = codecs.BOM_UTF16_BE if name == "utf-16" else codecs.BOM_UTF32_BE
            little_bom = codecs.BOM_UTF16_LE if name == "utf-16" else codecs.BOM_UTF32_LE
            order = "be" if data.startswith(big_bom) else "le" if data.startswith(little_bom) else ("le" if sys.byteorder == "little" else "be")
            return name + "-" + order
        return name
    if data.startswith(codecs.BOM_UTF32_LE):
        return "utf-32-le"
    if data.startswith(codecs.BOM_UTF32_BE):
        return "utf-32-be"
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be"
    try:
        # An incomplete final character is expected at a bounded head or while
        # a process is still writing. It is not evidence of another encoding.
        codecs.getincrementaldecoder("utf-8-sig")("strict").decode(data, final=not truncated)
        return "utf-8-sig"
    except UnicodeDecodeError:
        preferred = locale.getpreferredencoding(False) or "utf-8"
        try:
            codecs.getincrementaldecoder(preferred)("strict").decode(data, final=not truncated)
            return codecs.lookup(preferred).name
        except (UnicodeDecodeError, LookupError):
            return "utf-8"


def decode_output(data: bytes, requested: str = "auto", *, truncated: bool = False) -> tuple[str, str]:
    encoding = output_encoding(data, requested, truncated=truncated)
    text = codecs.getincrementaldecoder(encoding)("replace").decode(data, final=not truncated)
    return text.removeprefix("\ufeff"), encoding


class BoundedOutput:
    """Keep the beginning and latest end of a stream with original byte offsets."""

    def __init__(self, capacity: int = MAX_OUTPUT_BYTES) -> None:
        self.capacity = capacity
        self._head_limit = capacity // 2
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._complete = False
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            if self._complete:
                return
            self._total += len(data)
            prefix = min(len(data), self._head_limit - len(self._head))
            self._head.extend(data[:prefix])
            self._tail.extend(data[prefix:])
            excess = len(self._tail) - (self.capacity - self._head_limit)
            if excess > 0:
                del self._tail[:excess]

    def finish(self) -> None:
        with self._lock:
            self._complete = True

    def _snapshot(self) -> tuple[bytes, bytes, int, bool]:
        with self._lock:
            return bytes(self._head), bytes(self._tail), self._total, self._complete

    @staticmethod
    def _metadata(head: bytes, tail: bytes, total: int, complete: bool) -> dict[str, Any]:
        start = total - len(tail)
        return {
            "total_bytes": total,
            "retained_bytes": len(head) + len(tail),
            "retained_start_offset": start if start > len(head) else 0,
            "retained_ranges": [[0, len(head)], [start, total]] if start > len(head) else [[0, total]],
            "truncated": total > len(head) + len(tail),
            "complete": complete,
        }

    def summary(self) -> dict[str, Any]:
        return self._metadata(*self._snapshot())

    @staticmethod
    def _align_start(data: bytes, offset: int, encoding: str) -> tuple[bytes, int]:
        skipped = 0
        if offset > 0 and encoding in {"utf-8", "utf-8-sig"}:
            while skipped < len(data) and data[skipped] & 0xC0 == 0x80:
                skipped += 1
        elif encoding in {"utf-16", "utf-16-le", "utf-16-be"}:
            skipped = min(len(data), (-offset) % 2)
        elif encoding in {"utf-32", "utf-32-le", "utf-32-be"}:
            skipped = min(len(data), (-offset) % 4)
        return data[skipped:], offset + skipped

    def render(self, requested: str = "auto") -> tuple[str, str, bool]:
        head, tail, total, complete = self._snapshot()
        truncated = total > len(head) + len(tail)
        if not truncated:
            text, encoding = decode_output(head + tail, requested, truncated=not complete)
            return text, encoding, False
        encoding = output_encoding(head, requested, truncated=True)
        first, _ = decode_output(head, encoding, truncated=True)
        tail, _ = self._align_start(tail, total - len(tail), encoding)
        last, _ = decode_output(tail, encoding, truncated=not complete)
        omitted = total - len(head) - len(tail)
        return first + f"\n[... {omitted} output bytes omitted ...]\n" + last, encoding, True

    def tail_text(self, requested: str = "auto", limit: int = 4096) -> str:
        head, tail, total, complete = self._snapshot()
        encoding = output_encoding(head, requested, truncated=not complete or total > len(head))
        data = (head + tail)[-limit:]
        data, _ = self._align_start(data, total - len(data), encoding)
        return decode_output(data, encoding, truncated=not complete)[0]

    def page(self, offset: int, limit: int, requested: str = "auto") -> dict[str, Any]:
        head, tail, total, complete = self._snapshot()
        metadata = self._metadata(head, tail, total, complete)
        if offset > total:
            raise RelayError("invalid_output_offset", "Output offset exceeds bytes observed so far.", details={"total_bytes": total})
        encoding = output_encoding(head, requested, truncated=not complete or total > len(head))
        tail_start = total - len(tail)
        gap = None
        if len(head) <= offset < tail_start:
            gap = {"from": offset, "to": tail_start}
            offset = tail_start
        if tail_start == len(head):
            segment = head + tail
            segment_start = 0
        elif offset < len(head):
            segment, segment_start = head, 0
        else:
            segment, segment_start = tail, tail_start
        end = min(segment_start + len(segment), offset + limit)
        data = segment[offset - segment_start:end - segment_start]
        data, offset = self._align_start(data, offset, encoding)
        if gap is not None:
            gap["to"] = offset
        decoder = codecs.getincrementaldecoder(encoding)("replace")
        final = end == segment_start + len(segment) and (complete or end < total)
        text = decoder.decode(data, final=final)
        pending = decoder.getstate()[0]
        next_offset = end - len(pending)
        if next_offset == offset and end < segment_start + len(segment):
            # A caller may choose a byte page smaller than one encoded
            # character. Distinguish this from a live writer that has not yet
            # emitted the remaining bytes, otherwise next_offset loops forever.
            maximum_probe = min(segment_start + len(segment), offset + 8)
            for probe_end in range(end + 1, maximum_probe + 1):
                probe = segment[offset - segment_start:probe_end - segment_start]
                trial = codecs.getincrementaldecoder(encoding)("replace")
                trial.decode(probe, final=complete and probe_end == total)
                if len(probe) > len(trial.getstate()[0]):
                    raise RelayError(
                        "output_page_limit_too_small",
                        "The page limit cannot fit the next encoded character.",
                        details={"minimum_limit": probe_end - offset, "offset": offset},
                    )
        result = {
            **metadata, "encoding": encoding,
            "text": text.removeprefix("\ufeff") if offset == 0 else text,
            "offset": offset, "next_offset": next_offset,
        }
        if gap is not None:
            result["gap"] = gap
        return result


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    """Own a Windows process tree and kill it when the handle is closed."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int | None) -> None:
        self._handle = handle
        self._lock = threading.RLock()

    @classmethod
    def create(cls) -> "WindowsJob":
        if os.name != "nt":
            return cls(None)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise RelayError(
                "job_object_failed",
                "Windows could not create a process-tree containment job.",
                details={"winerror": ctypes.get_last_error()},
            )
        job = cls(int(handle))
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(job._handle),
            cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            job.close()
            raise RelayError(
                "job_object_failed",
                "Windows could not configure process-tree containment.",
                details={"winerror": error},
            )
        return job

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = int(getattr(process, "_handle"))
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle),
            wintypes.HANDLE(process_handle),
        ):
            raise RelayError(
                "job_assignment_failed",
                "The approved process could not be contained in a Windows Job Object.",
                details={"winerror": ctypes.get_last_error()},
            )

    def terminate(self, exit_code: int = 1) -> None:
        with self._lock:
            if self._handle is None:
                return
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            if not kernel32.TerminateJobObject(
                wintypes.HANDLE(self._handle),
                wintypes.UINT(exit_code),
            ):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle = self._handle
            self._handle = None
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(handle))
