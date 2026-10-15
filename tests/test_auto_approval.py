"""Automatic-approval checks using isolated hidden windows and consent cards."""

from __future__ import annotations

import sys
import json
import os
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import test_contract as contracts
import relay_mcp_server
from relay_broker import load_ui_settings, parse_auto_approve_wait_seconds
from relay_protocol import RelayError


class AutoApprovalTests(unittest.TestCase):
    def test_button_countdown_tracks_selected_card_and_clears_when_paused(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_broker.save_ui_settings"):
            first = self.submit_consent(window, "countdown-first")
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
                self.assertEqual(window.approve_button.cget("text"), "同意\n5 秒")
            with patch("relay_broker.time.monotonic", return_value=start + 2.1):
                second = self.submit_consent(window, "countdown-second")
                window.refresh()
                self.assertEqual(window.approve_button.cget("text"), "同意\n5 秒")
                window._select_item(window._request_item_id(first))
                self.assertEqual(window.approve_button.cget("text"), "同意\n3 秒")
            with patch("relay_broker.time.monotonic", return_value=start + 4.01):
                window._render_detail(first)
                self.assertEqual(window.approve_button.cget("text"), "同意\n1 秒")
                window.auto_approve_wait_value.set("0")
                window._apply_auto_approve_wait()
                window.refresh()
                self.assertEqual(window.approve_button.cget("text"), "同意\n本次")
                self.assertEqual(window.store.detail_for_gui(first)["state"], "pending")
            window.store.reject_from_gui(first)
            window._render_detail(first)
            self.assertEqual(window.approve_button.cget("text"), "同意\n本次")

    @staticmethod
    def submit_consent(window, key="auto-approval-test", **extra):
        snapshot, _replay, _claim = window.store.submit("consent", {
            "exact_action": "Approve one isolated test card.",
            "target": "Test memory only",
            "risk": "No external effects.",
            "recovery": "Discard the isolated test store.",
            "idempotency_key": key,
            "approved_reply": "同意",
            **extra,
        }, contracts.CLIENT_A)
        return snapshot["request_id"]

    def test_hidden_fixture_never_raises_or_activates_notifications(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch.object(
            window.root, "deiconify"
        ) as deiconify, patch.object(window.root, "lift") as lift, patch(
            "relay_broker.activate_native_window", return_value=False
        ) as activate:
            self.submit_consent(window)
            window.refresh()
            window._activate_new_request_window()
            window.root.update()
            self.assertEqual(window.root.state(), "withdrawn")
            deiconify.assert_not_called()
            lift.assert_not_called()
            activate.assert_not_called()

    def test_wait_input_rejects_non_integer_and_out_of_range_values(self):
        for value in ("", "-1", "1.5", "nan", "abc", "3601", "9" * 5000, True, 1.5, None):
            with self.subTest(value=str(value)[:30]):
                with self.assertRaises(ValueError):
                    parse_auto_approve_wait_seconds(value)
        for value, expected in ((" 5 ", 5), ("0", 0), (3600, 3600)):
            self.assertEqual(parse_auto_approve_wait_seconds(value), expected)

    def test_each_pending_card_has_its_own_timer_independent_of_selection(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            first = self.submit_consent(window, "first-card")
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 3):
                second = self.submit_consent(window, "second-card")
                window.refresh()
                self.assertEqual(window.current_request_id, second)
            with patch("relay_broker.time.monotonic", return_value=start + 5):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(first)["state"], "approved")
                self.assertEqual(window.store.detail_for_gui(second)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 8):
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(second)["state"], "approved")
            self.assertEqual(window.store.active_counts(), (0, 0))

    def test_manual_decisions_are_never_overwritten_by_the_timer(self):
        for action, state in (("approve_selected", "approved"),
                              ("reject_selected", "rejected"), ("stop_selected", "cancelled")):
            with self.subTest(action=action), contracts.AppearanceTests.hidden_relay() as window:
                request_id = self.submit_consent(window)
                start = time.monotonic()
                with patch("relay_broker.time.monotonic", return_value=start):
                    window.refresh()
                getattr(window, action)()
                before = window.store.detail_for_gui(request_id)
                with patch("relay_broker.time.monotonic", return_value=start + 10):
                    window.refresh()
                after = window.store.detail_for_gui(request_id)
                self.assertEqual(after["state"], state)
                self.assertEqual(after["receipt"], before["receipt"])
                self.assertNotIn(request_id, window._auto_approve_started_at)
                if action == "approve_selected":
                    self.assertEqual(after["receipt"]["approval_mode"], "manual")
                    self.assertEqual(after["receipt"]["human_action"], "approved_once")

    def test_expired_requests_are_not_approved(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_broker.save_ui_settings"):
            window.auto_approve_wait_value.set("20")
            window.auto_approve_apply_button.invoke()
            request_id = self.submit_consent(window, expires_in_seconds=10)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 21):
                window.refresh()
            detail = window.store.detail_for_gui(request_id)
            self.assertEqual(detail["state"], "expired")
            self.assertIsNone(detail.get("receipt"))

    def test_zero_disables_approval_and_unapplied_edits_pause_it(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch("relay_broker.save_ui_settings"):
            request_id = self.submit_consent(window)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            for value in ("invalid", "0"):
                window.auto_approve_wait_value.set(value)
                window.auto_approve_apply_button.invoke()
                with patch("relay_broker.time.monotonic", return_value=start + 100):
                    window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            self.assertIn("已关闭", window.auto_approve_status.cget("text"))

    def test_command_auto_approval_reuses_the_existing_worker_once(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            snapshot, _replay, _claim = window.store.submit("command", {
                "exact_action": "Exercise the worker transition without executing a process.",
                "executable": sys.executable, "arguments": ["-c", "print('unused')"],
                "working_directory": str(Path(__file__).resolve().parent),
                "target": "Isolated test memory", "risk": "Worker target is intercepted.",
                "recovery": "Discard test store.", "idempotency_key": "auto-command-test",
            }, contracts.CLIENT_A)
            request_id = snapshot["request_id"]
            started = threading.Event()
            start = time.monotonic()
            with patch.object(window.store, "_execute_command", side_effect=lambda _id: started.set()) as worker:
                with patch("relay_broker.time.monotonic", return_value=start):
                    window.refresh()
                with patch("relay_broker.time.monotonic", return_value=start + 5):
                    window.refresh()
                self.assertTrue(started.wait(timeout=1))
                with patch("relay_broker.time.monotonic", return_value=start + 10):
                    window.refresh()
                worker.assert_called_once_with(request_id)
            detail = window.store.detail_for_gui(request_id)
            self.assertEqual(detail["state"], "running")
            self.assertEqual(detail["receipt"]["approval_mode"], "automatic")
            window.store.shutdown()

    def test_live_tk_refresh_approves_after_the_real_five_second_wait(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            # Let only the constructor's scheduled refresh loop run.
            started = time.monotonic()
            deadline = started + 7
            while time.monotonic() < deadline:
                window.root.update()
                if window.store.detail_for_gui(request_id)["state"] == "approved":
                    break
                time.sleep(0.01)
            elapsed = time.monotonic() - started
            self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")
            self.assertGreaterEqual(elapsed, 5)
            self.assertLess(elapsed, 7)

    def test_mcp_descriptions_disclose_automatic_approval_instead_of_claiming_a_click(self):
        tools = {tool["name"]: tool for tool in relay_mcp_server.TOOLS}
        for name in ("request_consent", "request_command_execution"):
            with self.subTest(tool=name):
                self.assertIn("automatic", tools[name]["description"])
                self.assertIn("receipt.approval_mode", tools[name]["description"])

    def test_automatic_receipt_requires_a_positive_bounded_integer(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            for invalid in (0, -1, 3601, True, 5.5, "5"):
                with self.subTest(value=invalid):
                    with self.assertRaises(RelayError) as raised:
                        window.store.approve_from_gui(request_id, automatic_wait_seconds=invalid)
                    self.assertEqual(raised.exception.code, "invalid_auto_approval_wait")
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")

    def test_corrupt_saved_settings_disable_automatic_approval(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            settings_path = Path(temporary) / "ui-settings.json"
            self.assertEqual(load_ui_settings(settings_path)["auto_approve_wait_seconds"], 5)
            for text in ("{broken-json", "[]", '{"auto_approve_wait_seconds":false}',
                         '{"auto_approve_wait_seconds":-1}', '{"auto_approve_wait_seconds":5.5}'):
                with self.subTest(text=text):
                    settings_path.write_text(text, encoding="utf-8")
                    self.assertEqual(load_ui_settings(settings_path)["auto_approve_wait_seconds"], 0)

    def test_sound_save_warning_pauses_nested_dialogs_and_restarts_all_cards(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_ids = [self.submit_consent(window, key) for key in ("modal-first", "modal-second")]
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()

            def refresh_while_modal(elapsed):
                self.assertIsNone(window.root.grab_current())
                with patch("relay_broker.time.monotonic", return_value=start + elapsed):
                    window.refresh()
                self.assertFalse(window._auto_approve_started_at)
                for request_id in request_ids:
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")

            def warning(title, *_args, **_kwargs):
                # Check synchronous entry before any 250 ms refresh observes the dialog.
                self.assertFalse(window._auto_approve_started_at)
                if title == "无法保存提示音设置":
                    refresh_while_modal(10)
                    window._apply_auto_approve_wait()
                    refresh_while_modal(20)  # Inner apply dialog must not resume the outer one.
                    window.on_close()
                    refresh_while_modal(21)  # Cancelling inner close must also remain paused.
                    window.open_computer_use_guidance()
                    refresh_while_modal(23)
                else:
                    self.assertEqual(title, "无法保存自动审批设置")
                    refresh_while_modal(11)

            def cancel_close(*_args, **_kwargs):
                refresh_while_modal(22)
                return False

            with patch("relay_broker.save_ui_settings", side_effect=OSError("isolated save failure")), patch(
                "relay_broker.messagebox.showwarning", side_effect=warning
            ), patch("relay_broker.messagebox.askyesno", side_effect=cancel_close), patch(
                "relay_broker.resolve_computer_use_guidance", side_effect=FileNotFoundError("isolated missing file")
            ), patch("relay_broker.messagebox.showerror", side_effect=lambda *_a, **_kw: refresh_while_modal(23)):
                window._on_sound_toggle()

            self.assertFalse(window.closing)
            self.assertFalse(window._auto_approve_started_at)
            with patch("relay_broker.time.monotonic", return_value=start + 30):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 34.99):
                window.refresh()
                for request_id in request_ids:
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 35):
                window.refresh()
            for request_id in request_ids:
                detail = window.store.detail_for_gui(request_id)
                self.assertEqual(detail["state"], "approved")
                self.assertEqual(detail["receipt"]["approval_mode"], "automatic")
                self.assertNotIn("human_action", detail["receipt"])

    def test_modal_exit_clears_timers_even_when_dialog_raises(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()

            def fail_dialog(*_args, **_kwargs):
                self.assertFalse(window._auto_approve_started_at)
                # Prove cleanup on exit, not merely cleanup by refresh while modal.
                window._auto_approve_started_at[request_id] = start
                raise RuntimeError("isolated dialog failure")

            with patch("relay_broker.save_ui_settings", side_effect=OSError("isolated save failure")), patch(
                "relay_broker.messagebox.showwarning", side_effect=fail_dialog
            ):
                with self.assertRaisesRegex(RuntimeError, "isolated dialog failure"):
                    window._on_sound_toggle()
            self.assertFalse(window._auto_approve_started_at)
            with patch("relay_broker.time.monotonic", return_value=start + 10):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 14.99):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 15):
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")

    def test_each_nonclose_native_modal_blocks_deadline_refresh(self):
        actions = ("approve_selected", "reject_selected", "stop_selected", "request_safe_restart",
                   "restart_success", "restart_failure", "guidance_missing", "guidance_open_failure")
        for action in actions:
            with self.subTest(action=action), contracts.AppearanceTests.hidden_relay() as window:
                request_id = self.submit_consent(window)
                start = time.monotonic()
                with patch("relay_broker.time.monotonic", return_value=start):
                    window.refresh()

                def modal(*_args, **_kwargs):
                    self.assertFalse(window._auto_approve_started_at)
                    self.assertIsNone(window.root.grab_current())
                    with patch("relay_broker.time.monotonic", return_value=start + 10):
                        window.refresh()
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")

                with patch("relay_broker.messagebox.showerror", side_effect=modal) as error, patch(
                    "relay_broker.messagebox.showwarning", side_effect=modal
                ) as warning, patch("relay_broker.messagebox.showinfo", side_effect=modal) as info, patch.object(
                    window.store, "approve_from_gui", side_effect=RelayError("isolated", "test only")
                ), patch.object(window.store, "reject_from_gui", side_effect=RelayError("isolated", "test only")), patch.object(
                    window.store, "cancel_from_gui", side_effect=RelayError("isolated", "test only")
                ), patch("relay_broker.resolve_computer_use_guidance", **(
                    {"side_effect": FileNotFoundError("isolated")} if action == "guidance_missing"
                    else {"return_value": Path("isolated.md")}
                )), patch("relay_broker.os.startfile", create=True, side_effect=OSError("isolated")):
                    if action.startswith("restart_"):
                        from codex_lifecycle import RestartResult
                        window._finish_safe_restart(RestartResult(action == "restart_success", "isolated", "test only"))
                    elif action.startswith("guidance_"):
                        window.open_computer_use_guidance()
                    else:
                        getattr(window, action)()
                    self.assertEqual(error.call_count + warning.call_count + info.call_count, 1)
                self.assertFalse(window._auto_approve_started_at)
                with patch("relay_broker.time.monotonic", return_value=start + 11):
                    window.refresh()
                with patch("relay_broker.time.monotonic", return_value=start + 15.99):
                    window.refresh()
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
                with patch("relay_broker.time.monotonic", return_value=start + 16):
                    window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")

    def test_detail_open_and_close_between_ticks_restarts_the_full_wait(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 4.75):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 4.9):
                window.toggle_detail()
                self.assertTrue(window._detail_expanded)
                self.assertFalse(window._auto_approve_started_at)
            with patch("relay_broker.time.monotonic", return_value=start + 4.95):
                window.toggle_detail()
                self.assertFalse(window._detail_expanded)
                self.assertFalse(window._auto_approve_started_at)
            with patch("relay_broker.time.monotonic", return_value=start + 5):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 9.99):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 10):
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(request_id)["receipt"]["approval_mode"], "automatic")

    def test_cancelled_button_interactions_between_ticks_restart_wait(self):
        from types import SimpleNamespace
        interactions = ("mouse_release_outside", "mouse_leave", "mouse_motion_outside", "mouse_focus_out",
                        "keyboard_focus_out")
        for button_name in ("approve_button", "reject_button", "stop_button"):
            for interaction in interactions:
                with self.subTest(button=button_name, interaction=interaction), contracts.AppearanceTests.hidden_relay() as window:
                    request_id = self.submit_consent(window)
                    start = time.monotonic()
                    with patch("relay_broker.time.monotonic", return_value=start):
                        window.refresh()
                    button = getattr(window, button_name)
                    center = button._diameter // 2
                    inside = SimpleNamespace(x=center, y=center)
                    outside = SimpleNamespace(x=-1, y=-1)
                    with patch("relay_broker.time.monotonic", return_value=start + 4.9):
                        if interaction == "keyboard_focus_out":
                            button._on_space_press(inside)
                        else:
                            # Deliver a real Tk mouse binding even though the toplevel is hidden.
                            button._on_enter(inside)
                            button.event_generate("<ButtonPress-1>", x=center, y=center)
                        self.assertTrue(button._pressed)
                        self.assertFalse(window._auto_approve_started_at)
                        if interaction == "mouse_release_outside":
                            button._on_release(outside)
                        elif interaction == "mouse_leave":
                            button._on_leave(outside)
                        elif interaction == "mouse_motion_outside":
                            button._on_motion(outside)
                        else:
                            button._on_focus_out(outside)
                        self.assertFalse(button._pressed)
                        self.assertFalse(window._auto_approve_started_at)
                    with patch("relay_broker.time.monotonic", return_value=start + 5):
                        window.refresh()
                        self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
                    with patch("relay_broker.time.monotonic", return_value=start + 9.99):
                        window.refresh()
                        self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
                    with patch("relay_broker.time.monotonic", return_value=start + 10):
                        window.refresh()
                    self.assertEqual(window.store.detail_for_gui(request_id)["receipt"]["approval_mode"], "automatic")

    def test_button_decisions_between_ticks_preserve_manual_outcome_and_other_wait(self):
        import ctypes
        from types import SimpleNamespace
        from relay_broker import BUTTON_MESSAGE_CLICK
        for button_name, state in (("approve_button", "approved"), ("reject_button", "rejected"),
                                   ("stop_button", "cancelled")):
            for interaction in ("return", "invoke", "native", "mouse", "space"):
                if interaction == "native" and os.name != "nt":
                    continue
                with self.subTest(button=button_name, interaction=interaction), contracts.AppearanceTests.hidden_relay() as window:
                    other = self.submit_consent(window, "other-card")
                    selected = self.submit_consent(window, "selected-card")
                    start = time.monotonic()
                    with patch("relay_broker.time.monotonic", return_value=start):
                        window.refresh()
                    window._select_item(window._request_item_id(selected))
                    button = getattr(window, button_name)
                    center = button._diameter // 2
                    event = SimpleNamespace(x=center, y=center)
                    with patch("relay_broker.time.monotonic", return_value=start + 4.9):
                        if interaction == "return":
                            button._on_return(event)
                        elif interaction == "invoke":
                            button.invoke()
                        elif interaction == "native":
                            self.assertTrue(button._native_subclass_installed)
                            ctypes.windll.user32.SendMessageW(ctypes.c_void_p(button.winfo_id()), BUTTON_MESSAGE_CLICK, 0, 0)
                            self.assertTrue(button._native_invoke_pending)
                            self.assertFalse(window._auto_approve_started_at)
                            button._drain_native_invoke()
                        elif interaction == "mouse":
                            button.event_generate("<Enter>", x=center, y=center)
                            button.event_generate("<ButtonPress-1>", x=center, y=center)
                            button.event_generate("<ButtonRelease-1>", x=center, y=center)
                        else:
                            button._on_space_press(event)
                            button._on_space_release(event)
                            # Exercise Tk's keyboard class path, not Python invoke().
                            button.tk.call("tk::ButtonInvokeEnd", str(button), "normal", "flat")
                        self.assertFalse(window._auto_approve_started_at)
                    manual = window.store.detail_for_gui(selected)
                    self.assertEqual(manual["state"], state)
                    if button_name == "approve_button":
                        self.assertEqual(manual["receipt"]["approval_mode"], "manual")
                        self.assertEqual(manual["receipt"]["human_action"], "approved_once")
                    with patch("relay_broker.time.monotonic", return_value=start + 5):
                        window.refresh()
                    with patch("relay_broker.time.monotonic", return_value=start + 9.99):
                        window.refresh()
                        self.assertEqual(window.store.detail_for_gui(other)["state"], "pending")
                    with patch("relay_broker.time.monotonic", return_value=start + 10):
                        window.refresh()
                    self.assertEqual(window.store.detail_for_gui(other)["receipt"]["approval_mode"], "automatic")
                    after = window.store.detail_for_gui(selected)
                    self.assertEqual(after["state"], state)
                    self.assertEqual(after["receipt"], manual["receipt"])

    @unittest.skipUnless(os.name == "nt", "Native button bridge is Windows-only")
    def test_queued_native_button_click_blocks_refresh_before_tk_drain(self):
        import ctypes
        from relay_broker import BUTTON_MESSAGE_CLICK
        with contracts.AppearanceTests.hidden_relay() as window:
            other = self.submit_consent(window, "native-other")
            selected = self.submit_consent(window, "native-selected")
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            window._select_item(window._request_item_id(selected))
            button = window.reject_button
            with patch("relay_broker.time.monotonic", return_value=start + 4.9):
                ctypes.windll.user32.SendMessageW(ctypes.c_void_p(button.winfo_id()), BUTTON_MESSAGE_CLICK, 0, 0)
                self.assertTrue(button._native_invoke_pending)
            with patch("relay_broker.time.monotonic", return_value=start + 10):
                window.refresh()
                self.assertFalse(window._auto_approve_started_at)
                self.assertEqual(window.store.detail_for_gui(selected)["state"], "pending")
                self.assertEqual(window.store.detail_for_gui(other)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 11):
                button._drain_native_invoke()
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(selected)["state"], "rejected")
            with patch("relay_broker.time.monotonic", return_value=start + 15.99):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(other)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 16):
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(other)["state"], "approved")

    def test_selection_and_repaint_do_not_restart_pending_timers(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_ids = [self.submit_consent(window, key) for key in ("repaint-first", "repaint-second")]
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            before = dict(window._auto_approve_started_at)
            with patch("relay_broker.time.monotonic", return_value=start + 4.9):
                for request_id in request_ids:
                    window._select_item(window._request_item_id(request_id))
                    window._render_detail(request_id)
                for button in (window.approve_button, window.reject_button, window.stop_button):
                    button._redraw()
                    button.apply_theme(window.palette, parent_background=window.palette.surface)
                window._set_detail_expanded(False)  # No state change is not an interaction.
                self.assertEqual(window._auto_approve_started_at, before)
            with patch("relay_broker.time.monotonic", return_value=start + 5):
                window.refresh()
            for request_id in request_ids:
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")

    def test_close_confirmation_pauses_approval_and_cancel_restarts_the_wait(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()

            def cancel_close(*_args, **_kwargs):
                with patch("relay_broker.time.monotonic", return_value=start + 10):
                    window.refresh()
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
                return False

            with patch("relay_broker.messagebox.askyesno", side_effect=cancel_close):
                window.on_close()
            with patch("relay_broker.time.monotonic", return_value=start + 11):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 15.99):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 16):
                window.refresh()
            self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")

    def test_ui_wait_parameter_is_persisted_and_restarts_the_countdown(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            settings_path = Path(temporary) / "ui-settings.json"
            with contracts.AppearanceTests.hidden_relay() as window, patch(
                "relay_broker.ui_settings_path", return_value=settings_path
            ):
                self.assertTrue(hasattr(window, "auto_approve_wait_value"))
                self.assertEqual(window.auto_approve_wait_value.get(), "5")
                request_id = self.submit_consent(window)
                start = time.monotonic()
                with patch("relay_broker.time.monotonic", return_value=start):
                    window.refresh()
                with patch("relay_broker.time.monotonic", return_value=start + 3):
                    window.auto_approve_wait_value.set("9")
                    window.auto_approve_apply_button.invoke()
                    window.refresh()
                    self.assertIn("9 秒", window.auto_approve_status.cget("text"))
                self.assertEqual(load_ui_settings(settings_path)["auto_approve_wait_seconds"], 9)
                self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))[
                    "auto_approve_wait_seconds"
                ], 9)
                with patch("relay_broker.time.monotonic", return_value=start + 11.99):
                    window.refresh()
                    self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
                with patch("relay_broker.time.monotonic", return_value=start + 12):
                    window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "approved")

    def test_pending_card_is_automatically_approved_after_five_seconds(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            request_id = self.submit_consent(window)
            start = time.monotonic()
            with patch("relay_broker.time.monotonic", return_value=start):
                window.refresh()
            with patch("relay_broker.time.monotonic", return_value=start + 4.99):
                window.refresh()
                self.assertEqual(window.store.detail_for_gui(request_id)["state"], "pending")
            with patch("relay_broker.time.monotonic", return_value=start + 5):
                window.refresh()
            approved = window.store.detail_for_gui(request_id)
            self.assertEqual(approved["state"], "approved")
            self.assertEqual(approved["result"]["reply_text"], "同意")
            self.assertEqual(approved["receipt"]["approval_mode"], "automatic")
            self.assertEqual(approved["receipt"]["auto_approve_wait_seconds"], 5)
            self.assertNotIn("human_action", approved["receipt"])


if __name__ == "__main__":
    unittest.main()
