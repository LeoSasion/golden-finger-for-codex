"""Current-user sealed submission receipts, partitioned by local client scope.

No command, argument, output, project name or thread ID is stored. The scope is
only a lookup partition; the random recovery capability grants recovery access.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

from relay_protocol import RelayError, endpoint_directory, new_client_instance_id

MAX_JOURNAL_ENTRIES = 256
MAX_JOURNAL_BYTES = 1_048_576
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, entropy: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("Current-user DPAPI protection is unavailable on this platform")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    data_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(entropy)
    incoming = _Blob(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    optional_entropy = _Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(incoming), None, ctypes.byref(optional_entropy), None, None, 1, ctypes.byref(outgoing))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(incoming), "Golden Finger submission recovery", ctypes.byref(optional_entropy), None, None, 1, ctypes.byref(outgoing))
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise OSError(ctypes.get_last_error(), "Current-user recovery protection failed")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, wintypes.HLOCAL))


class RecoveryJournal:
    def __init__(self, scope: str, *, root: Path | None = None):
        self.partition = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self.root = root if root is not None else endpoint_directory() / "recovery"
        self.path = self.root / (self.partition + ".sealed")
        self.entropy = ("GoldenFingerRecoveryV1:" + self.partition).encode("ascii")
        with _LOCKS_LOCK:
            self._lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock_path = self.path.with_suffix(".lock")
                with lock_path.open("a+b") as lock_file:
                    if lock_file.tell() == 0:
                        lock_file.write(b"0")
                        lock_file.flush()
                    if os.name == "nt":
                        import msvcrt
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        if os.name == "nt":
                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except RelayError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise RelayError(
                    "recovery_storage_unavailable",
                    "The protected submission journal is unavailable.",
                ) from exc

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "namespace": new_client_instance_id(), "entries": {}}
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ValueError("Recovery journal exceeds its storage limit")
        data = json.loads(_dpapi(base64.b64decode(raw, validate=True), self.entropy, decrypt=True))
        if data.get("version") != 1 or not isinstance(data.get("entries"), dict) or not isinstance(data.get("namespace"), str):
            raise ValueError("Invalid recovery journal")
        return data

    def _write(self, data):
        plaintext = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        sealed = base64.b64encode(_dpapi(plaintext, self.entropy))
        if len(sealed) > MAX_JOURNAL_BYTES:
            raise ValueError("Recovery journal exceeds its storage limit")
        descriptor, temporary = tempfile.mkstemp(prefix="receipt-", suffix=".tmp", dir=self.root)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(sealed)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def prepare(self, payload_hash: str):
        with self._transaction():
            data = self._read()
            entry = data["entries"].get(payload_hash)
            if entry is not None:
                if entry.get("phase") == "not_submitted":
                    entry.update(phase="prepared", updated_at=time.time())
                    self._write(data)
                    return data["namespace"], dict(entry), True
                return data["namespace"], dict(entry), False
            while len(data["entries"]) >= MAX_JOURNAL_ENTRIES:
                finished = [key for key, value in data["entries"].items() if value.get("phase") in {"terminal", "not_submitted"}]
                if not finished:
                    raise RelayError("recovery_storage_full", "Unresolved recovery receipts fill the journal; no submission was sent.", details={"submission_outcome": "not_submitted"})
                oldest = min(finished, key=lambda key: data["entries"][key].get("updated_at", 0))
                del data["entries"][oldest]
            entry = {"payload_hash": payload_hash, "recovery_token": "gfrc_" + secrets.token_urlsafe(32), "phase": "prepared", "updated_at": time.time()}
            data["entries"][payload_hash] = entry
            self._write(data)
            return data["namespace"], dict(entry), True

    def observe(self, request_id: str, claim_token: str, *, terminal: bool,
                recovery_token: str | None = None, payload_hash: str | None = None):
        """Update only an existing receipt in this caller's partition."""
        if not self.path.exists():
            return
        with self._transaction():
            data = self._read()
            changed = False
            for key, entry in data["entries"].items():
                bound_claim = entry.get("request_id") == request_id and secrets.compare_digest(entry.get("claim_token", ""), claim_token)
                recovered = payload_hash == key and recovery_token is not None and secrets.compare_digest(entry.get("recovery_token", ""), recovery_token)
                if bound_claim or recovered:
                    entry.update(request_id=request_id, claim_token=claim_token,
                                 phase="terminal" if terminal or entry.get("phase") == "terminal" else "submitted", updated_at=time.time())
                    changed = True
            if changed:
                self._write(data)

    def record(self, payload_hash: str, *, phase: str, request_id=None, claim_token=None):
        with self._transaction():
            data = self._read()
            entry = data["entries"][payload_hash]
            if entry.get("phase") != "terminal":
                entry["phase"] = phase
            entry["updated_at"] = time.time()
            if request_id is not None:
                entry["request_id"] = request_id
            if claim_token is not None:
                entry["claim_token"] = claim_token
            self._write(data)


def recovery_journal(codex_thread_id: str | None, process_scope: str) -> RecoveryJournal:
    # Host-provided thread metadata partitions lookups, never authorizes a read.
    scope = "thread:" + codex_thread_id if codex_thread_id else "process:" + process_scope
    return RecoveryJournal(scope)
