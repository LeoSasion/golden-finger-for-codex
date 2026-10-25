"""Recovery/short polling/progress regression tests; isolated receipts, no commands."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
import relay_mcp_server as mcp
import relay_recovery as recovery
from relay_protocol import RelayError, canonical_hash, json_ascii, MAX_WIRE_BYTES

THREAD = "01a0382e-9681-7082-b7a6-62884a6da9da"
OTHER_THREAD = "01a0382e-9681-7082-b7a6-62884a6da9db"
CLIENT = "gfi_" + "A" * 32
CAPABILITY = "gfrc_" + "a" * 43


def payload(key="enhancement-request-0001"):
    return {"exact_action": "Read only consent fixture", "target": "in-memory state",
            "risk": "No command execution", "recovery": "No external changes",
            "idempotency_key": key, "approved_reply": "Yes"}


class StoreWaitingTests(unittest.TestCase):
    def setUp(self):
        self.store = broker.RequestStore(elevated=False)
        self.snapshot, _, self.claim = self.store.submit("consent", payload(), CLIENT)
        self.request_id = self.snapshot["request_id"]

    def test_eleven_concurrent_waits_do_not_expire_or_extend_deadline(self):
        deadline = self.store._records[self.request_id].expires_monotonic
        barrier = threading.Barrier(11)
        def wait():
            barrier.wait()
            return self.store.wait(self.request_id, self.claim, .025)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=11) as pool:
            results = list(pool.map(lambda _: wait(), range(11)))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(all(item["state"] == "pending" for item in results))
        current = self.store.status(self.request_id, self.claim)
        self.assertEqual(current["poll_policy"]["pending_polls_used"], 11)
        self.assertIsNone(current["poll_policy"]["pending_polls_remaining"])
        self.assertEqual(current["poll_policy"]["expiry_basis"], "monotonic_deadline")
        self.assertEqual(self.store._records[self.request_id].expires_monotonic, deadline)

    def test_short_wait_and_monotonic_expiry(self):
        # Verify the chosen deadline directly. Subsecond wall-clock thresholds
        # measure Windows scheduling delays as well as the broker's policy.
        for expiry, wait, expected in ((.03, .2, "expired"), (10, .02, "pending")):
            with self.subTest(expiry=expiry, wait=wait):
                clock = [1000.0]
                waits = []
                with self.store._condition:
                    record = self.store._records[self.request_id]
                    record.state = "pending"
                    record.terminal_monotonic = None
                    record.expires_monotonic = clock[0] + expiry
                    self.store._next_expiry_check_monotonic = 0

                def advance(*, timeout):
                    waits.append(timeout)
                    clock[0] += timeout

                with patch.object(broker.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                    self.store._condition, "wait", side_effect=advance,
                ):
                    result = self.store.wait(self.request_id, self.claim, wait)
                self.assertEqual(len(waits), 1)
                self.assertAlmostEqual(waits[0], min(expiry, wait))
                self.assertAlmostEqual(clock[0] - 1000.0, min(expiry, wait))
                self.assertEqual(result["state"], expected)

    def test_wait_rejects_nonfinite_and_out_of_range(self):
        for value in (float("nan"), float("inf"), -1, 901, True):
            with self.subTest(value=value), self.assertRaises(RelayError):
                self.store.wait(self.request_id, self.claim, value)

    def test_recovery_requires_random_capability_and_matching_hash(self):
        data = payload("recovery-fixture-0001")
        digest = canonical_hash({"kind": "consent", "payload": data})
        result, _, claim = self.store.submit("consent", data, CLIENT, recovery_token=CAPABILITY, payload_hash=digest)
        recovered = self.store.recover(CAPABILITY, digest)
        self.assertEqual(recovered["request"]["request_id"], result["request_id"])
        self.assertEqual(recovered["claim_token"], claim)
        for token, hash_value, code in (("gfrc_" + "b" * 43, digest, "recovery_not_found"), (CAPABILITY, "0" * 64, "claim_mismatch")):
            with self.assertRaises(RelayError) as caught:
                self.store.recover(token, hash_value)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(RelayError):
            self.store.submit("consent", data, CLIENT, recovery_token=CAPABILITY, payload_hash="0" * 64)

    def test_recovery_mapping_evicted_with_terminal_history(self):
        data = payload("recovery-history-0001")
        digest = canonical_hash({"kind": "consent", "payload": data})
        result, _, claim = self.store.submit("consent", data, CLIENT, recovery_token=CAPABILITY, payload_hash=digest)
        self.store.cancel(self.request_id, self.claim)
        self.store.cancel(result["request_id"], claim)
        with patch.object(broker, "MAX_RETAINED_REQUESTS", 1):
            self.store.submit("consent", payload("replacement-history-0001"), CLIENT)
        self.assertEqual(self.store._recovery_claims, {})


@unittest.skipUnless(os.name == "nt", "Current-user journal uses Windows DPAPI")
class RecoveryAndToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = broker.RequestStore(elevated=False)
        self.calls = []
        self.lose_ack = False
        def journal(thread, process):
            return recovery.RecoveryJournal("thread:" + thread if thread else "process:" + process, root=self.root)
        self.journal_factory = journal
        owner = self
        class Client:
            def call(self, operation, **kwargs):
                owner.calls.append((operation, kwargs))
                if operation == "submit":
                    request, replay, claim = owner.store.submit(kwargs["kind"], kwargs["payload"], kwargs["client_instance_id"], kwargs.get("codex_thread_id"), kwargs["recovery_token"], kwargs["payload_hash"])
                    if owner.lose_ack:
                        owner.lose_ack = False
                        raise RelayError("broker_unavailable", "ACK lost")
                    return {"ok": True, "request": request, "claim_token": claim, "idempotent_replay": replay}
                if operation == "recover":
                    return owner.store.recover(kwargs["recovery_token"], kwargs["payload_hash"])
                if operation == "wait":
                    return {"ok": True, "request": owner.store.status(kwargs["request_id"], kwargs["claim_token"])}
                if operation == "cancel":
                    return {"ok": True, "request": owner.store.cancel(kwargs["request_id"], kwargs["claim_token"])}
                if operation == "acknowledge_receipt":
                    return {"ok": True, "request": owner.store.acknowledge_receipt(
                        kwargs["request_id"], kwargs["claim_token"], kwargs["request_hash"], kwargs["observed_state"])}
                if operation == "preflight":
                    return {"ok": True, "preflight": kwargs["payload"]}
                if operation == "read_output":
                    return {"ok": True, "request_id": kwargs["request_id"], "text": "\x00" * kwargs["limit"], "offset": kwargs.get("offset", 0), "next_offset": kwargs.get("offset", 0) + kwargs["limit"]}
                raise AssertionError(operation)
        for target, name, replacement in ((mcp, "recovery_journal", journal), (mcp, "RelayClient", Client)):
            mocked = patch.object(target, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        with mcp._RPC_STATE_LOCK:
            mcp._RPC_REQUEST_CLAIMS.clear()
            mcp._ACTIVE_RPC_CALLS.clear()
            mcp._RECEIPT_JOURNALS.clear()

    def submit(self, key="enhancement-request-0001", thread=THREAD, wait=0):
        return mcp.call_tool("request_consent", {**payload(key), "wait_seconds": wait}, codex_thread_id=thread)

    def test_lost_ack_recovers_after_connector_restart_without_resubmit(self):
        self.lose_ack = True
        with self.assertRaises(RelayError) as caught:
            self.submit()
        self.assertEqual(caught.exception.details["submission_outcome"], "unknown")
        first_id = next(iter(self.store._records))
        with patch.object(mcp, "CLIENT_INSTANCE_ID", "gfi_" + "B" * 32):
            result = self.submit()
        self.assertEqual(result["request"]["request_id"], first_id)
        self.assertEqual([op for op, _ in self.calls], ["submit", "recover"])
        self.assertEqual(len(self.store._records), 1)

    def test_missing_recovery_never_replays_submission(self):
        self.submit()
        self.store = broker.RequestStore(elevated=False)
        with self.assertRaises(RelayError) as caught:
            self.submit()
        self.assertEqual(caught.exception.code, "recovery_not_found")
        self.assertEqual(caught.exception.details["submission_outcome"], "unknown")
        self.assertEqual([op for op, _ in self.calls], ["submit", "recover"])
        self.assertEqual(len(self.store._records), 0)

    def test_threads_and_anonymous_processes_do_not_share_claims(self):
        first = self.submit()
        other = self.submit(thread=OTHER_THREAD)
        anonymous = self.submit(thread=None)
        with patch.object(mcp, "CLIENT_INSTANCE_ID", "gfi_" + "B" * 32):
            restarted = self.submit(thread=None)
        self.assertEqual(len({item["request"]["request_id"] for item in (first, other, anonymous, restarted)}), 4)
        explicit = mcp.call_tool("recover_authorization", first["recovery"], codex_thread_id=OTHER_THREAD)
        self.assertEqual(explicit["claim_token"], first["claim_token"])

    def test_journal_is_dpapi_sealed_and_metadata_only(self):
        result = self.submit()
        journal = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)
        raw = journal.path.read_bytes()
        for sensitive in (THREAD, payload()["exact_action"], result["claim_token"], result["recovery"]["recovery_token"]):
            self.assertNotIn(sensitive.encode(), raw)
        data = journal._read()
        entry = next(iter(data["entries"].values()))
        self.assertEqual(set(entry), {"payload_hash", "recovery_token", "phase", "updated_at", "request_id", "claim_token"})
        self.assertEqual(self.journal_factory(THREAD, "different-process")._read()["namespace"], data["namespace"])

    def test_storage_failure_before_send_is_not_submitted(self):
        with patch.object(recovery.RecoveryJournal, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(RelayError) as caught:
                self.submit()
        self.assertEqual(caught.exception.details["submission_outcome"], "not_submitted")
        self.assertEqual(self.calls, [])

    def test_storage_failure_after_ack_is_accepted_and_recoverable(self):
        with patch.object(recovery.RecoveryJournal, "record", side_effect=RelayError("recovery_storage_unavailable", "disk full")):
            with self.assertRaises(RelayError) as caught:
                self.submit()
        details = caught.exception.details
        self.assertEqual(details["submission_outcome"], "accepted")
        recovered = mcp.call_tool("recover_authorization", details["recovery_arguments"], codex_thread_id=THREAD)
        self.assertEqual(recovered["request"]["request_id"], details["request_id"])
        self.assertEqual(recovered["claim_token"], details["claim_token"])

    def test_later_wait_and_cancel_reclaim_finished_receipts(self):
        with patch.object(recovery, "MAX_JOURNAL_ENTRIES", 2):
            first = self.submit("lifecycle-request-0001")
            second = self.submit("lifecycle-request-0002")
            self.store.cancel(first["request"]["request_id"], first["claim_token"])
            mcp.call_tool("wait_for_authorization", {"request_id": first["request"]["request_id"], "claim_token": first["claim_token"], "wait_seconds": 0}, codex_thread_id=THREAD)
            mcp.call_tool("cancel_authorization", {"request_id": second["request"]["request_id"], "claim_token": second["claim_token"]}, codex_thread_id=THREAD)
            self.submit("lifecycle-request-0003")
        entries = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)._read()["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(item["phase"] for item in entries.values()), ["submitted", "terminal"])

    def test_notification_cancellation_marks_finished_receipt(self):
        result = mcp.call_tool("request_consent", {**payload(), "wait_seconds": 0}, rpc_request_id=71, codex_thread_id=THREAD)
        mcp.handle_request({"method": "notifications/cancelled", "params": {"requestId": 71}})
        entry = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)._read()["entries"][result["recovery"]["payload_hash"]]
        self.assertEqual(entry["phase"], "terminal")

    def test_cancellation_before_submit_ack_marks_receipt_terminal(self):
        base_client = mcp.RelayClient
        class CancelBeforeAck(base_client):
            def call(self, operation, **kwargs):
                result = super().call(operation, **kwargs)
                if operation == "submit":
                    mcp.handle_request({"method": "notifications/cancelled", "params": {"requestId": 72}})
                return result
        with mcp._RPC_STATE_LOCK:
            mcp._ACTIVE_RPC_CALLS["72"] = mcp._RpcCallState()
        try:
            with patch.object(mcp, "RelayClient", CancelBeforeAck), self.assertRaises(RelayError) as caught:
                mcp.call_tool("request_consent", {**payload(), "wait_seconds": 0}, rpc_request_id=72, codex_thread_id=THREAD)
            self.assertEqual(caught.exception.code, "request_cancelled")
            self.assertEqual(caught.exception.details["broker_cancellation"], "acknowledged")
            self.assertEqual(caught.exception.details["state"], "cancelled")
            journal = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)
            self.assertEqual(next(iter(journal._read()["entries"].values()))["phase"], "terminal")
        finally:
            with mcp._RPC_STATE_LOCK:
                mcp._ACTIVE_RPC_CALLS.pop("72", None)

    def test_explicit_recovery_updates_original_receipt(self):
        result = self.submit()
        self.store.cancel(result["request"]["request_id"], result["claim_token"])
        mcp.call_tool("recover_authorization", result["recovery"], codex_thread_id=THREAD)
        entry = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)._read()["entries"][result["recovery"]["payload_hash"]]
        self.assertEqual(entry["phase"], "terminal")

    def test_late_pending_receipts_cannot_reverse_terminal_phase(self):
        result = self.submit()
        journal = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)
        digest = result["recovery"]["payload_hash"]
        request_id, claim = result["request"]["request_id"], result["claim_token"]
        journal.observe(request_id, claim, terminal=True)
        journal.observe(request_id, claim, terminal=False)
        journal.record(digest, phase="submitted")
        journal.record(digest, phase="unknown")
        self.assertEqual(journal._read()["entries"][digest]["phase"], "terminal")

    def test_cancel_before_submission_records_safe_retry_without_send(self):
        with patch.object(mcp, "_check_cancelled", side_effect=[None, RelayError("request_cancelled", "cancelled")]):
            with self.assertRaises(RelayError):
                self.submit()
        self.assertEqual(self.calls, [])
        journal = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)
        self.assertEqual(next(iter(journal._read()["entries"].values()))["phase"], "not_submitted")
        self.submit()
        self.assertEqual([op for op, _ in self.calls], ["submit"])

    def test_cancelled_before_send_receipts_can_be_evicted(self):
        journal = self.journal_factory(THREAD, mcp.CLIENT_INSTANCE_ID)
        with patch.object(recovery, "MAX_JOURNAL_ENTRIES", 1):
            journal.prepare("a" * 64)
            journal.record("a" * 64, phase="not_submitted")
            _, entry, first = journal.prepare("b" * 64)
        self.assertTrue(first)
        self.assertEqual(entry["payload_hash"], "b" * 64)
        self.assertEqual(set(journal._read()["entries"]), {"b" * 64})

    def test_chosen_short_wait_is_forwarded_and_preserved(self):
        for wait in (0, 7, 30, 900):
            with self.subTest(wait=wait):
                result = self.submit(f"short-wait-request-{wait:04}", wait=wait)
                self.assertEqual(result["polling"]["next_arguments"]["wait_seconds"], wait)
                if wait:
                    self.assertEqual(self.calls[-1][1]["wait_seconds"], wait)
        self.assertEqual(mcp._bounded_wait({}), 30)

    def test_preflight_and_output_tools_preserve_safe_fields_and_cursor(self):
        fields = {"executable": "python", "arguments": ["@args.txt"], "working_directory": "D:/fixture", "environment": {"LANG": "zh"}, "output_encoding": "cp936", "response_files": ["args.txt"]}
        self.assertEqual(mcp.call_tool("preflight_command", fields)["preflight"], fields)
        claim = "gfcap_" + "a" * 40
        page = mcp.call_tool("read_command_output", {"request_id": "gfr_RequestA", "claim_token": claim, "offset": 5, "limit": 65536})
        self.assertEqual(len(page["text"]), 16384)
        self.assertEqual(page["next_offset"], 5 + len(page["text"]))
        wrapped = mcp.mcp_tool_result(page, rpc_request_id="x" * 250)
        self.assertEqual(wrapped["structuredContent"]["text"], page["text"])
        self.assertLess(len(json_ascii({"jsonrpc": "2.0", "id": "x" * 250, "result": wrapped})), MAX_WIRE_BYTES)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.responses = []
        self.dispatcher = mcp.McpDispatcher(self.responses.append)
        self.dispatcher._progress_stop.set()
        self.addCleanup(self.dispatcher.close)
        with mcp._RPC_STATE_LOCK:
            mcp._ACTIVE_RPC_CALLS.clear()

    def test_progress_is_elapsed_throttled_strictly_increasing_and_active_only(self):
        state = mcp._RpcCallState(progress_token="token", progress_owner=self.dispatcher, progress_started=100)
        with mcp._RPC_STATE_LOCK:
            mcp._ACTIVE_RPC_CALLS["1"] = state
        self.dispatcher.emit_progress(100.5)
        self.dispatcher.emit_progress(101)
        self.assertEqual(len(self.responses), 1)
        state.progress_phase = "running"
        self.dispatcher.emit_progress(100.5)
        self.assertEqual(len(self.responses), 1)
        self.dispatcher.emit_progress(101.1)
        self.dispatcher.emit_progress(103.2)
        self.assertEqual(len(self.responses), 3)
        values = [entry["params"]["progress"] for entry in self.responses]
        self.assertTrue(all(a < b for a, b in zip(values, values[1:])))
        self.assertTrue(all("total" not in entry["params"] for entry in self.responses))
        self.assertIn("running", self.responses[1]["params"]["message"])
        with mcp._RPC_STATE_LOCK:
            mcp._ACTIVE_RPC_CALLS.pop("1")
        self.dispatcher.emit_progress(200)
        self.assertEqual(len(self.responses), 3)

    def test_only_string_or_integer_progress_tokens_are_accepted(self):
        release, entered = threading.Event(), threading.Event()
        def call(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"ok": True}
        with patch.object(mcp, "call_tool", call):
            for index, token in enumerate(("ok", 17, True, 1.5, None)):
                release.clear(); entered.clear()
                self.dispatcher.dispatch({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": "relay_health", "arguments": {}, "_meta": {"progressToken": token}}})
                self.assertTrue(entered.wait(1))
                state = mcp._ACTIVE_RPC_CALLS[str(index)]
                self.assertEqual(state.progress_token, token if index < 2 else None)
                release.set()
                self.assertTrue(self.dispatcher.wait_idle())


if __name__ == "__main__":
    unittest.main()
