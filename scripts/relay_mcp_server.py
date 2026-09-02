"""STDIO MCP connector for Codex金手指 (Golden Finger for Codex).

The connector never launches the broker. It only submits, waits, reads, or
cancels requests over the authenticated loopback protocol.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import os
import sys
from typing import Any

from codex_lifecycle import extract_codex_thread_id, normalize_codex_thread_id
from relay_protocol import (
    CLAIM_TOKEN_PREFIX,
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_APPROVAL_EXPIRY_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_POLL_WAIT_SECONDS,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    REQUEST_ID_PREFIX,
    RUNTIME_BUILD_ID,
    TERMINAL_STATES,
    RelayClient,
    RelayError,
    json_ascii,
    new_client_instance_id,
)


SERVER_NAME = "golden-finger"
SERVER_VERSION = PACKAGE_VERSION
SERVER_BUILD_ID = RUNTIME_BUILD_ID
CLIENT_INSTANCE_ID = new_client_instance_id()
MAX_TRACKED_RPC_REQUESTS = 128
_RPC_REQUEST_CLAIMS: OrderedDict[str, tuple[str, str]] = OrderedDict()
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
    "作用域解释为批准或拒绝当前动作。request_consent 返回当前工具"
    "调用里的文本决定；request_command_execution 仅在窗口点击后以 shell=False 执行"
    "冻结的 executable+arguments。中继是拉取式机制，不会唤醒已经结束的 Codex 回合。"
    "pending/running 是非终态：必须保持当前回合，逐字复用 polling.next_arguments "
    "wait_for_authorization，直到 completed/approved/rejected/cancelled/expired/failed/"
    "timed_out；活动状态下不得返回最终答复、不得让用户额外发送‘已完成’，也不要以新 key "
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
        "enum": [0, DEFAULT_POLL_WAIT_SECONDS],
        "default": DEFAULT_POLL_WAIT_SECONDS,
        "description": (
            "Zero performs an immediate non-consuming read; 900 uses the fixed "
            "per-request wait."
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
            "window. Clicking approve executes no program and returns approved_reply "
            "to this MCP tool call. This is not a native Codex approval. A pending "
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
                    "description": "Exact text returned only after the one-time click.",
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
        "name": "request_command_execution",
        "title": "请求执行一次精确命令",
        "description": (
            "Display and, only after a one-time GUI click, execute an exact local "
            "executable plus argument array with shell=False. The broker rejects "
            "Codex/self-control targets and retains bounded output. A pending/running "
            "result requires same-turn polling until terminal."
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
                    "enum": [0, DEFAULT_POLL_WAIT_SECONDS],
                    "default": DEFAULT_POLL_WAIT_SECONDS,
                    "description": (
                        "Zero reads immediately; 900 uses the request's fixed "
                        "per-call duration."
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


def mcp_tool_result(
    structured: dict[str, Any],
    *,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, indent=2),
            }
        ],
        "structuredContent": structured,
        "isError": is_error,
    }


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
    return json_ascii(rpc_request_id)


def _remember_rpc_claim(
    rpc_request_id: Any,
    relay_request_id: str,
    claim_token: str,
) -> None:
    key = _rpc_request_key(rpc_request_id)
    if key is None:
        return
    _RPC_REQUEST_CLAIMS[key] = (relay_request_id, claim_token)
    _RPC_REQUEST_CLAIMS.move_to_end(key)
    while len(_RPC_REQUEST_CLAIMS) > MAX_TRACKED_RPC_REQUESTS:
        _RPC_REQUEST_CLAIMS.popitem(last=False)


def _forget_rpc_claim(rpc_request_id: Any) -> None:
    key = _rpc_request_key(rpc_request_id)
    if key is not None:
        _RPC_REQUEST_CLAIMS.pop(key, None)


def _forget_request_claim(relay_request_id: str, claim_token: str) -> None:
    stale = [
        key
        for key, value in _RPC_REQUEST_CLAIMS.items()
        if value == (relay_request_id, claim_token)
    ]
    for key in stale:
        _RPC_REQUEST_CLAIMS.pop(key, None)


def _sync_rpc_claim(
    rpc_request_id: Any,
    snapshot: dict[str, Any],
    claim_token: str,
) -> None:
    relay_request_id = snapshot.get("request_id")
    if not isinstance(relay_request_id, str):
        _forget_rpc_claim(rpc_request_id)
        return
    if snapshot.get("state") in {"pending", "running"}:
        _remember_rpc_claim(rpc_request_id, relay_request_id, claim_token)
    else:
        _forget_request_claim(relay_request_id, claim_token)


def _handle_cancelled_notification(message: dict[str, Any]) -> None:
    params = message.get("params")
    if not isinstance(params, dict):
        return
    key = _rpc_request_key(params.get("requestId"))
    if key is None:
        return
    claimed = _RPC_REQUEST_CLAIMS.pop(key, None)
    if claimed is None:
        return
    relay_request_id, claim_token = claimed
    try:
        RelayClient().call(
            "cancel",
            request_id=relay_request_id,
            claim_token=claim_token,
            client_instance_id=CLIENT_INSTANCE_ID,
        )
    except RelayError:
        # Cancellation is best effort. An approval may already have started a
        # command, or the broker may have become unavailable in the meantime.
        pass
    finally:
        _forget_request_claim(relay_request_id, claim_token)


def _bounded_wait(arguments: dict[str, Any]) -> int:
    wait = arguments.get("wait_seconds", DEFAULT_POLL_WAIT_SECONDS)
    if isinstance(wait, bool) or not isinstance(wait, int):
        raise RelayError("invalid_wait", "wait_seconds must be an integer.")
    if wait < 0:
        raise RelayError("invalid_wait", "wait_seconds must be zero or positive.")
    return DEFAULT_POLL_WAIT_SECONDS if wait > 0 else 0


def _polling_directive(
    snapshot: dict[str, Any],
    claim_token: str,
) -> dict[str, Any]:
    state = snapshot.get("state")
    active = state in {"pending", "running"}
    policy = snapshot.get("poll_policy")
    if not isinstance(policy, dict):
        policy = {}
    configured_wait = policy.get("wait_seconds", DEFAULT_POLL_WAIT_SECONDS)
    if isinstance(configured_wait, bool) or not isinstance(configured_wait, int):
        configured_wait = DEFAULT_POLL_WAIT_SECONDS
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
                "pending_polls_remaining": policy.get("pending_polls_remaining"),
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
    wait_seconds = _bounded_wait(arguments)
    payload = {key: value for key, value in arguments.items() if key != "wait_seconds"}
    client = RelayClient()
    submitted = client.call(
        "submit",
        kind=kind,
        payload=payload,
        client_instance_id=CLIENT_INSTANCE_ID,
        codex_thread_id=codex_thread_id,
    )
    snapshot = submitted["request"]
    claim_token = submitted.get("claim_token")
    if not isinstance(claim_token, str):
        raise RelayError(
            "invalid_response",
            "The relay did not return a request claim capability.",
        )
    _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
    if snapshot.get("state") in {"pending", "running"} and wait_seconds > 0:
        policy = snapshot.get("poll_policy")
        if isinstance(policy, dict) and isinstance(policy.get("wait_seconds"), int):
            wait_seconds = policy["wait_seconds"]
        try:
            waited = client.call(
                "wait",
                request_id=snapshot["request_id"],
                claim_token=claim_token,
                wait_seconds=wait_seconds,
                client_instance_id=CLIENT_INSTANCE_ID,
                codex_thread_id=codex_thread_id,
            )
        except RelayError:
            _forget_rpc_claim(rpc_request_id)
            raise
        snapshot = waited["request"]
        _sync_rpc_claim(rpc_request_id, snapshot, claim_token)
    result = {
        "ok": True,
        "request": snapshot,
        "claim_token": claim_token,
        "idempotent_replay": bool(submitted.get("idempotent_replay")),
        "continuation": _continuation_for(snapshot, claim_token),
        "polling": _polling_directive(snapshot, claim_token),
    }
    scope_recovery = _scope_recovery_for_snapshot(snapshot)
    if scope_recovery is not None:
        result["scope_recovery"] = scope_recovery
    return result


def _continuation_for(snapshot: dict[str, Any], claim_token: str) -> str:
    state = snapshot.get("state")
    if state in {"pending", "running"}:
        polling = _polling_directive(snapshot, claim_token)
        next_arguments = polling.get("next_arguments", {})
        wait_seconds = next_arguments.get("wait_seconds", DEFAULT_POLL_WAIT_SECONDS)
        pending_remaining = polling.get("pending_polls_remaining")
        pending_suffix = (
            f" The frozen pending budget has {pending_remaining} poll(s) remaining."
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


def call_tool(
    name: str,
    arguments: Any,
    *,
    rpc_request_id: Any = None,
    codex_thread_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise RelayError("invalid_arguments", "Tool arguments must be an object.")
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
        try:
            response = RelayClient().call(
                "wait",
                request_id=request_id,
                claim_token=claim_token,
                wait_seconds=wait_seconds,
                client_instance_id=CLIENT_INSTANCE_ID,
                codex_thread_id=codex_thread_id,
            )
        except RelayError:
            _forget_rpc_claim(rpc_request_id)
            raise
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
        return result
    if name == "cancel_authorization":
        unknown = set(arguments) - {"request_id", "claim_token"}
        if unknown:
            raise RelayError("invalid_arguments", "Unsupported cancel arguments.")
        request_id = arguments.get("request_id")
        claim_token = _required_claim_token(arguments)
        response = RelayClient().call(
            "cancel",
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
            tool_result = mcp_tool_result(result)
        except RelayError as exc:
            tool_result = mcp_tool_result(_error_payload(exc), is_error=True)
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
    encoded = (json_ascii(message) + "\n").encode("ascii")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> int:
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        message: Any = None
        try:
            decoded = raw.decode("utf-8")
            message = json.loads(decoded)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object.")
            response = handle_request(message)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": f"Parse error: {exc}",
                },
            }
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {
                    "code": -32603,
                    "message": "Internal MCP server error.",
                },
            }
        if response is not None:
            write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
