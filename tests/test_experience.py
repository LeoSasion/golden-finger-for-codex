"""First-success flow, exact result receipts, stable selection and safe practice."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import tkinter as tk
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import relay_broker as broker
import relay_mcp_server as mcp
from relay_experience import CHAT_TEST_PROMPT, PracticeSession, delivery_copy, repair_prompt
from relay_diagnostics import connection_steps
from relay_protocol import RelayClient, RelayError, TERMINAL_STATES
from relay_preferences import save_preferences, load_preferences
import test_contract as contracts
from test_decisions import question, CLIENT


def widgets(root):
    for child in root.winfo_children():
        yield child
        yield from widgets(child)


class ReceiptTests(TestCase):
    def setUp(self):
        self.store = broker.RequestStore(elevated=False)
        self.snapshot, _, self.claim = self.store.submit("decision", question(), CLIENT)
        self.request_id = self.snapshot["request_id"]

    def acknowledge(self, snapshot=None, claim=None, request_hash=None, state=None):
        current = snapshot or self.store.status(self.request_id, self.claim)
        return self.store.acknowledge_receipt(self.request_id, claim or self.claim,
                                            request_hash or current["request_hash"], state or current["state"])

    def test_only_the_exact_terminal_snapshot_can_be_acknowledged(self):
        with self.assertRaises(RelayError) as caught:
            self.acknowledge()
        self.assertEqual(caught.exception.code, "receipt_mismatch")
        result = self.store.reject_from_gui(self.request_id)
        self.assertIn(result["state"], TERMINAL_STATES)
        for changes in ({"state": "approved"}, {"request_hash": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(RelayError) as caught:
                self.acknowledge(**changes)
            self.assertEqual(caught.exception.code, "receipt_mismatch")
        self.assertIsNone(self.store.recent_delivery())
        received = self.acknowledge()
        self.assertIs(received["result"]["answer"]["value"], False)
        self.assertEqual(received["delivery"]["status"], "connector_received")
        self.assertGreaterEqual(received["delivery"]["receipt_latency_ms"], 0)

    def test_wrong_capability_cannot_mark_another_question_as_received(self):
        _, _, other = self.store.submit("decision", question(key="another-receipt-question"), CLIENT)
        self.store.approve_from_gui(self.request_id)
        with self.assertRaises(RelayError) as caught:
            self.acknowledge(claim=other)
        self.assertEqual(caught.exception.code, "claim_mismatch")
        self.assertEqual(self.store.status(self.request_id, self.claim)["delivery"]["status"], "awaiting_connector")

    def test_repeated_receipts_do_not_change_answer_timestamp_or_replay(self):
        self.store.approve_from_gui(self.request_id)
        first = self.acknowledge()
        revision = self.store.gui_snapshot()[0]
        second = self.acknowledge()
        self.assertEqual(first, second)
        self.assertEqual(revision, self.store.gui_snapshot()[0])
        self.assertEqual(len(self.store._records), 1)
        self.assertEqual(self.store.active_counts(), (0, 0))

    def test_cancelled_question_receipt_does_not_approve_or_execute(self):
        self.store.cancel_from_gui(self.request_id)
        with patch.object(self.store, "approve_from_gui") as approve, patch.object(self.store, "_schedule_execution_locked") as execute:
            result = self.acknowledge()
        self.assertEqual(result["state"], "cancelled")
        self.assertNotIn("answer", result["result"])
        approve.assert_not_called()
        execute.assert_not_called()

    def test_acknowledgement_runs_over_authenticated_wire(self):
        server = broker.RelayTCPServer(("127.0.0.1", 0), broker.RelayRequestHandler,
                                       token="test-receipt-wire", store=self.store)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            metadata = {"host": "127.0.0.1", "port": server.server_address[1], "token": "test-receipt-wire"}
            with patch("relay_protocol.read_endpoint", return_value=metadata):
                client = RelayClient()
                current = self.store.reject_from_gui(self.request_id)
                result = client.call("acknowledge_receipt", request_id=self.request_id, claim_token=self.claim,
                                     request_hash=current["request_hash"], observed_state="answered")
            self.assertIs(result["request"]["result"]["answer"]["value"], False)
            self.assertEqual(result["request"]["delivery"]["status"], "connector_received")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_failed_acknowledgement_keeps_valid_result_and_terminal_journal(self):
        current = self.store.reject_from_gui(self.request_id)
        result = {"request": current}
        with patch.object(mcp, "RelayClient") as client, patch.object(mcp, "recovery_journal") as journal:
            client.return_value.call.side_effect = RelayError("broker_unavailable", "fixture")
            mcp._observe_receipt(result, current, self.claim, None)
            journal.return_value.observe.assert_called_once()
            self.assertTrue(journal.return_value.observe.call_args.kwargs["terminal"])
        self.assertIs(result["request"]["result"]["answer"]["value"], False)
        self.assertIn("Do not resubmit", result["delivery_warning"])

    def test_handshake_and_demo_are_not_current_chat_success(self):
        snapshot = {"component_match": True, "last_connector": {}, "plugin_enabled": True}
        self.assertEqual(connection_steps(snapshot)[3][1], "需在目标聊天验证")
        self.store.approve_from_gui(self.request_id)
        self.acknowledge()
        snapshot["recent_delivery"] = self.store.recent_delivery()
        step = connection_steps(snapshot)[3]
        self.assertEqual(step[1], "最近实测已取回")
        self.assertIn("不认证当前聊天", step[2])
        self.assertIn("不代表 Agent 已执行", step[2])

    def test_recent_receipt_follows_delivery_order_not_submission_order(self):
        second, _, second_claim = self.store.submit("decision", question(key="receipt-later-submission", source_task="Second"), CLIENT)
        self.store.approve_from_gui(second["request_id"])
        with patch("relay_broker.time.monotonic", return_value=1000):
            self.store.acknowledge_receipt(second["request_id"], second_claim, second["request_hash"], "answered")
        self.store.approve_from_gui(self.request_id)
        with patch("relay_broker.time.monotonic", return_value=1001):
            self.acknowledge()
        self.assertEqual(self.store.recent_delivery()["source_task"], "Semantic decision validation")


class CopyAndPracticeTests(TestCase):
    def test_repair_task_never_copies_secrets_or_caller_controlled_body(self):
        secret = "VERY-PRIVATE-CLAIM-AND-COMMAND"
        snapshot = {key: secret for key in ("token", "claim_token", "exact_action", "source_project",
                                           "source_task", "endpoint_path", "plugin_config_error", "last_connector")}
        snapshot.update(broker_version="0.5.0", broker_build_id="rel-1234567890abcdef12345678", protocol=9,
                        runtime_mode="self-contained-exe", plugin_enabled=True, component_match=True)
        self.assertNotIn(secret, repair_prompt(snapshot))
        snapshot["broker_version"] = "0.5.0\n" + secret
        self.assertNotIn(secret, repair_prompt(snapshot))

    def test_practice_covers_first_option_negative_question_and_no_answer(self):
        session = PracticeSession()
        self.assertFalse(session.advance())
        self.assertIn("第 1 项：保留现状", session.answer("agree"))
        self.assertIn("没有选择", session.answer("reject"))
        self.assertIn("不作答", session.answer("stop"))
        self.assertTrue(session.advance())
        self.assertIn("不再提示", session.answer("agree"))
        self.assertIn("保留提示", session.answer("reject"))
        self.assertFalse(session.advance())
        with self.assertRaises(ValueError):
            session.answer("execute")

    def test_view_preferences_cannot_write_approval_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            save_preferences(path, {"compact_mode": True, "auto_approve_wait_seconds": 100})
            self.assertTrue(load_preferences(path)["compact_mode"])
            self.assertNotIn("auto_approve_wait_seconds", path.read_text())


class ExperienceWindowTests(TestCase):
    @staticmethod
    def submit(window, key="experience-window-first", kind="boolean"):
        return window.store.submit("decision", question(kind, key=key), CLIENT)[0]["request_id"]

    def test_current_card_stays_stable_during_interaction(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch.object(window, "_process_auto_approval", return_value=False):
            first = self.submit(window)
            window.refresh()
            window._note_decision_interaction()
            self.submit(window, key="experience-window-next")
            window.refresh()
            self.assertEqual(window.current_request_id, first)
            self.assertIn("2", window.queue_button.cget("text"))
            window.store.reject_from_gui(first)
            window.refresh()
            self.assertNotEqual(window.current_request_id, first)

    def test_pending_native_button_invoke_cannot_switch_to_new_request(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch.object(window, "_process_auto_approval", return_value=False):
            first = self.submit(window)
            window.refresh()
            window.approve_button._native_invoke_pending = True
            self.submit(window, key="experience-native-next")
            window.refresh()
            self.assertEqual(window.current_request_id, first)
            window.approve_button._native_invoke_pending = False

    def test_compact_mode_keeps_controls_and_selection_without_new_refresh_loop(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_experience_ui.save_preferences") as save:
            request_id = self.submit(window)
            window.refresh()
            before = tuple(window.root.tk.call("after", "info"))
            window.toggle_compact_mode()
            self.assertTrue(window._compact_mode)
            self.assertFalse(window.task_panel.grid_info())
            self.assertEqual(int(window.decision_panel.grid_info()["columnspan"]), 2)
            self.assertEqual(window.current_request_id, request_id)
            self.assertTrue(window.approve_button._enabled)
            self.assertEqual(window._auto_approve_wait_seconds, 5)
            window.show_request_queue()
            self.assertFalse(window._compact_mode)
            self.assertTrue(window.task_panel.grid_info())
            self.assertEqual(window.current_request_id, request_id)
            self.assertEqual(save.call_count, 2)
            # Only the existing refresh callback is scheduled; mode switching
            # may add layout callbacks but must not multiply refresh loops.
            callbacks = [str(window.root.tk.call("after", "info", job)) for job in window.root.tk.call("after", "info")]
            self.assertLessEqual(sum("refresh" in callback for callback in callbacks),
                                 sum("refresh" in str(window.root.tk.call("after", "info", job)) for job in before if job in window.root.tk.call("after", "info")))

    def test_hidden_practice_creates_no_real_request_or_evidence(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_mcp_server.RelayClient") as client:
            window.show_practice()
            practice = window._experience_windows["practice"]
            self.assertEqual(practice.state(), "withdrawn")
            for label in ("同意", "拒绝", "停止"):
                button = next(item for item in widgets(practice) if isinstance(item, tk.ttk.Button) and item.cget("text") == label)
                button.invoke()
            self.assertEqual(window.store.active_counts(), (0, 0))
            self.assertEqual(len(window.store._records), 0)
            self.assertIsNone(window.store.recent_delivery())
            client.assert_not_called()
            practice.destroy()
            self.assertEqual(window._auto_approval_modal_depth, 0)

    def test_guide_exposes_real_next_steps_and_does_not_mark_chat_ready(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_broker.collect_install_diagnostics", return_value={}), patch.object(window.root, "clipboard_clear"), patch.object(window.root, "clipboard_append") as clipboard:
            window.show_connection_guide()
            guide = window._experience_windows["guide"]
            buttons = [item for item in widgets(guide) if isinstance(item, tk.ttk.Button)]
            copy = next(item for item in buttons if item.cget("text") == "复制聊天测试提示词")
            copy.invoke()
            clipboard.assert_called_once_with(CHAT_TEST_PROMPT)
            self.assertIsNone(window.store.recent_delivery())
            self.assertTrue(any(item.cget("text") == "复制修复任务" for item in buttons))
            guide.destroy()

    def test_pending_and_delivered_question_keep_distinct_outcomes(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit(window)
            window.refresh()
            self.assertEqual(window.decision_heading_label.cget("text"), "回答问题")
            self.assertIn("不再提示", window._summary_values[1])
            current = window.store.reject_from_gui(request_id)
            window._render_detail(request_id)
            self.assertIn("等待连接器取回", window.guard_label.cget("text"))
            record = window.store._records[request_id]
            window.store.acknowledge_receipt(request_id, record.claim_token, current["request_hash"], "answered")
            window._render_detail(request_id)
            self.assertIn("已取回", window.guard_label.cget("text"))
            self.assertIn("不代表 Agent 已执行", window.guard_label.cget("text"))


if __name__ == "__main__":
    main()
