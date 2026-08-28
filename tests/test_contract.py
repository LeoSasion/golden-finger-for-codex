"""External-only unit tests for Golden Finger for Codex.

Run these from a user-controlled terminal. They do not launch Codex, the GUI,
or an actual command process.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from relay_broker import (
    RequestStore,
    WINDOW_HIT_SHIELD_OPACITY,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    acrylic_gradient_color,
    execution_mode_label,
    load_ui_settings,
    normalize_poll_policy,
    read_codex_theme_preference,
    resolve_theme,
    safe_transparent_client,
    save_ui_settings,
)
from relay_mcp_server import TOOLS, _polling_directive, handle_request
from relay_protocol import (
    DEFAULT_APPROVAL_EXPIRY_SECONDS,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_PENDING_POLL_CYCLES,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    RelayError,
    normalize_request,
)


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
        self.assertEqual(frozen["expires_in_seconds"], 3_600)

    def test_command_defaults_to_one_hour_and_rejects_above_hard_bound(self) -> None:
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


class StoreTests(unittest.TestCase):
    def test_gui_consent_decision_is_one_shot(self) -> None:
        store = RequestStore(elevated=False)
        submitted, replay = store.submit(
            "consent",
            {
                "exact_action": "Continue one bounded test.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-0001",
                "approved_reply": "授权",
            },
        )
        self.assertFalse(replay)
        approved = store.approve_from_gui(submitted["request_id"])
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["result"]["reply_text"], "授权")
        with self.assertRaises(RelayError):
            store.approve_from_gui(submitted["request_id"])

    def test_idempotency_conflict_fails_closed(self) -> None:
        store = RequestStore(elevated=False)
        base = {
            "exact_action": "Action A",
            "target": "Target",
            "risk": "Risk",
            "recovery": "Recovery",
            "idempotency_key": "test-store-0002",
        }
        store.submit("consent", base)
        store.update_poll_policy(
            {
                "sound_enabled": True,
                "poll_wait_seconds": 10,
                "max_pending_polls": 12,
                "command_timeout_seconds": 600,
            }
        )
        replayed, replay = store.submit("consent", base)
        self.assertTrue(replay)
        self.assertEqual(replayed["poll_policy"]["wait_seconds"], 50)
        changed = dict(base)
        changed["exact_action"] = "Action B"
        with self.assertRaises(RelayError) as raised:
            store.submit("consent", changed)
            self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_window_policy_is_frozen_and_pending_poll_limit_expires(self) -> None:
        store = RequestStore(
            elevated=False,
            settings={
                "sound_enabled": True,
                "poll_wait_seconds": 10,
                "max_pending_polls": 3,
                "command_timeout_seconds": 120,
            },
        )
        submitted, _replay = store.submit(
            "consent",
            {
                "exact_action": "Wait for a bounded polling test.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-policy-0001",
                "approved_reply": "同意",
            },
        )
        self.assertEqual(submitted["poll_policy"]["wait_seconds"], 10)
        self.assertEqual(submitted["poll_policy"]["max_pending_polls"], 3)
        self.assertEqual(submitted["poll_policy"]["pending_wait_budget_seconds"], 30)
        self.assertEqual(submitted["poll_policy"]["command_timeout_seconds"], 30)
        request_id = submitted["request_id"]
        with store._condition:
            store._records[request_id].pending_polls_used = 3
        snapshot = store.wait(request_id, 1)
        self.assertEqual(snapshot["state"], "expired")
        self.assertEqual(snapshot["poll_policy"]["pending_polls_used"], 3)

    def test_minimum_poll_and_single_cycle_form_a_valid_ten_second_budget(self) -> None:
        policy = normalize_poll_policy(
            {
                "poll_wait_seconds": 10,
                "max_pending_polls": 1,
            }
        )
        self.assertEqual(policy["pending_wait_budget_seconds"], 10)
        self.assertEqual(policy["command_timeout_seconds"], 10)

    def test_window_policy_replaces_legacy_one_hour_defaults(self) -> None:
        store = RequestStore(
            elevated=False,
            settings={
                "sound_enabled": True,
                "poll_wait_seconds": 50,
                "max_pending_polls": 100,
                "command_timeout_seconds": 5_000,
            },
        )
        submitted, _replay = store.submit(
            "consent",
            {
                "exact_action": "Use the configured default pending budget.",
                "target": "test state",
                "risk": "No external effect.",
                "recovery": "No recovery needed.",
                "idempotency_key": "test-store-policy-0002",
                "approved_reply": "同意",
            },
        )
        detail = store.detail_for_gui(submitted["request_id"])
        self.assertEqual(detail["frozen"]["expires_in_seconds"], 5_000)
        with tempfile.TemporaryDirectory() as temporary_directory:
            command, _replay = store.submit(
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
            )
            command_detail = store.detail_for_gui(command["request_id"])
            self.assertEqual(command_detail["frozen"]["timeout_seconds"], 5_000)


class AppearanceTests(unittest.TestCase):
    def test_window_baseline_keeps_fixed_chrome_visible(self) -> None:
        self.assertGreaterEqual(WINDOW_MIN_WIDTH, 920)
        self.assertGreaterEqual(WINDOW_MIN_HEIGHT, 600)

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

    def test_ui_preferences_roundtrip_is_bounded(self) -> None:
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
                    "poll_wait_seconds": 10,
                    "max_pending_polls": 12,
                    "command_timeout_seconds": 120,
                },
            )


class MCPShapeTests(unittest.TestCase):
    def test_tool_surface_has_no_approve_tool(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(
            names,
            {
                "relay_health",
                "request_consent",
                "request_command_execution",
                "wait_for_authorization",
                "cancel_authorization",
            },
        )
        self.assertFalse(any("approve" in name for name in names))

    def test_active_state_requires_same_turn_polling(self) -> None:
        active = _polling_directive(
            {
                "request_id": "har_TestRequest",
                "state": "running",
                "poll_policy": {
                    "wait_seconds": 12,
                    "pending_polls_used": 3,
                    "pending_polls_remaining": 9,
                    "policy_revision": "test-policy",
                },
            }
        )
        self.assertFalse(active["terminal"])
        self.assertTrue(active["must_continue_current_turn"])
        self.assertEqual(active["next_tool"], "wait_for_authorization")
        self.assertEqual(
            active["next_arguments"],
            {"request_id": "har_TestRequest", "wait_seconds": 12},
        )
        self.assertEqual(active["pending_polls_remaining"], 9)
        self.assertTrue(active["do_not_wait_for_user_message"])

        fallback = _polling_directive(
            {"request_id": "har_DefaultPolicy", "state": "pending"}
        )
        self.assertEqual(
            fallback["next_arguments"]["wait_seconds"],
            DEFAULT_POLL_WAIT_SECONDS,
        )
        self.assertEqual(DEFAULT_PENDING_POLL_CYCLES, 72)

        terminal = _polling_directive(
            {"request_id": "har_TestRequest", "state": "completed"}
        )
        self.assertTrue(terminal["terminal"])
        self.assertFalse(terminal["must_continue_current_turn"])

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


if __name__ == "__main__":
    unittest.main()
