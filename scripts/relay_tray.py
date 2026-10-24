"""Optional Windows tray bridge; callbacks never call Tk from a worker thread."""
from __future__ import annotations
from queue import SimpleQueue
import os
import threading


class TrayBridge:
    def __init__(self) -> None:
        self.events: SimpleQueue[str] = SimpleQueue()
        self.icon = None
        self.ready = threading.Event()
        self.failed = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()

    def _notify_failure(self, expected_icon=None) -> None:
        with self._state_lock:
            if expected_icon is not None and self.icon is not expected_icon:
                return
            self.ready.clear()
            if not self._stopping.is_set() and not self.failed.is_set():
                self.failed.set()
                self.events.put("show")

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            # Repeated clicks during startup share the same icon and ready event.
            return not self.failed.is_set() and not self._stopping.is_set()
        if os.name != "nt":
            return False
        self.ready.clear()
        self.failed.clear()
        self._stopping.clear()
        try:
            import pystray
            from PIL import Image, ImageDraw
            # A small native UI monogram; the README hero is never transformed.
            graphic = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            painter = ImageDraw.Draw(graphic)
            painter.ellipse((2, 2, 61, 61), fill="#D4AD51")
            painter.text((32, 32), "GF", fill="#171717", anchor="mm", font_size=25)
            icon = pystray.Icon("golden-finger", graphic, "Codex金手指", menu=pystray.Menu(
                pystray.MenuItem("打开窗口", lambda *_: self.events.put("show"), default=True),
                pystray.MenuItem("退出金手指", lambda *_: self.events.put("exit")),
            ))
            self.icon = icon
            def setup(icon):
                try:
                    with self._state_lock:
                        if self._stopping.is_set() or self.icon is not icon:
                            return
                        icon.visible = True
                        self.ready.set()
                except Exception:
                    self._notify_failure(icon)
                    try:
                        icon.stop()
                    except Exception:
                        pass
            def run():
                try:
                    icon.run(setup=setup)
                except Exception:
                    pass
                finally:
                    # pystray's Windows loop can consume errors and return normally.
                    # A returned loop has no accessible icon, even without an exception.
                    self._notify_failure(icon)
            self._thread = threading.Thread(target=run, name="golden-finger-tray", daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._notify_failure()
            return False

    def stop(self) -> None:
        with self._state_lock:
            self._stopping.set()
            self.ready.clear()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                # Optional tray failure must not interrupt broker/process shutdown.
                pass
