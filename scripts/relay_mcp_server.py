"""STDIO MCP connector for Codex金手指 (Golden Finger for Codex).

The connector never launches the broker. It only submits, waits, reads, or
cancels requests over the authenticated loopback protocol.
"""

from __future__ import annotations

import sys

# Source connectors run from a manifest-verified installation. Imports must not
# create unlisted __pycache__ files inside that immutable release directory.
sys.dont_write_bytecode = True

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, BinaryIO, Callable

from codex_lifecycle import extract_codex_thread_id, normalize_codex_thread_id
from relay_recovery import recovery_journal
from relay_protocol import (
    CLAIM_TOKEN_PREFIX,
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_APPROVAL_EXPIRY_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_POLL_WAIT_SECONDS,
    MAX_WIRE_BYTES,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    REQUEST_ID_PREFIX,
    RUNTIME_BUILD_ID,
    TERMINAL_STATES,
    RelayClient,
    RelayError,
    fit_wire_response,
    json_ascii,
    new_client_instance_id,
    canonical_hash,
    validate_recovery_token,
)


SERVER_NAME = "golden-finger"
SERVER_VERSION = PACKAGE_VERSION
SERVER_BUILD_ID = RUNTIME_BUILD_ID
CLIENT_INSTANCE_ID = new_client_instance_id()
DEFAULT_MCP_WAIT_SECONDS = 30
MAX_TRACKED_RPC_REQUESTS = 128
MAX_CONCURRENT_RPC_REQUESTS = 16
MAX_CONCURRENT_CONTROL_REQUESTS = 2
CANCELLATION_ACK_TIMEOUT_SECONDS = 5.0
PROGRESS_INTERVAL_SECONDS = 2.0
CONTROL_TOOLS = frozenset({"cancel_authorization", "terminate_command", "relay_health", "recover_authorization", "read_command_output"})
_RPC_REQUEST_CLAIMS: OrderedDict[str, tuple[str, str]] = OrderedDict()
_RECEIPT_JOURNALS: OrderedDict[tuple[str, str], Any] = OrderedDict()
_RPC_STATE_LOCK = threading.RLock()


@dataclass
class _RpcCallState:
    cancelled: threading.Event = field(default_factory=threading.Event)
    cancellation_finished: threading.Event = field(default_factory=threading.Event)
    claim: tuple[str, str] | None = None
    cancellation_outcome: str = "not_submitted"
    cancellation_state: str | None = None
    cancellation_started: bool = False
    progress_token: str | int | None = None
    progress_started: float = field(default_factory=time.monotonic)
    progress_phase: str = "starting"
    progress_last_phase: str = ""
    progress_last_sent: float = 0.0
    progress_last_value: float = -1.0
    progress_owner: object | None = None
    receipt_journal: Any = None


_ACTIVE_RPC_CALLS: dict[str, _RpcCallState] = {}
SUPPORTED_MCP_PROTOCOLS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_INSTRUCTIONS = (
    "Codex金手指是面向 Yes工程师的人机授权中继，只请求一次、精确、可见的人类决定。它不替代 Codex 原生审批或沙箱，"
    "不提供 standing consent，也不得用于启动/控制 Codex、App Server、任务、数据库或 "
    "rollout。首次采用时必须先在普通对话中约定金手指的使用作用域；作用域只决定何时路由，"
    "绝不是动作授权。窗口必须由用户独立启动；每次准备提交前先检查 relay_health。不可达时"
    "失败关闭，并让用户选择启动窗口后保留作用域，或取消作用域；不得自动启动、不得把取消"
    "作用域解释为批准或拒绝当前动作。request_decision 按完整语义归一化普通是/否和有序选项问询，"
    "不要求源参数是布尔类型，也不依赖关键词；同意选择原始第一项。answered 的布尔 false 也是有效答案。"
    "不能接管已经显示的宿主卡片。request_consent 返回当前工具"
    "调用里的文本决定；request_command_execution 经窗口手动或配置的自动审批后以 shell=False 执行"
    "冻结的 executable+arguments。中继是拉取式机制，不会唤醒已经结束的 Codex 回合。"
    "pending/running 是非终态：必须保持当前回合，逐字复用 polling.next_arguments "
    "wait_for_authorization，直到 answered/completed/approved/rejected/cancelled/expired/failed/"
    "timed_out/terminated；活动状态下不得返回最终答复、不得让用户额外发送‘已完成’，也不要以新 key "
    "重提或把 request_id/claim_token 转交其他任务。request_id 仅用于定位，claim_token 才是"
    "当前请求的认领凭据。中继只返回自身启动的命令结果，不能观察由其他终端或服务启动的进程；"
    "这不限制它在精确一次性批准后自行启动允许的有界测试或测试 supervisor。"
)


def _scope_recovery(reason_code: str) -> dict[str, Any]:
    """Return recovery choices without claiming persistent broker-side scope state."""

    return {
        "required": True,
        "reason_code": reason_code,
        "scope_kind": "routing_preference_only",
        "scope_is_authorization": False,
        "must_ask_user": True,
        "background_monitoring_supported": False,
        "choices": [
            {
                "id": "start_window",
                "label": "启动金手指窗口并继续作用域",
                "effect": (
                    "The user launches the independent window, then Codex calls "
                    "relay_health again before creating any new request."
                ),
            },
            {
                "id": "cancel_scope",
                "label": "取消金手指作用域",
                "effect": (
                    "Stop routing future eligible actions through Golden Finger in the "
                    "agreed scope. This neither approves nor rejects the current action."
                ),
            },
        ],
        "launch_guidance": [
            "Open the user-created Codex金手指 desktop or Start menu shortcut.",
            (
                "If no shortcut exists, open scripts\\launch_relay.pyw from the installed "
                "or source plugin directory; in development, run python "
                "scripts\\relay_broker.py from an ordinary unelevated terminal."
            ),
        ],
        "old_request_resumable_after_restart": False,
        "do_not_resubmit_without_a_fresh_user_choice": True,
    }


def _scope_recovery_for_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    result = snapshot.get("result")
    if (
        snapshot.get("state") == "cancelled"
        and isinstance(result, dict)
        and result.get("reason_code") == "relay_window_closed"
    ):
        return _scope_recovery("relay_window_closed")
    return None


def _error_payload(exc: RelayError) -> dict[str, Any]:
    details = dict(exc.details)
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            **({"details": details} if details else {}),
        },
    }
    recovery_reason = {
        "broker_unavailable": "relay_window_unavailable",
        "broker_stopping": "relay_window_closing",
    }.get(exc.code)
    if recovery_reason is not None:
        payload["scope_recovery"] = _scope_recovery(recovery_reason)
    return payload


COMMON_PROPERTIES: dict[str, Any] = {
    "exact_action": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000,
        "description": "The one exact action this approval would authorize.",
    },
    "target": {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
        "description": "Exact process, file, task, or object affected.",
    },
    "risk": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000,
        "description": "Concrete impact, interruption risk, and important side effects.",
    },
    "recovery": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000,
        "description": "Concrete rollback or recovery path.",
    },
    "expires_in_seconds": {
        "type": "integer",
        "minimum": 30,
        "maximum": MAX_APPROVAL_EXPIRY_SECONDS,
        "default": DEFAULT_APPROVAL_EXPIRY_SECONDS,
        "description": (
            "Optional shorter pending validity. The fixed 9,000-second relay policy "
            "is the authoritative cap for new requests."
        ),
    },
    "wait_seconds": {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_POLL_WAIT_SECONDS,
        "default": DEFAULT_MCP_WAIT_SECONDS,
        "description": (
            "Zero reads immediately; positive values wait up to that many seconds. "
            "The total pending deadline is independent of polling frequency."
        ),
    },
    "idempotency_key": {
        "type": "string",
        "minLength": 8,
        "maxLength": 128,
        "description": "Fresh opaque key. Reuse only for the identical frozen request.",
    },
    "source_label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "default": "Codex current MCP tool call",
        "description": "Human-readable origin label; it is not an authenticated task identity.",
    },
    "source_project": {
        "type": "string",
        "minLength": 1,
        "maxLength": 4096,
        "description": (
            "Human-readable Codex project root, or '(projectless)'. This is displayed "
            "for cross-project disambiguation and is not an authenticated identity."
        ),
    },
    "source_task": {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": (
            "Human-readable current Codex task title or stable user-facing label. "
            "This is displayed for disambiguation and is not authenticated."
        ),
    },
}
COMMON_REQUIRED = [
    "exact_action",
    "target",
    "risk",
    "recovery",
    "idempotency_key",
    "source_label",
    "source_project",
    "source_task",
]


TOOLS: list[dict[str, Any]] = [
    {
        "name": "relay_health",
        "title": "检查 Codex金手指窗口",
        "description": (
            "Check whether the separately launched local relay window is reachable. "
            "Call this when a Golden Finger scope starts and before each new request. "
            "This never launches the window or changes a request. If the window is not "
            "running, the result returns start-window or cancel-scope choices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "request_consent",
        "title": "请求一次自然语言授权",
        "description": (
            "Display one exact natural-language consent request in the independent "
            "window. Approval by a GUI click or the window's configured automatic "
            "timer executes no program and returns approved_reply to this MCP tool call. "
            "Check receipt.approval_mode: automatic is not evidence of a human click. "
            "This is not a native Codex approval. A pending "
            "result requires same-turn polling until terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_PROPERTIES,
                "approved_reply": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "default": "同意",
                    "description": "Exact text returned after manual or window-configured automatic approval.",
                },
            },
            "required": COMMON_REQUIRED,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "request_decision",
        "title": "转发一次语义确认或选项问询",
        "description": (
            "Route an ordinary user question by its meaning, never by keyword or the source card's schema. "
            "Normalize a semantic yes/no confirmation to boolean; normalize any ordered options to single_select. "
            "Keep the full original question and option order. A GUI agreement or configured automatic timer "
            "returns true or the FIRST original option, regardless of recommended labels. Boolean rejection "
            "answers false; single_select rejection chooses nothing. Stop cancels without an answer. "
            "An answered result records only this question's answer and executes no program. "
            "Read receipt.approval_mode: automatic is not evidence of a human click. "
            "This does not intercept existing host cards or replace native Codex permission approvals. "
            "Pending results require same-turn polling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_PROPERTIES,
                "exact_action": {"type": "string", "minLength": 1, "maxLength": 4000,
                                 "description": "Full original question, including negation and scope. This is not a command to execute."},
                "decision_type": {"type": "string", "enum": ["boolean", "single_select"],
                                  "description": "Internal normalization of the question's semantics; does not require a boolean source parameter."},
                "question_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "true_label": {"type": "string", "minLength": 1, "maxLength": 200, "default": "是"},
                "false_label": {"type": "string", "minLength": 1, "maxLength": 200, "default": "否"},
                "options": {"type": "array", "minItems": 1, "maxItems": 20,
                            "description": "Original options in original order. Never sort by recommendation or wording.",
                            "items": {"type": "object", "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                                "label": {"type": "string", "minLength": 1, "maxLength": 500},
                                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                                "recommended": {"type": "boolean", "default": False},
                            }, "required": ["id", "label"], "additionalProperties": False}},
            },
            "required": [*COMMON_REQUIRED, "decision_type"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "request_command_execution",
        "title": "请求执行一次精确命令",
        "description": (
            "Display and, after a GUI click or the window's configured automatic timer, "
            "execute an exact local "
            "executable plus argument array with shell=False. The broker rejects "
            "Codex/self-control targets and retains bounded output. A pending/running "
            "result requires same-turn polling until terminal. "
            "Check receipt.approval_mode: automatic is not evidence of a human click."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **COMMON_PROPERTIES,
                "executable": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1024,
                    "description": "Absolute executable path or a PATH-resolvable program name.",
                },
                "arguments": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "string",
                        "maxLength": 8192,
                    },
                    "description": "Exact argument vector. Do not provide a shell-composed argv.",
                },
                "working_directory": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
                    "description": "Existing absolute working directory.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                    "default": DEFAULT_COMMAND_TIMEOUT_SECONDS,
                    "description": (
                        "Optional shorter process timeout after approval. The fixed "
                        "9,000-second relay policy is the authoritative cap."
                    ),
                },
                "environment": {"type": "object", "maxProperties": 64, "additionalProperties": {"type": "string"}},
                "output_encoding": {"type": "string", "default": "auto", "description": "Output encoding hint: auto, utf-8, utf-16, cp936, or another supported codec."},
                "response_files": {"type": "array", "maxItems": 128, "items": {"type": "string"}, "description": "Explicit @path response-file arguments to freeze and verify."},
            },
            "required": [
                *COMMON_REQUIRED,
                "executable",
                "arguments",
                "working_directory",
            ],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "wait_for_authorization",
        "title": "等待已有授权请求",
        "description": (
            "Read or wait for an existing relay request. It cannot approve anything. "
            "Reuse the original request_id plus claim_token instead of submitting a "
            "duplicate. request_id alone is insufficient. If state is still pending/running, "
            "keep this Codex turn open and follow polling.next_arguments exactly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": f"^{REQUEST_ID_PREFIX}[A-Za-z0-9]+$",
                    "description": "Weak request locator; insufficient without claim_token.",
                },
                "claim_token": {
                    "type": "string",
                    "minLength": 32,
                    "maxLength": 256,
                    "pattern": f"^{CLAIM_TOKEN_PREFIX}[A-Za-z0-9_-]+$",
                    "description": (
                        "Per-request capability copied exactly from polling.next_arguments. "
                        "Never reconstruct or transfer it to another task."
                    ),
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_POLL_WAIT_SECONDS,
                    "default": DEFAULT_MCP_WAIT_SECONDS,
                    "description": (
                        "Zero reads immediately; positive values are bounded per-call seconds."
                    ),
                },
            },
            "required": ["request_id", "claim_token"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "cancel_authorization",
        "title": "取消待确认请求",
        "description": (
            "Cancel only a still-pending relay request. It cannot revoke an approval "
            "or undo a process that already started. Both request_id and its exact "
            "claim_token are required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": f"^{REQUEST_ID_PREFIX}[A-Za-z0-9]+$",
                    "description": "Weak request locator; insufficient without claim_token.",
                },
                "claim_token": {
                    "type": "string",
                    "minLength": 32,
                    "maxLength": 256,
                    "pattern": f"^{CLAIM_TOKEN_PREFIX}[A-Za-z0-9_-]+$",
                    "description": "Per-request capability returned for this exact request.",
                },
            },
            "required": ["request_id", "claim_token"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


TOOLS.append(
    {
        "name": "terminate_command",
        "title": "终止已启动的命令",
        "description": (
            "Explicitly terminate this relay request's queued or running command and "
            "its contained process tree. Requires its exact claim_token. Already produced "
            "side effects are not undone. This is separate from cancelling a pending "
            "authorization or stopping an MCP wait."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "pattern": f"^{REQUEST_ID_PREFIX}[A-Za-z0-9]+$"},
                "claim_token": {
                    "type": "string", "minLength": 32, "maxLength": 256,
                    "pattern": f"^{CLAIM_TOKEN_PREFIX}[A-Za-z0-9_-]+$",
                },
            },
            "required": ["request_id", "claim_token"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": True, "openWorldHint": False,
        },
    }
)


_command_properties = next(tool for tool in TOOLS if tool["name"] == "request_command_execution")["inputSchema"]["properties"]
TOOLS.extend([
    {
        "name": "preflight_command", "title": "检查命令兼容性",
        "description": "Inspect executable resolution, arguments, environment overrides and encoding without submitting or executing a command.",
        "inputSchema": {"type": "object", "properties": {
            key: _command_properties[key] for key in ("executable", "arguments", "working_directory", "environment", "output_encoding", "response_files")
        }, "required": ["executable", "arguments", "working_directory"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recover_authorization", "title": "恢复已有请求回执",
        "description": "Recover an already submitted request using its random recovery capability and exact payload hash. Never submits or re-executes a command; not-found is not permission to replay.",
        "inputSchema": {"type": "object", "properties": {
            "recovery_token": {"type": "string", "pattern": "^gfrc_[A-Za-z0-9_-]+$", "minLength": 40, "maxLength": 128},
            "payload_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        }, "required": ["recovery_token", "payload_hash"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "read_command_output", "title": "分页读取命令输出",
        "description": "Read bounded output using the request's exact claim. Offsets are raw byte cursors; follow next_offset and retained_start_offset when a gap is reported.",
        "inputSchema": {"type": "object", "properties": {
            "request_id": {"type": "string", "pattern": f"^{REQUEST_ID_PREFIX}[A-Za-z0-9]+$"},
            "claim_token": {"type": "string", "minLength": 32, "maxLength": 256},
            "stream": {"type": "string", "enum": ["stdout", "stderr"], "default": "stdout"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 16384},
        }, "required": ["request_id", "claim_token"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
])


def mcp_tool_result(
    structured: dict[str, Any],
    *,
    is_error: bool = False,
    rpc_request_id: Any = None,
) -> dict[str, Any]:
    # MCP carries the same data as text and structuredContent. Leave space for
    # both copies and the outer JSON-RPC envelope after Unicode escaping.
    budget = (MAX_WIRE_BYTES - 1_024) // 3
    while True:
        bounded = fit_wire_response(structured, max_bytes=budget)
        result = {
            "content": [{"type": "text", "text": json.dumps(bounded, ensure_ascii=False, indent=2)}],
            "structuredContent": bounded,
            "isError": is_error or bounded.get("ok") is False,
        }
        size = len(json_ascii({"jsonrpc": "2.0", "id": rpc_request_id, "result": result})) + 1
        if size <= MAX_WIRE_BYTES:
            return result
        # Check the actual two-copy frame, including escaping, indentation and
        # the request ID. The initial division is only a conservative estimate.
        next_budget = max(1_024, budget - (size - MAX_WIRE_BYTES) - 256)
        if next_budget == budget:
            raise RelayError("response_too_large", "MCP response metadata exceeded the frame budget.")
        budget = next_budget


def _required_claim_token(arguments: dict[str, Any]) -> str:
    claim_token = arguments.get("claim_token")
    if (
        not isinstance(claim_token, str)
        or not claim_token.isascii()
        or not claim_token.startswith(CLAIM_TOKEN_PREFIX)
        or not (32 <= len(claim_token) <= 256)
    ):
        raise RelayError(
            "claim_mismatch",
            "The relay request is not claimed by this caller.",
        )
    return claim_token


def _rpc_request_key(rpc_request_id: Any) -> str | None:
    if isinstance(rpc_request_id, bool) or not isinstance(
        rpc_request_id,
        (str, int, float),
    ):
        return None
    if isinstance(rpc_request_id, float) and not math.isfinite(rpc_request_id):
        return None
    return json_ascii(rpc_request_id)


def _remember_rpc_claim(
    rpc_request_id: Any,
    relay_request_id: str,
    claim_token: str,
) -> None:
    key = _rpc_request_key(rpc_request_id)
    if key is None:
        return
    with _RPC_STATE_LOCK:
        state = _ACTIVE_RPC_CALLS.get(key)
        if state is not None:
            state.claim = (relay_request_id, claim_token)
        cancelled = state is not None and state.cancelled.is_set()
        should_cancel = cancelled and not state.cancellation_started
        if should_cancel:
            state.cancellation_started = True
        if not cancelled:
            _RPC_REQUEST_CLAIMS[key] = (relay_request_id, claim_token)
            _RPC_REQUEST_CLAIMS.move_to_end(key)
            while len(_RPC_REQUEST_CLAIMS) > MAX_TRACKED_RPC_REQUESTS:
                _RPC_REQUEST_CLAIMS.popitem(last=False)
    if should_cancel:
        # A cancellation can arrive while submit is in flight, before we know
        # the claim. Bind its original capability before attempting cancellation.
        _cancel_claim((relay_request_id, claim_token))


def _forget_rpc_claim(rpc_request_id: Any) -> None:
    key = _rpc_request_key(rpc_request_id)
    if key is not None:
        with _RPC_STATE_LOCK:
            _RPC_REQUEST_CLAIMS.pop(key, None)


def _forget_request_claim(relay_request_id: str, claim_token: str) -> None:
    with _RPC_STATE_LOCK:
        stale = [
            key
            for key, value in _RPC_REQUEST_CLAIMS.items()
            if value == (relay_request_id, claim_token)
        ]
        for key in stale:
            _RPC_REQUEST_CLAIMS.pop(key, None)


def _cancellation_event(rpc_request_id: Any) -> threading.Event | None:
    with _RPC_STATE_LOCK:
        state = _ACTIVE_RPC_CALLS.get(_rpc_request_key(rpc_request_id))
        return state.cancelled if state is not None else None


def _check_cancelled(rpc_request_id: Any) -> None:
    event = _cancellation_event(rpc_request_id)
    if event is not None and event.is_set():
        raise _cancelled_error(rpc_request_id)


def _cancelled_error(rpc_request_id: Any) -> RelayError:
    with _RPC_STATE_LOCK:
        state = _ACTIVE_RPC_CALLS.get(_rpc_request_key(rpc_request_id))
    details: dict[str, Any] = {}
    if state is not None and state.claim is not None:
        # Interrupting the socket only stops waiting. Do not return until the
        # separate pending-cancel attempt completes, and report its actual result.
        acknowledged = state.cancellation_finished.wait(CANCELLATION_ACK_TIMEOUT_SECONDS)
        details = {
            "request_id": state.claim[0],
            "claim_token": state.claim[1],
            "broker_cancellation": state.cancellation_outcome if acknowledged else "ack_timeout",
            "state": state.cancellation_state,
        }
    return RelayError(
        "request_cancelled",
        "The MCP wait was stopped. Check broker_cancellation; running commands are not terminated.",
        details=details,
    )


def _sync_rpc_claim(
    rpc_request_id: Any,
    snapshot: dict[str, Any],
    claim_token: str,
) -> None:
    with _RPC_STATE_LOCK:
        state = _ACTIVE_RPC_CALLS.get(_rpc_request_key(rpc_request_id))
        if state is not None:
            state.progress_phase = str(snapshot.get("execution_phase") or snapshot.get("state") or "waiting")
    relay_request_id = snapshot.get("request_id")
    if not isinstance(relay_request_id, str):
        _forget_rpc_claim(rpc_request_id)
        return
    if snapshot.get("state") in {"pending", "running"}:
        _remember_rpc_claim(rpc_request_id, relay_request_id, claim_token)
    else:
        _forget_request_claim(relay_request_id, claim_token)


def _take_cancelled_claim(message: dict[str, Any]) -> tuple[str, str] | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    key = _rpc_request_key(params.get("requestId"))
    if key is None:
        return None
    with _RPC_STATE_LOCK:
        state = _ACTIVE_RPC_CALLS.get(key)
        claimed = _RPC_REQUEST_CLAIMS.pop(key, None)
        if state is not None:
            state.cancelled.set()
            # The bounded replay map may have evicted this key, or another
            # waiter may have observed a terminal result and cleared it. The
            # active RPC still owns its exact capability until it finishes.
            claimed = state.claim or claimed
            if claimed is not None:
                if state.cancellation_started:
                    return None
                state.cancellation_started = True
        return claimed


def _finish_cancel_claim(claimed: tuple[str, str], outcome: str, request_state: str | None) -> None:
    with _RPC_STATE_LOCK:
        for state in _ACTIVE_RPC_CALLS.values():
            if state.claim == claimed and state.cancelled.is_set():
                state.cancellation_outcome = outcome
                state.cancellation_state = request_state
                state.cancellation_finished.set()


def _cancel_claim(claimed: tuple[str, str]) -> None:
    relay_request_id, claim_token = claimed
    outcome = "unavailable"
    request_state = None
    try:
        result = RelayClient().call(
            "cancel",
            request_id=relay_request_id,
            claim_token=claim_token,
            client_instance_id=CLIENT_INSTANCE_ID,
        )
        outcome = "acknowledged"
        request_state = result.get("request", {}).get("state")
        with _RPC_STATE_LOCK:
            journals = [state.receipt_journal for state in _ACTIVE_RPC_CALLS.values()
                        if state.claim == claimed and state.receipt_journal is not None]
            cached_journal = _RECEIPT_JOURNALS.pop(claimed, None)
            if cached_journal is not None and cached_journal not in journals:
                journals.append(cached_journal)
        for journal in journals:
            _observe_receipt(result, result.get("request", {}), claim_token, None, journal=journal)
    except RelayError as exc:
        # Cancellation is best effort. An approval may already have started a
        # command, or the broker may have become unavailable in the meantime.
        outcome = exc.code
    except Exception:
        outcome = "internal_error"
    finally:
        _finish_cancel_claim(claimed, outcome, request_state)


def _handle_cancelled_notification(message: dict[str, Any]) -> None:
    claimed = _take_cancelled_claim(message)
    if claimed is not None:
        _cancel_claim(claimed)


def _bounded_wait(arguments: dict[str, Any]) -> int:
    wait = arguments.get("wait_seconds", DEFAULT_MCP_WAIT_SECONDS)
    if isinstance(wait, bool) or not isinstance(wait, int):
        raise RelayError("invalid_wait", "wait_seconds must be an integer.")
    if not 0 <= wait <= MAX_POLL_WAIT_SECONDS:
        raise RelayError("invalid_wait", "wait_seconds must be between zero and 900.")
    return wait


def _polling_directive(
    snapshot: dict[str, Any],
    claim_token: str,
    wait_seconds: int | None = None,
) -> dict[str, Any]:
    state = snapshot.get("state")
    active = state in {"pending", "running"}
    policy = snapshot.get("poll_policy")
    if not isinstance(policy, dict):
        policy = {}
    configured_wait = wait_seconds if wait_seconds is not None else DEFAULT_MCP_WAIT_SECONDS
    if isinstance(configured_wait, bool) or not isinstance(configured_wait, int):
        configured_wait = DEFAULT_MCP_WAIT_SECONDS
    configured_wait = max(0, min(configured_wait, MAX_POLL_WAIT_SECONDS))
    directive: dict[str, Any] = {
        "terminal": not active,
        "must_continue_current_turn": active,
    }
    if active:
        directive.update(
            {
                "mechanism": "pull",
                "next_tool": "wait_for_authorization",
                "next_arguments": {
                    "request_id": snapshot.get("request_id"),
                    "claim_token": claim_token,
                    "wait_seconds": configured_wait,
                },
                "policy_revision": policy.get("policy_revision"),
                "pending_polls_used": policy.get("pending_polls_used"),
                "expiry_basis": policy.get("expiry_basis", "monotonic_deadline"),
                "remaining_seconds": policy.get("remaining_seconds"),
                "repeat_until_state": sorted(TERMINAL_STATES),
                "do_not_wait_for_user_message": True,
                "do_not_resubmit": True,
            }
        )
    return directive


def _request_and_wait(
    kind: str,
    arguments: dict[str, Any],
    *,
    rpc_request_id: Any = None,
    codex_thread_id: str | None = None,
) -> dict[str, Any]:
    _check_cancelled(rpc_request_id)
    wait_seconds = _bounded_wait(arguments)
    payload = {key: value for key, value in arguments.items() if key != "wait_seconds"}
    client = RelayClient()
    payload_hash = canonical_hash({"kind": kind, "payload": payload})
    journal = recovery_journal(codex_thread_id, CLIENT_INSTANCE_ID)
    try:
        namespace, entry, first_submission = journal.prepare(payload_hash)
    except RelayError as exc:
        raise RelayError(exc.code, exc.message, details={**exc.details, "submission_outcome": "not_submitted"}) from exc
    with _RPC_STATE_LOCK:
        active = _ACTIVE_RPC_CALLS.get(_rpc_request_key(rpc_request_id))
        if active is not None:
            active.receipt_journal = journal
    recovery = {"recovery_token": entry["recovery_token"], "payload_hash": payload_hash}
    recovery_details = {
        **recovery, "do_not_resubmit": True,
        "recovery_tool": "recover_authorization", "recovery_arguments": recovery,
    }
    try:
        _check_cancelled(rpc_request_id)
    except RelayError:
        if first_submission:
            try:
                journal.record(payload_hash, phase="not_submitted")
            except RelayError:
                pass
        raise
    try:
        if first_submission:
            submitted = client.call(
                "submit", kind=kind, payload=payload, client_instance_id=namespace,
                codex_thread_id=codex_thread_id, **recovery,
            )
        else:
            # A prepared receipt may mean an ACK was lost. Recovery is read-only;
            # even a not-found result never automatically resends the command.
            submitted = client.call("recover", client_instance_id=namespace, codex_thread_id=codex_thread_id, **recovery)
    except RelayError as exc:
        try:
            journal.record(payload_hash, phase="unknown")
        except RelayError:
            pass
        raise RelayError(exc.code, exc.message, details={
            **exc.details, **recovery_details, "submission_outcome": "unknown",
        }) from exc
    snapshot = submitted["request"]
    claim_token = submitted.get("claim_token")
    if not isinstance(claim_token, str):
        raise RelayError(
            "invalid_response",
            "The relay did not return a request claim capability.",
        )
    with _RPC_STATE_LOCK:
        receipt_key = (snapshot["request_id"], claim_token)
        _RECEIPT_JOURNALS[receipt_key] = journal
        _RECEIPT_JOURNALS.move_to_end(receipt_key)
        while len(_RECEIPT_JOURNALS) > MAX_TRACKED_RPC_REQUESTS:
            _RECEIPT_JOURNALS.popitem(last=False)
    try:
        # Bind the receipt before exposing the claim to pending cancellation:
        # its ACK can synchronously mark this same receipt terminal.
        journal.record(payload_hash, phase="terminal" if snapshot.get("state") in TERMINAL_STATES else "submitted", request_id=snapshot["request_id"], claim_token=claim_token)
    except RelayError as exc:
        # A local storage failure must not lose the accepted claim or suppress
        # a cancellation that arrived while submission was in flight.
        _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
        raise RelayError("recovery_storage_unavailable", "The request was accepted but its local receipt could not be saved.", details={
            **recovery_details, "submission_outcome": "accepted", "request_id": snapshot["request_id"], "claim_token": claim_token,
        }) from exc
    _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
    _check_cancelled(rpc_request_id)
    if snapshot.get("state") in {"pending", "running"} and wait_seconds > 0:
        try:
            waited = client.call(
                "wait",
                request_id=snapshot["request_id"],
                claim_token=claim_token,
                wait_seconds=wait_seconds,
                client_instance_id=CLIENT_INSTANCE_ID,
                codex_thread_id=codex_thread_id,
                cancellation_event=_cancellation_event(rpc_request_id),
            )
        except RelayError as exc:
            if exc.code == "request_cancelled":
                _forget_rpc_claim(rpc_request_id)
                raise _cancelled_error(rpc_request_id) from exc
            # Submission already succeeded. A failed first poll must not lose
            # its capability or encourage duplicate work with a new key.
            raise RelayError(exc.code, exc.message, details={
                **exc.details,
                "request_id": snapshot["request_id"],
                "claim_token": claim_token,
                "do_not_resubmit": True,
                "recovery_tool": "wait_for_authorization",
                "recovery_arguments": {
                    "request_id": snapshot["request_id"],
                    "claim_token": claim_token,
                    "wait_seconds": 0,
                },
            }) from exc
        snapshot = waited["request"]
        _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
    result = {
        "ok": True,
        "request": snapshot,
        "claim_token": claim_token,
        "idempotent_replay": bool(submitted.get("idempotent_replay")),
        "continuation": _continuation_for(snapshot, claim_token, wait_seconds),
        "polling": _polling_directive(snapshot, claim_token, wait_seconds),
        "recovery": recovery,
    }
    scope_recovery = _scope_recovery_for_snapshot(snapshot)
    if scope_recovery is not None:
        result["scope_recovery"] = scope_recovery
    if snapshot.get("state") in TERMINAL_STATES:
        _observe_receipt(result, snapshot, claim_token, codex_thread_id, journal=journal)
    return result


def _continuation_for(snapshot: dict[str, Any], claim_token: str, wait_seconds: int | None = None) -> str:
    state = snapshot.get("state")
    if state in {"pending", "running"}:
        polling = _polling_directive(snapshot, claim_token, wait_seconds)
        next_arguments = polling.get("next_arguments", {})
        wait_seconds = next_arguments.get("wait_seconds", DEFAULT_POLL_WAIT_SECONDS)
        pending_remaining = polling.get("remaining_seconds")
        pending_suffix = (
            f" The pending deadline has about {pending_remaining} second(s) remaining."
            if state == "pending" and isinstance(pending_remaining, int)
            else ""
        )
        return (
            f"ACTIVE state={state}. Keep the current Codex turn open and immediately call "
            "wait_for_authorization again with polling.next_arguments exactly, including "
            f"its claim_token and wait_seconds={wait_seconds}. Re-read those arguments "
            "on every response "
            "until a terminal state. Do not return a final answer, ask the user to send "
            f"another message, or resubmit with a new idempotency key.{pending_suffix}"
        )
    if state == "approved":
        return "Use only result.reply_text as the answer to this exact consent request."
    if state == "answered":
        return (
            "Use result.answer for this exact original question. Boolean false is a valid answer, not a failed call. "
            "single_select selected_index is zero-based in the original option order. Continue the task using that "
            "answer without treating it as permission for unrelated actions or a native approval."
        )
    if state == "completed":
        return "Inspect exit_code, stdout, stderr, and truncation flags before continuing."
    if _scope_recovery_for_snapshot(snapshot) is not None:
        return (
            "The independently launched relay window closed before a decision. Ask the user "
            "whether to start the window and keep the agreed routing scope, or cancel that "
            "scope. Neither choice approves or rejects the interrupted action, and a restarted "
            "broker cannot resume this request."
        )
    return "The request is terminal. Do not perform the proposed action unless state permits it."


def _observe_receipt(result, snapshot, claim, codex_thread_id, *, recovery=None, journal=None):
    if (snapshot.get("state") in TERMINAL_STATES
            and (snapshot.get("delivery") or {}).get("status") == "awaiting_connector"):
        try:
            acknowledged = RelayClient().call(
                "acknowledge_receipt", request_id=snapshot["request_id"], claim_token=claim,
                request_hash=snapshot["request_hash"], observed_state=snapshot["state"],
                client_instance_id=CLIENT_INSTANCE_ID, codex_thread_id=codex_thread_id)
            delivery = acknowledged.get("request", {}).get("delivery")
            if not isinstance(delivery, dict) or delivery.get("status") != "connector_received":
                raise RelayError("invalid_response", "Missing connector receipt.")
            snapshot["delivery"] = delivery
        except RelayError:
            result["delivery_warning"] = "The result is valid; the connector receipt could not be recorded. Do not resubmit."
    try:
        journal = journal or recovery_journal(codex_thread_id, CLIENT_INSTANCE_ID)
        journal.observe(snapshot.get("request_id", ""), claim,
                        terminal=snapshot.get("state") in TERMINAL_STATES,
                        **(recovery or {}))
    except RelayError:
        result["recovery_journal_warning"] = "The broker result is valid, but its local receipt update failed."


def call_tool(
    name: str,
    arguments: Any,
    *,
    rpc_request_id: Any = None,
    codex_thread_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise RelayError("invalid_arguments", "Tool arguments must be an object.")
    if name == "preflight_command":
        allowed = {"executable", "arguments", "working_directory", "environment", "output_encoding", "response_files"}
        if set(arguments) - allowed:
            raise RelayError("invalid_arguments", "Unsupported preflight arguments.")
        return RelayClient().call("preflight", payload=arguments, client_instance_id=CLIENT_INSTANCE_ID, codex_thread_id=codex_thread_id)
    if name == "recover_authorization":
        if set(arguments) != {"recovery_token", "payload_hash"}:
            raise RelayError("invalid_arguments", "Recovery requires its exact capability and payload hash.")
        validate_recovery_token(arguments["recovery_token"])
        recovered = RelayClient().call("recover", **arguments, client_instance_id=CLIENT_INSTANCE_ID, codex_thread_id=codex_thread_id)
        snapshot, claim = recovered["request"], recovered["claim_token"]
        _sync_rpc_claim(rpc_request_id, snapshot, claim)
        result = {**recovered, "recovery": arguments, "polling": _polling_directive(snapshot, claim), "continuation": _continuation_for(snapshot, claim)}
        _observe_receipt(result, snapshot, claim, codex_thread_id, recovery=arguments)
        return result
    if name == "read_command_output":
        if set(arguments) - {"request_id", "claim_token", "stream", "offset", "limit"}:
            raise RelayError("invalid_arguments", "Unsupported output arguments.")
        _required_claim_token(arguments)
        limit = arguments.get("limit", 16384)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 65536:
            raise RelayError("invalid_output_limit", "Output limit must be an integer from 1 to 65536.")
        # Bound before reading, so the byte cursor always describes every text
        # byte delivered even after both MCP copies and JSON escaping.
        page_arguments = {**arguments, "limit": min(limit, 16384)}
        return RelayClient().call("read_output", **page_arguments, client_instance_id=CLIENT_INSTANCE_ID, codex_thread_id=codex_thread_id)
    if name == "relay_health":
        if arguments:
            raise RelayError("invalid_arguments", "relay_health takes no arguments.")
        try:
            health = RelayClient().call(
                "health",
                client_instance_id=CLIENT_INSTANCE_ID,
                codex_thread_id=codex_thread_id,
            )
        except RelayError as exc:
            recovery_reason = {
                "broker_unavailable": "relay_window_unavailable",
                "broker_stopping": "relay_window_closing",
            }.get(exc.code)
            if recovery_reason is None:
                raise
            return {
                "ok": True,
                "reachable": False,
                "window_state": (
                    "closing"
                    if exc.code == "broker_stopping"
                    else "not_running_or_unreachable"
                ),
                "reason_code": exc.code,
                "message": exc.message,
                "scope_recovery": _scope_recovery(recovery_reason),
            }
        return {
            "ok": True,
            "reachable": True,
            "window_state": "running",
            "protocol": health.get("protocol", PROTOCOL_VERSION),
            "connector_version": SERVER_VERSION,
            "connector_build_id": SERVER_BUILD_ID,
            "broker_version": health.get("broker_version"),
            "broker_build_id": health.get("broker_build_id"),
            "broker_elevated": health.get("broker_elevated"),
            "poll_policy": health.get("poll_policy"),
        }
    if name == "request_consent":
        return _request_and_wait(
            "consent",
            arguments,
            rpc_request_id=rpc_request_id,
            codex_thread_id=codex_thread_id,
        )
    if name == "request_decision":
        return _request_and_wait(
            "decision", arguments, rpc_request_id=rpc_request_id,
            codex_thread_id=codex_thread_id,
        )
    if name == "request_command_execution":
        return _request_and_wait(
            "command",
            arguments,
            rpc_request_id=rpc_request_id,
            codex_thread_id=codex_thread_id,
        )
    if name == "wait_for_authorization":
        unknown = set(arguments) - {"request_id", "claim_token", "wait_seconds"}
        if unknown:
            raise RelayError("invalid_arguments", "Unsupported wait arguments.")
        request_id = arguments.get("request_id")
        claim_token = _required_claim_token(arguments)
        wait_seconds = _bounded_wait(arguments)
        if isinstance(request_id, str):
            _remember_rpc_claim(rpc_request_id, request_id, claim_token)
        _check_cancelled(rpc_request_id)
        try:
            response = RelayClient().call(
                "wait",
                request_id=request_id,
                claim_token=claim_token,
                wait_seconds=wait_seconds,
                client_instance_id=CLIENT_INSTANCE_ID,
                codex_thread_id=codex_thread_id,
                cancellation_event=_cancellation_event(rpc_request_id),
            )
        except RelayError as exc:
            _forget_rpc_claim(rpc_request_id)
            if exc.code == "request_cancelled":
                raise _cancelled_error(rpc_request_id) from exc
            raise
        snapshot = response["request"]
        _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
        result = {
            "ok": True,
            "request": snapshot,
            "claim_token": claim_token,
            "continuation": _continuation_for(snapshot, claim_token, wait_seconds),
            "polling": _polling_directive(snapshot, claim_token, wait_seconds),
        }
        scope_recovery = _scope_recovery_for_snapshot(snapshot)
        if scope_recovery is not None:
            result["scope_recovery"] = scope_recovery
        _observe_receipt(result, snapshot, claim_token, codex_thread_id)
        return result
    if name in {"cancel_authorization", "terminate_command"}:
        unknown = set(arguments) - {"request_id", "claim_token"}
        if unknown:
            raise RelayError("invalid_arguments", "Unsupported request control arguments.")
        request_id = arguments.get("request_id")
        claim_token = _required_claim_token(arguments)
        response = RelayClient().call(
            "terminate" if name == "terminate_command" else "cancel",
            request_id=request_id,
            claim_token=claim_token,
            client_instance_id=CLIENT_INSTANCE_ID,
            codex_thread_id=codex_thread_id,
        )
        snapshot = response["request"]
        _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
        result = {
            "ok": True,
            "request": snapshot,
            "claim_token": claim_token,
            "continuation": _continuation_for(snapshot, claim_token),
            "polling": _polling_directive(snapshot, claim_token),
        }
        scope_recovery = _scope_recovery_for_snapshot(snapshot)
        if scope_recovery is not None:
            result["scope_recovery"] = scope_recovery
        _observe_receipt(result, snapshot, claim_token, codex_thread_id)
        return result
    raise RelayError("unknown_tool", f"Unknown relay tool: {name}")


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        _handle_cancelled_notification(message)
        return None
    if request_id is None:
        return None
    # A client may reuse a JSON-RPC ID after its previous response. Clear that
    # stale association before the new call can establish its own mapping.
    _forget_rpc_claim(request_id)

    if method == "initialize":
        params = message.get("params")
        requested_protocol = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol = (
            requested_protocol
            if requested_protocol in SUPPORTED_MCP_PROTOCOLS
            else SUPPORTED_MCP_PROTOCOLS[-1]
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": SERVER_INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS},
        }
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": "Invalid tools/call parameters.",
                },
            }
        try:
            codex_thread_id = extract_codex_thread_id(
                params.get("_meta")
            ) or normalize_codex_thread_id(os.environ.get("CODEX_THREAD_ID"))
            result = call_tool(
                params["name"],
                params.get("arguments", {}),
                rpc_request_id=request_id,
                codex_thread_id=codex_thread_id,
            )
            tool_result = mcp_tool_result(result, rpc_request_id=request_id)
        except RelayError as exc:
            tool_result = mcp_tool_result(_error_payload(exc), is_error=True, rpc_request_id=request_id)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": tool_result,
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


def write_message(message: dict[str, Any]) -> None:
    encoded = (json_ascii(fit_mcp_message(message)) + "\n").encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": code, "message": message},
    }


def fit_mcp_message(message: dict[str, Any]) -> dict[str, Any]:
    """Enforce the final STDIO frame budget, including its JSON-RPC envelope."""
    if len(json_ascii(message)) + 1 <= MAX_WIRE_BYTES:
        return message
    result = message.get("result")
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        message = {
            **message,
            "result": mcp_tool_result(
                result["structuredContent"],
                is_error=bool(result.get("isError")),
                rpc_request_id=message.get("id"),
            ),
        }
        if len(json_ascii(message)) + 1 <= MAX_WIRE_BYTES:
            return message
    request_id = message.get("id")
    key = _rpc_request_key(request_id)
    if key is None or len(key) > 256:
        request_id = None
    return _rpc_error(request_id, -32603, "MCP response exceeded the frame limit.")


class McpDispatcher:
    """Keep the STDIO reader free while bounding work and serializing replies."""

    def __init__(
        self,
        writer: Callable[[dict[str, Any]], None] = write_message,
        *,
        max_workers: int = MAX_CONCURRENT_RPC_REQUESTS,
    ) -> None:
        self._writer = writer
        self._write_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_workers)
        self._control_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONTROL_REQUESTS)
        self._workers: set[threading.Thread] = set()
        self._workers_condition = threading.Condition()
        self._cancel_queue: queue.Queue[tuple[str, str] | None] = queue.Queue(
            maxsize=MAX_TRACKED_RPC_REQUESTS
        )
        self._closed = False
        self._cancel_threads = [
            threading.Thread(target=self._drain_cancellations, daemon=True, name=f"mcp-cancel-{i}")
            for i in range(2)
        ]
        for worker in self._cancel_threads:
            worker.start()
        self._progress_stop = threading.Event()
        self._progress_thread = threading.Thread(target=self._progress_loop, daemon=True, name="mcp-progress")
        self._progress_thread.start()

    def emit(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self._writer(fit_mcp_message(message))

    def _progress_loop(self) -> None:
        while not self._progress_stop.wait(0.2):
            try:
                self.emit_progress()
            except (BrokenPipeError, OSError):
                return

    def emit_progress(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with _RPC_STATE_LOCK:
            keys = [key for key, state in _ACTIVE_RPC_CALLS.items() if state.progress_owner is self]
        for key in keys:
            with self._write_lock:
                with _RPC_STATE_LOCK:
                    state = _ACTIVE_RPC_CALLS.get(key)
                    if state is None or state.progress_owner is not self or state.progress_token is None:
                        continue
                    changed = state.progress_phase != state.progress_last_phase
                    if not changed and now - state.progress_last_sent < PROGRESS_INTERVAL_SECONDS:
                        continue
                    elapsed = max(0.0, now - state.progress_started)
                    if elapsed <= state.progress_last_value:
                        continue
                    notification = {
                        "jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": state.progress_token, "progress": elapsed,
                                   "message": f"{state.progress_phase}; elapsed {elapsed:.1f} seconds"},
                    }
                    state.progress_last_sent = now
                    state.progress_last_value = elapsed
                    state.progress_last_phase = state.progress_phase
                # Serialize against final replies, but keep the claim lock free.
                self._writer(notification)

    def _drain_cancellations(self) -> None:
        while True:
            claim = self._cancel_queue.get()
            try:
                if claim is None:
                    return
                _cancel_claim(claim)
            finally:
                self._cancel_queue.task_done()

    def _queue_cancel(self, claim: tuple[str, str] | None) -> None:
        if claim is None:
            return
        try:
            self._cancel_queue.put_nowait(claim)
        except queue.Full:
            # Never block the STDIO reader on network I/O. A live caller gets an
            # explicit failed cancellation ACK and can retry through the tool.
            _finish_cancel_claim(claim, "cancel_capacity_exceeded", None)

    def dispatch(self, message: dict[str, Any]) -> None:
        if message.get("method") == "notifications/cancelled":
            self._queue_cancel(_take_cancelled_claim(message))
            return
        if message.get("id") is None:
            return
        request_id = message["id"]
        key = _rpc_request_key(request_id)
        if key is None or len(key) > 256:
            self.emit(_rpc_error(None, -32600, "Invalid or oversized JSON-RPC request ID."))
            return
        with _RPC_STATE_LOCK:
            if key in _ACTIVE_RPC_CALLS:
                duplicate = True
            else:
                duplicate = False
                _RPC_REQUEST_CLAIMS.pop(key, None)
        if duplicate:
            self.emit(_rpc_error(request_id, -32600, "JSON-RPC request ID is already active."))
            return
        if message.get("method") != "tools/call":
            response = handle_request(message)
            if response is not None:
                self.emit(response)
            return
        params = message.get("params")
        tool_name = params.get("name") if isinstance(params, dict) else None
        slots = self._control_slots if isinstance(tool_name, str) and tool_name in CONTROL_TOOLS else self._slots
        if self._closed or not slots.acquire(blocking=False):
            self.emit(_rpc_error(request_id, -32001, "MCP request capacity is full; retry after an active call finishes."))
            return
        with _RPC_STATE_LOCK:
            metadata = params.get("_meta") if isinstance(params, dict) else None
            progress_token = metadata.get("progressToken") if isinstance(metadata, dict) else None
            if isinstance(progress_token, bool) or not isinstance(progress_token, (str, int)) or len(json_ascii(progress_token)) > 256:
                progress_token = None
            _ACTIVE_RPC_CALLS[key] = _RpcCallState(
                progress_token=progress_token, progress_owner=self,
                progress_phase="submitting" if tool_name in {"request_consent", "request_decision", "request_command_execution"} else "waiting",
            )
        worker = threading.Thread(
            target=self._run, args=(key, message, slots), daemon=True, name="mcp-request"
        )
        with self._workers_condition:
            self._workers.add(worker)
        try:
            worker.start()
        except RuntimeError:
            with _RPC_STATE_LOCK:
                _ACTIVE_RPC_CALLS.pop(key, None)
            slots.release()
            with self._workers_condition:
                self._workers.discard(worker)
                self._workers_condition.notify_all()
            self.emit(_rpc_error(request_id, -32603, "MCP could not start a request worker."))

    def _run(
        self, key: str, message: dict[str, Any], slots: threading.BoundedSemaphore
    ) -> None:
        try:
            response = handle_request(message)
        except Exception:
            response = _rpc_error(message.get("id"), -32603, "Internal MCP server error.")
        try:
            with _RPC_STATE_LOCK:
                _ACTIVE_RPC_CALLS.pop(key, None)
            # Do not hold the claim lock while STDOUT may be backpressured: the
            # input reader must still be able to signal cancellation.
            if response is not None:
                self.emit(response)
        except (BrokenPipeError, OSError):
            pass
        finally:
            slots.release()
            with self._workers_condition:
                self._workers.discard(threading.current_thread())
                self._workers_condition.notify_all()

    def wait_idle(self, timeout: float = 3.5) -> bool:
        deadline = time.monotonic() + timeout
        with self._workers_condition:
            while self._workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._workers_condition.wait(remaining)
        return True

    def close(self) -> None:
        self._closed = True
        self._progress_stop.set()
        with _RPC_STATE_LOCK:
            active_ids = list(_ACTIVE_RPC_CALLS)
        for key in active_ids:
            self._queue_cancel(_take_cancelled_claim({"params": {"requestId": json.loads(key)}}))
        self.wait_idle()
        for _ in self._cancel_threads:
            try:
                self._cancel_queue.put_nowait(None)
            except queue.Full:
                break
        # A completed RPC can still have its cancellation receipt in flight.
        # Let queued cancellation ACKs finish before callers dispose resources.
        deadline = time.monotonic() + CANCELLATION_ACK_TIMEOUT_SECONDS
        for worker in self._cancel_threads:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._progress_thread.join(timeout=0.3)


def serve(input_stream: BinaryIO, dispatcher: McpDispatcher) -> None:
    while True:
        raw = input_stream.readline(MAX_WIRE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_WIRE_BYTES:
            # Discard the remainder in bounded chunks so its tail cannot become
            # a second request. No line or error contains the rejected payload.
            while raw and not raw.endswith(b"\n"):
                raw = input_stream.readline(MAX_WIRE_BYTES + 1)
            dispatcher.emit(_rpc_error(None, -32600, "MCP request exceeds the frame limit."))
            continue
        if not raw.strip():
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            dispatcher.emit(_rpc_error(None, -32700, "Invalid JSON-RPC frame."))
            continue
        dispatcher.dispatch(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden Finger MCP connector")
    parser.add_argument("--self-test-output", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.self_test_output is not None:
        arguments.self_test_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.self_test_output.write_text(json.dumps({
            "ok": True,
            "mcp_version": SERVER_VERSION,
            "mcp_build_id": SERVER_BUILD_ID,
            "runtime_mode": "self-contained-exe" if getattr(sys, "frozen", False) else "source-python",
            "protocol": PROTOCOL_VERSION,
        }, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        return 0
    dispatcher = McpDispatcher()
    try:
        serve(sys.stdin.buffer, dispatcher)
    finally:
        dispatcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
