"""Tray lifecycle tests with fake icons; never create a real notification icon."""

from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from relay_tray import TrayBridge


class FakeIcon:
    def __init__(self, behavior="wait"):
        self.behavior = behavior
        self.visible = False
        self.release = threading.Event()
        self.stop_calls = 0

    def run(self, *, setup):
        setup(self)
        if self.behavior == "raise":
            raise RuntimeError("notification loop failed")
        if self.behavior == "wait":
            self.release.wait(5)

    def stop(self):
        self.stop_calls += 1
        self.release.set()


class TrayRuntimeTests(unittest.TestCase):
    def start_fake(self, behavior):
        icons = []
        def make_icon(*args, **kwargs):
            icon = FakeIcon(behavior)
            icons.append(icon)
            return icon
        fake = types.SimpleNamespace(Icon=make_icon, Menu=lambda *items: items, MenuItem=lambda *args, **kwargs: args)
        imports = patch.dict(sys.modules, {"pystray": fake})
        imports.start()
        self.addCleanup(imports.stop)
        platform = patch("relay_tray.os.name", "nt")
        platform.start()
        self.addCleanup(platform.stop)
        bridge = TrayBridge()
        def cleanup():
            bridge.stop()
            if bridge._thread is not None:
                bridge._thread.join(2)
        self.addCleanup(cleanup)
        self.assertTrue(bridge.start())
        return bridge, icons

    def test_unexpected_normal_loop_exit_restores_window(self):
        bridge, _ = self.start_fake("return")
        bridge._thread.join(2)
        self.assertFalse(bridge._thread.is_alive())
        self.assertFalse(bridge.ready.is_set())
        self.assertTrue(bridge.failed.is_set())
        self.assertEqual(bridge.events.get_nowait(), "show")
        self.assertTrue(bridge.events.empty())

    def test_exception_loop_exit_restores_window(self):
        bridge, _ = self.start_fake("raise")
        bridge._thread.join(2)
        self.assertFalse(bridge.ready.is_set())
        self.assertTrue(bridge.failed.is_set())
        self.assertEqual(bridge.events.get_nowait(), "show")

    def test_explicit_stop_does_not_reopen_window(self):
        bridge, icons = self.start_fake("wait")
        self.assertTrue(bridge.ready.wait(2))
        bridge.stop()
        bridge._thread.join(2)
        self.assertFalse(bridge.ready.is_set())
        self.assertFalse(bridge.failed.is_set())
        self.assertTrue(bridge.events.empty())
        self.assertEqual(icons[0].stop_calls, 1)

    def test_repeated_start_reuses_live_icon_and_can_restart_stopped_icon(self):
        bridge, icons = self.start_fake("wait")
        self.assertTrue(bridge.ready.wait(2))
        self.assertTrue(bridge.start())
        self.assertEqual(len(icons), 1)
        bridge.stop()
        bridge._thread.join(2)
        self.assertTrue(bridge.start())
        self.assertTrue(bridge.ready.wait(2))
        self.assertEqual(len(icons), 2)
        self.assertTrue(bridge.events.empty())

    def test_optional_tray_stop_error_does_not_abort_shutdown(self):
        bridge, icons = self.start_fake("wait")
        self.assertTrue(bridge.ready.wait(2))
        with patch.object(icons[0], "stop", side_effect=OSError("tray unavailable")):
            bridge.stop()
        icons[0].release.set()
        bridge._thread.join(2)
        self.assertTrue(bridge.events.empty())


if __name__ == "__main__":
    unittest.main()
