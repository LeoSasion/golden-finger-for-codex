"""User-invoked, no-console launcher for Codex金手指 on Windows.

This entry point is intended for a desktop or Start Menu shortcut.  It starts
only the independent broker in the current user context; Codex and the MCP
connector never invoke it automatically.
"""

from __future__ import annotations

import io
from pathlib import Path
import sys
sys.dont_write_bytecode = True
import tkinter as tk
from tkinter import messagebox


WINDOW_TITLE = "Codex金手指"


def show_startup_error(detail: str) -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror(
            WINDOW_TITLE,
            f"无法启动 Codex金手指。\n\n{detail}",
            parent=root,
        )
    finally:
        root.destroy()


def main() -> int:
    scripts_directory = Path(__file__).resolve().parent
    scripts_text = str(scripts_directory)
    if scripts_text not in sys.path:
        sys.path.insert(0, scripts_text)

    captured_stderr = io.StringIO()
    previous_stderr = sys.stderr
    exit_code = 1
    try:
        sys.stderr = captured_stderr
        from relay_broker import main as broker_main

        exit_code = broker_main()
    except Exception as exc:  # pragma: no cover - visible last-resort reporting
        detail = captured_stderr.getvalue().strip()
        if detail:
            detail = f"{detail}\n{type(exc).__name__}: {exc}"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        show_startup_error(detail)
        return 1
    finally:
        sys.stderr = previous_stderr

    if exit_code != 0:
        detail = captured_stderr.getvalue().strip()
        show_startup_error(detail or f"启动器返回错误代码 {exit_code}。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
