"""Bounded transport and cancellation tests; no GUI or process execution."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import socket
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import relay_mcp_server as mcp
from relay_recovery import RecoveryJournal
from relay_protocol import (
    MAX_OUTPUT_BYTES,
    MAX_WIRE_BYTES,
    RelayClient,
    RelayError,
    fit_wire_response,
    json_ascii,
)


CLAIM_A = "gfcap_" + "a" * 40
CLAIM_B = "gfcap_" + "b" * 40


def tool_call(rpc_id: object, name: str = "wait_for_authorization", **arguments: object) -> dict:
    if not arguments:
        arguments = {"request_id": "gfr_RequestA", "claim_token": CLAIM_A}
    return {
        "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def cancellation(rpc_id: object) -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rpc_id}}


def snapshot(request_id: str = "gfr_RequestA", state: str = "pending") -> dict:
    return {"request_id": request_id, "state": state}


class WireBudgetTests(unittest.TestCase):
    def test_backslash_and_quote_streams_include_long_rpc_id_in_frame_budget(self) -> None:
        for character in ("\\", '"'):
            with self.subTest(character=character):
                response = {"ok": True, "claim_token": CLAIM_A, "request": {
                    "request_id": "gfr_RequestA", "state": "completed", "result": {
                        "stdout": character * MAX_OUTPUT_BYTES,
                        "stderr": character * MAX_OUTPUT_BYTES, "exit_code": 0,
                    },
                }}
                rpc_id = "x" * 250
                result = mcp.mcp_tool_result(response, rpc_request_id=rpc_id)
                frame = mcp.fit_mcp_message({"jsonrpc": "2.0", "id": rpc_id, "result": result})
                self.assertLessEqual(len(json_ascii(frame)) + 1, MAX_WIRE_BYTES)
                self.assertEqual(frame["id"], rpc_id)
                self.assertEqual(frame["result"]["structuredContent"]["request"]["result"]["exit_code"], 0)
                self.assertEqual(json.loads(frame["result"]["content"][0]["text"]), frame["result"]["structuredContent"])

    def test_both_unicode_streams_fit_after_json_escaping(self) -> None:
        text = "中" * (MAX_OUTPUT_BYTES // 3)
        response = {"ok": True, "request": {
            "request_id": "gfr_RequestA", "state": "completed",
            "result": {"stdout": text, "stderr": text, "exit_code": 0},
        }}
        original = copy.deepcopy(response)
        self.assertGreater(len(json_ascii(response)) + 1, MAX_WIRE_BYTES)
        bounded = fit_wire_response(response)
        self.assertLessEqual(len(json_ascii(bounded)) + 1, MAX_WIRE_BYTES)
        self.assertEqual(bounded["request"]["state"], "completed")
        self.assertEqual(bounded["request"]["result"]["exit_code"], 0)
        self.assertTrue(bounded["request"]["result"]["wire_output_truncated"])
        self.assertTrue(bounded["request"]["result"]["stdout_truncated"])
        self.assertEqual(response, original)

    def test_control_characters_and_surrogate_pairs_are_budgeted(self) -> None:
        for text in ("\x00" * MAX_OUTPUT_BYTES, "\U0001f680" * (MAX_OUTPUT_BYTES // 4)):
            with self.subTest(text=text[:1]):
                response = {"request": {"state": "failed", "result": {
                    "stdout": text, "stderr": text, "exit_code": 2,
                }}}
                bounded = fit_wire_response(response, max_bytes=8_192)
                self.assertLessEqual(len(json_ascii(bounded)) + 1, 8_192)
                self.assertEqual(bounded["request"]["result"]["exit_code"], 2)
                self.assertEqual(json.loads(json_ascii(bounded)), bounded)

    def test_small_output_is_unchanged(self) -> None:
        response = {"request": {"state": "completed", "result": {"stdout": "hello", "exit_code": 0}}}
        self.assertIs(fit_wire_response(response), response)

    def test_oversized_metadata_still_returns_outcome_under_budget(self) -> None:
        response = {"request": {"request_id": "gfr_A", "state": "completed", "result": {
            "stdout": "small", "exit_code": 0, "extra": "x" * MAX_WIRE_BYTES,
        }}}
        bounded = fit_wire_response(response)
        self.assertLessEqual(len(json_ascii(bounded)) + 1, MAX_WIRE_BYTES)
        self.assertEqual(bounded["error"]["details"]["state"], "completed")
        self.assertEqual(bounded["error"]["details"]["exit_code"], 0)

    def test_mcp_text_and_structured_copies_fit_together(self) -> None:
        response = {"ok": True, "claim_token": CLAIM_A, "request": {
            "request_id": "gfr_RequestA", "state": "completed", "result": {
                "stdout": "\x00" * MAX_OUTPUT_BYTES, "stderr": "中" * 87_381, "exit_code": 0,
            },
        }}
        result = mcp.mcp_tool_result(response)
        frame = {"jsonrpc": "2.0", "id": "budget", "result": result}
        self.assertLessEqual(len(json_ascii(frame)) + 1, MAX_WIRE_BYTES)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        self.assertEqual(result["structuredContent"]["claim_token"], CLAIM_A)


class McpDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal_patch = patch.object(mcp, "recovery_journal", lambda thread, process: RecoveryJournal("thread:" + thread if thread else "process:" + process, root=Path(temporary.name)))
        journal_patch.start()
        self.addCleanup(journal_patch.stop)
        with mcp._RPC_STATE_LOCK:
            mcp._RPC_REQUEST_CLAIMS.clear()
            mcp._ACTIVE_RPC_CALLS.clear()
        self.responses: list[dict] = []
        self.dispatcher = mcp.McpDispatcher(self.responses.append)

    def tearDown(self) -> None:
        self.dispatcher.close()
        with mcp._RPC_STATE_LOCK:
            mcp._RPC_REQUEST_CLAIMS.clear()
            mcp._ACTIVE_RPC_CALLS.clear()

    def _check_cancellation_after_claim_map_removal(self, *, terminal_response: bool) -> None:
        entered = threading.Event()
        cancelled = threading.Event()
        calls = []

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                calls.append(operation)
                if operation == "wait" and kwargs.get("wait_seconds"):
                    entered.set()
                    if not kwargs["cancellation_event"].wait(2):
                        raise AssertionError("Long wait was not cancelled")
                    raise RelayError("request_cancelled", "Local wait interrupted")
                if operation == "cancel":
                    cancelled.set()
                    return {"request": snapshot(state="cancelled")}
                return {"request": snapshot(state="completed" if terminal_response else "pending")}

        with patch.object(mcp, "RelayClient", Client), patch.object(mcp, "MAX_TRACKED_RPC_REQUESTS", 2):
            self.dispatcher.dispatch(tool_call(1))
            self.assertTrue(entered.wait(2))
            for rpc_id in (2, 3):
                self.dispatcher.dispatch(tool_call(rpc_id, request_id="gfr_RequestA", claim_token=CLAIM_A, wait_seconds=0))
                deadline = time.monotonic() + 2
                while not any(response.get("id") == rpc_id for response in self.responses) and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertTrue(any(response.get("id") == rpc_id for response in self.responses))
            self.assertNotIn("1", mcp._RPC_REQUEST_CLAIMS)
            self.assertIsNotNone(mcp._ACTIVE_RPC_CALLS["1"].claim)
            self.dispatcher.dispatch(cancellation(1))
            self.dispatcher.dispatch(cancellation(1))
            self.assertTrue(self.dispatcher.wait_idle())
            self.assertTrue(cancelled.is_set())
        result = next(response for response in self.responses if response.get("id") == 1)
        self.assertEqual(result["result"]["structuredContent"]["error"]["details"]["broker_cancellation"], "acknowledged")
        self.assertEqual(calls.count("cancel"), 1)

    def test_cancel_active_wait_after_lru_claim_eviction(self) -> None:
        self._check_cancellation_after_claim_map_removal(terminal_response=False)

    def test_cancel_active_wait_after_parallel_terminal_response_clears_claim_map(self) -> None:
        self._check_cancellation_after_claim_map_removal(terminal_response=True)

    def test_cancel_ack_timeout_is_explicit_and_does_not_leak_worker(self) -> None:
        entered = threading.Event()
        release_cancel = threading.Event()

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                if operation == "wait":
                    entered.set()
                    kwargs["cancellation_event"].wait(2)
                    raise RelayError("request_cancelled", "Local wait interrupted")
                release_cancel.wait(2)
                return {"request": snapshot(state="cancelled")}

        with patch.object(mcp, "RelayClient", Client), patch.object(mcp, "CANCELLATION_ACK_TIMEOUT_SECONDS", 0.02):
            try:
                self.dispatcher.dispatch(tool_call(1))
                self.assertTrue(entered.wait(2))
                self.dispatcher.dispatch(cancellation(1))
                self.assertTrue(self.dispatcher.wait_idle(timeout=1))
                details = self.responses[0]["result"]["structuredContent"]["error"]["details"]
                self.assertEqual(details["broker_cancellation"], "ack_timeout")
            finally:
                release_cancel.set()
                self.dispatcher._cancel_queue.join()

    def test_worker_creation_failure_releases_slot_and_rpc_id(self) -> None:
        with patch("relay_mcp_server.threading.Thread.start", side_effect=RuntimeError("test failure")):
            self.dispatcher.dispatch(tool_call(1))
        self.assertTrue(self.dispatcher.wait_idle())
        self.assertNotIn("1", mcp._ACTIVE_RPC_CALLS)
        self.assertEqual(self.responses[0]["error"]["code"], -32603)

    def test_reader_handles_cancellation_and_ping_during_long_wait(self) -> None:
        entered = threading.Event()
        cancelled = threading.Event()
        calls = []

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                calls.append((operation, kwargs))
                if operation == "wait":
                    entered.set()
                    if not cancelled.wait(2):
                        raise AssertionError("Reader did not deliver cancellation during wait")
                elif operation == "cancel":
                    cancelled.set()
                else:
                    raise AssertionError(operation)
                return {"ok": True, "request": snapshot(state="cancelled")}

        class Input:
            def __init__(self) -> None:
                self.index = 0

            def readline(self, size: int) -> bytes:
                messages = [tool_call(1), cancellation(1), {"id": 2, "method": "ping"}]
                if self.index >= len(messages):
                    return b""
                if self.index == 1 and not entered.wait(2):
                    raise AssertionError("Wait did not start")
                value = (json_ascii(messages[self.index]) + "\n").encode()
                self.index += 1
                return value

        with patch.object(mcp, "RelayClient", Client):
            mcp.serve(Input(), self.dispatcher)
            self.assertTrue(self.dispatcher.wait_idle())
        self.assertTrue(cancelled.is_set())
        self.assertEqual({item["id"] for item in self.responses}, {1, 2})
        self.assertEqual([name for name, _ in calls].count("cancel"), 1)
        self.assertEqual(calls[1][1]["claim_token"], CLAIM_A)

    def test_cancel_during_submit_uses_its_claim_when_submit_returns(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                calls.append((operation, kwargs))
                if operation == "submit":
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("submit not released")
                    return {"request": snapshot(), "claim_token": CLAIM_A}
                if operation == "cancel":
                    return {"request": snapshot(state="cancelled")}
                raise AssertionError(operation)

        with patch.object(mcp, "RelayClient", Client):
            self.dispatcher.dispatch(tool_call(10, "request_consent", wait_seconds=900))
            self.assertTrue(entered.wait(2))
            self.dispatcher.dispatch(cancellation(10))
            release.set()
            self.assertTrue(self.dispatcher.wait_idle())
        self.assertEqual([name for name, _ in calls], ["submit", "cancel"])
        self.assertEqual(calls[1][1]["request_id"], "gfr_RequestA")
        self.assertEqual(calls[1][1]["claim_token"], CLAIM_A)
        self.assertEqual(self.responses[0]["result"]["structuredContent"]["error"]["code"], "request_cancelled")

    def test_first_wait_failure_preserves_submitted_claim_for_recovery(self) -> None:
        for error_code in ("wait_capacity", "broker_unavailable"):
            with self.subTest(error_code=error_code), patch.object(mcp, "RelayClient") as client:
                client.return_value.call.side_effect = [
                    {"request": snapshot(), "claim_token": CLAIM_A},
                    RelayError(error_code, "First wait failed"),
                ]
                response = mcp.handle_request(tool_call(12, "request_consent", wait_seconds=900))
                details = response["result"]["structuredContent"]["error"]["details"]
                self.assertEqual(details["claim_token"], CLAIM_A)
                self.assertTrue(details["do_not_resubmit"])
                self.assertEqual(details["recovery_arguments"], {
                    "request_id": "gfr_RequestA", "claim_token": CLAIM_A, "wait_seconds": 0,
                })
                self.assertEqual(mcp._RPC_REQUEST_CLAIMS["12"], ("gfr_RequestA", CLAIM_A))

    def test_running_wait_cancellation_does_not_terminate_execution(self) -> None:
        entered = threading.Event()
        cancel_received = threading.Event()
        calls = []

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                calls.append(operation)
                if operation == "wait":
                    entered.set()
                    if not kwargs["cancellation_event"].wait(2):
                        raise AssertionError("wait cancellation was not signalled")
                    raise RelayError("request_cancelled", "Stopped waiting")
                if operation == "cancel":
                    cancel_received.set()
                    raise RelayError("already_started", "Command already started")
                raise AssertionError(operation)

        with patch.object(mcp, "RelayClient", Client):
            self.dispatcher.dispatch(tool_call(20))
            self.assertTrue(entered.wait(2))
            self.dispatcher.dispatch(cancellation(20))
            self.assertTrue(self.dispatcher.wait_idle())
            self.assertTrue(cancel_received.wait(2))
        self.assertNotIn("terminate", calls)

    def test_stopped_socket_wait_does_not_reply_before_cancel_acknowledgment(self) -> None:
        entered = threading.Event()
        cancel_entered = threading.Event()
        release_cancel = threading.Event()

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                if operation == "wait":
                    entered.set()
                    if not kwargs["cancellation_event"].wait(2):
                        raise AssertionError("wait cancellation was not signalled")
                    raise RelayError("request_cancelled", "Stopped local socket")
                if operation == "cancel":
                    cancel_entered.set()
                    if not release_cancel.wait(2):
                        raise AssertionError("cancel acknowledgment not released")
                    return {"request": snapshot(state="cancelled")}
                raise AssertionError(operation)

        with patch.object(mcp, "RelayClient", Client):
            self.dispatcher.dispatch(tool_call(21))
            self.assertTrue(entered.wait(2))
            self.dispatcher.dispatch(cancellation(21))
            self.assertTrue(cancel_entered.wait(2))
            self.assertFalse(self.dispatcher.wait_idle(timeout=0.01))
            self.assertFalse(self.responses)
            release_cancel.set()
            self.assertTrue(self.dispatcher.wait_idle())
        details = self.responses[0]["result"]["structuredContent"]["error"]["details"]
        self.assertEqual(details["broker_cancellation"], "acknowledged")
        self.assertEqual(details["state"], "cancelled")
        self.assertEqual(details["claim_token"], CLAIM_A)

    def test_bound_concurrency_rejects_extra_work_but_ping_remains_available(self) -> None:
        self.dispatcher.close()
        self.dispatcher = mcp.McpDispatcher(self.responses.append, max_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def call_tool(*args: object, **kwargs: object) -> dict:
            entered.set()
            if not release.wait(2):
                raise AssertionError("work not released")
            return {"ok": True}

        with patch.object(mcp, "call_tool", side_effect=call_tool) as called:
            self.dispatcher.dispatch(tool_call(1))
            self.assertTrue(entered.wait(2))
            self.dispatcher.dispatch(tool_call(2))
            self.dispatcher.dispatch(tool_call(1))
            self.dispatcher.dispatch({"id": 3, "method": "ping"})
            release.set()
            self.assertTrue(self.dispatcher.wait_idle())
        self.assertEqual(called.call_count, 1)
        self.assertTrue(any(r.get("error", {}).get("code") == -32001 for r in self.responses))
        self.assertTrue(any(r.get("error", {}).get("code") == -32600 for r in self.responses))
        self.assertIn({"jsonrpc": "2.0", "id": 3, "result": {}}, self.responses)

    def test_reused_rpc_id_only_cancels_new_claim(self) -> None:
        submitted = iter([("gfr_RequestA", CLAIM_A), ("gfr_RequestB", CLAIM_B)])
        cancel_received = threading.Event()
        cancellations = []

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                if operation == "submit":
                    request_id, claim = next(submitted)
                    return {"request": snapshot(request_id), "claim_token": claim}
                if operation == "cancel":
                    cancellations.append(kwargs)
                    cancel_received.set()
                    return {"request": snapshot("gfr_RequestB", "cancelled")}
                raise AssertionError(operation)

        with patch.object(mcp, "RelayClient", Client):
            for index in range(2):
                self.dispatcher.dispatch(tool_call(1, "request_consent", wait_seconds=0, idempotency_key=f"distinct-request-{index}"))
                self.assertTrue(self.dispatcher.wait_idle())
            self.dispatcher.dispatch(cancellation(1))
            self.assertTrue(cancel_received.wait(2))
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(cancellations[0]["request_id"], "gfr_RequestB")
        self.assertEqual(cancellations[0]["claim_token"], CLAIM_B)

    def test_terminate_has_capacity_when_normal_wait_slots_are_full(self) -> None:
        self.dispatcher.close()
        self.dispatcher = mcp.McpDispatcher(self.responses.append, max_workers=1)
        entered = threading.Event()
        terminated = threading.Event()

        class Client:
            def call(self, operation: str, **kwargs: object) -> dict:
                if operation == "wait":
                    entered.set()
                    if not terminated.wait(2):
                        raise AssertionError("terminate starved behind long wait")
                elif operation == "terminate":
                    terminated.set()
                else:
                    raise AssertionError(operation)
                return {"request": snapshot(state="terminated")}

        with patch.object(mcp, "RelayClient", Client):
            self.dispatcher.dispatch(tool_call(1))
            self.assertTrue(entered.wait(2))
            self.dispatcher.dispatch(tool_call(2, "terminate_command"))
            self.assertTrue(self.dispatcher.wait_idle())
        self.assertTrue(terminated.is_set())
        self.assertEqual({response["id"] for response in self.responses}, {1, 2})
        self.assertTrue(all("error" not in response for response in self.responses))

    def test_oversized_frame_is_discarded_without_executing_its_tail(self) -> None:
        frame = b"x" * (MAX_WIRE_BYTES + 20) + b'\n{"id":7,"method":"ping"}\n'
        mcp.serve(io.BytesIO(frame), self.dispatcher)
        self.assertEqual(len(self.responses), 2)
        self.assertEqual(self.responses[0]["error"]["code"], -32600)
        self.assertEqual(self.responses[1], {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_terminate_is_explicit_and_claim_bound(self) -> None:
        with patch.object(mcp, "RelayClient") as client:
            client.return_value.call.return_value = {"request": snapshot(state="terminated")}
            result = mcp.call_tool("terminate_command", {
                "request_id": "gfr_RequestA", "claim_token": CLAIM_A,
            })
        self.assertEqual(client.return_value.call.call_args.args, ("terminate",))
        self.assertEqual(client.return_value.call.call_args.kwargs["claim_token"], CLAIM_A)
        self.assertTrue(result["polling"]["terminal"])


class RelayTransportTests(unittest.TestCase):
    def test_mcp_self_test_reports_exact_component_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(mcp, "RelayClient") as client:
            path = Path(directory) / "probe.json"
            self.assertEqual(mcp.main(["--self-test-output", str(path)]), 0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["mcp_version"], mcp.SERVER_VERSION)
            self.assertEqual(payload["mcp_build_id"], mcp.SERVER_BUILD_ID)
            self.assertEqual(payload["runtime_mode"], "source-python")
            self.assertNotIn("token", json.dumps(payload))
            client.assert_not_called()

    def test_cancelled_socket_wait_exits_without_sending_terminate(self) -> None:
        cancelled = threading.Event()

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def settimeout(self, timeout):
                pass

            def sendall(self, payload):
                request = json.loads(payload)
                self.operation = request["operation"]
                if "cancellation_event" in request:
                    raise AssertionError("Local event leaked onto the wire")

            def shutdown(self, how):
                pass

            def recv(self, count):
                cancelled.set()
                raise socket.timeout()

        connection = Connection()
        with patch("relay_protocol.socket.create_connection", return_value=connection):
            with self.assertRaises(RelayError) as caught:
                RelayClient({"host": "127.0.0.1", "port": 12345, "token": "x" * 40}).call(
                    "wait", request_id="gfr_RequestA", claim_token=CLAIM_A,
                    wait_seconds=900, cancellation_event=cancelled,
                )
        self.assertEqual(caught.exception.code, "request_cancelled")
        self.assertEqual(connection.operation, "wait")


if __name__ == "__main__":
    unittest.main()
