"""Shared protocol and validation for Golden Finger for Codex.

The transport intentionally exposes no approval operation. Only the GUI process
may transition a pending request into an approved/running state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import sys
import tempfile
from typing import Any


PROTOCOL_VERSION = 4
PACKAGE_VERSION = "0.2.0"
PACKAGE_BUILD_ID = "source"
PRODUCT_NAME = "GoldenFinger"
ENDPOINT_FILENAME = "endpoint.json"
REQUEST_ID_PREFIX = "gfr_"
CLIENT_INSTANCE_PREFIX = "gfi_"
CLAIM_TOKEN_PREFIX = "gfcap_"
CLAIM_FINGERPRINT_HEX_LENGTH = 12
MAX_WIRE_BYTES = 1_048_576
MAX_PENDING_REQUESTS = 16
MAX_OUTPUT_BYTES = 262_144
MAX_EXECUTABLE_BYTES = 1_073_741_824
MAX_SCRIPT_BYTES = 67_108_864
SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".py", ".pyw"})
MIN_APPROVAL_EXPIRY_SECONDS = 10
DEFAULT_APPROVAL_EXPIRY_SECONDS = 3_600
MAX_APPROVAL_EXPIRY_SECONDS = 36_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 3_600
MAX_COMMAND_TIMEOUT_SECONDS = 36_000
MIN_POLL_WAIT_SECONDS = 10
DEFAULT_POLL_WAIT_SECONDS = 50
MAX_POLL_WAIT_SECONDS = 50
MIN_PENDING_POLL_CYCLES = 1
DEFAULT_PENDING_POLL_CYCLES = 72
MAX_PENDING_POLL_CYCLES = 720
TERMINAL_STATES = {
    "approved",
    "rejected",
    "cancelled",
    "expired",
    "completed",
    "failed",
    "timed_out",
}


class RelayError(Exception):
    """A stable error safe to return across the local protocol."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            result["error"]["details"] = self.details
        return result


def json_ascii(value: Any) -> str:
    """Produce a single-line ASCII JSON wire value."""

    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    encoded = json_ascii(value).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def runtime_build_id() -> str:
    """Identify the exact source/runtime bundle shared by MCP and Broker."""

    if getattr(sys, "frozen", False) or PACKAGE_BUILD_ID != "source":
        return PACKAGE_BUILD_ID
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("relay_protocol.py", "relay_mcp_server.py", "relay_broker.py"):
        path = root / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RelayError(
                "runtime_unreadable",
                f"Golden Finger runtime component {name!r} could not be read.",
            ) from exc
        digest.update(name.encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return "src-" + digest.hexdigest()[:24]


RUNTIME_BUILD_ID = runtime_build_id()


def endpoint_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / PRODUCT_NAME


def endpoint_path() -> Path:
    return endpoint_directory() / ENDPOINT_FILENAME


def new_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def new_client_instance_id() -> str:
    """Return an opaque namespace for one MCP connector process."""

    return CLIENT_INSTANCE_PREFIX + secrets.token_urlsafe(24)


def normalize_client_instance_id(value: Any) -> str:
    """Validate a bounded connector namespace without treating it as task identity."""

    if not isinstance(value, str) or not value.startswith(CLIENT_INSTANCE_PREFIX):
        raise RelayError(
            "invalid_client_instance",
            "client_instance_id is missing or invalid.",
        )
    suffix = value[len(CLIENT_INSTANCE_PREFIX) :]
    if not (24 <= len(suffix) <= 128) or any(
        not (character.isascii() and (character.isalnum() or character in "-_"))
        for character in suffix
    ):
        raise RelayError(
            "invalid_client_instance",
            "client_instance_id is missing or invalid.",
        )
    return value


def new_claim_token() -> str:
    """Return the capability required to observe or cancel one request."""

    return CLAIM_TOKEN_PREFIX + secrets.token_urlsafe(32)


def claim_fingerprint(claim_token: str) -> str:
    """Return a short non-secret correlation value for the relay UI."""

    digest = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
    return digest[:CLAIM_FINGERPRINT_HEX_LENGTH]


def write_endpoint(metadata: dict[str, Any]) -> Path:
    """Atomically publish connection metadata for the current user."""

    directory = endpoint_directory()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / ENDPOINT_FILENAME
    fd, temporary_name = tempfile.mkstemp(
        prefix="endpoint-",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(json_ascii(metadata))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows chmod is best-effort. The security model explicitly treats
            # the token as same-user isolation, not a hard same-user boundary.
            pass
        os.replace(temporary_name, destination)
        return destination
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def read_endpoint() -> dict[str, Any]:
    path = endpoint_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise RelayError(
            "broker_unavailable",
            "The Codex金手指 human-authorization relay window is not running. Launch it manually first.",
        ) from exc
    except OSError as exc:
        raise RelayError(
            "endpoint_unreadable",
            "The relay endpoint metadata could not be read.",
        ) from exc

    if len(raw) > 16_384:
        raise RelayError("invalid_endpoint", "Relay endpoint metadata is too large.")
    try:
        metadata = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("invalid_endpoint", "Relay endpoint metadata is invalid.") from exc
    if not isinstance(metadata, dict):
        raise RelayError("invalid_endpoint", "Relay endpoint metadata is not an object.")

    required = {
        "protocol": int,
        "broker_version": str,
        "broker_build_id": str,
        "host": str,
        "port": int,
        "token": str,
        "pid": int,
        "started_at": str,
    }
    for key, expected_type in required.items():
        if not isinstance(metadata.get(key), expected_type):
            raise RelayError(
                "invalid_endpoint",
                f"Relay endpoint field {key!r} is missing or invalid.",
            )
    if metadata["protocol"] != PROTOCOL_VERSION:
        raise RelayError(
            "protocol_mismatch",
            "The relay window and plugin use different protocol versions.",
        )
    if (
        metadata["broker_version"] != PACKAGE_VERSION
        or metadata["broker_build_id"] != RUNTIME_BUILD_ID
    ):
        raise RelayError(
            "component_mismatch",
            "The Golden Finger plugin and relay window are different builds.",
            details={
                "expected_version": PACKAGE_VERSION,
                "observed_broker_version": metadata["broker_version"],
                "expected_build_id": RUNTIME_BUILD_ID,
                "observed_broker_build_id": metadata["broker_build_id"],
            },
        )
    if metadata["host"] != "127.0.0.1":
        raise RelayError("invalid_endpoint", "Relay endpoint is not loopback-only.")
    if not (1 <= metadata["port"] <= 65535):
        raise RelayError("invalid_endpoint", "Relay endpoint port is invalid.")
    if not (32 <= len(metadata["token"]) <= 256):
        raise RelayError("invalid_endpoint", "Relay endpoint token is invalid.")
    return metadata


def remove_endpoint_if_owned(pid: int, token: str) -> None:
    path = endpoint_path()
    try:
        metadata = json.loads(path.read_text(encoding="ascii"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if (
        isinstance(metadata, dict)
        and metadata.get("pid") == pid
        and isinstance(metadata.get("token"), str)
        and hmac.compare_digest(metadata["token"], token)
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _bounded_text(
    value: Any,
    field: str,
    *,
    minimum: int = 1,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise RelayError("invalid_request", f"{field} must be a string.")
    normalized = value.strip()
    if not (minimum <= len(normalized) <= maximum):
        raise RelayError(
            "invalid_request",
            f"{field} must contain between {minimum} and {maximum} characters.",
        )
    _reject_unsafe_display_characters(normalized, field)
    return normalized


def _reject_unsafe_display_characters(value: str, field: str) -> None:
    """Reject characters that can make displayed text disagree with its order."""

    unsafe_codepoints = {
        0x061C,
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    }
    for character in value:
        codepoint = ord(character)
        if (
            codepoint == 0
            or codepoint == 0x7F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in unsafe_codepoints
            or (codepoint < 0x20 and character not in "\t\n\r")
        ):
            raise RelayError(
                "unsafe_display_text",
                f"{field} contains a control character that cannot be displayed safely.",
            )


def _bounded_integer(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RelayError("invalid_request", f"{field} must be an integer.")
    if not (minimum <= value <= maximum):
        raise RelayError(
            "invalid_request",
            f"{field} must be between {minimum} and {maximum}.",
        )
    return value


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RelayError(
            "invalid_request",
            "The request contains unsupported fields.",
            details={"fields": unknown},
        )


def _resolve_executable(value: Any) -> str:
    requested = _bounded_text(value, "executable", maximum=1_024)
    requested_path = Path(requested)
    if requested_path.is_absolute():
        resolved = requested_path
    elif any(separator in requested for separator in ("/", "\\")):
        raise RelayError(
            "invalid_request",
            "A relative executable containing a path separator is not allowed.",
        )
    else:
        found = shutil.which(requested)
        if not found:
            raise RelayError(
                "executable_not_found",
                f"Executable {requested!r} was not found in the broker PATH.",
            )
        resolved = Path(found)

    try:
        resolved = resolved.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RelayError(
            "executable_not_found",
            f"Executable {requested!r} does not exist.",
        ) from exc
    if not resolved.is_file():
        raise RelayError("invalid_request", "Executable does not resolve to a file.")
    if resolved.suffix.casefold() in {".bat", ".cmd", ".ps1", ".py"}:
        raise RelayError(
            "unsupported_executable",
            "Script files cannot be the executable. Use an explicit interpreter executable and pass the script as an argument.",
        )
    return str(resolved)


def fingerprint_executable(executable: str) -> tuple[int, str]:
    """Return a bounded size and SHA-256 fingerprint for an executable file."""

    path = Path(executable)
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise RelayError(
            "executable_not_found",
            "The resolved executable can no longer be read.",
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise RelayError("invalid_request", "The executable is not a regular file.")
    if file_stat.st_size > MAX_EXECUTABLE_BYTES:
        raise RelayError(
            "unsupported_executable",
            "The executable exceeds the 1 GiB fingerprinting limit.",
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1_048_576)
                if not chunk:
                    break
                digest.update(chunk)
        final_stat = path.stat()
    except OSError as exc:
        raise RelayError(
            "executable_unreadable",
            "The resolved executable could not be fingerprinted.",
        ) from exc
    if (
        final_stat.st_size != file_stat.st_size
        or final_stat.st_mtime_ns != file_stat.st_mtime_ns
        or final_stat.st_ino != file_stat.st_ino
    ):
        raise RelayError(
            "executable_changed",
            "The executable changed while it was being fingerprinted.",
        )
    return file_stat.st_size, digest.hexdigest()


def fingerprint_script(script_path: str) -> tuple[int, str]:
    """Return a bounded size and SHA-256 fingerprint for one direct script input."""

    path = Path(script_path)
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise RelayError(
            "script_not_found",
            "A direct script dependency can no longer be read.",
            details={"path": str(path)},
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise RelayError(
            "invalid_request",
            "A direct script dependency is not a regular file.",
            details={"path": str(path)},
        )
    if file_stat.st_size > MAX_SCRIPT_BYTES:
        raise RelayError(
            "unsupported_script",
            "A direct script dependency exceeds the 64 MiB fingerprinting limit.",
            details={"path": str(path)},
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1_048_576)
                if not chunk:
                    break
                digest.update(chunk)
        final_stat = path.stat()
    except OSError as exc:
        raise RelayError(
            "script_unreadable",
            "A direct script dependency could not be fingerprinted.",
            details={"path": str(path)},
        ) from exc
    if (
        final_stat.st_size != file_stat.st_size
        or final_stat.st_mtime_ns != file_stat.st_mtime_ns
        or final_stat.st_ino != file_stat.st_ino
    ):
        raise RelayError(
            "script_changed",
            "A direct script dependency changed while it was being fingerprinted.",
            details={"path": str(path)},
        )
    return file_stat.st_size, digest.hexdigest()


def _script_argument_candidates(
    executable: str,
    arguments: list[str],
) -> list[tuple[int, str]]:
    """Find direct script argv entries without interpreting a composed shell command."""

    executable_name = Path(executable).name.casefold()
    candidates: list[tuple[int, str]] = []
    if executable_name in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
        for index, argument in enumerate(arguments):
            lowered = argument.casefold()
            if lowered in {"-file", "-f"} and index + 1 < len(arguments):
                candidates.append((index + 1, arguments[index + 1]))
            elif lowered.startswith("-file:") or lowered.startswith("-file="):
                candidates.append((index, argument[6:]))
    elif executable_name in {"python.exe", "pythonw.exe", "python", "pythonw", "py.exe", "py"}:
        for index, argument in enumerate(arguments):
            lowered = argument.casefold()
            if lowered in {"-c", "-m"}:
                break
            if Path(argument).suffix.casefold() in {".py", ".pyw"}:
                candidates.append((index, argument))
                break
    elif executable_name in {"cmd.exe", "cmd"}:
        for index, argument in enumerate(arguments):
            if argument.casefold() in {"/c", "/k"} and index + 1 < len(arguments):
                following = arguments[index + 1]
                if Path(following).suffix.casefold() in {".bat", ".cmd"}:
                    candidates.append((index + 1, following))
                break

    known = {(index, value) for index, value in candidates}
    for index, argument in enumerate(arguments):
        if Path(argument).suffix.casefold() in SCRIPT_EXTENSIONS:
            known.add((index, argument))
    return sorted(known)


def freeze_script_dependencies(
    executable: str,
    arguments: list[str],
    working_directory: str,
) -> list[dict[str, Any]]:
    """Resolve and fingerprint direct script files named by the approved argv."""

    dependencies: list[dict[str, Any]] = []
    working_path = Path(working_directory)
    for argument_index, argument_value in _script_argument_candidates(executable, arguments):
        requested = Path(argument_value)
        candidate = requested if requested.is_absolute() else working_path / requested
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise RelayError(
                "script_not_found",
                "A direct script argument does not exist.",
                details={
                    "argument_index": argument_index,
                    "argument_value": argument_value,
                },
            ) from exc
        if resolved.suffix.casefold() not in SCRIPT_EXTENSIONS:
            continue
        size, sha256 = fingerprint_script(str(resolved))
        dependencies.append(
            {
                "argument_index": argument_index,
                "argument_value": argument_value,
                "path": str(resolved),
                "size": size,
                "sha256": sha256,
            }
        )
    return dependencies


def verify_script_dependencies(
    dependencies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return direct script dependencies whose current bytes differ from the card."""

    mismatches: list[dict[str, Any]] = []
    for dependency in dependencies:
        current_size, current_sha256 = fingerprint_script(str(dependency["path"]))
        if (
            current_size != dependency["size"]
            or current_sha256 != dependency["sha256"]
        ):
            mismatches.append(
                {
                    "path": dependency["path"],
                    "expected_size": dependency["size"],
                    "observed_size": current_size,
                    "expected_sha256": dependency["sha256"],
                    "observed_sha256": current_sha256,
                }
            )
    return mismatches


def _enforce_protected_boundary(executable: str, arguments: list[str]) -> None:
    executable_name = Path(executable).name.casefold()
    if executable_name in {"codex", "codex.exe", "codex.cmd", "codex.com"}:
        raise RelayError(
            "protected_target",
            "The relay is not allowed to launch or control Codex.",
        )

    command_surface = "\n".join(arguments).casefold().replace("/", "\\")
    protected_fragments = {
        "app-server",
        "thread\\resume",
        "turn\\start",
        "state_5.sqlite",
        "session_index.jsonl",
        "\\.codex\\sessions",
        "\\.codex\\rollouts",
        "rollout-",
        "relay_broker.py",
        "relay_mcp_server.py",
        "humanauthorizationrelay\\endpoint.json",
        "--dangerously-bypass-approvals-and-sandbox",
    }
    matched = sorted(fragment for fragment in protected_fragments if fragment in command_surface)
    if matched:
        raise RelayError(
            "protected_target",
            "The request crosses an unsupported Codex/self-control boundary.",
            details={"matched_policy": matched},
        )


def normalize_request(kind: Any, payload: Any) -> dict[str, Any]:
    """Validate, resolve, and freeze a request before it is displayed."""

    if kind not in {"consent", "command"}:
        raise RelayError("invalid_request", "kind must be consent or command.")
    if not isinstance(payload, dict):
        raise RelayError("invalid_request", "payload must be an object.")

    common_fields = {
        "exact_action",
        "target",
        "risk",
        "recovery",
        "expires_in_seconds",
        "idempotency_key",
        "source_label",
        "source_project",
        "source_task",
    }
    kind_fields = (
        {"approved_reply"}
        if kind == "consent"
        else {
            "executable",
            "arguments",
            "working_directory",
            "timeout_seconds",
        }
    )
    _reject_unknown_fields(payload, common_fields | kind_fields)

    frozen: dict[str, Any] = {
        "kind": kind,
        "exact_action": _bounded_text(
            payload.get("exact_action"),
            "exact_action",
            maximum=4_000,
        ),
        "target": _bounded_text(payload.get("target"), "target", maximum=2_000),
        "risk": _bounded_text(payload.get("risk"), "risk", maximum=4_000),
        "recovery": _bounded_text(
            payload.get("recovery"),
            "recovery",
            maximum=4_000,
        ),
        "expires_in_seconds": _bounded_integer(
            payload.get("expires_in_seconds", DEFAULT_APPROVAL_EXPIRY_SECONDS),
            "expires_in_seconds",
            minimum=MIN_APPROVAL_EXPIRY_SECONDS,
            maximum=MAX_APPROVAL_EXPIRY_SECONDS,
        ),
        "idempotency_key": _bounded_text(
            payload.get("idempotency_key"),
            "idempotency_key",
            minimum=8,
            maximum=128,
        ),
        "source_label": _bounded_text(
            payload.get("source_label", "Codex current MCP tool call"),
            "source_label",
            maximum=200,
        ),
        "source_project": _bounded_text(
            payload.get("source_project", "(project not supplied)"),
            "source_project",
            maximum=4_096,
        ),
        "source_task": _bounded_text(
            payload.get("source_task", "(task not supplied)"),
            "source_task",
            maximum=300,
        ),
    }

    if kind == "consent":
        frozen["approved_reply"] = _bounded_text(
            payload.get("approved_reply", "同意"),
            "approved_reply",
            maximum=200,
        )
    else:
        arguments = payload.get("arguments")
        if not isinstance(arguments, list):
            raise RelayError("invalid_request", "arguments must be an array of strings.")
        if len(arguments) > 128:
            raise RelayError("invalid_request", "arguments contains too many entries.")
        normalized_arguments: list[str] = []
        total_argument_characters = 0
        for index, argument in enumerate(arguments):
            if not isinstance(argument, str):
                raise RelayError(
                    "invalid_request",
                    f"arguments[{index}] must be a string.",
                )
            if len(argument) > 8_192:
                raise RelayError(
                    "invalid_request",
                    f"arguments[{index}] is too long.",
                )
            _reject_unsafe_display_characters(argument, f"arguments[{index}]")
            normalized_arguments.append(argument)
            total_argument_characters += len(argument)
        if total_argument_characters > 65_536:
            raise RelayError("invalid_request", "arguments are too large.")

        working_directory = _bounded_text(
            payload.get("working_directory"),
            "working_directory",
            maximum=4_096,
        )
        working_path = Path(working_directory)
        if not working_path.is_absolute():
            raise RelayError(
                "invalid_request",
                "working_directory must be an absolute path.",
            )
        try:
            resolved_working_path = working_path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise RelayError(
                "working_directory_not_found",
                "working_directory does not exist.",
            ) from exc
        if not resolved_working_path.is_dir():
            raise RelayError(
                "invalid_request",
                "working_directory does not resolve to a directory.",
            )

        resolved_executable = _resolve_executable(payload.get("executable"))
        executable_size, executable_sha256 = fingerprint_executable(resolved_executable)
        _enforce_protected_boundary(resolved_executable, normalized_arguments)
        frozen.update(
            {
                "executable": resolved_executable,
                "executable_size": executable_size,
                "executable_sha256": executable_sha256,
                "arguments": normalized_arguments,
                "working_directory": str(resolved_working_path),
                "script_dependencies": freeze_script_dependencies(
                    resolved_executable,
                    normalized_arguments,
                    str(resolved_working_path),
                ),
                "timeout_seconds": _bounded_integer(
                    payload.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS),
                    "timeout_seconds",
                    minimum=1,
                    maximum=MAX_COMMAND_TIMEOUT_SECONDS,
                ),
            }
        )

    # This hash is what the user sees. Nothing executable is added later.
    frozen["request_hash"] = canonical_hash(frozen)
    return frozen


class RelayClient:
    """Small authenticated client for the loopback broker."""

    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = metadata or read_endpoint()

    def call(self, operation: str, **parameters: Any) -> dict[str, Any]:
        wait_seconds = parameters.get("wait_seconds", 0)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            wait_seconds = 0
        socket_timeout = (
            min(float(MAX_POLL_WAIT_SECONDS) + 5.0, 60.0)
            if operation == "wait" and float(wait_seconds) > 0
            else 3.0
        )
        request = {
            "protocol": PROTOCOL_VERSION,
            "client_version": PACKAGE_VERSION,
            "client_build_id": RUNTIME_BUILD_ID,
            "token": self.metadata["token"],
            "operation": operation,
            **parameters,
        }
        encoded = (json_ascii(request) + "\n").encode("ascii")
        if len(encoded) > MAX_WIRE_BYTES:
            raise RelayError("request_too_large", "Relay request exceeds the wire limit.")

        try:
            with socket.create_connection(
                (self.metadata["host"], self.metadata["port"]),
                timeout=socket_timeout,
            ) as connection:
                connection.settimeout(socket_timeout)
                connection.sendall(encoded)
                connection.shutdown(socket.SHUT_WR)
                chunks = bytearray()
                while True:
                    chunk = connection.recv(65_536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_WIRE_BYTES:
                        raise RelayError(
                            "response_too_large",
                            "Relay response exceeds the wire limit.",
                        )
        except RelayError:
            raise
        except (ConnectionError, OSError, socket.timeout) as exc:
            raise RelayError(
                "broker_unavailable",
                "The Codex金手指 human-authorization relay window could not be reached.",
            ) from exc

        try:
            response = json.loads(bytes(chunks).decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelayError(
                "invalid_response",
                "The relay returned an invalid response.",
            ) from exc
        if not isinstance(response, dict):
            raise RelayError("invalid_response", "The relay response is not an object.")
        if response.get("ok") is False:
            error = response.get("error")
            if isinstance(error, dict):
                raise RelayError(
                    str(error.get("code", "relay_error")),
                    str(error.get("message", "Relay operation failed.")),
                    details=error.get("details")
                    if isinstance(error.get("details"), dict)
                    else None,
                )
            raise RelayError("relay_error", "Relay operation failed.")
        return response


def broker_is_reachable() -> bool:
    try:
        response = RelayClient().call("health")
    except RelayError:
        return False
    return response.get("ok") is True
