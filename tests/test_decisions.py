"""Semantic question receipts, frozen choices, and isolated MCP/hidden GUI checks."""
from __future__ import annotations

from copy import deepcopy
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
from relay_decisions import decision_detail, decision_summary
from relay_protocol import RelayError, TERMINAL_STATES, normalize_request
import test_contract as contracts

CLIENT = "gfi_" + "D" * 32


def question(kind="boolean", key="semantic-decision-0001", **extra):
    data = {"exact_action": "是否不再提示？", "target": "本题的提示偏好",
            "risk": "仅记录答案，不执行任何命令。", "recovery": "后续可重新选择。",
            "idempotency_key": key, "decision_type": kind, "question_id": "prompt-setting",
            "source_label": "isolated decision test", "source_project": "(projectless)",
            "source_task": "Semantic decision validation"}
    if kind == "single_select":
        data["options"] = [
            {"id": "keep", "label": "保留现状", "description": "仅观察", "recommended": False},
            {"id": "change", "label": "调整（推荐）", "recommended": True},
        ]
    else:
        data.update(true_label="不再提示", false_label="保留提示")
    return {**data, **extra}


class DecisionValidationTests(unittest.TestCase):
    def test_question_negation_and_arbitrary_wording_are_preserved(self):
        for text in ("是否不再提示？", "保留这些结果，可以吗？", "Ready?", "このままでよいですか？"):
            with self.subTest(question=text):
                frozen = normalize_request("decision", question(exact_action=text))
                self.assertEqual(frozen["exact_action"], text)
                self.assertEqual(frozen["true_label"], "不再提示")

    def test_first_option_is_never_reordered_by_recommendation(self):
        data = question("single_select")
        frozen = normalize_request("decision", data)
        data["options"][0]["label"] = "mutated"
        data["options"].reverse()
        self.assertEqual([item["id"] for item in frozen["options"]], ["keep", "change"])
        self.assertEqual(frozen["options"][0]["label"], "保留现状")
        self.assertIn("第 1 项：保留现状", decision_summary(frozen))
        self.assertIn("调整（推荐）", "\n".join(decision_detail(frozen)))

    def test_changed_question_labels_or_option_order_change_hash(self):
        original = question("single_select")
        first_hash = normalize_request("decision", original)["request_hash"]
        for field in ("question", "order", "label", "description", "recommended", "question_id"):
            data = deepcopy(original)
            if field == "question":
                data["exact_action"] = "是否继续提示？"
            elif field == "order":
                data["options"].reverse()
            elif field in {"label", "description", "recommended"}:
                data["options"][0][field] = True if field == "recommended" else "changed"
            else:
                data["question_id"] = "another-question"
            with self.subTest(field=field):
                self.assertNotEqual(normalize_request("decision", data)["request_hash"], first_hash)

    def test_invalid_variants_fail_as_protocol_errors(self):
        boolean = question()
        choice = question("single_select")
        invalid = [
            {**boolean, "decision_type": []}, {**boolean, "decision_type": "unknown"},
            {**boolean, "options": []}, {**boolean, "false_label": "不再提示"},
            {**boolean, "true_label": ""}, {**boolean, "question_id": "x" * 129},
            {**boolean, "executable": "powershell.exe"}, {**boolean, "arguments": []},
            {**boolean, "approved_reply": "Yes"}, {**choice, "true_label": "Yes"},
            {**choice, "options": []}, {**choice, "options": [choice["options"][0]] * 2},
            {**choice, "options": [{"id": "a", "label": "one", "recommended": 1}]},
            {**choice, "options": [{"id": "a", "label": "one", "hidden": True}]},
            {**choice, "options": [{"id": "a", "label": "x" * 501}]},
            {**choice, "options": ["one"]},
        ]
        missing = deepcopy(choice)
        del missing["options"]
        invalid.append(missing)
        for index, data in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(RelayError):
                normalize_request("decision", data)


class DecisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = broker.RequestStore(elevated=False)

    def submit(self, kind="boolean", key="semantic-decision-0001", **extra):
        snapshot, replay, claim = self.store.submit("decision", question(kind, key, **extra), CLIENT)
        self.assertFalse(replay)
        return snapshot["request_id"], claim

    def test_boolean_yes_and_no_are_terminal_answers_without_execution(self):
        for affirmative in (True, False):
            with self.subTest(affirmative=affirmative):
                request_id, claim = self.submit(key=f"semantic-boolean-{affirmative}")
                with patch.object(self.store, "_schedule_execution_locked") as execute:
                    answer = (self.store.approve_from_gui if affirmative else self.store.reject_from_gui)(request_id)
                execute.assert_not_called()
                self.assertEqual(answer["state"], "answered")
                self.assertIn(answer["state"], TERMINAL_STATES)
                self.assertEqual(answer["result"]["answer"], {
                    "type": "boolean", "value": affirmative, "question_id": "prompt-setting"})
                self.assertEqual(answer["result"]["reply_text"], "不再提示" if affirmative else "保留提示")
                self.assertEqual(answer["receipt"]["approval_mode"], "manual")
                self.assertEqual(self.store.wait(request_id, claim, 0)["result"], answer["result"])
                self.assertEqual(self.store.active_counts(), (0, 0))
                for action in (self.store.approve_from_gui, self.store.reject_from_gui):
                    with self.assertRaises(RelayError):
                        action(request_id)

    def test_selection_is_original_first_even_without_recommended_marker(self):
        for recommended in (True, False):
            data = question("single_select", key=f"selection-first-{recommended}")
            data["options"][1]["recommended"] = recommended
            snapshot, _, _ = self.store.submit("decision", data, CLIENT)
            result = self.store.approve_from_gui(snapshot["request_id"])
            self.assertEqual(result["result"]["answer"], {
                "type": "single_select", "selected_index": 0, "option_id": "keep",
                "label": "保留现状", "question_id": "prompt-setting"})

    def test_selection_rejection_does_not_choose_second_option(self):
        request_id, _ = self.submit("single_select")
        with patch.object(self.store, "_schedule_execution_locked") as execute:
            result = self.store.reject_from_gui(request_id)
        self.assertEqual(result["state"], "rejected")
        self.assertNotIn("answer", result["result"])
        execute.assert_not_called()

    def test_stop_has_no_answer_and_wakes_the_waiter(self):
        for kind in ("boolean", "single_select"):
            request_id, claim = self.submit(kind, key=f"stop-question-{kind}")
            entered = threading.Event()
            result = []
            def wait():
                entered.set()
                result.append(self.store.wait(request_id, claim, 30))
            worker = threading.Thread(target=wait, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(1))
            stopped = self.store.cancel_from_gui(request_id)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(stopped["state"], "cancelled")
            self.assertEqual(result[0]["result"]["reason_code"], "stopped_by_user")
            self.assertNotIn("answer", stopped["result"])

    def test_automatic_answer_is_disclosed_without_a_human_click(self):
        request_id, _ = self.submit()
        result = self.store.approve_from_gui(request_id, automatic_wait_seconds=5)
        self.assertIs(result["result"]["answer"]["value"], True)
        self.assertEqual(result["receipt"]["approval_mode"], "automatic")
        self.assertNotIn("human_action", result["receipt"])

    def test_idempotency_replays_only_identical_question(self):
        data = question("single_select")
        snapshot, _, claim = self.store.submit("decision", data, CLIENT)
        data["options"].reverse()
        with self.assertRaises(RelayError) as caught:
            self.store.submit("decision", data, CLIENT)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        result = self.store.approve_from_gui(snapshot["request_id"])
        replayed, replay, same_claim = self.store.submit("decision", question("single_select"), CLIENT)
        self.assertTrue(replay)
        self.assertEqual(same_claim, claim)
        self.assertEqual(replayed["result"], result["result"])

    def test_question_claims_are_isolated_and_answers_do_not_leak(self):
        first, first_claim = self.submit()
        second, second_claim = self.submit(key="other-question-0002")
        for operation in (self.store.status, self.store.cancel):
            with self.assertRaises(RelayError) as caught:
                operation(first, second_claim)
            self.assertEqual(caught.exception.code, "claim_mismatch")
        self.store.reject_from_gui(first)
        self.assertEqual(self.store.status(second, second_claim)["state"], "pending")
        self.assertIs(self.store.status(first, first_claim)["result"]["answer"]["value"], False)


class DecisionWindowTests(unittest.TestCase):
    def test_hidden_window_renders_mapping_all_options_and_answer(self):
        for kind in ("boolean", "single_select"):
            with self.subTest(kind=kind), contracts.AppearanceTests.hidden_relay() as window:
                snapshot, _, _ = window.store.submit("decision", question(kind), CLIENT)
                window.refresh()
                request_id = snapshot["request_id"]
                self.assertEqual(window.current_request_id, request_id)
                self.assertIn("同意 =", window._summary_values[2])
                text = window.detail.get("1.0", "end")
                self.assertIn("是否不再提示？", text)
                self.assertIn("原始问题", text)
                self.assertIn("不运行程序", text)
                if kind == "single_select":
                    self.assertIn("保留现状", text)
                    self.assertIn("调整（推荐）", text)
                window.reject_selected()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"],
                                 "answered" if kind == "boolean" else "rejected")
                self.assertFalse(window.approve_button._enabled)

    def test_decision_cards_reuse_existing_window_timer(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            snapshot, _, _ = window.store.submit("decision", question("single_select"), CLIENT)
            started = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=started):
                window.refresh()
            self.assertEqual(window.approve_button.cget("text"), "同意\n5 秒")
            with patch("relay_broker.time.monotonic", return_value=started + 5):
                window.refresh()
            result = window.store.detail_for_gui(snapshot["request_id"])
            self.assertEqual(result["state"], "answered")
            self.assertEqual(result["result"]["answer"]["option_id"], "keep")
            self.assertEqual(result["receipt"]["approval_mode"], "automatic")


@unittest.skipUnless(os.name == "nt", "Recovery journal uses current-user Windows DPAPI")
class DecisionMCPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = broker.RequestStore(elevated=False)
        self.operations = []
        owner = self
        class Client:
            def call(self, operation, **kwargs):
                owner.operations.append(operation)
                if operation == "submit":
                    snapshot, replay, claim = owner.store.submit(
                        kwargs["kind"], kwargs["payload"], kwargs["client_instance_id"],
                        recovery_token=kwargs["recovery_token"], payload_hash=kwargs["payload_hash"])
                    return {"ok": True, "request": snapshot, "claim_token": claim, "idempotent_replay": replay}
                if operation == "wait":
                    return {"ok": True, "request": owner.store.status(kwargs["request_id"], kwargs["claim_token"])}
                if operation == "recover":
                    return owner.store.recover(kwargs["recovery_token"], kwargs["payload_hash"])
                raise AssertionError(operation)
        def journal(thread, process):
            return recovery.RecoveryJournal("process:" + process, root=self.root)
        for name, replacement in (("RelayClient", Client), ("recovery_journal", journal)):
            mocked = patch.object(mcp, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_mcp_negative_answer_is_terminal_and_recovered_without_resubmission(self):
        args = {**question(), "wait_seconds": 0}
        response = mcp.handle_request({"jsonrpc": "2.0", "id": 901, "method": "tools/call",
                                      "params": {"name": "request_decision", "arguments": args}})
        self.assertFalse(response["result"].get("isError", False))
        result = response["result"]["structuredContent"]
        request_id = result["request"]["request_id"]
        self.assertTrue(result["polling"]["must_continue_current_turn"])
        self.store.reject_from_gui(request_id)
        final = mcp.call_tool("wait_for_authorization", result["polling"]["next_arguments"])
        self.assertEqual(final["request"]["state"], "answered")
        self.assertIs(final["request"]["result"]["answer"]["value"], False)
        self.assertTrue(final["polling"]["terminal"])
        self.assertFalse(final["polling"]["must_continue_current_turn"])
        self.assertIn("valid answer", final["continuation"])
        replayed = mcp.call_tool("request_decision", args)
        self.assertEqual(replayed["request"]["request_id"], request_id)
        self.assertEqual(self.operations.count("submit"), 1)
        journal = recovery.RecoveryJournal("process:" + mcp.CLIENT_INSTANCE_ID, root=self.root)
        entry = journal.prepare(result["recovery"]["payload_hash"])[1]
        self.assertEqual(entry["phase"], "terminal")


if __name__ == "__main__":
    unittest.main()
