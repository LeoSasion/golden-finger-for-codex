"""Execution queue, cancellation, capacity and Windows containment regressions."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import relay_broker as broker
import relay_execution as execution
from relay_protocol import RelayClient, RelayError


CLIENT_ID = "gfi_" + "Q" * 32


def eventually(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Condition did not become true before deadline")


def submit_command(store, directory, key, code="print('done')", *, timeout_seconds=20):
    snapshot, _, claim = store.submit("command", {
        "exact_action": "Run an isolated execution regression.",
        "target": "temporary test state", "risk": "Test-owned process only.",
        "recovery": "Terminate test process.", "idempotency_key": key,
        "executable": sys.executable, "arguments": ["-c", code],
        "working_directory": str(directory), "timeout_seconds": timeout_seconds,
        "source_project": "display-only-untrusted-project",
    }, CLIENT_ID)
    return snapshot["request_id"], claim


class ExecutionQueueTests(unittest.TestCase):
    def test_wire_response_retains_outcome_for_two_large_chinese_streams(self):
        store = broker.RequestStore(elevated=False)
        server = broker.RelayTCPServer(("127.0.0.1", 0), broker.RelayRequestHandler, token="T" * 40, store=store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch("relay_broker.MAX_EXECUTION_WORKERS", 0):
                request = submit_command(store, directory, "wire-large-chinese")
                store.approve_from_gui(request[0])
                large_text = "中" * (broker.MAX_OUTPUT_BYTES // 3)
                store._finish_execution(
                    request[0], state="completed", message="Done.",
                    result={"exit_code": 0, "stdout": large_text, "stderr": large_text},
                )
                client = RelayClient({"host": "127.0.0.1", "port": server.server_address[1], "token": server.token})
                response = client.call("status", request_id=request[0], claim_token=request[1], client_instance_id=CLIENT_ID)
                self.assertEqual(response["request"]["state"], "completed")
                self.assertEqual(response["request"]["result"]["exit_code"], 0)
                self.assertTrue(response["request"]["result"]["wire_output_truncated"])
                self.assertEqual(store.status(*request)["result"]["stdout"], large_text)
        finally:
            store.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_same_repository_subdirectories_share_execution_group(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / "one").mkdir()
            (root / "two").mkdir()
            first = submit_command(store, root / "one", "repository-one")
            second = submit_command(store, root / "two", "repository-two")
            self.assertEqual(
                store._execution_group(store._records[first[0]]),
                store._execution_group(store._records[second[0]]),
            )

    def test_termination_during_verification_prevents_process_creation(self):
        store = broker.RequestStore(elevated=False)
        entered = threading.Event()
        released = threading.Event()
        original_fingerprint = broker.fingerprint_executable

        def hold_verification(path):
            entered.set()
            released.wait(5)
            return original_fingerprint(path)

        with tempfile.TemporaryDirectory() as directory:
            request = submit_command(store, directory, "terminate-before-start")
            with patch("relay_broker.fingerprint_executable", side_effect=hold_verification), \
                    patch("relay_broker.start_contained_process") as create:
                try:
                    store.approve_from_gui(request[0])
                    self.assertTrue(entered.wait(2))
                    store.terminate_from_gui(request[0])
                    released.set()
                    eventually(lambda: store.status(*request)["state"] == "terminated")
                    create.assert_not_called()
                    self.assertEqual(store.status(*request)["result"]["reason_code"], "terminated_by_user")
                finally:
                    released.set()
                    store.shutdown()

    def test_same_directory_serializes_and_global_limit_allows_other_directory(self):
        store = broker.RequestStore(elevated=False)
        released = threading.Event()
        started = []
        lock = threading.Lock()

        def run(request_id):
            with lock:
                started.append(request_id)
            released.wait(5)
            store._finish_execution(request_id, state="completed", message="Test complete.")

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
                tempfile.TemporaryDirectory() as third, \
                patch("relay_broker.MAX_EXECUTION_WORKERS", 2), \
                patch.object(store, "_execute_command", side_effect=run):
            requests = [submit_command(store, directory, f"queue-test-{index}") for index, directory in enumerate(
                [first, first, second, third]
            )]
            try:
                # Establish occupied slots explicitly: unrelated directories
                # may finish asynchronous probing in either order. Their probe
                # speed must not determine what this capacity test asserts.
                store.approve_from_gui(requests[0][0])
                eventually(lambda: started == [requests[0][0]])
                store.approve_from_gui(requests[1][0])
                store.approve_from_gui(requests[2][0])
                eventually(lambda: len(started) == 2)
                store.approve_from_gui(requests[3][0])
                eventually(lambda: store._records[requests[3][0]].execution_group is not None)
                self.assertEqual(set(started), {requests[0][0], requests[2][0]})
                self.assertEqual(store.status(*requests[1])["execution_phase"], "queued")
                self.assertEqual(store.status(*requests[3])["execution_phase"], "queued")
                released.set()
                eventually(lambda: len(started) == 4)
                eventually(lambda: all(store.status(*request)["state"] == "completed" for request in requests))
            finally:
                released.set()
                store.shutdown()

    def test_queued_termination_does_not_start_process_and_requires_claim(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory, patch("relay_broker.MAX_EXECUTION_WORKERS", 0):
            request = submit_command(store, directory, "queue-cancel")
            with self.assertRaises(RelayError) as pending:
                store.terminate(*request)
            self.assertEqual(pending.exception.code, "not_started")
            store.approve_from_gui(request[0])
            with self.assertRaises(RelayError):
                store.terminate(request[0], "gfc_" + "x" * 43)
            with self.assertRaises(RelayError) as cancel:
                store.cancel(*request)
            self.assertEqual(cancel.exception.code, "already_started")
            result = store.terminate(*request)
            self.assertEqual(result["state"], "terminated")
            self.assertEqual(result["result"]["reason_code"], "terminated_by_user")
            self.assertEqual(store.terminate(*request), result)
            self.assertEqual(store.active_counts(), (0, 0))

    def test_shutdown_cancels_queued_execution(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory, patch("relay_broker.MAX_EXECUTION_WORKERS", 0):
            request = submit_command(store, directory, "queue-shutdown")
            store.approve_from_gui(request[0])
            store.shutdown()
            snapshot = store.status(*request)
            self.assertEqual(snapshot["state"], "cancelled")
            self.assertEqual(snapshot["result"]["reason_code"], "relay_window_closed")

    def test_shutdown_preserves_an_earlier_user_termination_reason(self):
        store = broker.RequestStore(elevated=False)
        entered = threading.Event()
        finish = threading.Event()

        def held_worker(request_id):
            entered.set()
            if not finish.wait(5):
                raise AssertionError("Test worker was not released")
            store._finish_execution(
                request_id, state="completed", message="Worker collected final output.",
                result={"exit_code": 1, "stdout": "retained output"},
            )

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(store, "_execute_command", side_effect=held_worker):
            request = submit_command(store, directory, "terminate-then-shutdown")
            try:
                store.approve_from_gui(request[0])
                self.assertTrue(entered.wait(2))
                store.terminate(*request)
                store.shutdown()
                finish.set()
                eventually(lambda: store.status(*request)["state"] == "terminated")
                result = store.status(*request)["result"]
                self.assertEqual(result["reason_code"], "terminated_by_user")
                self.assertEqual(result["stdout"], "retained output")
                self.assertEqual(store.audit_snapshot()[0]["reason_code"], "terminated_by_user")
            finally:
                finish.set()
                store.shutdown()

    def test_terminal_history_is_bounded_and_audit_is_sanitized(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory, patch("relay_broker.MAX_RETAINED_REQUESTS", 3):
            requests = []
            for index in range(5):
                request = submit_command(store, directory, f"history-{index}", "print('secret command output')")
                requests.append(request)
                store.cancel(*request)
            self.assertEqual(len(store._records), 3)
            self.assertEqual(len(store._idempotency), 3)
            with self.assertRaises(RelayError):
                store.status(*requests[0])
            audit = json.dumps(store.audit_snapshot())
            for secret in ("secret command output", "display-only-untrusted-project", CLIENT_ID, directory, requests[-1][1]):
                self.assertNotIn(secret, audit)
            self.assertEqual(len(store.audit_snapshot()), 3)

    def test_connection_capacity_rejects_without_spawning_thread(self):
        store = broker.RequestStore(elevated=False)
        with patch("relay_broker.MAX_CLIENT_CONNECTIONS", 1):
            server = broker.RelayTCPServer(("127.0.0.1", 0), broker.RelayRequestHandler, token="T" * 40, store=store)
        left, right = socket.socketpair()
        try:
            self.assertTrue(server._connection_slots.acquire(blocking=False))
            with patch("socketserver.ThreadingTCPServer.process_request") as spawn:
                server.process_request(left, ("127.0.0.1", 12345))
                spawn.assert_not_called()
            self.assertEqual(right.recv(1), b"")
        finally:
            left.close()
            right.close()
            server._connection_slots.release()
            server.server_close()

    def test_two_connectors_cannot_starve_control_connections_with_long_waits(self):
        store = broker.RequestStore(elevated=False)
        server = broker.RelayTCPServer(("127.0.0.1", 0), broker.RelayRequestHandler, token="T" * 40, store=store)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        client = RelayClient({"host": "127.0.0.1", "port": server.server_address[1], "token": server.token})
        connector_ids = [CLIENT_ID, "gfi_" + "R" * 32]
        admitted = []
        results = []
        workers = []
        lock = threading.Lock()
        original_wait = store.wait

        def tracked_wait(request_id, claim, seconds):
            if seconds > 0:
                with lock:
                    admitted.append(request_id)
            return original_wait(request_id, claim, seconds)

        def long_wait(request, connector_id):
            try:
                result = client.call(
                    "wait", request_id=request[0], claim_token=request[1],
                    wait_seconds=900, client_instance_id=connector_id,
                )
                with lock:
                    results.append(result)
            except RelayError as exc:
                with lock:
                    results.append(exc.code)

        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch("relay_broker.MAX_EXECUTION_WORKERS", 0), \
                    patch.object(store, "wait", side_effect=tracked_wait):
                requests = [submit_command(store, directory, f"wait-capacity-{index}") for index in range(16)]
                for index, request in enumerate(requests):
                    store.approve_from_gui(request[0])
                    worker = threading.Thread(target=long_wait, args=(request, connector_ids[index % 2]), daemon=True)
                    workers.append(worker)
                    worker.start()
                eventually(lambda: len(admitted) == 16)
                # Each connector attempts eight more long polls. They must fail
                # promptly instead of filling the other sixteen connection slots.
                for index, request in enumerate(requests):
                    with self.assertRaises(RelayError) as full:
                        client.call(
                            "wait", request_id=request[0], claim_token=request[1],
                            wait_seconds=900, client_instance_id=connector_ids[index % 2],
                        )
                    self.assertEqual(full.exception.code, "wait_capacity")
                health = client.call("health", client_instance_id=CLIENT_ID)
                self.assertTrue(health["ok"])
                immediate = client.call(
                    "wait", request_id=requests[0][0], claim_token=requests[0][1],
                    wait_seconds=0, client_instance_id=CLIENT_ID,
                )
                self.assertEqual(immediate["request"]["execution_phase"], "queued")
                stopped = client.call(
                    "terminate", request_id=requests[0][0], claim_token=requests[0][1],
                    client_instance_id=CLIENT_ID,
                )
                self.assertEqual(stopped["request"]["state"], "terminated")
                eventually(lambda: len(results) >= 1)
                self.assertEqual(len(admitted), 16)
        finally:
            store.shutdown()
            for worker in workers:
                worker.join(3)
            server.shutdown()
            server.server_close()
            serving.join(2)
        self.assertEqual(len(results), 16)
        self.assertTrue(all(isinstance(result, dict) for result in results), results)


@unittest.skipUnless(os.name == "nt", "Windows process containment")
class WindowsExecutionTests(unittest.TestCase):
    def test_execution_timeout_retains_output(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            request = submit_command(
                store, directory, "timeout-test-output",
                "import time; print('OUTPUT-BEFORE-TIMEOUT',flush=True); time.sleep(20)",
                timeout_seconds=2,
            )
            try:
                store.approve_from_gui(request[0])
                eventually(lambda: store.status(*request)["state"] == "timed_out")
                self.assertIn("OUTPUT-BEFORE-TIMEOUT", store.status(*request)["result"]["stdout"])
            finally:
                store.shutdown()

    def test_process_is_suspended_until_job_is_assigned(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started.txt"
            job = broker.WindowsJob.create()
            original_resume = execution.resume_initial_thread

            def inspect_then_resume(process):
                self.assertFalse(marker.exists())
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
                kernel32.IsProcessInJob.restype = wintypes.BOOL
                in_job = wintypes.BOOL()
                self.assertTrue(kernel32.IsProcessInJob(int(process._handle), job._handle, ctypes.byref(in_job)))
                self.assertTrue(in_job.value)
                original_resume(process)

            process = None
            try:
                with patch.object(execution, "resume_initial_thread", side_effect=inspect_then_resume) as resumed:
                    process = execution.start_contained_process(
                        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"],
                        working_directory=directory, job=job,
                    )
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(marker.exists())
                resumed.assert_called_once()
            finally:
                job.close()
                if process is not None:
                    process.wait(timeout=5)
                    for stream in (process.stdout, process.stderr):
                        stream.close()

    def test_failed_assignment_never_runs_child(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "should-not-exist.txt"
            job = broker.WindowsJob.create()
            try:
                with patch.object(job, "assign", side_effect=RelayError("job_assignment_failed", "test failure")):
                    with self.assertRaises(RelayError):
                        execution.start_contained_process(
                            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')"],
                            working_directory=directory, job=job,
                        )
                self.assertFalse(marker.exists())
            finally:
                job.close()

    def test_terminate_running_command_retains_output_and_kills_descendant(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            code = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                "print('OUTPUT-BEFORE-TERMINATE',flush=True); "
                f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(20)"
            )
            request = submit_command(store, directory, "terminate-real-child", code)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = None
            try:
                store.approve_from_gui(request[0])
                eventually(lambda: marker.exists() and marker.stat().st_size)
                handle = kernel32.OpenProcess(0x00100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                snapshot = store.terminate(*request)
                self.assertIn(snapshot["execution_phase"], {"terminating", "finished"})
                eventually(lambda: store.status(*request)["state"] == "terminated")
                result = store.status(*request)["result"]
                self.assertEqual(result["reason_code"], "terminated_by_user")
                self.assertIn("OUTPUT-BEFORE-TERMINATE", result["stdout"])
                self.assertEqual(kernel32.WaitForSingleObject(handle, 2000), 0)
            finally:
                store.shutdown()
                if handle:
                    kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
