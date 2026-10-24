"""Bounded live logs, encoding, and non-blocking Windows startup regressions."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import relay_broker as broker
import relay_execution as execution
from relay_protocol import RelayError


CLIENT = "gfi_" + "E" * 32


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Condition did not become true")


def submit(store, directory, key, code="print('done')", **options):
    snapshot, _, claim = store.submit("command", {
        "exact_action": "Run an isolated regression command", "target": "temporary test files",
        "risk": "Only test-owned children", "recovery": "Close the isolated test store",
        "idempotency_key": key, "executable": sys.executable, "arguments": ["-c", code],
        "working_directory": str(directory), "timeout_seconds": 15, **options,
    }, CLIENT)
    return snapshot["request_id"], claim


class OutputBufferTests(unittest.TestCase):
    def test_reverse_probe_completion_preserves_approval_order_without_blocking_other_project(self):
        for repository in (False, True):
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory, \
                    tempfile.TemporaryDirectory() as other_directory:
                root = Path(directory)
                first_directory = second_directory = root
                if repository:
                    (root / ".git").mkdir()
                    first_directory = root / "one"
                    second_directory = root / "two"
                    first_directory.mkdir()
                    second_directory.mkdir()
                store = broker.RequestStore(elevated=False)
                first_probe_entered = threading.Event()
                release_probe = threading.Event()
                release_workers = threading.Event()
                started = []
                # Submission order deliberately differs from approval order.
                second = submit(store, second_directory, "approved-second-created-first")
                first = submit(store, first_directory, "approved-first-created-second")
                other = submit(store, other_directory, "independent-project-request")
                original_group = store._execution_group

                def resolve(record):
                    if record.request_id == first[0]:
                        first_probe_entered.set()
                        release_probe.wait(5)
                    return original_group(record)

                def execute(request_id):
                    started.append(request_id)
                    release_workers.wait(5)
                    store._finish_execution(request_id, state="completed", message="Isolated queue test")

                with patch("relay_broker.MAX_EXECUTION_WORKERS", 2), \
                        patch.object(store, "_execution_group", side_effect=resolve), \
                        patch.object(store, "_execute_command", side_effect=execute):
                    try:
                        store.approve_from_gui(first[0])
                        self.assertTrue(first_probe_entered.wait(2))
                        store.approve_from_gui(second[0])
                        eventually(lambda: store._records[second[0]].execution_group is not None)
                        self.assertEqual(store.status(*second)["execution_phase"], "queued")
                        self.assertNotIn(second[0], started)
                        store.approve_from_gui(other[0])
                        eventually(lambda: other[0] in started)
                        self.assertEqual(started, [other[0]])
                        release_probe.set()
                        eventually(lambda: first[0] in started)
                        self.assertNotIn(second[0], started)
                        release_workers.set()
                        eventually(lambda: all(store.status(*request)["state"] == "completed" for request in (first, second, other)))
                        self.assertLess(started.index(first[0]), started.index(second[0]))
                    finally:
                        release_probe.set()
                        release_workers.set()
                        store.shutdown()

    def test_incomplete_utf8_tail_does_not_reinterpret_entire_output_as_gbk(self):
        data = ("测" * 90000).encode("utf-8")[:broker.MAX_OUTPUT_BYTES]
        with patch("relay_execution.locale.getpreferredencoding", return_value="cp936"):
            text, encoding = broker.RequestStore._decode_output(data, truncated=True)
        self.assertEqual(text, "测" * (len(data) // 3))
        self.assertEqual(encoding, "utf-8-sig")

    def test_bom_and_explicit_encodings_preserve_text(self):
        for encoding in ("utf-16", "utf-16-be", "utf-32", "gbk", "cp437"):
            text = "中文日志" if encoding != "cp437" else "café"
            data = text.encode(encoding)
            decoded, _ = execution.decode_output(data, encoding)
            self.assertEqual(decoded, text)
        for bom, encoding in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
            self.assertEqual(execution.decode_output(bom + "中文".encode(encoding))[0], "中文")

    def test_head_tail_and_page_gap_are_bounded_and_explicit(self):
        output = execution.BoundedOutput(64)
        output.append(b"HEAD" + b"a" * 96 + b"FINAL ERROR")
        output.finish()
        summary = output.summary()
        self.assertEqual(summary["retained_bytes"], 64)
        self.assertEqual(summary["total_bytes"], 111)
        text, _, truncated = output.render()
        self.assertTrue(text.startswith("HEAD"))
        self.assertTrue(text.endswith("FINAL ERROR"))
        self.assertTrue(truncated)
        first = output.page(0, 64)
        self.assertEqual(first["next_offset"], 32)
        last = output.page(first["next_offset"], 64)
        self.assertEqual(last["gap"], {"from": 32, "to": 79})
        self.assertTrue(last["text"].endswith("FINAL ERROR"))
        self.assertEqual(last["next_offset"], 111)
        self.assertTrue(last["complete"])

    def test_multibyte_pages_do_not_split_complete_characters(self):
        output = execution.BoundedOutput()
        output.append("测试分页".encode("utf-8"))
        output.finish()
        parts, offset = [], 0
        while offset < output.summary()["total_bytes"]:
            page = output.page(offset, 4)
            parts.append(page["text"])
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(parts), "测试分页")

    def test_explicit_utf16_pages_resolve_byte_order_once_from_head(self):
        output = execution.BoundedOutput()
        output.append("第一行第二行".encode("utf-16"))
        output.finish()
        first = output.page(0, 8, "utf-16")
        last = output.page(first["next_offset"], 30, "utf-16")
        self.assertEqual(first["text"] + last["text"], "第一行第二行")

    def test_tiny_page_limit_reports_minimum_instead_of_stalling_cursor(self):
        for encoding, character, minimum in (("utf-8", "中", 3), ("utf-16-le", "中", 2), ("utf-32-le", "中", 4), ("gbk", "中", 2)):
            for limit in range(1, minimum):
                with self.subTest(encoding=encoding, limit=limit):
                    output = execution.BoundedOutput()
                    output.append((character * 2).encode(encoding))
                    output.finish()
                    with self.assertRaises(RelayError) as caught:
                        output.page(0, limit, encoding)
                    self.assertEqual(caught.exception.code, "output_page_limit_too_small")
                    self.assertEqual(caught.exception.details["minimum_limit"], minimum)

    def test_live_partial_character_waits_for_more_bytes_without_error(self):
        output = execution.BoundedOutput()
        encoded = "中".encode("utf-8")
        output.append(encoded[:1])
        page = output.page(0, 1, "utf-8")
        self.assertEqual(page["text"], "")
        self.assertEqual(page["next_offset"], 0)
        self.assertFalse(page["complete"])
        output.append(encoded[1:])
        self.assertEqual(output.page(0, 3, "utf-8")["text"], "中")

    def test_slow_directory_probe_does_not_hold_store_lock(self):
        store = broker.RequestStore(elevated=False)
        entered, release = threading.Event(), threading.Event()
        original = store._execution_group

        def blocked(record):
            entered.set()
            release.wait(5)
            return original(record)

        with tempfile.TemporaryDirectory() as directory, patch.object(store, "_execution_group", side_effect=blocked):
            request = submit(store, directory, "slow-directory-regression")
            try:
                store.approve_from_gui(request[0])
                self.assertTrue(entered.wait(2))
                start = time.monotonic()
                self.assertEqual(store.status(*request)["execution_phase"], "queued")
                self.assertEqual(store.terminate(*request)["state"], "terminated")
                self.assertLess(time.monotonic() - start, 0.5)
            finally:
                release.set()
                eventually(lambda: not store._group_resolution_slots)
                store.shutdown()


@unittest.skipUnless(os.name == "nt", "Windows suspended process startup")
class LiveExecutionTests(unittest.TestCase):
    def test_live_short_output_is_readable_before_exit_and_claim_bound(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            request = submit(store, directory, "live-output-regression", "import time; print('PROGRESS-LINE',flush=True); time.sleep(10)")
            try:
                store.approve_from_gui(request[0])
                eventually(lambda: "PROGRESS-LINE" in store.read_output(*request)["text"])
                snapshot = store.status(*request)
                self.assertEqual(snapshot["state"], "running")
                self.assertIn("PROGRESS-LINE", snapshot["stdout_tail"])
                self.assertGreater(snapshot["output_revision"], 0)
                self.assertFalse(store.read_output(*request)["complete"])
                with self.assertRaises(RelayError):
                    store.read_output(request[0], "gfcap_" + "x" * 40)
                store.terminate(*request)
                eventually(lambda: store.status(*request)["state"] == "terminated")
                self.assertTrue(store.read_output(*request)["complete"])
            finally:
                store.shutdown()

    def test_environment_overrides_and_explicit_gbk_reach_real_child(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            request = submit(store, directory, "environment-encoding-regression",
                "import os,sys; sys.stdout.buffer.write((os.environ['GF_TEST_VALUE']+'中文').encode('gbk'))",
                environment={"GF_TEST_VALUE": "VALUE:"}, output_encoding="gbk")
            try:
                store.approve_from_gui(request[0])
                eventually(lambda: store.status(*request)["state"] == "completed")
                self.assertEqual(store.status(*request)["result"]["stdout"], "VALUE:中文")
                self.assertEqual(store.read_output(*request)["text"], "VALUE:中文")
            finally:
                store.shutdown()

    def test_large_real_output_keeps_initial_context_and_final_error(self):
        store = broker.RequestStore(elevated=False)
        with tempfile.TemporaryDirectory() as directory:
            request = submit(store, directory, "large-output-tail-regression",
                "import sys; print('START-MARKER'); print('x'*300000); print('FINAL-ERROR'); sys.exit(2)")
            try:
                store.approve_from_gui(request[0])
                eventually(lambda: store.status(*request)["state"] == "failed")
                result = store.status(*request)["result"]
                self.assertTrue(result["stdout"].startswith("START-MARKER"))
                self.assertIn("FINAL-ERROR", result["stdout"])
                self.assertTrue(result["stdout_truncated"])
                summary = store.status(*request)["output_summary"]["stdout"]
                self.assertLessEqual(summary["retained_bytes"], broker.MAX_OUTPUT_BYTES)
            finally:
                store.shutdown()

    def test_shutdown_during_unpublished_startup_prevents_child_code(self):
        store = broker.RequestStore(elevated=False)
        entered, release = threading.Event(), threading.Event()
        original = broker.start_contained_process

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch("relay_broker.start_contained_process", side_effect=blocked):
            marker = Path(directory) / "must-not-run.txt"
            request = submit(store, directory, "shutdown-before-publish", f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')")
            try:
                store.approve_from_gui(request[0])
                self.assertTrue(entered.wait(2))
                start = time.monotonic()
                store.shutdown()
                self.assertLess(time.monotonic() - start, 0.5)
                release.set()
                eventually(lambda: store.status(*request)["state"] == "cancelled")
                self.assertFalse(marker.exists())
            finally:
                release.set()
                store.shutdown()

    def test_terminate_during_resume_keeps_repository_slot_until_worker_exits(self):
        store = broker.RequestStore(elevated=False)
        entered, release = threading.Event(), threading.Event()
        original = execution.resume_initial_thread
        calls = 0

        def blocked(process):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(5)
            return original(process)

        with tempfile.TemporaryDirectory() as directory, patch.object(execution, "resume_initial_thread", side_effect=blocked):
            forbidden = Path(directory) / "first.txt"
            second_marker = Path(directory) / "second.txt"
            first = submit(store, directory, "resume-first-regression", f"from pathlib import Path; Path({str(forbidden)!r}).write_text('bad')")
            second = submit(store, directory, "resume-second-regression", f"from pathlib import Path; Path({str(second_marker)!r}).write_text('ok')")
            try:
                store.approve_from_gui(first[0])
                self.assertTrue(entered.wait(2))
                store.terminate(*first)
                store.approve_from_gui(second[0])
                eventually(lambda: store._records[second[0]].execution_group is not None)
                self.assertEqual(store.status(*second)["execution_phase"], "queued")
                self.assertFalse(second_marker.exists())
                release.set()
                eventually(lambda: store.status(*first)["state"] == "terminated")
                eventually(lambda: store.status(*second)["state"] == "completed")
                self.assertFalse(forbidden.exists())
                self.assertTrue(second_marker.exists())
            finally:
                release.set()
                store.shutdown()


if __name__ == "__main__":
    unittest.main()
