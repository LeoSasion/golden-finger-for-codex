"""External-only unit tests for Golden Finger for Codex.

Run these from a user-controlled terminal. They do not launch Codex or any
user-visible GUI. On Windows, hidden Tk controls verify native button
semantics and one bounded Python child verifies Job Object termination.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from build_windows_release import ignore_python_cache
from relay_broker import (
    GUI_TERMINAL_HISTORY_LIMIT,
    THEME_PALETTES,
    CircularButton,
    RequestStore,
    RelayWindow,
    RelayRequestHandler,
    RelayTCPServer,
    WindowsJob,
    WINDOW_HIT_SHIELD_OPACITY,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_SNAP_MIN_WIDTH,
    antialiased_circle_coverage,
    acrylic_gradient_color,
    circular_button_visual_state,
    collect_install_diagnostics,
    contrast_ratio,
    decision_button_layout,
    ellipsize_display_text,
    empty_state_copy,
    execution_mode_label,
    fit_summary_lines,
    format_install_diagnostics,
    load_ui_settings,
    normalize_poll_policy,
    read_codex_theme_preference,
    relay_connection_summary,
    responsive_window_layout,
    resolve_computer_use_guidance,
    resolve_theme,
    safe_transparent_client,
    save_ui_settings,
    write_broker_self_test,
)
from codex_lifecycle import (
    APP_TOOLS_SCHEMA_VERSION,
    CodexLifecycleError,
    CodexTask,
    CodexTaskInventory,
    CodexTaskMonitor,
    extract_codex_thread_id,
    is_codex_desktop_executable,
    parse_list_threads_payload,
)
import relay_mcp_server
from relay_recovery import RecoveryJournal
from relay_mcp_server import TOOLS, _polling_directive, handle_request
from relay_protocol import (
    CLAIM_TOKEN_PREFIX,
    canonical_hash,
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_PENDING_POLL_CYCLES,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    RUNTIME_BUILD_ID,
    RelayError,
    RelayClient,
    read_endpoint,
    new_client_instance_id,
    normalize_request,
    verify_script_dependencies,
)


CLIENT_A = "gfi_" + "A" * 32
CLIENT_B = "gfi_" + "B" * 32
TEST_CLAIM = CLAIM_TOKEN_PREFIX + "T" * 43
THREAD_A = "01a0382e-9681-7082-b7a6-62884a6da9da"
THREAD_B = "01a03f18-7fc6-7172-9b0d-8d898f903224"


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def inventory_payload() -> dict[str, object]:
        return {
            "schemaVersion": APP_TOOLS_SCHEMA_VERSION,
            "pinnedThreads": [
                {
                    "id": THREAD_A,
                    "title": "  当前\x00任务\u202e  ",
                    "status": "active",
                    "cwd": r"C:\finger",
                    "kind": "codex",
                    "updatedAt": 10,
                }
            ],
            "threads": [
                {
                    "id": THREAD_B,
                    "title": "Idle task",
                    "status": "idle",
                    "cwd": r"C:\bridge",
                    "kind": "codex",
                    "updatedAt": 20,
                }
            ],
            "unavailableHosts": [],
            "unavailableSources": [],
        }

    def test_thread_metadata_extraction_is_bounded_to_host_ids(self) -> None:
        self.assertEqual(
            extract_codex_thread_id({"openai/threadId": THREAD_A.upper()}),
            THREAD_A,
        )
        self.assertEqual(
            extract_codex_thread_id(
                {"x-codex-turn-metadata": '{"thread_id":"' + THREAD_B + '"}'}
            ),
            THREAD_B,
        )
        self.assertIsNone(extract_codex_thread_id({"threadId": "../../fake"}))

    def test_inventory_includes_pinned_tasks_and_sanitizes_display_text(self) -> None:
        tasks = parse_list_threads_payload(self.inventory_payload())
        self.assertEqual([task.task_id for task in tasks], [THREAD_A, THREAD_B])
        self.assertEqual(tasks[0].title, "当前 任务")
        self.assertTrue(tasks[0].active)

    def test_inventory_contract_fails_closed_on_drift_or_partial_sources(self) -> None:
        wrong_schema = self.inventory_payload()
        wrong_schema["schemaVersion"] = APP_TOOLS_SCHEMA_VERSION + 1
        with self.assertRaises(CodexLifecycleError) as raised:
            parse_list_threads_payload(wrong_schema)
        self.assertEqual(raised.exception.code, "inventory_schema_mismatch")

        partial = self.inventory_payload()
        partial["unavailableSources"] = ["remote"]
        with self.assertRaises(CodexLifecycleError) as raised:
            parse_list_threads_payload(partial)
        self.assertEqual(raised.exception.code, "inventory_partial")

        unsupported = self.inventory_payload()
        unsupported["threads"][0]["status"] = "newFutureState"  # type: ignore[index]
        with self.assertRaises(CodexLifecycleError) as raised:
            parse_list_threads_payload(unsupported)
        self.assertEqual(raised.exception.code, "inventory_status_unsupported")

        truncated = self.inventory_payload()
        truncated["pinnedThreads"] = []
        truncated["threads"] = [
            {
                "id": f"00000000-0000-0000-0000-{index:012x}",
                "title": f"Task {index}",
                "status": "idle",
                "cwd": r"C:\finger",
                "kind": "codex",
                "updatedAt": index,
            }
            for index in range(50)
        ]
        with self.assertRaises(CodexLifecycleError) as raised:
            parse_list_threads_payload(truncated)
        self.assertEqual(raised.exception.code, "inventory_truncated")

    def test_inventory_freshness_expires_before_restart_can_use_it(self) -> None:
        stale = CodexTaskInventory(
            available=True,
            tasks=(),
            checked_monotonic=time.monotonic() - 60,
            source_count=1,
        )
        self.assertFalse(stale.is_fresh())

    def test_inventory_monitor_uses_only_registered_thread_ids(self) -> None:
        observed: list[tuple[str, ...]] = []

        def fake_probe(values: object) -> CodexTaskInventory:
            normalized = tuple(values)  # type: ignore[arg-type]
            observed.append(normalized)
            return CodexTaskInventory(
                available=True,
                tasks=(),
                checked_monotonic=time.monotonic(),
                source_count=1,
            )

        monitor = CodexTaskMonitor(probe=fake_probe)
        monitor.register_thread_ids(["invalid", THREAD_A, THREAD_A])
        snapshot = monitor.probe_now()
        self.assertTrue(snapshot.is_fresh())
        self.assertEqual(observed, [(THREAD_A,)])

    def test_codex_desktop_executable_allowlist_is_exact(self) -> None:
        self.assertTrue(
            is_codex_desktop_executable(
                r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3_x64__test\app\ChatGPT.exe"
            )
        )
        self.assertFalse(is_codex_desktop_executable(r"C:\Temp\ChatGPT.exe"))
        self.assertFalse(is_codex_desktop_executable(r"C:\Windows\System32\cmd.exe"))


class ProtocolTests(unittest.TestCase):
    def test_consent_request_is_hashed_and_frozen(self) -> None:
        frozen = normalize_request(
            "consent",
            {
                "exact_action": "Allow the current bounded validation step.",
                "target": "one test target",
                "risk": "No external side effect.",
                "recovery": "No recovery is needed.",
                "idempotency_key": "test-consent-0001",
                "approved_reply": "同意",
            },
        )
        self.assertEqual(frozen["kind"], "consent")
        self.assertEqual(len(frozen["request_hash"]), 64)
        self.assertEqual(frozen["approved_reply"], "同意")
        self.assertEqual(frozen["expires_in_seconds"], 9_000)

    def test_source_labels_are_frozen_but_explicitly_human_readable(self) -> None:
        frozen = normalize_request(
            "consent",
            {
                "exact_action": "Authorize one test step.",
                "target": "one test target",
                "risk": "No external side effect.",
                "recovery": "No recovery is needed.",
                "idempotency_key": "test-source-labels-0001",
                "source_label": "C:\\finger validation",
                "source_project": "C:\\finger",
                "source_task": "Golden Finger contract tests",
            },
        )
        self.assertEqual(frozen["source_project"], "C:\\finger")
        self.assertEqual(frozen["source_task"], "Golden Finger contract tests")

    def test_command_defaults_to_fixed_budget_and_rejects_above_hard_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            payload = {
                "exact_action": "Wait for one bounded test command.",
                "target": "test process",
                "risk": "A process would run only after approval.",
                "recovery": "The bounded timeout terminates the process.",
                "idempotency_key": "test-command-hour-0001",
                "executable": sys.executable,
                "arguments": ["-c", "print('ok')"],
                "working_directory": temporary_directory,
            }
            frozen = normalize_request("command", payload)
            self.assertEqual(
                frozen["expires_in_seconds"], DEFAULT_APPROVAL_EXPIRY_SECONDS
            )
            self.assertEqual(
                frozen["timeout_seconds"], DEFAULT_COMMAND_TIMEOUT_SECONDS
            )

            too_long = dict(payload)
            too_long["idempotency_key"] = "test-command-hour-0002"
            too_long["timeout_seconds"] = MAX_COMMAND_TIMEOUT_SECONDS + 1
            with self.assertRaises(RelayError) as raised:
                normalize_request("command", too_long)
            self.assertEqual(raised.exception.code, "invalid_request")

    def test_reject_unknown_field(self) -> None:
        with self.assertRaises(RelayError) as raised:
            normalize_request(
                "consent",
                {
                    "exact_action": "One action",
                    "target": "One target",
                    "risk": "One risk",
                    "recovery": "One recovery",
                    "idempotency_key": "test-consent-0002",
                    "hidden_instruction": "must not pass",
                },
            )
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_routing_scope_is_not_part_of_the_frozen_request(self) -> None:
        with self.assertRaises(RelayError) as raised:
            normalize_request(
                "consent",
                {
                    "exact_action": "Keep routing preference outside the request.",
                    "target": "one authorization card",
                    "risk": "Scope must not become standing consent.",
                    "recovery": "Remove scope from the proposal.",
                    "idempotency_key": "test-scope-not-protocol-0001",
                    "scope": "project",
                },
            )
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_command_resolves_interpreter_and_rejects_codex_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            frozen = normalize_request(
                "command",
                {
                    "exact_action": "Print a test value.",
                    "target": "test stdout",
                    "risk": "A process would run if approved; this test does not approve it.",
                    "recovery": "No files are changed.",
                    "idempotency_key": "test-command-0001",
                    "executable": sys.executable,
                    "arguments": ["-c", "print('ok')"],
                    "working_directory": temporary_directory,
                },
            )
            self.assertTrue(Path(frozen["executable"]).is_absolute())
            self.assertEqual(frozen["arguments"][0], "-c")

            with self.assertRaises(RelayError) as raised:
                normalize_request(
                    "command",
                    {
                        "exact_action": "Forbidden self-control.",
                        "target": "Codex App Server",
                        "risk": "Would cross the protected boundary.",
                        "recovery": "Do not start it.",
                        "idempotency_key": "test-command-0002",
                        "executable": sys.executable,
                        "arguments": ["codex", "app-server"],
                        "working_directory": temporary_directory,
                    },
                )
            self.assertEqual(raised.exception.code, "protected_target")

    def test_direct_script_bytes_are_frozen_and_reverified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / "approved.py"
            script.write_text("print('approved')\n", encoding="utf-8")
            frozen = normalize_request(
                "command",
                {
                    "exact_action": "Run one direct Python script.",
                    "target": str(script),
                    "risk": "The script is not executed by this unit test.",
                    "recovery": "Delete the temporary directory.",
                    "idempotency_key": "test-script-freeze-0001",
                    "executable": sys.executable,
                    "arguments": [str(script)],
                    "working_directory": temporary_directory,
                },
            )
            dependencies = frozen["script_dependencies"]
            self.assertEqual(len(dependencies), 1)
            self.assertEqual(dependencies[0]["path"], str(script.resolve()))
            self.assertEqual(verify_script_dependencies(dependencies), [])
            script.write_text("print('changed')\n", encoding="utf-8")
            mismatches = verify_script_dependencies(dependencies)
            self.assertEqual(len(mismatches), 1)
            self.assertNotEqual(
                mismatches[0]["expected_sha256"],
                mismatches[0]["observed_sha256"],
            )

    def test_inline_python_has_no_script_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            frozen = normalize_request(
                "command",
                {
                    "exact_action": "Keep one inline command visible.",
                    "target": "test stdout",
                    "risk": "The command is not executed by this unit test.",
                    "recovery": "No recovery is needed.",
                    "idempotency_key": "test-script-freeze-0002",
                    "executable": sys.executable,
                    "arguments": ["-c", "print('inline')"],
                    "working_directory": temporary_directory,
                },
            )
            self.assertEqual(frozen["script_dependencies"], [])

    def test_endpoint_requires_exact_component_version_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            endpoint = Path(temporary_directory) / "endpoint.json"
            metadata = {
                "protocol": PROTOCOL_VERSION,
                "broker_version": PACKAGE_VERSION,
                "broker_build_id": "wrong-build",
                "host": "127.0.0.1",
                "port": 12345,
                "token": "T" * 32,
                "pid": 123,
                "started_at": "2026-08-29T00:00:00Z",
            }
            endpoint.write_text(__import__("json").dumps(metadata), encoding="ascii")
            with patch("relay_protocol.endpoint_path", return_value=endpoint):
                with self.assertRaises(RelayError) as raised:
                    read_endpoint()
            self.assertEqual(raised.exception.code, "component_mismatch")
            metadata["broker_build_id"] = RUNTIME_BUILD_ID
            endpoint.write_text(__import__("json").dumps(metadata), encoding="ascii")
            with patch("relay_protocol.endpoint_path", return_value=endpoint):
                self.assertEqual(read_endpoint()["broker_version"], PACKAGE_VERSION)


class StoreTests(unittest.TestCase):
    def test_wire_handshake_rejects_a_same_version_different_build(self) -> None:
        store = RequestStore(elevated=False)
        server = RelayTCPServer(
            ("127.0.0.1", 0),
            RelayRequestHandler,
            token="wire-test-token-" + "T" * 24,
            store=store,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        client = RelayClient(
            {
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "token": server.token,
            }
        )
        try:
            with self.assertRaises(RelayError) as raised:
                client.call(
                    "health",
                    client_build_id="same-version-wrong-build",
                    client_instance_id=CLIENT_A,
                )
            self.assertEqual(raised.exception.code, "component_mismatch")
            self.assertIsNone(server.connector_snapshot())
            health = client.call("health", client_instance_id=CLIENT_A)
            self.assertEqual(health["broker_build_id"], RUNTIME_BUILD_ID)
            self.assertEqual(
                server.connector_snapshot()["connector_fingerprint"],
                canonical_hash(CLIENT_A)[:12],
            )
        finally:
            store.shutdown()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_gui_consent_decision_is_one_shot(self) -> None:
        store = RequestStore(elevated=False)
        submitted, replay, claim_token = store.submit(
            "consent",
            {
                "exact_action": "Continue one bounded test.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-0001",
                "approved_reply": "授权",
            },
            CLIENT_A,
        )
        self.assertFalse(replay)
        self.assertTrue(claim_token.startswith(CLAIM_TOKEN_PREFIX))
        self.assertNotIn("claim_token", submitted)
        self.assertNotIn("client_instance_id", submitted)
        self.assertEqual(len(submitted["claim_fingerprint"]), 12)
        approved = store.approve_from_gui(submitted["request_id"])
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["result"]["reply_text"], "授权")
        with self.assertRaises(RelayError):
            store.approve_from_gui(submitted["request_id"])

    def test_gui_stop_is_neither_decision_and_wakes_a_long_wait(self) -> None:
        store = RequestStore(elevated=False)
        submitted, _replay, claim_token = store.submit(
            "consent",
            {
                "exact_action": "Wait until the user stops this test.",
                "target": "one pending test request",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-stop-0001",
                "approved_reply": "同意",
            },
            CLIENT_A,
        )
        request_id = submitted["request_id"]
        waiter_started = threading.Event()
        result: dict[str, object] = {}

        def wait_for_result() -> None:
            waiter_started.set()
            result["snapshot"] = store.wait(
                request_id,
                claim_token,
                DEFAULT_POLL_WAIT_SECONDS,
            )

        waiter = threading.Thread(target=wait_for_result, daemon=True)
        waiter.start()
        try:
            self.assertTrue(waiter_started.wait(timeout=1))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                snapshot = store.status(request_id, claim_token)
                if snapshot["poll_policy"]["pending_polls_used"] == 1:
                    break
                time.sleep(0.005)
            self.assertEqual(
                store.status(request_id, claim_token)["poll_policy"][
                    "pending_polls_used"
                ],
                1,
            )

            stopped_at = time.monotonic()
            stopped = store.cancel_from_gui(request_id)
            waiter.join(timeout=2)
            self.assertFalse(waiter.is_alive())
            self.assertLess(time.monotonic() - stopped_at, 2)
            self.assertEqual(stopped["state"], "cancelled")
            self.assertEqual(stopped["receipt"]["human_action"], "stopped_waiting_once")
            self.assertEqual(stopped["result"]["decision"], "cancelled")
            self.assertEqual(stopped["result"]["reason_code"], "stopped_by_user")
            self.assertNotIn("reply_text", stopped["result"])
            self.assertEqual(result["snapshot"], stopped)
            with self.assertRaises(RelayError):
                store.cancel_from_gui(request_id)
        finally:
            if waiter.is_alive():
                store.shutdown()
                waiter.join(timeout=2)

    def test_restart_gate_is_atomic_and_thread_binding_stays_weak(self) -> None:
        store = RequestStore(elevated=False)
        submitted, _replay, _claim_token = store.submit(
            "consent",
            {
                "exact_action": "Approve one correlated request.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "Reject the request.",
                "idempotency_key": "test-restart-gate-0001",
            },
            CLIENT_A,
            THREAD_A,
        )
        row = store.rows_for_gui()[0]
        self.assertEqual(row["codex_thread_id"], THREAD_A)
        self.assertNotIn("codex_thread_id", submitted)
        acquired, reason = store.try_begin_codex_restart()
        self.assertFalse(acquired)
        self.assertEqual(reason, "relay_requests_active")

        store.reject_from_gui(submitted["request_id"])
        acquired, reason = store.try_begin_codex_restart()
        self.assertTrue(acquired)
        self.assertEqual(reason, "restart_gate_acquired")
        with self.assertRaises(RelayError) as raised:
            store.submit(
                "consent",
                {
                    "exact_action": "Do not enter while the restart gate is held.",
                    "target": "test state",
                    "risk": "No external effect.",
                    "recovery": "Release the gate.",
                    "idempotency_key": "test-restart-gate-0002",
                },
                CLIENT_A,
            )
        self.assertEqual(raised.exception.code, "broker_restarting_codex")
        store.end_codex_restart()
        self.assertFalse(store.restart_is_pending())

    def test_idempotency_conflict_fails_closed(self) -> None:
        store = RequestStore(elevated=False)
        base = {
            "exact_action": "Action A",
            "target": "Target",
            "risk": "Risk",
            "recovery": "Recovery",
            "idempotency_key": "test-store-0002",
        }
        _submitted, _replay, first_claim = store.submit("consent", base, CLIENT_A)
        fixed_policy = store.update_poll_policy(
            {
                "sound_enabled": True,
                "poll_wait_seconds": 10,
                "max_pending_polls": 12,
                "command_timeout_seconds": 600,
            }
        )
        self.assertEqual(fixed_policy["wait_seconds"], 900)
        self.assertEqual(fixed_policy["max_pending_polls"], 10)
        replayed, replay, replay_claim = store.submit("consent", base, CLIENT_A)
        self.assertTrue(replay)
        self.assertEqual(replay_claim, first_claim)
        self.assertEqual(replayed["poll_policy"]["wait_seconds"], 900)
        changed = dict(base)
        changed["exact_action"] = "Action B"
        with self.assertRaises(RelayError) as raised:
            store.submit("consent", changed, CLIENT_A)
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_request_id_alone_cannot_observe_or_cancel_another_claim(self) -> None:
        store = RequestStore(elevated=False)
        submitted, _replay, claim_token = store.submit(
            "consent",
            {
                "exact_action": "Protect one claimed request.",
                "target": "claimed test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-claim-0001",
            },
            CLIENT_A,
        )
        request_id = submitted["request_id"]
        self.assertTrue(request_id.startswith("gfr_"))
        self.assertEqual(store.status(request_id, claim_token)["state"], "pending")

        for wrong_claim in (None, CLAIM_TOKEN_PREFIX + "Z" * 43):
            with self.assertRaises(RelayError) as raised:
                store.status(request_id, wrong_claim)
            self.assertEqual(raised.exception.code, "claim_mismatch")
            with self.assertRaises(RelayError) as raised:
                store.cancel(request_id, wrong_claim)
            self.assertEqual(raised.exception.code, "claim_mismatch")

        self.assertEqual(store.status(request_id, claim_token)["state"], "pending")

    def test_idempotency_is_namespaced_by_connector_instance(self) -> None:
        store = RequestStore(elevated=False)
        proposal = {
            "exact_action": "Create one connector-scoped request.",
            "target": "connector isolation test",
            "risk": "No external effect.",
            "recovery": "Reject either independent card.",
            "idempotency_key": "test-store-scope-0001",
        }
        first, first_replay, first_claim = store.submit(
            "consent",
            proposal,
            CLIENT_A,
        )
        second, second_replay, second_claim = store.submit(
            "consent",
            proposal,
            CLIENT_B,
        )
        self.assertFalse(first_replay)
        self.assertFalse(second_replay)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first_claim, second_claim)
        self.assertNotEqual(
            first["claim_fingerprint"],
            second["claim_fingerprint"],
        )
        first_detail = store.detail_for_gui(first["request_id"])["frozen"]
        second_detail = store.detail_for_gui(second["request_id"])["frozen"]
        self.assertEqual(len(first_detail["connector_fingerprint"]), 12)
        self.assertNotEqual(
            first_detail["connector_fingerprint"],
            second_detail["connector_fingerprint"],
        )

    def test_connector_instance_ids_are_random_and_bounded(self) -> None:
        first = new_client_instance_id()
        second = new_client_instance_id()
        self.assertTrue(first.startswith("gfi_"))
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 132)

    def test_fixed_policy_is_frozen_and_only_total_deadline_expires(self) -> None:
        store = RequestStore(
            elevated=False,
            settings={
                "sound_enabled": True,
                "poll_wait_seconds": 10,
                "max_pending_polls": 3,
                "command_timeout_seconds": 120,
            },
        )
        submitted, _replay, claim_token = store.submit(
            "consent",
            {
                "exact_action": "Wait for a bounded polling test.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-policy-0001",
                "approved_reply": "同意",
            },
            CLIENT_A,
        )
        self.assertEqual(submitted["poll_policy"]["wait_seconds"], 900)
        self.assertEqual(submitted["poll_policy"]["max_pending_polls"], 10)
        self.assertEqual(submitted["poll_policy"]["pending_wait_budget_seconds"], 9_000)
        self.assertEqual(submitted["poll_policy"]["command_timeout_seconds"], 9_000)
        request_id = submitted["request_id"]
        with store._condition:
            store._records[request_id].pending_polls_used = 10
        snapshot = store.wait(request_id, claim_token, 0.01)
        self.assertEqual(snapshot["state"], "pending")
        self.assertEqual(snapshot["poll_policy"]["pending_polls_used"], 11)
        self.assertEqual(snapshot["poll_policy"]["expiry_basis"], "monotonic_deadline")
        self.assertIsNone(snapshot["poll_policy"]["pending_polls_remaining"])
        with store._condition:
            store._records[request_id].expires_monotonic = time.monotonic() - 1
            store._next_expiry_check_monotonic = 0
        self.assertEqual(store.wait(request_id, claim_token, 0)["state"], "expired")

    def test_legacy_policy_inputs_are_ignored_in_favor_of_fixed_policy(self) -> None:
        policy = normalize_poll_policy(
            {
                "poll_wait_seconds": 10,
                "max_pending_polls": 1,
            }
        )
        self.assertEqual(policy["wait_seconds"], 900)
        self.assertEqual(policy["max_pending_polls"], 10)
        self.assertEqual(policy["pending_wait_budget_seconds"], 9_000)
        self.assertEqual(policy["command_timeout_seconds"], 9_000)

    def test_fixed_policy_sets_pending_and_command_caps(self) -> None:
        store = RequestStore(
            elevated=False,
            settings={
                "sound_enabled": True,
                "poll_wait_seconds": 50,
                "max_pending_polls": 100,
                "command_timeout_seconds": 5_000,
            },
        )
        submitted, _replay, _claim_token = store.submit(
            "consent",
            {
                "exact_action": "Use the configured default pending budget.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-policy-0002",
                "approved_reply": "同意",
            },
            CLIENT_A,
        )
        detail = store.detail_for_gui(submitted["request_id"])
        self.assertEqual(detail["frozen"]["expires_in_seconds"], 9_000)
        with tempfile.TemporaryDirectory() as temporary_directory:
            command, _replay, _claim_token = store.submit(
                "command",
                {
                    "exact_action": "Use the configured default command timeout.",
                    "target": "test process",
                    "risk": "The unit test never approves this request.",
                    "recovery": "No process is started.",
                    "idempotency_key": "test-store-policy-0003",
                    "executable": sys.executable,
                    "arguments": ["-c", "print('not started')"],
                    "working_directory": temporary_directory,
                },
                CLIENT_A,
            )
            command_detail = store.detail_for_gui(command["request_id"])
            self.assertEqual(command_detail["frozen"]["timeout_seconds"], 9_000)

    def test_graceful_window_shutdown_marks_pending_request_with_reason(self) -> None:
        store = RequestStore(elevated=False)
        submitted, _replay, claim_token = store.submit(
            "consent",
            {
                "exact_action": "Wait for the window-shutdown contract test.",
                "target": "one pending test card",
                "risk": "No external effect.",
                "recovery": "The store shutdown cancels the card.",
                "idempotency_key": "test-window-shutdown-0001",
                "approved_reply": "同意",
            },
            CLIENT_A,
        )
        store.shutdown()
        self.assertTrue(store.is_stopping())
        snapshot = store.status(submitted["request_id"], claim_token)
        self.assertEqual(snapshot["state"], "cancelled")
        self.assertEqual(snapshot["result"]["reason_code"], "relay_window_closed")

        with self.assertRaises(RelayError) as raised:
            store.submit(
                "consent",
                {
                    "exact_action": "Do not accept a new card while closing.",
                    "target": "stopping Broker",
                    "risk": "No external effect.",
                    "recovery": "Restart the window or cancel the scope.",
                    "idempotency_key": "test-window-shutdown-0002",
                },
                CLIENT_A,
            )
        self.assertEqual(raised.exception.code, "broker_stopping")

    def test_gui_snapshot_is_revisioned_and_caps_only_terminal_history(self) -> None:
        store = RequestStore(elevated=False)
        active, _replay, _claim = store.submit(
            "consent",
            {
                "exact_action": "Keep the oldest request active.",
                "target": "bounded GUI history",
                "risk": "No external effect.",
                "recovery": "Reject the request.",
                "idempotency_key": "test-gui-cache-active",
            },
            CLIENT_A,
        )
        first_revision, first_rows, pending, running = store.gui_snapshot()
        self.assertIsNotNone(first_rows)
        self.assertEqual((pending, running), (1, 0))
        same_revision, unchanged_rows, pending, running = store.gui_snapshot(first_revision)
        self.assertEqual(same_revision, first_revision)
        self.assertIsNone(unchanged_rows)
        self.assertEqual((pending, running), (1, 0))

        for index in range(GUI_TERMINAL_HISTORY_LIMIT + 5):
            submitted, _replay, _claim = store.submit(
                "consent",
                {
                    "exact_action": f"Reject terminal request {index}.",
                    "target": "bounded GUI history",
                    "risk": "No external effect.",
                    "recovery": "No recovery required.",
                    "idempotency_key": f"test-gui-cache-terminal-{index:04d}",
                },
                CLIENT_A,
            )
            store.reject_from_gui(str(submitted["request_id"]))

        final_revision, final_rows, pending, running = store.gui_snapshot(first_revision)
        assert final_rows is not None
        terminal_rows = [
            row for row in final_rows if row["state"] not in {"pending", "running"}
        ]
        self.assertGreater(final_revision, first_revision)
        self.assertEqual(len(terminal_rows), GUI_TERMINAL_HISTORY_LIMIT)
        self.assertEqual(len(final_rows), GUI_TERMINAL_HISTORY_LIMIT + 1)
        self.assertIn(active["request_id"], {row["request_id"] for row in final_rows})
        self.assertEqual((pending, running), (1, 0))


class ComputerUseGuidanceTests(unittest.TestCase):
    @staticmethod
    def write_guidance(codex_home: Path, version: str) -> Path:
        path = (
            codex_home / "plugins" / "cache" / "openai-bundled"
            / "computer-use" / version / "docs" / "guidance.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Test guidance\n", encoding="utf-8")
        return path.resolve()

    @staticmethod
    def write_catalog(codex_home: Path, version: str) -> Path:
        source = codex_home / "bundled marketplace"
        catalog = source / ".agents" / "plugins" / "marketplace.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(
            json.dumps({"plugins": [{
                "name": "computer-use",
                "source": {"source": "local", "path": "./plugins/computer-use"},
            }]}),
            encoding="utf-8",
        )
        manifest = source / "plugins" / "computer-use" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"name": "computer-use", "version": version}),
            encoding="utf-8",
        )
        (codex_home / "config.toml").write_text(
            "[marketplaces.openai-bundled]\nsource_type = 'local'\n"
            f"source = {json.dumps(str(source))}\n",
            encoding="utf-8",
        )
        return catalog

    def test_resolver_honors_custom_codex_home_with_spaces_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root) / "另一用户" / "custom codex home"
            expected = self.write_guidance(home, "27.123.456")
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertEqual(resolve_computer_use_guidance(), expected.resolve())

    def test_resolver_defaults_to_the_current_users_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            profile = Path(temp_root) / "different-user"
            expected = self.write_guidance(profile / ".codex", "27.1.2")
            with patch.dict(os.environ, {"CODEX_HOME": ""}), patch(
                "relay_broker.Path.home", return_value=profile
            ):
                self.assertEqual(resolve_computer_use_guidance(), expected.resolve())

    def test_resolver_compares_versions_numerically_and_skips_incomplete_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            self.write_guidance(home, "26.9.999")
            expected = self.write_guidance(home, "26.10.1")
            self.write_guidance(home, "latest")
            (expected.parents[2] / "26.99.1" / "docs").mkdir(parents=True)
            self.assertEqual(resolve_computer_use_guidance(home), expected)

    def test_current_catalog_version_wins_over_newer_leftover_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            expected = self.write_guidance(home, "26.10.1")
            self.write_guidance(home, "27.1.1")
            self.write_catalog(home, "26.10.1")
            self.assertEqual(resolve_computer_use_guidance(home), expected)

    def test_missing_current_version_falls_back_to_an_installed_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            expected = self.write_guidance(home, "26.10.1")
            self.write_catalog(home, "27.1.1")
            self.assertEqual(resolve_computer_use_guidance(home), expected)

    def test_broken_catalog_and_config_do_not_hide_installed_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            expected = self.write_guidance(home, "26.10.1")
            catalog = self.write_catalog(home, "26.10.1")
            catalog.write_text("broken JSON", encoding="utf-8")
            self.assertEqual(resolve_computer_use_guidance(home), expected)
            (home / "config.toml").write_text("[broken TOML", encoding="utf-8")
            self.assertEqual(resolve_computer_use_guidance(home), expected)

    def test_catalog_version_cannot_escape_the_plugin_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            expected = self.write_guidance(home, "26.10.1")
            self.write_catalog(home, "../../some-other-directory")
            self.assertEqual(resolve_computer_use_guidance(home), expected)

    def test_resolver_observes_new_versions_on_later_clicks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            older = self.write_guidance(home, "26.10.1")
            self.assertEqual(resolve_computer_use_guidance(home), older)
            newer = self.write_guidance(home, "27.1.1")
            self.assertEqual(resolve_computer_use_guidance(home), newer)

    def test_missing_guidance_explains_where_to_check_and_how_to_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            home = Path(temp_root)
            with self.assertRaises(FileNotFoundError) as raised:
                resolve_computer_use_guidance(home)
            self.assertIn(str(home.resolve() / "plugins" / "cache"), str(raised.exception))
            self.assertIn("安装或更新", str(raised.exception))

    def test_button_reresolves_and_uses_the_system_default_open_verb(self) -> None:
        window = RelayWindow.__new__(RelayWindow)
        window._auto_approval_modal_depth = 0
        window._auto_approve_started_at = {}
        window.root = object()
        paths = [Path("C:/user-one/guide.md"), Path("D:/codex-home/guide.md")]
        with patch("relay_broker.resolve_computer_use_guidance", side_effect=paths) as resolve, patch(
            "relay_broker.os.startfile", create=True
        ) as startfile:
            window.open_computer_use_guidance()
            window.open_computer_use_guidance()
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(startfile.call_args_list[0].args, (str(paths[0]),))
        self.assertEqual(startfile.call_args_list[1].args, (str(paths[1]),))
        self.assertEqual(startfile.call_args_list[0].kwargs, {})

    def test_button_reports_missing_files_without_launching_an_app(self) -> None:
        window = RelayWindow.__new__(RelayWindow)
        window._auto_approval_modal_depth = 0
        window._auto_approve_started_at = {}
        window.root = object()
        with patch(
            "relay_broker.resolve_computer_use_guidance",
            side_effect=FileNotFoundError("请安装或更新 Computer Use 插件"),
        ), patch("relay_broker.os.startfile", create=True) as startfile, patch(
            "relay_broker.messagebox.showerror"
        ) as showerror:
            window.open_computer_use_guidance()
        startfile.assert_not_called()
        self.assertIn("安装或更新", showerror.call_args.args[1])
        self.assertIs(showerror.call_args.kwargs["parent"], window.root)

    def test_button_reports_default_app_failures_with_recovery_advice(self) -> None:
        window = RelayWindow.__new__(RelayWindow)
        window._auto_approval_modal_depth = 0
        window._auto_approve_started_at = {}
        window.root = object()
        guidance = Path("C:/some-user/guidance.md")
        with patch(
            "relay_broker.resolve_computer_use_guidance", return_value=guidance
        ), patch(
            "relay_broker.os.startfile", create=True,
            side_effect=OSError("No application is associated with this file"),
        ), patch("relay_broker.messagebox.showerror") as showerror:
            window.open_computer_use_guidance()
        self.assertIn(str(guidance), showerror.call_args.args[1])
        self.assertIn("默认打开软件", showerror.call_args.args[1])


class AppearanceTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def hidden_relay():
        root = tk.Tk()
        root.withdraw()
        store = RequestStore(elevated=False)
        server = RelayTCPServer(
            ("127.0.0.1", 0), RelayRequestHandler, token="layout-test", store=store
        )
        with patch("relay_broker.CodexTaskMonitor.start"), patch(
            "relay_broker.apply_native_window_appearance", return_value=False
        ), patch("relay_broker.read_codex_theme_preference", return_value="light"), patch.object(
            RelayWindow, "_notify_new_request"
        ), patch.object(RelayWindow, "_activate_new_request_window"):
            try:
                window = RelayWindow(
                    root, store=store, server=server, token="layout-test",
                    settings={"sound_enabled": False},
                )
                yield window
            finally:
                window.closing = True
                window.task_monitor.stop()
                server.server_close()
                for job in root.tk.call("after", "info"):
                    root.after_cancel(job)
                root.destroy()

    def test_summary_line_budget_preserves_short_copy_and_marks_long_copy(self) -> None:
        self.assertEqual(
            fit_summary_lines("  一次  操作  ", measure=len, width=20, max_lines=2),
            "一次 操作",
        )
        for width in (12, 20, 30):
            preview = fit_summary_lines(
                "请核对这个很长的单次动作和 exact frozen target before approval。" * 3,
                measure=len, width=width, max_lines=2,
            )
            self.assertLessEqual(len(preview.splitlines()), 2)
            self.assertTrue(preview.endswith("…"))
            self.assertTrue(all(len(line) <= width for line in preview.splitlines()))
        self.assertEqual(fit_summary_lines("", measure=len, width=20, max_lines=2), "")

    def test_summary_uses_actual_native_font_measure(self) -> None:
        from tkinter import font as tkfont
        root = tk.Tk()
        root.withdraw()
        try:
            font = tkfont.Font(root=root, family="Microsoft YaHei UI", size=13, weight="bold")
            preview = fit_summary_lines(
                "执行一次可核对的 Golden Finger 界面检查并保留完整冻结参数。" * 4,
                measure=font.measure, width=240, max_lines=2,
            )
            self.assertEqual(len(preview.splitlines()), 2)
            self.assertTrue(preview.endswith("…"))
            self.assertTrue(all(font.measure(line) <= 240 for line in preview.splitlines()))
        finally:
            root.destroy()

    def test_empty_and_read_only_modes_keep_disabled_native_controls(self) -> None:
        with self.hidden_relay() as window:
            for mode in ("empty", "task"):
                window.current_request_id = "stale-selection"
                for button in (window.stop_button, window.approve_button, window.reject_button):
                    button.set_state(tk.NORMAL)
                window._set_decision_mode(mode)
                self.assertEqual(window.decision_stage.winfo_manager(), "grid")
                self.assertIsNone(window.current_request_id)
                for button in (window.stop_button, window.approve_button, window.reject_button):
                    self.assertFalse(button._enabled)
                    with patch.object(window.store, "approve_from_gui") as approve:
                        button._native_invoke_pending = True
                        button._drain_native_invoke()
                        approve.assert_not_called()
                with patch.object(window.store, "approve_from_gui") as approve:
                    window.approve_selected()
                    approve.assert_not_called()

    def test_request_preview_does_not_truncate_frozen_details(self) -> None:
        with self.hidden_relay() as window:
            action = "仅用于界面布局测试，不执行任何命令。" * 12
            target = "布局测试的长目标路径/" * 15
            risk = "仅提交模拟文本，不控制 Codex 或其他程序。" * 12
            snapshot, _replay, _claim = window.store.submit("consent", {
                "exact_action": action, "target": target, "risk": risk,
                "recovery": "关闭测试窗口即可。", "approved_reply": "模拟回复",
                "idempotency_key": "layout-preview-test",
            }, CLIENT_A)
            window.current_request_id = snapshot["request_id"]
            window._render_detail(snapshot["request_id"])
            self.assertTrue(window._summary_has_ellipsis)
            self.assertIn("含省略", window.detail_toggle_button.cget("text"))
            self.assertIn(action, window._detail_text)
            self.assertIn(target, window._detail_text)
            self.assertIn(risk, window._detail_text)
            self.assertEqual(window.decision_stage.winfo_manager(), "grid")
            self.assertTrue(window.approve_button._enabled)
            self.assertEqual(window.store.detail_for_gui(snapshot["request_id"])["state"], "pending")

    def test_empty_tree_prompt_is_not_a_synthetic_task_row(self) -> None:
        with self.hidden_relay() as window:
            self.assertFalse(window.tree.get_children())
            self.assertEqual(window.tree_empty_label.winfo_manager(), "place")
            self.assertIn("暂无", window.tree_empty_label.cget("text"))
            window._request_rows = {"ended": {
                "request_id": "ended", "state": "cancelled", "exact_action": "已结束模拟请求",
            }}
            window._refresh_task_tree()
            self.assertIn("筛选无结果", window.tree_empty_label.cget("text"))
            window._set_task_filter("all")
            self.assertEqual(len(window.tree.get_children()), 1)
            self.assertEqual(window.tree_empty_label.winfo_manager(), "")

    def test_read_only_task_render_cannot_reuse_a_previous_request_selection(self) -> None:
        with self.hidden_relay() as window:
            task = CodexTask(
                task_id=THREAD_A, title="模拟只读任务", status="active", cwd="C:/layout-test",
                kind="codex", updated_at=1.0,
            )
            window._codex_tasks = {task.task_id: task}
            window.current_request_id = "previous-request"
            window._render_codex_task(task.task_id)
            self.assertEqual(window.decision_stage.winfo_manager(), "grid")
            self.assertIsNone(window.current_request_id)
            self.assertFalse(window.approve_button._enabled)
            self.assertFalse(window.stop_button._enabled)
            self.assertFalse(window.reject_button._enabled)
            self.assertIn("只读", window.source_label.cget("text"))

    def test_window_baseline_keeps_fixed_chrome_visible(self) -> None:
        self.assertGreaterEqual(WINDOW_MIN_WIDTH, 920)
        self.assertGreaterEqual(WINDOW_MIN_HEIGHT, 600)
        self.assertLessEqual(WINDOW_SNAP_MIN_WIDTH, 640)

    def test_responsive_layout_preserves_two_operable_lanes_at_snap_width(self) -> None:
        compact = responsive_window_layout(WINDOW_SNAP_MIN_WIDTH)
        comfortable = responsive_window_layout(WINDOW_MIN_WIDTH)
        self.assertTrue(compact["compact"])
        self.assertFalse(comfortable["compact"])
        self.assertEqual(compact["detail_columnspan"], 2)
        self.assertGreaterEqual(compact["left_minsize"], 280)
        self.assertGreaterEqual(compact["decision_minsize"], 260)
        self.assertFalse(compact["show_wait_policy"])
        self.assertTrue(comfortable["show_wait_policy"])

    def test_status_copy_distinguishes_ready_waiting_and_inventory_failures(self) -> None:
        self.assertEqual(
            relay_connection_summary(server_open=True, connector_seen=False)[1],
            "● 本地中继就绪 · 等待 Codex",
        )
        self.assertIn(
            "最近已连接 Codex",
            relay_connection_summary(server_open=True, connector_seen=True)[1],
        )
        self.assertIn(
            "不可用",
            relay_connection_summary(server_open=False, connector_seen=False)[1],
        )
        filtered = empty_state_copy(
            task_filter="active",
            has_hidden_items=True,
            inventory_available=True,
            inventory_error_code=None,
        )
        unavailable = empty_state_copy(
            task_filter="active",
            has_hidden_items=False,
            inventory_available=False,
            inventory_error_code="probe_failed",
        )
        self.assertIn("全部", filtered[1])
        self.assertIn("不可用", unavailable[0])

    @unittest.skipUnless(os.name == "nt", "Native button semantics are Windows-only")
    def test_circular_decision_control_exposes_native_button_semantics(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            invocations: list[str] = []
            button = CircularButton(
                root,
                text="同意\n本次",
                command=lambda: invocations.append("approved"),
                variant="primary",
                diameter=120,
            )
            root.update_idletasks()
            hwnd = button.winfo_id()
            class_buffer = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            name_buffer = ctypes.create_unicode_buffer(64)
            ctypes.windll.user32.GetWindowTextW(hwnd, name_buffer, len(name_buffer))
            self.assertEqual(class_buffer.value, "Button")
            self.assertEqual(name_buffer.value, "同意 本次")
            self.assertTrue(ctypes.windll.user32.IsWindowEnabled(hwnd))
            self.assertTrue(button._native_subclass_installed)
            button._native_invoke_pending = True
            button._drain_native_invoke()
            self.assertEqual(invocations, ["approved"])
            button.set_state(tk.DISABLED)
            self.assertFalse(ctypes.windll.user32.IsWindowEnabled(hwnd))
            button._native_invoke_pending = True
            button._drain_native_invoke()
            self.assertEqual(invocations, ["approved"])
            owner = button.winfo_toplevel()
            existing_job = getattr(owner, "_golden_finger_native_drain_job", None)
            if existing_job is not None:
                owner.after_cancel(existing_job)
                setattr(owner, "_golden_finger_native_drain_job", None)
            CircularButton._drain_native_invoke_group(owner)
            idle_job = getattr(owner, "_golden_finger_native_drain_job", None)
            self.assertIsNotNone(idle_job)
            button.set_state(tk.NORMAL)
            active_job = getattr(owner, "_golden_finger_native_drain_job", None)
            self.assertIsNotNone(active_job)
            self.assertNotEqual(active_job, idle_job)
        finally:
            root.destroy()

    def test_circular_button_has_antialiased_edges_and_three_interaction_states(self) -> None:
        self.assertEqual(antialiased_circle_coverage(10.0, 10.0), 0.5)
        self.assertEqual(antialiased_circle_coverage(9.0, 10.0), 1.0)
        self.assertEqual(antialiased_circle_coverage(11.0, 10.0), 0.0)
        self.assertEqual(
            circular_button_visual_state(enabled=True, hovered=False, pressed=False),
            "default",
        )
        self.assertEqual(
            circular_button_visual_state(enabled=True, hovered=True, pressed=False),
            "hover",
        )
        self.assertEqual(
            circular_button_visual_state(enabled=True, hovered=True, pressed=True),
            "pressed",
        )
        self.assertEqual(
            circular_button_visual_state(enabled=False, hovered=True, pressed=True),
            "disabled",
        )

    def test_decision_canvases_never_overlap_at_supported_widths(self) -> None:
        for width, height in (
            (250, 96),
            (250, 110),
            (250, 250),
            (310, 250),
            (420, 320),
            (700, 420),
        ):
            layout = decision_button_layout(width, height)
            stop_x, stop_y, stop_size = layout["stop"]
            approve_x, approve_y, approve_size = layout["approve"]
            reject_x, reject_y, reject_size = layout["reject"]
            self.assertLessEqual(stop_x + stop_size, approve_x)
            self.assertLessEqual(approve_x + approve_size, reject_x)
            for x, y, size in layout.values():
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + size, width)
                self.assertLessEqual(y + size, height)
            if height >= 110:
                self.assertGreaterEqual(stop_size, 40)
                self.assertGreaterEqual(reject_size, 40)
            self.assertLessEqual(approve_y + approve_size, height - 8)
        self.assertEqual(
            decision_button_layout(310, 250)["approve"][2],
            decision_button_layout(313, 250)["approve"][2],
        )

    def test_decision_controls_share_a_centerline_and_keep_snap_primary_prominent(self) -> None:
        for width, height in ((250, 168), (264, 180), (360, 250), (600, 420)):
            layout = decision_button_layout(width, height)
            centerlines = [y + size / 2 for _x, y, size in layout.values()]
            self.assertLessEqual(max(centerlines) - min(centerlines), 1)
            self.assertGreaterEqual(layout["approve"][2], 138)
            self.assertGreaterEqual(layout["stop"][2], 40)
            self.assertGreaterEqual(layout["reject"][2], 40)

    def test_tree_titles_use_width_aware_ellipsis_for_mixed_text(self) -> None:
        title = "在当前项目中执行一次受限的 Windows 发布前验证"
        compact = ellipsize_display_text(title, max_units=12)
        comfortable = ellipsize_display_text(title, max_units=30)
        self.assertTrue(compact.endswith("…"))
        self.assertTrue(comfortable.endswith("…"))
        self.assertLess(len(compact), len(comfortable))
        self.assertEqual(ellipsize_display_text("批准", max_units=12), "批准")

    def test_decision_and_selection_tokens_meet_contrast_floors(self) -> None:
        for palette in THEME_PALETTES.values():
            for background in (
                palette.accent,
                palette.accent_hover,
                palette.accent_pressed,
            ):
                self.assertGreaterEqual(
                    contrast_ratio(background, palette.accent_text),
                    4.5,
                )
            for background in (
                palette.danger,
                palette.danger_hover,
                palette.danger_pressed,
            ):
                self.assertGreaterEqual(contrast_ratio(background, "#FFFFFF"), 4.5)
            self.assertGreaterEqual(contrast_ratio(palette.accent, palette.surface), 4.5)
            self.assertGreaterEqual(contrast_ratio(palette.selection, palette.surface), 3.0)
            self.assertGreaterEqual(
                contrast_ratio(palette.selection, palette.selection_text),
                4.5,
            )

    @unittest.skipUnless(os.name == "nt", "Native ttk styles are Windows-only")
    def test_toolbar_button_style_preserves_label_contrast_when_pressed(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            window = RelayWindow.__new__(RelayWindow)
            window.style = ttk.Style(root)
            window.style.theme_use("clam")
            for theme, palette in THEME_PALETTES.items():
                window._configure_styles(palette, window_background=palette.window)
                for state in ((), ("active",), ("pressed",), ("active", "pressed")):
                    with self.subTest(theme=theme, state=state):
                        foreground = str(window.style.lookup(
                            "Diag.HAR.TButton", "foreground", state
                        ))
                        background = str(window.style.lookup(
                            "Diag.HAR.TButton", "background", state
                        ))
                        self.assertGreaterEqual(contrast_ratio(background, foreground), 4.5)
                self.assertEqual(
                    window.style.lookup("Diag.HAR.TButton", "foreground", ("pressed",)),
                    palette.accent_text,
                )
        finally:
            root.destroy()

    def test_tree_selection_handler_does_not_self_trigger(self) -> None:
        class FakeTree:
            def __init__(self) -> None:
                self.selected = ("request:gfr_new",)
                self.focused = "request:gfr_new"
                self.selection_set_calls = 0
                self.see_calls = 0

            @staticmethod
            def exists(_item_id: str) -> bool:
                return True

            def selection(self) -> tuple[str, ...]:
                return self.selected

            def selection_set(self, item_id: str) -> None:
                self.selection_set_calls += 1
                self.selected = (item_id,)

            def focus(self, item_id: str | None = None) -> str:
                if item_id is not None:
                    self.focused = item_id
                return self.focused

            def see(self, _item_id: str) -> None:
                self.see_calls += 1

        tree = FakeTree()
        window = object.__new__(RelayWindow)
        window.tree = tree
        window.current_item_id = "request:gfr_old"
        window.current_request_id = "gfr_old"
        rendered: list[str] = []
        window._render_detail = rendered.append  # type: ignore[method-assign]

        window.on_select(None)  # type: ignore[arg-type]
        window.on_select(None)  # type: ignore[arg-type]

        self.assertEqual(tree.selection_set_calls, 0)
        self.assertEqual(tree.see_calls, 1)
        self.assertEqual(rendered, ["gfr_new"])
        self.assertEqual(window.current_item_id, "request:gfr_new")

    def test_request_detail_keeps_privilege_disclosure_without_a_banner(self) -> None:
        self.assertEqual(
            execution_mode_label("command", True),
            "执行权限: 管理员（获批命令将继承此权限）",
        )
        self.assertEqual(
            execution_mode_label("command", False),
            "执行权限: 普通用户",
        )
        self.assertEqual(
            execution_mode_label("consent", True),
            "执行方式: 不运行程序，仅返回确认文本",
        )

    def test_codex_theme_preference_and_system_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.toml"
            config_path.write_text(
                '[desktop]\nappearanceTheme = "dark"\n',
                encoding="utf-8",
            )
            self.assertEqual(read_codex_theme_preference(config_path), "dark")
            config_path.write_text(
                'appearanceTheme = "light"\n[desktop]\nappearanceTheme = "dark"\n',
                encoding="utf-8",
            )
            self.assertEqual(read_codex_theme_preference(config_path), "dark")
            config_path.write_text('appearanceTheme = "system"\n', encoding="utf-8")
            self.assertEqual(read_codex_theme_preference(config_path), "system")
            config_path.write_text('appearanceTheme = "unknown"\n', encoding="utf-8")
            self.assertEqual(read_codex_theme_preference(config_path), "system")
        self.assertEqual(resolve_theme("system", system_theme="dark"), "dark")
        self.assertEqual(resolve_theme("light", system_theme="dark"), "light")

    def test_acrylic_tint_keeps_the_requested_background_alpha(self) -> None:
        self.assertEqual(acrylic_gradient_color("#181818", 0.60), 0x99181818)
        self.assertEqual(acrylic_gradient_color("#F7F7F5", 0.60), 0x99F5F7F7)

    def test_transparent_client_requires_a_pointer_hit_shield(self) -> None:
        self.assertGreater(WINDOW_HIT_SHIELD_OPACITY, 0.0)
        self.assertTrue(
            safe_transparent_client(
                backdrop_enabled=True,
                hit_shield_ready=True,
            )
        )
        self.assertFalse(
            safe_transparent_client(
                backdrop_enabled=True,
                hit_shield_ready=False,
            )
        )

    def test_ui_preferences_persist_sound_and_auto_approval_but_ignore_legacy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "ui-settings.json"
            save_ui_settings(
                {
                    "sound_enabled": False,
                    "poll_wait_seconds": 10,
                    "max_pending_polls": 12,
                    "command_timeout_seconds": 600,
                },
                settings_path,
            )
            self.assertEqual(
                load_ui_settings(settings_path),
                {
                    "sound_enabled": False,
                    "poll_wait_seconds": 900,
                    "auto_approve_wait_seconds": 5,
                    "max_pending_polls": 10,
                    "command_timeout_seconds": 9_000,
                },
            )
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {"sound_enabled": False, "auto_approve_wait_seconds": 5},
            )

    def test_install_diagnostics_are_sanitized_and_detect_exact_cache(self) -> None:
        class FakeServer:
            store = RequestStore(elevated=False)

            @staticmethod
            def connector_snapshot() -> dict[str, object]:
                return {
                    "seen_at": "2026-08-29T00:00:00Z",
                    "connector_version": PACKAGE_VERSION,
                    "connector_build_id": RUNTIME_BUILD_ID,
                    "connector_fingerprint": "0123456789ab",
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory) / ".codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                '[plugins."golden-finger@personal"]\nenabled = true\n',
                encoding="utf-8",
            )
            cached = (
                codex_home
                / "plugins"
                / "cache"
                / "personal"
                / "golden-finger"
                / PACKAGE_VERSION
            )
            cached.mkdir(parents=True)
            (cached / "BUILD-ID.txt").write_text(
                RUNTIME_BUILD_ID + "\n",
                encoding="ascii",
            )
            diagnostics = collect_install_diagnostics(
                FakeServer(),
                codex_home=codex_home,
            )
            self.assertTrue(diagnostics["plugin_enabled"])
            self.assertTrue(diagnostics["exact_version_cached"])
            self.assertTrue(diagnostics["exact_build_cached"])
            self.assertTrue(diagnostics["component_match"])
            rendered = format_install_diagnostics(diagnostics)
            self.assertNotIn("claim_token", rendered)
            self.assertNotIn(CLAIM_TOKEN_PREFIX, rendered)

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_windows_job_object_can_be_created_without_a_process(self) -> None:
        job = WindowsJob.create()
        try:
            self.assertTrue(job.active)
        finally:
            job.close()

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_windows_job_object_terminates_a_contained_process(self) -> None:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        job = WindowsJob.create()
        try:
            job.assign(process)
            job.terminate(91)
            self.assertEqual(process.wait(timeout=5), 91)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            job.close()

    def test_broker_self_test_writes_a_sanitized_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "self-test.json"
            result = write_broker_self_test(destination)
            self.assertTrue(result["ok"])
            self.assertEqual(result["broker_version"], PACKAGE_VERSION)
            self.assertEqual(result["broker_build_id"], RUNTIME_BUILD_ID)
            rendered = destination.read_text(encoding="utf-8")
            self.assertNotIn("token", rendered.casefold())


class MCPShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journal_patch = patch.object(relay_mcp_server, "recovery_journal", lambda thread, process: RecoveryJournal("thread:" + thread if thread else "process:" + process, root=Path(temporary.name)))
        journal_patch.start()
        self.addCleanup(journal_patch.stop)

    def test_tool_surface_has_no_approve_tool(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(
            names,
            {
                "relay_health",
                "request_consent",
                "request_decision",
                "request_command_execution",
                "wait_for_authorization",
                "cancel_authorization",
                "terminate_command",
                "preflight_command",
                "recover_authorization",
                "read_command_output",
            },
        )
        self.assertFalse(any("approve" in name for name in names))

    def test_wait_and_cancel_require_the_per_request_claim(self) -> None:
        tools_by_name = {tool["name"]: tool for tool in TOOLS}
        for name in ("wait_for_authorization", "cancel_authorization", "terminate_command"):
            schema = tools_by_name[name]["inputSchema"]
            self.assertIn("request_id", schema["required"])
            self.assertIn("claim_token", schema["required"])
            self.assertIn("claim_token", schema["properties"])
            self.assertNotIn("client_instance_id", schema["properties"])
        self.assertEqual(
            tools_by_name["wait_for_authorization"]["inputSchema"]["properties"][
                "wait_seconds"
            ]["default"],
            30,
        )

    def test_new_request_schemas_require_cross_project_source_evidence(self) -> None:
        tools_by_name = {tool["name"]: tool for tool in TOOLS}
        for name in ("request_consent", "request_decision", "request_command_execution"):
            schema = tools_by_name[name]["inputSchema"]
            for field in ("source_label", "source_project", "source_task"):
                self.assertIn(field, schema["required"])
                self.assertIn(field, schema["properties"])

    def test_host_thread_metadata_is_forwarded_outside_the_frozen_request(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class HealthyRelayClient:
            def call(self, operation: str, **parameters: object) -> dict[str, object]:
                calls.append((operation, parameters))
                return {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "broker_version": PACKAGE_VERSION,
                    "broker_build_id": RUNTIME_BUILD_ID,
                }

        with patch.object(relay_mcp_server, "RelayClient", HealthyRelayClient):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 701,
                    "method": "tools/call",
                    "params": {
                        "name": "relay_health",
                        "arguments": {},
                        "_meta": {"openai/threadId": THREAD_A},
                    },
                }
            )
        self.assertEqual(response["id"], 701)
        self.assertEqual(calls, [("health", {"client_instance_id": relay_mcp_server.CLIENT_INSTANCE_ID, "codex_thread_id": THREAD_A})])

        with (
            patch.object(relay_mcp_server, "RelayClient", HealthyRelayClient),
            patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_B}),
        ):
            handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 702,
                    "method": "tools/call",
                    "params": {"name": "relay_health", "arguments": {}},
                }
            )
        self.assertEqual(calls[-1][1]["codex_thread_id"], THREAD_B)

    def test_health_unavailable_returns_scope_recovery_without_submitting(self) -> None:
        calls: list[str] = []

        class UnavailableRelayClient:
            def call(self, operation: str, **_parameters: object) -> dict[str, object]:
                calls.append(operation)
                raise RelayError(
                    "broker_unavailable",
                    "The independent relay window is not running.",
                )

        with patch.object(relay_mcp_server, "RelayClient", UnavailableRelayClient):
            result = relay_mcp_server.call_tool("relay_health", {})

        self.assertEqual(calls, ["health"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["reachable"])
        self.assertEqual(result["window_state"], "not_running_or_unreachable")
        recovery = result["scope_recovery"]
        self.assertFalse(recovery["scope_is_authorization"])
        self.assertTrue(recovery["must_ask_user"])
        self.assertFalse(recovery["background_monitoring_supported"])
        self.assertEqual(
            [choice["id"] for choice in recovery["choices"]],
            ["start_window", "cancel_scope"],
        )

    def test_health_does_not_mislabel_protocol_mismatch_as_closed_window(self) -> None:
        class IncompatibleRelayClient:
            def call(self, _operation: str, **_parameters: object) -> dict[str, object]:
                raise RelayError(
                    "protocol_mismatch",
                    "The relay window and plugin use different protocol versions.",
                )

        with patch.object(relay_mcp_server, "RelayClient", IncompatibleRelayClient):
            with self.assertRaises(RelayError) as raised:
                relay_mcp_server.call_tool("relay_health", {})
        self.assertEqual(raised.exception.code, "protocol_mismatch")

    def test_health_stopping_returns_closing_scope_recovery(self) -> None:
        class StoppingRelayClient:
            def call(self, operation: str, **_parameters: object) -> dict[str, object]:
                self.assert_operation(operation)
                raise RelayError("broker_stopping", "The relay window is stopping.")

            @staticmethod
            def assert_operation(operation: str) -> None:
                if operation != "health":
                    raise AssertionError(f"unexpected operation: {operation}")

        with patch.object(relay_mcp_server, "RelayClient", StoppingRelayClient):
            result = relay_mcp_server.call_tool("relay_health", {})

        self.assertTrue(result["ok"])
        self.assertFalse(result["reachable"])
        self.assertEqual(result["window_state"], "closing")
        self.assertEqual(result["reason_code"], "broker_stopping")
        self.assertEqual(
            result["scope_recovery"]["reason_code"],
            "relay_window_closing",
        )

    def test_request_unavailability_returns_same_start_or_cancel_choices(self) -> None:
        class UnavailableRelayClient:
            def call(self, _operation: str, **_parameters: object) -> dict[str, object]:
                raise RelayError(
                    "broker_unavailable",
                    "The independent relay window became unavailable.",
                )

        with patch.object(relay_mcp_server, "RelayClient", UnavailableRelayClient):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 710,
                    "method": "tools/call",
                    "params": {
                        "name": "request_consent",
                        "arguments": {
                            "exact_action": "Create no card because the window is absent.",
                            "target": "MCP recovery test",
                            "risk": "No external effect.",
                            "recovery": "Start the window or cancel the scope.",
                            "idempotency_key": "test-scope-recovery-0001",
                        },
                    },
                }
            )
        assert response is not None
        tool_result = response["result"]
        self.assertTrue(tool_result["isError"])
        structured = tool_result["structuredContent"]
        self.assertEqual(structured["error"]["code"], "broker_unavailable")
        self.assertEqual(
            [choice["id"] for choice in structured["scope_recovery"]["choices"]],
            ["start_window", "cancel_scope"],
        )

    def test_request_while_window_is_stopping_returns_scope_recovery(self) -> None:
        class StoppingRelayClient:
            def call(self, _operation: str, **_parameters: object) -> dict[str, object]:
                raise RelayError("broker_stopping", "The relay window is stopping.")

        with patch.object(relay_mcp_server, "RelayClient", StoppingRelayClient):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 711,
                    "method": "tools/call",
                    "params": {
                        "name": "request_consent",
                        "arguments": {
                            "exact_action": "Create no card while the window closes.",
                            "target": "MCP shutdown-race test",
                            "risk": "No external effect.",
                            "recovery": "Restart the window or cancel the scope.",
                            "idempotency_key": "test-scope-recovery-0002",
                        },
                    },
                }
            )
        assert response is not None
        structured = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["code"], "broker_stopping")
        self.assertEqual(
            structured["scope_recovery"]["reason_code"],
            "relay_window_closing",
        )

    def test_graceful_window_close_returns_scope_recovery_and_no_resume_claim(self) -> None:
        class ClosedRelayClient:
            def call(self, operation: str, **_parameters: object) -> dict[str, object]:
                if operation != "wait":
                    raise AssertionError(f"unexpected operation: {operation}")
                return {
                    "ok": True,
                    "request": {
                        "request_id": "gfr_WindowClosed",
                        "state": "cancelled",
                        "result": {
                            "decision": "cancelled",
                            "reason_code": "relay_window_closed",
                            "message": "The relay window closed before a decision.",
                        },
                    },
                }

        with patch.object(relay_mcp_server, "RelayClient", ClosedRelayClient):
            result = relay_mcp_server.call_tool(
                "wait_for_authorization",
                {
                    "request_id": "gfr_WindowClosed",
                    "claim_token": TEST_CLAIM,
                    "wait_seconds": 0,
                },
            )
        self.assertEqual(result["request"]["state"], "cancelled")
        self.assertFalse(result["scope_recovery"]["old_request_resumable_after_restart"])
        self.assertIn("cannot resume this request", result["continuation"])

    def test_new_request_binds_instance_and_returns_exact_claim_arguments(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeRelayClient:
            def call(self, operation: str, **parameters: object) -> dict[str, object]:
                calls.append((operation, parameters))
                self.assert_submit(operation)
                return {
                    "ok": True,
                    "request": {
                        "request_id": "gfr_BoundRequest",
                        "claim_fingerprint": "0123456789ab",
                        "state": "pending",
                        "poll_policy": {
                            "wait_seconds": 17,
                            "pending_polls_used": 0,
                            "pending_polls_remaining": 8,
                        },
                    },
                    "claim_token": TEST_CLAIM,
                    "idempotent_replay": False,
                }

            @staticmethod
            def assert_submit(operation: str) -> None:
                if operation != "submit":
                    raise AssertionError(f"unexpected operation: {operation}")

        relay_mcp_server._RPC_REQUEST_CLAIMS.clear()
        with patch.object(relay_mcp_server, "RelayClient", FakeRelayClient):
            result = relay_mcp_server.call_tool(
                "request_consent",
                {
                    "exact_action": "Create one bound request.",
                    "target": "MCP contract test",
                    "risk": "No external effect.",
                    "recovery": "No recovery needed.",
                    "idempotency_key": "test-mcp-binding-0001",
                    "wait_seconds": 0,
                },
                rpc_request_id=77,
            )
        self.assertEqual(calls[0][0], "submit")
        self.assertTrue(calls[0][1]["client_instance_id"].startswith("gfi_"))
        self.assertNotEqual(calls[0][1]["client_instance_id"], relay_mcp_server.CLIENT_INSTANCE_ID)
        self.assertNotIn("client_instance_id", result["request"])
        self.assertEqual(result["claim_token"], TEST_CLAIM)
        self.assertEqual(
            result["polling"]["next_arguments"],
            {
                "request_id": "gfr_BoundRequest",
                "claim_token": TEST_CLAIM,
                "wait_seconds": 0,
            },
        )
        relay_mcp_server._RPC_REQUEST_CLAIMS.clear()

    def test_active_state_requires_same_turn_polling(self) -> None:
        active = _polling_directive(
            {
                "request_id": "gfr_TestRequest",
                "state": "running",
                "poll_policy": {
                    "wait_seconds": 12,
                    "pending_polls_used": 3,
                    "remaining_seconds": 9,
                    "policy_revision": "test-policy",
                },
            },
            TEST_CLAIM, 12,
        )
        self.assertFalse(active["terminal"])
        self.assertTrue(active["must_continue_current_turn"])
        self.assertEqual(active["next_tool"], "wait_for_authorization")
        self.assertEqual(
            active["next_arguments"],
            {
                "request_id": "gfr_TestRequest",
                "claim_token": TEST_CLAIM,
                "wait_seconds": 12,
            },
        )
        self.assertEqual(active["remaining_seconds"], 9)
        self.assertTrue(active["do_not_wait_for_user_message"])

        fallback = _polling_directive(
            {"request_id": "gfr_DefaultPolicy", "state": "pending"},
            TEST_CLAIM,
        )
        self.assertEqual(
            fallback["next_arguments"]["wait_seconds"],
            relay_mcp_server.DEFAULT_MCP_WAIT_SECONDS,
        )
        self.assertEqual(DEFAULT_PENDING_POLL_CYCLES, 10)

        terminal = _polling_directive(
            {"request_id": "gfr_TestRequest", "state": "completed"},
            TEST_CLAIM,
        )
        self.assertTrue(terminal["terminal"])
        self.assertFalse(terminal["must_continue_current_turn"])

    def test_mcp_cancellation_notification_cancels_the_claimed_pending_card(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeRelayClient:
            def call(self, operation: str, **parameters: object) -> dict[str, object]:
                calls.append((operation, parameters))
                return {
                    "ok": True,
                    "request": {
                        "request_id": "gfr_CancelledRequest",
                        "state": "cancelled",
                    },
                }

        relay_mcp_server._RPC_REQUEST_CLAIMS.clear()
        relay_mcp_server._remember_rpc_claim(
            91,
            "gfr_CancelledRequest",
            TEST_CLAIM,
        )
        with patch.object(relay_mcp_server, "RelayClient", FakeRelayClient):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 91, "reason": "test cancellation"},
                }
            )
        self.assertIsNone(response)
        self.assertEqual(
            calls,
            [
                (
                    "cancel",
                    {
                        "request_id": "gfr_CancelledRequest",
                        "claim_token": TEST_CLAIM,
                        "client_instance_id": relay_mcp_server.CLIENT_INSTANCE_ID,
                    },
                )
            ],
        )
        self.assertFalse(relay_mcp_server._RPC_REQUEST_CLAIMS)

    def test_reused_rpc_id_drops_the_previous_claim_mapping(self) -> None:
        relay_mcp_server._RPC_REQUEST_CLAIMS.clear()
        relay_mcp_server._remember_rpc_claim(12, "gfr_OldRequest", TEST_CLAIM)
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "ping",
            }
        )
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 12, "result": {}})
        self.assertFalse(relay_mcp_server._RPC_REQUEST_CLAIMS)

    def test_initialize_advertises_tools_only(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        assert response is not None
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(response["result"]["serverInfo"]["version"], PACKAGE_VERSION)
        self.assertEqual(PROTOCOL_VERSION, 9)


class ReleasePackagingTests(unittest.TestCase):
    def test_release_build_pins_the_antialiased_renderer(self) -> None:
        requirements = (PLUGIN_ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        self.assertIn("pillow==12.3.0", requirements.casefold())

    def test_python_bytecode_is_excluded_from_release_trees(self) -> None:
        self.assertEqual(
            ignore_python_cache(
                "ignored",
                ["__pycache__", "relay_broker.py", "relay_broker.pyc", "legacy.pyo"],
            ),
            {"__pycache__", "relay_broker.pyc", "legacy.pyo"},
        )

    def test_release_uses_one_shared_onedir_runtime(self) -> None:
        builder = (PLUGIN_ROOT / "scripts" / "build_windows_release.py").read_text(
            encoding="utf-8"
        )
        verifier = (PLUGIN_ROOT / "scripts" / "verify_windows_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("COLLECT(", builder)
        self.assertIn('"PIL.AvifImagePlugin"', builder)
        self.assertIn('"PIL.WebPImagePlugin"', builder)
        self.assertIn('"golden-finger.runtime"', builder)
        self.assertIn('"pyinstaller-shared-onedir"', builder)
        self.assertNotIn('"--onefile"', builder)
        self.assertIn('"golden-finger.runtime"', verifier)
        self.assertIn('"pyinstaller-shared-onedir"', verifier)


if __name__ == "__main__":
    unittest.main()
