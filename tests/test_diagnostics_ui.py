"""Integration evidence and user control regression tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from relay_diagnostics import read_plugin_configuration, format_diagnostics, connection_steps
from relay_preferences import load_preferences, save_preferences
from relay_broker import collect_install_diagnostics
from relay_protocol import RelayError
import test_contract as contracts


class DiagnosticsTests(unittest.TestCase):
    def test_non_ascii_cached_build_id_does_not_break_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "plugins" / "cache" / "personal" / "golden-finger" / "0.2.0"
            cache.mkdir(parents=True)
            (cache / "BUILD-ID.txt").write_bytes("损坏的构建标记".encode("utf-8"))
            result = collect_install_diagnostics(codex_home=Path(folder))
        self.assertEqual(result["cached_installs"][0]["build_id"], "(unreadable)")
        self.assertFalse(result["exact_build_cached"])
        self.assertIn("(unreadable)", format_diagnostics(result))

    def test_unreadable_marketplace_does_not_hide_other_cache_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "plugins" / "cache"
            blocked = cache / "blocked" / "golden-finger"
            readable = cache / "good" / "golden-finger" / "0.3.0"
            blocked.mkdir(parents=True)
            readable.mkdir(parents=True)
            original = Path.iterdir
            def guarded(path):
                if path.resolve() == blocked.resolve():
                    raise PermissionError("fixture")
                return original(path)
            with patch.object(Path, "iterdir", guarded):
                result = collect_install_diagnostics(codex_home=Path(folder))
        self.assertEqual(len(result["cached_installs"]), 1)
        self.assertEqual(result["cached_installs"][0]["marketplace"], "good")
        self.assertTrue(result["cache_scan_errors"])

    def test_marketplaces_and_conflicting_enablement_are_distinguished(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text('[plugins."golden-finger@team"]\nenabled = true\n', encoding="utf-8")
            self.assertEqual(read_plugin_configuration(path)[:2], (True, ["golden-finger@team"]))
            with path.open("a", encoding="utf-8") as stream:
                stream.write('[plugins."golden-finger@personal"]\nenabled = false\n')
            self.assertIsNone(read_plugin_configuration(path)[0])

    def test_matching_connector_does_not_claim_current_chat_is_integrated(self):
        snapshot = {"component_match": True, "last_connector": {"observed_thread_count": 3},
                    "plugin_enabled": None, "cached_installs": []}
        steps = connection_steps(snapshot)
        self.assertEqual(steps[1][1], "最近握手匹配")
        self.assertEqual(steps[3][1], "需在目标聊天验证")
        text = format_diagnostics(snapshot)
        self.assertIn("未发现缓存安装", text)
        self.assertIn("启用状态尚未确认", text)

    def test_job_failure_is_reported_instead_of_windows_implying_success(self):
        with tempfile.TemporaryDirectory() as folder, patch("relay_broker.os.name", "nt"), patch(
            "relay_broker.WindowsJob.create", side_effect=RelayError("job_unavailable", "probe failed")
        ):
            result = collect_install_diagnostics(codex_home=Path(folder))
        self.assertFalse(result["job_object_available"])
        self.assertEqual(result["job_object_error"], "job_unavailable")

    def test_notification_preferences_cannot_change_approval_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "window-preferences.json"
            save_preferences(path, {"raise_on_request": False, "auto_approve_wait_seconds": 0})
            self.assertFalse(load_preferences(path)["raise_on_request"])
            self.assertNotIn("auto_approve_wait_seconds", json.loads(path.read_text()))

    def test_running_stop_requires_confirmation_and_targets_only_selected_request(self):
        with contracts.AppearanceTests.hidden_relay() as window:
            window.current_request_id = "test-running"
            with patch.object(window.store, "detail_for_gui", return_value={"state": "running"}), patch.object(
                window.store, "terminate_from_gui", create=True
            ) as terminate, patch.object(window, "_render_detail"), patch(
                "relay_broker.messagebox.askyesno", return_value=False
            ):
                window.stop_selected()
                terminate.assert_not_called()
            with patch.object(window.store, "detail_for_gui", return_value={"state": "running"}), patch.object(
                window.store, "terminate_from_gui", create=True
            ) as terminate, patch.object(window, "_render_detail"), patch(
                "relay_broker.messagebox.askyesno", return_value=True
            ):
                window.stop_selected()
                terminate.assert_called_once_with("test-running")

    def test_preserved_selection_still_notifies_each_new_request_once(self):
        with contracts.AppearanceTests.hidden_relay() as window, patch.object(
            window, "_process_auto_approval", return_value=False
        ), patch.object(window, "_notify_new_request") as notify:
            window._window_preferences["select_new_request"] = False
            first = window.store.submit("consent", {
                "exact_action": "First text-only fixture", "target": "test",
                "risk": "No effects", "recovery": "No effects",
                "idempotency_key": "notify-first",
            }, contracts.CLIENT_A)[0]
            window.refresh()
            self.assertEqual(window.current_request_id, first["request_id"])
            notify.reset_mock()
            window.store.submit("consent", {
                "exact_action": "Second text-only fixture", "target": "test",
                "risk": "No effects", "recovery": "No effects",
                "idempotency_key": "notify-second",
            }, contracts.CLIENT_A)
            window.refresh()
            self.assertEqual(window.current_request_id, first["request_id"])
            notify.assert_called_once()
            window.refresh()
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
