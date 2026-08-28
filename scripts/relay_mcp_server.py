"""STDIO MCP connector for Codex金手指 (Golden Finger for Codex).

The connector never launches the broker. It only submits, waits, reads, or
cancels requests over the authenticated loopback protocol.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from relay_protocol import (
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_APPROVAL_EXPIRY_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_POLL_WAIT_SECONDS,
    PROTOCOL_VERSION,
    TERMINAL_STATES,
    RelayClient,
    RelayError,
    json_ascii,
)


SERVER_NAME = "golden-finger"
SERVER_VERSION = "0.1.9"
SUPPORTED_MCP_PROTOCOLS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_INSTRUCTIONS = (
    "Codex金手指是面向 Yes工程师的人机授权中继，只请求一次、精确、可见的人类决定。它不替代 Codex 原生审批或沙箱，"
    "不提供 standing consent，也不得用于启动/控制 Codex、App Server、任务、数据库或 "
    "rollout。窗口必须由用户独立启动；不可达时失败关闭。request_consent 返回当前工具"
    "调用里的文本决定；request_command_execution 仅在窗口点击后以 shell=False 执行"
    "冻结的 executable+arguments。中继是拉取式机制，不会唤醒已经结束的 Codex 回合。"
    "pending/running 是非终态：必须保持当前回合，用同一 request_id 重复调用 "
    "wait_for_authorization，直到 completed/approved/rejected/cancelled/expired/failed/"
    "timed_out；活动状态下不得返回最终答复、不得让用户额外发送‘已完成’，也不要以新 key "
    "重提。中继只返回自身启动的命令结果，不能观察由其他终端或服务启动的进程；"
    "这不限制它在精确一次性批准后自行启动允许的有界测试或测试 supervisor。"
)


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
            "Optional shorter pending validity. The relay window's saved pending-wait "
            "budget is the authoritative cap for new requests."
        ),
    },
    "wait_seconds": {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_POLL_WAIT_SECONDS,
        "default": DEFAULT_POLL_WAIT_SECONDS,
        "description": (
            "Zero performs a read without consuming a pending poll. Any positive value "
            "uses the window duration frozen for this request."
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
}
COMMON_REQUIRED = [
    "exact_action",
    "target",
    "risk",
    "recovery",
    "idempotency_key",
]


TOOLS: list[dict[str, Any]] = [
    {
        "name": "relay_health",
        "title": "检查 Codex金手指窗口",
        "description": (
            "Check whether the separately launched local relay window is reachable. "
            "This never launches it and never changes a request."
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
                        "Optional shorter process timeout after approval. The relay window's "
                        "saved command timeout is the authoritative cap for new requests."
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
            "Reuse the original request_id instead of submitting a duplicate. If state "
            "is still pending/running, keep this Codex turn open and follow the returned "
            "polling.next_arguments exactly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^har_[A-Za-z0-9]+$",
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_POLL_WAIT_SECONDS,
                    "default": DEFAULT_POLL_WAIT_SECONDS,
                    "description": (
                        "Zero reads immediately; any positive value uses the request's "
                        "frozen per-call duration."
                    ),
                },
            },
            "required": ["request_id"],
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
            "or undo a process that already started."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^har_[A-Za-z0-9]+$",
                }
            },
            "required": ["request_id"],
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


def _bounded_wait(arguments: dict[str, Any]) -> int:
    wait = arguments.get("wait_seconds", DEFAULT_POLL_WAIT_SECONDS)
    if isinstance(wait, bool) or not isinstance(wait, int):
        raise RelayError("invalid_wait", "wait_seconds must be an integer.")
    return max(0, min(wait, MAX_POLL_WAIT_SECONDS))


def _polling_directive(snapshot: dict[str, Any]) -> dict[str, Any]:
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


def _request_and_wait(kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
    wait_seconds = _bounded_wait(arguments)
    payload = {key: value for key, value in arguments.items() if key != "wait_seconds"}
    client = RelayClient()
    submitted = client.call("submit", kind=kind, payload=payload)
    snapshot = submitted["request"]
    if snapshot.get("state") in {"pending", "running"} and wait_seconds > 0:
        policy = snapshot.get("poll_policy")
        if isinstance(policy, dict) and isinstance(policy.get("wait_seconds"), int):
            wait_seconds = policy["wait_seconds"]
        waited = client.call(
            "wait",
            request_id=snapshot["request_id"],
            wait_seconds=wait_seconds,
        )
        snapshot = waited["request"]
    return {
        "ok": True,
        "request": snapshot,
        "idempotent_replay": bool(submitted.get("idempotent_replay")),
        "continuation": _continuation_for(snapshot),
        "polling": _polling_directive(snapshot),
    }


def _continuation_for(snapshot: dict[str, Any]) -> str:
    state = snapshot.get("state")
    if state in {"pending", "running"}:
        polling = _polling_directive(snapshot)
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
            "wait_for_authorization again with this same request_id and "
            f"wait_seconds={wait_seconds}. Follow polling.next_arguments on every response "
            "until a terminal state. Do not return a final answer, ask the user to send "
            f"another message, or resubmit with a new idempotency key.{pending_suffix}"
        )
    if state == "approved":
        return "Use only result.reply_text as the answer to this exact consent request."
    if state == "completed":
        return "Inspect exit_code, stdout, stderr, and truncation flags before continuing."
    return "The request is terminal. Do not perform the proposed action unless state permits it."


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise RelayError("invalid_arguments", "Tool arguments must be an object.")
    if name == "relay_health":
        if arguments:
            raise RelayError("invalid_arguments", "relay_health takes no arguments.")
        health = RelayClient().call("health")
        return {
            "ok": True,
            "reachable": True,
            "protocol": health.get("protocol", PROTOCOL_VERSION),
            "broker_version": health.get("broker_version"),
            "broker_elevated": health.get("broker_elevated"),
            "poll_policy": health.get("poll_policy"),
        }
    if name == "request_consent":
        return _request_and_wait("consent", arguments)
    if name == "request_command_execution":
        return _request_and_wait("command", arguments)
    if name == "wait_for_authorization":
        unknown = set(arguments) - {"request_id", "wait_seconds"}
        if unknown:
            raise RelayError("invalid_arguments", "Unsupported wait arguments.")
        request_id = arguments.get("request_id")
        wait_seconds = _bounded_wait(arguments)
        response = RelayClient().call(
            "wait",
            request_id=request_id,
            wait_seconds=wait_seconds,
        )
        snapshot = response["request"]
        return {
            "ok": True,
            "request": snapshot,
            "continuation": _continuation_for(snapshot),
            "polling": _polling_directive(snapshot),
        }
    if name == "cancel_authorization":
        unknown = set(arguments) - {"request_id"}
        if unknown:
            raise RelayError("invalid_arguments", "Unsupported cancel arguments.")
        response = RelayClient().call(
            "cancel",
            request_id=arguments.get("request_id"),
        )
        snapshot = response["request"]
        return {
            "ok": True,
            "request": snapshot,
            "continuation": _continuation_for(snapshot),
            "polling": _polling_directive(snapshot),
        }
    raise RelayError("unknown_tool", f"Unknown relay tool: {name}")


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        # The corresponding relay request, if already submitted, expires unless
        # Codex explicitly calls cancel_authorization with its relay request ID.
        return None
    if request_id is None:
        return None

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
            result = call_tool(params["name"], params.get("arguments", {}))
            tool_result = mcp_tool_result(result)
        except RelayError as exc:
            tool_result = mcp_tool_result(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        **({"details": exc.details} if exc.details else {}),
                    },
                },
                is_error=True,
            )
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
