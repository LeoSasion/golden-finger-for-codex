"""Independent approval window for Codex金手指 (Golden Finger for Codex).

This process is intentionally launched by the user, not by Codex or the MCP
connector. Approval transitions are reachable only from GUI callbacks.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ctypes
from ctypes import wintypes
import json
import locale
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, BinaryIO, Callable

import tkinter as tk
from tkinter import messagebox, ttk

from codex_lifecycle import (
    CodexTask,
    CodexTaskInventory,
    CodexTaskMonitor,
    RestartResult,
    graceful_restart_codex,
    normalize_codex_thread_id,
)
from relay_protocol import (
    DEFAULT_PENDING_POLL_CYCLES,
    DEFAULT_POLL_WAIT_SECONDS,
    MAX_APPROVAL_EXPIRY_SECONDS,
    MAX_OUTPUT_BYTES,
    MAX_PENDING_POLL_CYCLES,
    MAX_PENDING_REQUESTS,
    MAX_POLL_WAIT_SECONDS,
    MAX_WIRE_BYTES,
    MIN_APPROVAL_EXPIRY_SECONDS,
    MIN_PENDING_POLL_CYCLES,
    MIN_POLL_WAIT_SECONDS,
    PACKAGE_VERSION,
    PROTOCOL_VERSION,
    REQUEST_ID_PREFIX,
    RUNTIME_BUILD_ID,
    RelayError,
    broker_is_reachable,
    canonical_hash,
    claim_fingerprint,
    endpoint_directory,
    fingerprint_executable,
    verify_script_dependencies,
    json_ascii,
    new_bearer_token,
    new_claim_token,
    normalize_client_instance_id,
    normalize_request,
    remove_endpoint_if_owned,
    write_endpoint,
)


BROKER_VERSION = PACKAGE_VERSION
BROKER_BUILD_ID = RUNTIME_BUILD_ID
TERMINAL_RETENTION_SECONDS = 3_600
WINDOW_BACKGROUND_OPACITY = 0.60
WINDOW_CONTENT_OPACITY = 1.0
WINDOW_TRANSPARENT_COLOR = "#010203"
WINDOW_HIT_SHIELD_OPACITY = 0.01
WINDOW_DEFAULT_WIDTH = 980
WINDOW_DEFAULT_HEIGHT = 720
WINDOW_MIN_WIDTH = 920
WINDOW_MIN_HEIGHT = 600
WINDOW_MIN_CONTENT_HEIGHT = 260
WINDOW_DETAIL_WIDTH = 470
WINDOW_EXPANDED_MIN_WIDTH = 1_340
THEME_REFRESH_SECONDS = 2.0
POLICY_APPLY_DEBOUNCE_MS = 250
UI_SETTINGS_FILENAME = "ui-settings.json"
ACTIVE_STATES = {"pending", "running"}
TERMINAL_STATES = {
    "approved",
    "rejected",
    "cancelled",
    "expired",
    "completed",
    "failed",
    "timed_out",
}


@dataclass(frozen=True)
class ThemePalette:
    name: str
    window: str
    surface: str
    surface_alt: str
    text: str
    muted: str
    border: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    selection: str
    code_background: str
    success_text: str
    danger_text: str
    danger: str
    danger_hover: str
    danger_pressed: str
    shadow: str
    disabled_background: str
    disabled_text: str
    secondary_hover: str


THEME_PALETTES = {
    "light": ThemePalette(
        name="light",
        window="#F7F7F5",
        surface="#FFFFFF",
        surface_alt="#F0F1F2",
        text="#0D0D0D",
        muted="#62666D",
        border="#D9DCE0",
        accent="#0285FF",
        accent_hover="#0076E4",
        accent_pressed="#0068C8",
        accent_text="#FFFFFF",
        selection="#D9ECFF",
        code_background="#F4F5F6",
        success_text="#205C2F",
        danger_text="#8F1D1D",
        danger="#D92D20",
        danger_hover="#E5483C",
        danger_pressed="#B42318",
        shadow="#C7CBD1",
        disabled_background="#E5E7EA",
        disabled_text="#8A8F96",
        secondary_hover="#E6E8EB",
    ),
    "dark": ThemePalette(
        name="dark",
        window="#181818",
        surface="#202020",
        surface_alt="#2A2A2A",
        text="#FFFFFF",
        muted="#B3B3B3",
        border="#3A3A3A",
        accent="#339CFF",
        accent_hover="#57ADFF",
        accent_pressed="#2088ED",
        accent_text="#FFFFFF",
        selection="#163B59",
        code_background="#111111",
        success_text="#A8E4B8",
        danger_text="#FFB8B8",
        danger="#E5484D",
        danger_hover="#F05D61",
        danger_pressed="#C93C41",
        shadow="#0E0E0E",
        disabled_background="#303030",
        disabled_text="#777777",
        secondary_hover="#343434",
    ),
}


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def normalize_theme_preference(value: object) -> str | None:
    if isinstance(value, str) and value.casefold() in {"light", "dark", "system"}:
        return value.casefold()
    return None


def read_codex_theme_preference(config_path: Path | None = None) -> str:
    """Read Codex's public appearance choice, falling back without failing startup."""

    path = config_path or codex_config_path()
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return "system"
    desktop = config.get("desktop")
    candidates: list[object] = []
    if isinstance(desktop, dict):
        candidates.extend(
            (
                desktop.get("appearanceTheme"),
                desktop.get("appearance_theme"),
            )
        )
    candidates.extend((config.get("appearanceTheme"), config.get("appearance_theme")))
    for value in candidates:
        preference = normalize_theme_preference(value)
        if preference is not None:
            return preference
    return "system"


def read_windows_app_theme() -> str:
    """Return the current Windows app theme without requiring another package."""

    if os.name != "nt":
        return "light"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except (OSError, TypeError, ValueError):
        return "light"


def resolve_theme(preference: str, *, system_theme: str | None = None) -> str:
    if preference in THEME_PALETTES:
        return preference
    resolved_system = system_theme or read_windows_app_theme()
    return resolved_system if resolved_system in THEME_PALETTES else "light"


def ui_settings_path() -> Path:
    return endpoint_directory() / UI_SETTINGS_FILENAME


def default_ui_settings() -> dict[str, Any]:
    pending_wait_budget_seconds = (
        DEFAULT_POLL_WAIT_SECONDS * DEFAULT_PENDING_POLL_CYCLES
    )
    return {
        "sound_enabled": True,
        "poll_wait_seconds": DEFAULT_POLL_WAIT_SECONDS,
        "max_pending_polls": DEFAULT_PENDING_POLL_CYCLES,
        "command_timeout_seconds": pending_wait_budget_seconds,
    }


def normalize_poll_policy(settings: dict[str, Any]) -> dict[str, Any]:
    poll_wait_seconds = settings.get("poll_wait_seconds")
    max_pending_polls = settings.get("max_pending_polls")
    for name, value, minimum, maximum in (
        (
            "poll_wait_seconds",
            poll_wait_seconds,
            MIN_POLL_WAIT_SECONDS,
            MAX_POLL_WAIT_SECONDS,
        ),
        (
            "max_pending_polls",
            max_pending_polls,
            MIN_PENDING_POLL_CYCLES,
            MAX_PENDING_POLL_CYCLES,
        ),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
    pending_wait_budget_seconds = poll_wait_seconds * max_pending_polls
    if pending_wait_budget_seconds < MIN_APPROVAL_EXPIRY_SECONDS:
        raise ValueError(
            "poll_wait_seconds multiplied by max_pending_polls must be at least "
            f"{MIN_APPROVAL_EXPIRY_SECONDS} seconds"
        )
    if pending_wait_budget_seconds > MAX_APPROVAL_EXPIRY_SECONDS:
        raise ValueError(
            "poll_wait_seconds multiplied by max_pending_polls must not exceed "
            f"{MAX_APPROVAL_EXPIRY_SECONDS} seconds"
        )
    command_timeout_seconds = pending_wait_budget_seconds
    policy = {
        "wait_seconds": poll_wait_seconds,
        "max_pending_polls": max_pending_polls,
        "pending_wait_budget_seconds": pending_wait_budget_seconds,
        "command_timeout_seconds": command_timeout_seconds,
    }
    policy["policy_revision"] = canonical_hash(policy)[:16]
    return policy


def load_ui_settings(path: Path | None = None) -> dict[str, Any]:
    settings = default_ui_settings()
    try:
        value = json.loads((path or ui_settings_path()).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return settings
    if not isinstance(value, dict):
        return settings
    if isinstance(value.get("sound_enabled"), bool):
        settings["sound_enabled"] = value["sound_enabled"]
    policy_candidate = {
        key: value.get(key, settings[key])
        for key in (
            "poll_wait_seconds",
            "max_pending_polls",
        )
    }
    try:
        policy = normalize_poll_policy(policy_candidate)
    except ValueError:
        return settings
    settings.update(
        {
            "poll_wait_seconds": policy["wait_seconds"],
            "max_pending_polls": policy["max_pending_polls"],
            "command_timeout_seconds": policy["command_timeout_seconds"],
        }
    )
    return settings


def save_ui_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    policy = normalize_poll_policy(settings)
    destination = path or ui_settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix="ui-settings-",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "sound_enabled": bool(settings.get("sound_enabled", True)),
                    "poll_wait_seconds": policy["wait_seconds"],
                    "max_pending_polls": policy["max_pending_polls"],
                    "command_timeout_seconds": policy["command_timeout_seconds"],
                },
                handle,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_name, 0o600)
        except OSError:
            pass
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def acrylic_gradient_color(color: str, opacity: float) -> int:
    """Encode an acrylic tint as the Windows ACCENT_POLICY AABBGGRR value."""

    if len(color) != 7 or not color.startswith("#"):
        raise ValueError("color must be #RRGGBB")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    alpha = round(max(0.0, min(1.0, float(opacity))) * 255)
    return (alpha << 24) | (blue << 16) | (green << 8) | red


def enable_acrylic_backdrop(hwnd: int, *, color: str, opacity: float) -> bool:
    """Best-effort background-only acrylic; Windows owns the blur radius."""

    if os.name != "nt":
        return False
    try:
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        set_window_composition_attribute = (
            ctypes.windll.user32.SetWindowCompositionAttribute
        )
        set_window_composition_attribute.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(WindowCompositionAttributeData),
        )
        set_window_composition_attribute.restype = ctypes.c_int
        policy = AccentPolicy(
            AccentState=4,  # ACCENT_ENABLE_ACRYLICBLURBEHIND
            AccentFlags=2,
            GradientColor=acrylic_gradient_color(color, opacity),
            AnimationId=0,
        )
        data = WindowCompositionAttributeData(
            Attribute=19,  # WCA_ACCENT_POLICY
            Data=ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
            SizeOfData=ctypes.sizeof(policy),
        )
        return bool(
            set_window_composition_attribute(
                ctypes.c_void_p(hwnd),
                ctypes.byref(data),
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def safe_transparent_client(
    *, backdrop_enabled: bool, hit_shield_ready: bool
) -> bool:
    """Only expose color-keyed pixels when a nonzero-alpha hit target is behind them."""

    return bool(backdrop_enabled and hit_shield_ready)


def native_toplevel_hwnd(root: tk.Misc) -> int | None:
    if os.name != "nt":
        return None
    try:
        root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = (ctypes.c_void_p,)
        user32.GetParent.restype = ctypes.c_void_p
        hwnd = user32.GetParent(ctypes.c_void_p(root.winfo_id())) or root.winfo_id()
        return int(hwnd)
    except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
        return None


def place_native_window_behind(front: tk.Misc, back: tk.Misc) -> bool:
    """Keep a pointer-catching companion directly behind the visible Tk window."""

    if os.name != "nt":
        return False
    front_hwnd = native_toplevel_hwnd(front)
    back_hwnd = native_toplevel_hwnd(back)
    if front_hwnd is None or back_hwnd is None:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        user32.SetWindowPos.restype = ctypes.c_int
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0200
        return bool(
            user32.SetWindowPos(
                ctypes.c_void_p(back_hwnd),
                ctypes.c_void_p(front_hwnd),
                0,
                0,
                0,
                0,
                flags,
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def activate_native_window(root: tk.Tk) -> bool:
    """Best-effort one-shot foreground activation after a new request arrives."""

    hwnd = native_toplevel_hwnd(root)
    if hwnd is None:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.ShowWindow.restype = ctypes.c_int
        user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)
        user32.BringWindowToTop.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def apply_native_window_appearance(
    root: tk.Tk,
    *,
    dark: bool,
    backdrop_color: str,
    backdrop_opacity: float,
) -> bool:
    """Apply title-bar theming, rounded corners, and a background-only acrylic tint."""

    if os.name != "nt":
        return False
    hwnd = native_toplevel_hwnd(root)
    if hwnd is None:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        )
        corner_preference = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            33,
            ctypes.byref(corner_preference),
            ctypes.sizeof(corner_preference),
        )
        dark_value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            result = dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                attribute,
                ctypes.byref(dark_value),
                ctypes.sizeof(dark_value),
            )
            if result == 0:
                break
    except (AttributeError, OSError, TypeError, tk.TclError):
        pass
    return enable_acrylic_backdrop(
        hwnd,
        color=backdrop_color,
        opacity=backdrop_opacity,
    )


class CircularButton(tk.Canvas):
    """Responsive, keyboard-accessible circular decision control."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        command: Callable[[], None],
        variant: str,
        diameter: int,
    ) -> None:
        super().__init__(
            parent,
            width=diameter,
            height=diameter,
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="hand2",
        )
        self.button_text = text
        self.command = command
        self.variant = variant
        self._diameter = diameter
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self._focused = False
        self._palette = THEME_PALETTES["light"]
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<KeyPress-space>", self._on_space_press)
        self.bind("<KeyRelease-space>", self._on_space_release)
        self.bind("<Return>", self._on_return)

    def apply_theme(self, palette: ThemePalette, *, parent_background: str) -> None:
        self._palette = palette
        self.configure(background=parent_background)
        self._redraw()

    def set_state(self, state: str) -> None:
        enabled = state not in {tk.DISABLED, "disabled"}
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._hovered = False
            self._pressed = False
        self.configure(takefocus=1 if enabled else 0, cursor="hand2" if enabled else "")
        self._redraw()

    def set_diameter(self, diameter: int) -> None:
        bounded = max(42, int(diameter))
        if bounded == self._diameter:
            return
        self._diameter = bounded
        self.configure(width=bounded, height=bounded)
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        size = self._diameter
        palette = self._palette
        small = self.variant == "danger"
        if not self._enabled:
            fill = palette.disabled_background
            foreground = palette.disabled_text
        elif small:
            fill = (
                palette.danger_pressed
                if self._pressed
                else palette.danger_hover
                if self._hovered
                else palette.danger
            )
            foreground = "#FFFFFF"
        else:
            fill = (
                palette.accent_pressed
                if self._pressed
                else palette.accent_hover
                if self._hovered
                else palette.accent
            )
            foreground = palette.accent_text

        shift = 2 if self._pressed else 0
        shadow_offset = max(2, round(size * 0.025))
        outline_width = 3 if self._focused and self._enabled else 1
        outline = palette.text if self._focused and self._enabled else (
            palette.danger_hover if small else palette.accent_hover
        )
        if not self._pressed:
            self.create_oval(
                5 + shadow_offset,
                5 + shadow_offset,
                size - 5 + shadow_offset,
                size - 5 + shadow_offset,
                fill=palette.shadow,
                outline="",
            )
        self.create_oval(
            4 + shift,
            4 + shift,
            size - 7 + shift,
            size - 7 + shift,
            fill=fill,
            outline=outline,
            width=outline_width,
        )
        font_size = max(9, round(size * (0.16 if small else 0.105)))
        self.create_text(
            size // 2 + shift,
            size // 2 + shift,
            text=self.button_text,
            fill=foreground,
            justify=tk.CENTER,
            font=("Microsoft YaHei UI", font_size, "bold"),
        )

    def _on_enter(self, _event: tk.Event[Any]) -> None:
        if self._enabled:
            self._hovered = True
            self._redraw()

    def _on_leave(self, _event: tk.Event[Any]) -> None:
        self._hovered = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event: tk.Event[Any]) -> None:
        if self._enabled:
            self.focus_set()
            self._pressed = True
            self._redraw()

    def _on_release(self, event: tk.Event[Any]) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and 0 <= event.x <= self._diameter and 0 <= event.y <= self._diameter:
            self.command()

    def _on_focus_in(self, _event: tk.Event[Any]) -> None:
        self._focused = True
        self._redraw()

    def _on_focus_out(self, _event: tk.Event[Any]) -> None:
        self._focused = False
        self._pressed = False
        self._redraw()

    def _on_space_press(self, _event: tk.Event[Any]) -> str:
        if self._enabled:
            self._pressed = True
            self._redraw()
        return "break"

    def _on_space_release(self, _event: tk.Event[Any]) -> str:
        if self._enabled and self._pressed:
            self._pressed = False
            self._redraw()
            self.command()
        return "break"

    def _on_return(self, _event: tk.Event[Any]) -> str:
        if self._enabled:
            self.command()
        return "break"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_elevated() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def execution_mode_label(kind: str, elevated: bool) -> str:
    if kind != "command":
        return "执行方式: 不运行程序，仅返回确认文本"
    if elevated:
        return "执行权限: 管理员（获批命令将继承此权限）"
    return "执行权限: 普通用户"


class SingleInstanceLock:
    """OS-released lock preventing two brokers for the same user."""

    def __init__(self) -> None:
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        directory = endpoint_directory()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "instance.lock"
        handle = open(path, "a+b")
        try:
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RelayError(
                "broker_already_running",
                "Another Codex金手指 window already owns the instance lock.",
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        self._handle.close()
        self._handle = None


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    """Own a Windows process tree and kill it when the handle is closed."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int | None) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> "WindowsJob":
        if os.name != "nt":
            return cls(None)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise RelayError(
                "job_object_failed",
                "Windows could not create a process-tree containment job.",
                details={"winerror": ctypes.get_last_error()},
            )
        job = cls(int(handle))
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(job._handle),
            cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            job.close()
            raise RelayError(
                "job_object_failed",
                "Windows could not configure process-tree containment.",
                details={"winerror": error},
            )
        return job

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = int(getattr(process, "_handle"))
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self._handle),
            wintypes.HANDLE(process_handle),
        ):
            raise RelayError(
                "job_assignment_failed",
                "The approved process could not be contained in a Windows Job Object.",
                details={"winerror": ctypes.get_last_error()},
            )

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not kernel32.TerminateJobObject(
            wintypes.HANDLE(self._handle),
            wintypes.UINT(exit_code),
        ):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))


@dataclass
class RequestRecord:
    request_id: str
    client_instance_id: str = field(repr=False)
    claim_token: str = field(repr=False)
    claim_fingerprint: str
    frozen: dict[str, Any]
    created_at: str
    expires_at: str
    expires_monotonic: float
    elevated: bool
    codex_thread_id: str | None = field(default=None, repr=False)
    state: str = "pending"
    updated_at: str = field(default_factory=iso_timestamp)
    terminal_monotonic: float | None = None
    receipt: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    pending_polls_used: int = 0
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    process_job: WindowsJob | None = field(default=None, repr=False)

    @property
    def kind(self) -> str:
        return str(self.frozen["kind"])

    @property
    def request_hash(self) -> str:
        return str(self.frozen["request_hash"])

    def public_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "claim_fingerprint": self.claim_fingerprint,
            "kind": self.kind,
            "state": self.state,
            "exact_action": self.frozen["exact_action"],
            "target": self.frozen["target"],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "broker_elevated": self.elevated,
            "poll_policy": {
                **self.frozen["poll_policy"],
                "pending_polls_used": self.pending_polls_used,
                "pending_polls_remaining": max(
                    0,
                    int(self.frozen["poll_policy"]["max_pending_polls"])
                    - self.pending_polls_used,
                ),
            },
        }
        if self.receipt is not None:
            snapshot["receipt"] = dict(self.receipt)
        if self.result is not None:
            snapshot["result"] = dict(self.result)
        return snapshot


class RequestStore:
    def __init__(self, *, elevated: bool, settings: dict[str, Any] | None = None) -> None:
        self.elevated = elevated
        self._condition = threading.Condition(threading.RLock())
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()
        self._idempotency: dict[tuple[str, str], str] = {}
        self._shutting_down = False
        self._restart_pending = False
        self._poll_policy = normalize_poll_policy(settings or default_ui_settings())

    def current_poll_policy(self) -> dict[str, Any]:
        with self._condition:
            return dict(self._poll_policy)

    def is_stopping(self) -> bool:
        with self._condition:
            return self._shutting_down

    def update_poll_policy(self, settings: dict[str, Any]) -> dict[str, Any]:
        policy = normalize_poll_policy(settings)
        with self._condition:
            if self._shutting_down:
                raise RelayError("broker_stopping", "The relay window is stopping.")
            self._poll_policy = policy
            return dict(policy)

    @staticmethod
    def _freeze_with_policy(
        kind: Any,
        payload: Any,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        frozen = normalize_request(kind, payload)
        frozen["proposal_hash"] = frozen["request_hash"]
        payload_fields = payload if isinstance(payload, dict) else {}
        if "expires_in_seconds" in payload_fields:
            frozen["expires_in_seconds"] = min(
                int(frozen["expires_in_seconds"]),
                int(policy["pending_wait_budget_seconds"]),
            )
        else:
            frozen["expires_in_seconds"] = int(policy["pending_wait_budget_seconds"])
        if frozen["kind"] == "command":
            if "timeout_seconds" in payload_fields:
                frozen["timeout_seconds"] = min(
                    int(frozen["timeout_seconds"]),
                    int(policy["command_timeout_seconds"]),
                )
            else:
                frozen["timeout_seconds"] = int(policy["command_timeout_seconds"])
        frozen["poll_policy"] = dict(policy)
        frozen["request_hash"] = canonical_hash(frozen)
        return frozen

    @staticmethod
    def _expire_pending_locked(record: RequestRecord, *, message: str) -> None:
        now = time.monotonic()
        record.state = "expired"
        record.updated_at = iso_timestamp()
        record.terminal_monotonic = now
        record.result = {
            "decision": "expired",
            "message": message,
        }

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for record in self._records.values():
            if record.state == "pending" and now >= record.expires_monotonic:
                self._expire_pending_locked(
                    record,
                    message="The request expired before a human decision.",
                )
        removable = [
            request_id
            for request_id, record in self._records.items()
            if record.terminal_monotonic is not None
            and now - record.terminal_monotonic > TERMINAL_RETENTION_SECONDS
        ]
        for request_id in removable:
            record = self._records.pop(request_id)
            key = (
                record.client_instance_id,
                str(record.frozen["idempotency_key"]),
            )
            if self._idempotency.get(key) == request_id:
                del self._idempotency[key]
        if removable:
            self._condition.notify_all()

    def submit(
        self,
        kind: Any,
        payload: Any,
        client_instance_id: Any,
        codex_thread_id: Any = None,
    ) -> tuple[dict[str, Any], bool, str]:
        normalized_client_instance_id = normalize_client_instance_id(
            client_instance_id
        )
        normalized_codex_thread_id = normalize_codex_thread_id(codex_thread_id)
        policy = self.current_poll_policy()
        frozen = self._freeze_with_policy(kind, payload, policy)
        frozen["connector_fingerprint"] = canonical_hash(
            normalized_client_instance_id
        )[:12]
        frozen["request_hash"] = canonical_hash(frozen)
        with self._condition:
            if self._shutting_down:
                raise RelayError("broker_stopping", "The relay window is stopping.")
            if self._restart_pending:
                raise RelayError(
                    "broker_restarting_codex",
                    "Codex restart preparation is in progress; new requests are temporarily paused.",
                )
            self._expire_locked()
            idempotency_key = str(frozen["idempotency_key"])
            idempotency_scope = (normalized_client_instance_id, idempotency_key)
            existing_id = self._idempotency.get(idempotency_scope)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is None:
                    del self._idempotency[idempotency_scope]
                elif existing.frozen.get("proposal_hash") != frozen["proposal_hash"]:
                    raise RelayError(
                        "idempotency_conflict",
                        "The idempotency key was reused for a different frozen request.",
                        details={"request_id": existing.request_id},
                    )
                else:
                    if existing.codex_thread_id is None:
                        existing.codex_thread_id = normalized_codex_thread_id
                    return existing.public_snapshot(), True, existing.claim_token

            active_count = sum(
                record.state in ACTIVE_STATES for record in self._records.values()
            )
            if active_count >= MAX_PENDING_REQUESTS:
                raise RelayError(
                    "queue_full",
                    "The relay already has too many active requests.",
                )

            created = utc_now()
            expires = created + timedelta(seconds=frozen["expires_in_seconds"])
            request_id = REQUEST_ID_PREFIX + secrets.token_urlsafe(12).replace(
                "-", ""
            ).replace("_", "")
            claim_token = new_claim_token()
            record = RequestRecord(
                request_id=request_id,
                client_instance_id=normalized_client_instance_id,
                claim_token=claim_token,
                claim_fingerprint=claim_fingerprint(claim_token),
                frozen=frozen,
                created_at=iso_timestamp(created),
                expires_at=iso_timestamp(expires),
                expires_monotonic=time.monotonic() + frozen["expires_in_seconds"],
                elevated=self.elevated,
                codex_thread_id=normalized_codex_thread_id,
            )
            self._records[request_id] = record
            self._idempotency[idempotency_scope] = request_id
            self._condition.notify_all()
            return record.public_snapshot(), False, claim_token

    def _get_locked(self, request_id: Any) -> RequestRecord:
        if not isinstance(request_id, str) or not request_id.startswith(REQUEST_ID_PREFIX):
            raise RelayError("invalid_request_id", "request_id is invalid.")
        self._expire_locked()
        record = self._records.get(request_id)
        if record is None:
            raise RelayError("request_not_found", "The relay request was not found.")
        return record

    def _get_claimed_locked(
        self,
        request_id: Any,
        claim_token: Any,
    ) -> RequestRecord:
        record = self._get_locked(request_id)
        valid_claim = (
            isinstance(claim_token, str)
            and claim_token.isascii()
            and 32 <= len(claim_token) <= 256
            and secrets.compare_digest(claim_token, record.claim_token)
        )
        if not valid_claim:
            raise RelayError(
                "claim_mismatch",
                "The relay request is not claimed by this caller.",
            )
        return record

    def status(self, request_id: Any, claim_token: Any) -> dict[str, Any]:
        with self._condition:
            return self._get_claimed_locked(request_id, claim_token).public_snapshot()

    def wait(
        self,
        request_id: Any,
        claim_token: Any,
        wait_seconds: Any,
    ) -> dict[str, Any]:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise RelayError("invalid_wait", "wait_seconds must be a number.")
        with self._condition:
            record = self._get_claimed_locked(request_id, claim_token)
            policy = record.frozen["poll_policy"]
            requested_wait = float(wait_seconds)
            bounded_wait = (
                float(policy["wait_seconds"])
                if requested_wait > 0
                else 0.0
            )
            if record.state == "pending" and bounded_wait > 0:
                if record.pending_polls_used >= int(policy["max_pending_polls"]):
                    self._expire_pending_locked(
                        record,
                        message="The configured pending polling limit was reached.",
                    )
                    self._condition.notify_all()
                    return record.public_snapshot()
                record.pending_polls_used += 1
            deadline = time.monotonic() + bounded_wait
            while record.state in ACTIVE_STATES and not self._shutting_down:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                next_expiry = max(0.0, record.expires_monotonic - time.monotonic())
                self._condition.wait(timeout=min(remaining, next_expiry or remaining))
                record = self._get_claimed_locked(request_id, claim_token)
            if (
                record.state == "pending"
                and bounded_wait > 0
                and record.pending_polls_used >= int(policy["max_pending_polls"])
            ):
                self._expire_pending_locked(
                    record,
                    message="The configured pending polling limit was reached.",
                )
                self._condition.notify_all()
            return record.public_snapshot()

    def cancel(self, request_id: Any, claim_token: Any) -> dict[str, Any]:
        with self._condition:
            record = self._get_claimed_locked(request_id, claim_token)
            if record.state == "pending":
                record.state = "cancelled"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "cancelled",
                    "message": "The pending request was cancelled by the caller.",
                }
                self._condition.notify_all()
                return record.public_snapshot()
            if record.state == "running":
                raise RelayError(
                    "already_started",
                    "The command has already started; cancellation cannot revoke approval.",
                )
            return record.public_snapshot()

    def reject_from_gui(self, request_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._get_locked(request_id)
            if record.state != "pending":
                raise RelayError(
                    "decision_already_made",
                    "This request is no longer pending.",
                )
            now = iso_timestamp()
            record.state = "rejected"
            record.updated_at = now
            record.terminal_monotonic = time.monotonic()
            record.receipt = {
                "human_action": "rejected_once",
                "decided_at": now,
                "request_hash": record.request_hash,
            }
            record.result = {
                "decision": "rejected",
                "reply_text": "拒绝",
            }
            self._condition.notify_all()
            return record.public_snapshot()

    def approve_from_gui(self, request_id: str) -> dict[str, Any]:
        """The sole approval transition. Call only from a GUI event handler."""

        with self._condition:
            record = self._get_locked(request_id)
            if record.state != "pending":
                raise RelayError(
                    "decision_already_made",
                    "This request is no longer pending.",
                )
            now = iso_timestamp()
            record.receipt = {
                "human_action": "approved_once",
                "decided_at": now,
                "request_hash": record.request_hash,
            }
            record.updated_at = now
            if record.kind == "consent":
                record.state = "approved"
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "reply_text": record.frozen["approved_reply"],
                }
                self._condition.notify_all()
                return record.public_snapshot()

            record.state = "running"
            self._condition.notify_all()
            worker = threading.Thread(
                target=self._execute_command,
                args=(record.request_id,),
                name=f"relay-exec-{record.request_id[-8:]}",
                daemon=True,
            )
            worker.start()
            return record.public_snapshot()

    def _finish_execution(
        self,
        request_id: str,
        *,
        state: str,
        message: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        job: WindowsJob | None = None
        with self._condition:
            record = self._records.get(request_id)
            if record is None:
                return
            record.state = state
            record.updated_at = iso_timestamp()
            record.terminal_monotonic = time.monotonic()
            record.process = None
            job = record.process_job
            record.process_job = None
            record.result = {
                "decision": "approved",
                "message": message,
                **(result or {}),
            }
            self._condition.notify_all()
        if job is not None:
            job.close()

    @staticmethod
    def _drain_stream(
        stream: BinaryIO,
        retained: bytearray,
        truncated: list[bool],
    ) -> None:
        try:
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    break
                remaining = MAX_OUTPUT_BYTES - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _decode_output(data: bytes) -> tuple[str, str]:
        if not data:
            return "", "utf-8"
        candidates = ["utf-8-sig", locale.getpreferredencoding(False)]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return data.decode(encoding), encoding
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace"), "utf-8-replacement"

    def _execute_command(self, request_id: str) -> None:
        with self._condition:
            record = self._records.get(request_id)
            if record is None or record.state != "running":
                return
            if self._shutting_down:
                record.state = "cancelled"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": "The window closed before the approved process was created.",
                }
                self._condition.notify_all()
                return
            frozen = dict(record.frozen)

        try:
            current_size, current_sha256 = fingerprint_executable(frozen["executable"])
        except RelayError as exc:
            self._finish_execution(
                request_id,
                state="failed",
                message=f"Executable verification failed: {exc.message}",
            )
            return
        if (
            current_size != frozen["executable_size"]
            or current_sha256 != frozen["executable_sha256"]
        ):
            self._finish_execution(
                request_id,
                state="failed",
                message="The executable changed after display; the command was not started.",
                result={
                    "expected_executable_sha256": frozen["executable_sha256"],
                    "observed_executable_sha256": current_sha256,
                },
            )
            return

        try:
            script_mismatches = verify_script_dependencies(
                list(frozen.get("script_dependencies", []))
            )
        except RelayError as exc:
            self._finish_execution(
                request_id,
                state="failed",
                message=f"Script verification failed: {exc.message}",
                result={"verification_error": exc.as_dict()["error"]},
            )
            return
        if script_mismatches:
            self._finish_execution(
                request_id,
                state="failed",
                message="A script changed after display; the command was not started.",
                result={"script_mismatches": script_mismatches},
            )
            return

        with self._condition:
            record = self._records.get(request_id)
            if record is None or record.state != "running":
                return
            if self._shutting_down:
                record.state = "cancelled"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": "The window closed before the approved process was created.",
                }
                self._condition.notify_all()
                return
            command = [frozen["executable"], *frozen["arguments"]]
            try:
                process_job = WindowsJob.create()
            except RelayError as exc:
                record.state = "failed"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": f"Process containment failed: {exc.message}",
                    "error": exc.as_dict()["error"],
                }
                self._condition.notify_all()
                return
            creation_flags = 0
            if os.name == "nt":
                creation_flags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=frozen["working_directory"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    creationflags=creation_flags,
                )
            except (OSError, ValueError) as exc:
                process_job.close()
                record.state = "failed"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": f"Process creation failed: {type(exc).__name__}: {exc}",
                }
                self._condition.notify_all()
                return
            try:
                process_job.assign(process)
            except RelayError as exc:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                process_job.close()
                record.state = "failed"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": f"Process containment failed: {exc.message}",
                    "error": exc.as_dict()["error"],
                }
                self._condition.notify_all()
                return
            if self._shutting_down:
                try:
                    process_job.terminate(1)
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
                process_job.close()
                record.state = "cancelled"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": "The window closed as the approved process was created.",
                }
                self._condition.notify_all()
                return
            record.process = process
            record.process_job = process_job
            record.updated_at = iso_timestamp()
            self._condition.notify_all()

        stdout_bytes = bytearray()
        stderr_bytes = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=self._drain_stream,
            args=(process.stdout, stdout_bytes, stdout_truncated),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain_stream,
            args=(process.stderr, stderr_bytes, stderr_truncated),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            return_code = process.wait(timeout=frozen["timeout_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if process_job.active:
                    process_job.terminate(124)
                else:
                    process.terminate()
                return_code = process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    return_code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    return_code = None

        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if stdout_thread.is_alive():
            stdout_truncated[0] = True
        if stderr_thread.is_alive():
            stderr_truncated[0] = True
        stdout_text, stdout_encoding = self._decode_output(bytes(stdout_bytes))
        stderr_text, stderr_encoding = self._decode_output(bytes(stderr_bytes))
        result = {
            "exit_code": return_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated[0],
            "stderr_truncated": stderr_truncated[0],
            "stdout_encoding": stdout_encoding,
            "stderr_encoding": stderr_encoding,
            "shell": False,
        }
        if timed_out:
            self._finish_execution(
                request_id,
                state="timed_out",
                message="The approved command exceeded its execution timeout.",
                result=result,
            )
        elif return_code == 0:
            self._finish_execution(
                request_id,
                state="completed",
                message="The approved command completed.",
                result=result,
            )
        else:
            self._finish_execution(
                request_id,
                state="failed",
                message="The approved command returned a non-zero exit code.",
                result=result,
            )

    def rows_for_gui(self) -> list[dict[str, Any]]:
        with self._condition:
            self._expire_locked()
            return [
                {
                    **record.public_snapshot(),
                    "risk": record.frozen["risk"],
                    "source_label": record.frozen["source_label"],
                    "source_project": record.frozen["source_project"],
                    "source_task": record.frozen["source_task"],
                    "codex_thread_id": record.codex_thread_id,
                }
                for record in reversed(self._records.values())
            ]

    def detail_for_gui(self, request_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._get_locked(request_id)
            return {
                **record.public_snapshot(),
                "frozen": json.loads(json.dumps(record.frozen)),
            }

    def active_counts(self) -> tuple[int, int]:
        with self._condition:
            self._expire_locked()
            pending = sum(record.state == "pending" for record in self._records.values())
            running = sum(record.state == "running" for record in self._records.values())
            return pending, running

    def restart_is_pending(self) -> bool:
        with self._condition:
            return self._restart_pending

    def try_begin_codex_restart(self) -> tuple[bool, str]:
        """Atomically close the authorization intake gate before a safe restart."""

        with self._condition:
            if self._shutting_down:
                return False, "broker_stopping"
            if self._restart_pending:
                return False, "restart_already_pending"
            self._expire_locked()
            if any(record.state in ACTIVE_STATES for record in self._records.values()):
                return False, "relay_requests_active"
            self._restart_pending = True
            self._condition.notify_all()
            return True, "restart_gate_acquired"

    def end_codex_restart(self) -> None:
        with self._condition:
            self._restart_pending = False
            self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            self._shutting_down = True
            self._restart_pending = False
            processes: list[tuple[subprocess.Popen[bytes], WindowsJob | None]] = []
            for record in self._records.values():
                if record.state == "pending":
                    record.state = "cancelled"
                    record.updated_at = iso_timestamp()
                    record.terminal_monotonic = time.monotonic()
                    record.result = {
                        "decision": "cancelled",
                        "reason_code": "relay_window_closed",
                        "message": "The relay window closed before a decision.",
                    }
                elif record.state == "running" and record.process is not None:
                    processes.append((record.process, record.process_job))
            self._condition.notify_all()
        for process, process_job in processes:
            try:
                if process_job is not None and process_job.active:
                    process_job.terminate(1)
                else:
                    process.terminate()
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
            finally:
                if process_job is not None:
                    process_job.close()


class RelayRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(58)
        try:
            raw = self.rfile.readline(MAX_WIRE_BYTES + 1)
            if not raw or len(raw) > MAX_WIRE_BYTES:
                raise RelayError("invalid_wire", "Relay request is empty or too large.")
            try:
                request = json.loads(raw.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RelayError("invalid_wire", "Relay request is not valid ASCII JSON.") from exc
            if not isinstance(request, dict):
                raise RelayError("invalid_wire", "Relay request must be an object.")
            if request.get("protocol") != PROTOCOL_VERSION:
                raise RelayError("protocol_mismatch", "Relay protocol version mismatch.")
            token = request.get("token")
            server = self.server
            assert isinstance(server, RelayTCPServer)
            if not isinstance(token, str) or not secrets.compare_digest(token, server.token):
                raise RelayError("unauthorized", "Relay authentication failed.")
            if (
                request.get("client_version") != PACKAGE_VERSION
                or request.get("client_build_id") != RUNTIME_BUILD_ID
            ):
                raise RelayError(
                    "component_mismatch",
                    "The Golden Finger MCP connector and relay window are different builds.",
                    details={
                        "broker_version": BROKER_VERSION,
                        "broker_build_id": BROKER_BUILD_ID,
                    },
                )
            server.note_connector(
                client_version=str(request.get("client_version")),
                client_build_id=str(request.get("client_build_id")),
                client_instance_id=request.get("client_instance_id"),
                codex_thread_id=request.get("codex_thread_id"),
            )

            operation = request.get("operation")
            if operation == "health":
                if server.store.is_stopping():
                    raise RelayError("broker_stopping", "The relay window is stopping.")
                response = {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "broker_version": BROKER_VERSION,
                    "broker_build_id": BROKER_BUILD_ID,
                    "broker_elevated": server.store.elevated,
                    "poll_policy": server.store.current_poll_policy(),
                    "pid": os.getpid(),
                }
            elif operation == "submit":
                snapshot, replay, claim_token = server.store.submit(
                    request.get("kind"),
                    request.get("payload"),
                    request.get("client_instance_id"),
                    request.get("codex_thread_id"),
                )
                response = {
                    "ok": True,
                    "request": snapshot,
                    "idempotent_replay": replay,
                    "claim_token": claim_token,
                }
            elif operation == "status":
                response = {
                    "ok": True,
                    "request": server.store.status(
                        request.get("request_id"),
                        request.get("claim_token"),
                    ),
                }
            elif operation == "wait":
                response = {
                    "ok": True,
                    "request": server.store.wait(
                        request.get("request_id"),
                        request.get("claim_token"),
                        request.get("wait_seconds", 0),
                    ),
                }
            elif operation == "cancel":
                response = {
                    "ok": True,
                    "request": server.store.cancel(
                        request.get("request_id"),
                        request.get("claim_token"),
                    ),
                }
            else:
                # Deliberately no network/IPC approve operation.
                raise RelayError("unknown_operation", "Relay operation is unsupported.")
        except RelayError as exc:
            response = exc.as_dict()
        except Exception:
            response = RelayError(
                "internal_error",
                "The relay broker encountered an internal error.",
            ).as_dict()

        encoded = (json_ascii(response) + "\n").encode("ascii")
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def collect_install_diagnostics(
    server: "RelayTCPServer | None" = None,
    *,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Collect a sanitized, read-only installation snapshot for the diagnostics UI."""

    if codex_home is None:
        configured_home = os.environ.get("CODEX_HOME")
        codex_home = Path(configured_home) if configured_home else Path.home() / ".codex"
    resolved_codex_home = codex_home
    resolved_codex_home = resolved_codex_home.expanduser().resolve()
    config_path = resolved_codex_home / "config.toml"
    configured: bool | None = None
    config_error: str | None = None
    if config_path.is_file():
        try:
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
            plugins = config.get("plugins", {})
            plugin_entry = (
                plugins.get("golden-finger@personal", {})
                if isinstance(plugins, dict)
                else {}
            )
            if isinstance(plugin_entry, dict):
                value = plugin_entry.get("enabled")
                configured = value if isinstance(value, bool) else None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            config_error = f"{type(exc).__name__}: {exc}"

    cache_root = resolved_codex_home / "plugins" / "cache"
    cached: list[dict[str, Any]] = []
    if cache_root.is_dir():
        for plugin_root in sorted(cache_root.glob("*/golden-finger")):
            marketplace = plugin_root.parent.name
            for version_path in sorted(plugin_root.iterdir()):
                if not version_path.is_dir():
                    continue
                build_path = version_path / "BUILD-ID.txt"
                build_id: str | None = None
                if build_path.is_file():
                    try:
                        build_id = build_path.read_text(encoding="ascii").strip()
                    except OSError:
                        build_id = "(unreadable)"
                cached.append(
                    {
                        "marketplace": marketplace,
                        "version": version_path.name,
                        "build_id": build_id,
                        "path": str(version_path),
                    }
                )

    shortcut_roots = [Path.home() / "Desktop"]
    roaming = os.environ.get("APPDATA")
    if roaming:
        shortcut_roots.append(
            Path(roaming) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        )
    shortcuts: list[str] = []
    for root in shortcut_roots:
        if not str(root) or not root.is_dir():
            continue
        for candidate in root.glob("*Codex*金手指*.lnk"):
            shortcuts.append(str(candidate.resolve()))
        for candidate in root.glob("*Golden*Finger*.lnk"):
            resolved = str(candidate.resolve())
            if resolved not in shortcuts:
                shortcuts.append(resolved)

    runtime_mode = "self-contained-exe" if getattr(sys, "frozen", False) else "python-source"
    executable_path = Path(sys.executable).resolve()
    companion_path = executable_path.with_name("golden-finger-mcp.exe")
    last_connector = server.connector_snapshot() if server is not None else None
    exact_version_cached = any(
        item["version"] == BROKER_VERSION for item in cached
    )
    exact_build_cached = any(
        item.get("build_id") == BROKER_BUILD_ID for item in cached
    )
    return {
        "product": "Codex金手指",
        "protocol": PROTOCOL_VERSION,
        "broker_version": BROKER_VERSION,
        "broker_build_id": BROKER_BUILD_ID,
        "runtime_mode": runtime_mode,
        "runtime_executable": str(executable_path),
        "broker_elevated": is_elevated(),
        "job_object_available": os.name == "nt",
        "endpoint_path": str((endpoint_directory() / "endpoint.json").resolve()),
        "endpoint_present": (endpoint_directory() / "endpoint.json").is_file(),
        "companion_mcp_executable": (
            str(companion_path) if runtime_mode == "self-contained-exe" else None
        ),
        "companion_mcp_present": (
            companion_path.is_file() if runtime_mode == "self-contained-exe" else None
        ),
        "codex_home": str(resolved_codex_home),
        "plugin_config_path": str(config_path),
        "plugin_enabled": configured,
        "plugin_config_error": config_error,
        "cached_installs": cached,
        "exact_version_cached": exact_version_cached,
        "exact_build_cached": exact_build_cached,
        "shortcuts": shortcuts,
        "last_connector": last_connector,
        "component_match": (
            last_connector is not None
            and last_connector.get("connector_version") == BROKER_VERSION
            and last_connector.get("connector_build_id") == BROKER_BUILD_ID
        ),
        "security_note": (
            "No bearer token, claim token, request body, or raw connector instance ID "
            "is included in this diagnostic snapshot."
        ),
    }


def format_install_diagnostics(diagnostics: dict[str, Any]) -> str:
    """Render the diagnostics snapshot as copyable, human-readable text."""

    connector = diagnostics.get("last_connector")
    connector_lines = ["最近连接器: 尚未观察到"]
    if isinstance(connector, dict):
        connector_lines = [
            "最近连接器:",
            f"  版本/构建: {connector.get('connector_version')} / {connector.get('connector_build_id')}",
            f"  指纹: {connector.get('connector_fingerprint') or '(未提供)'}",
            f"  已观察任务线索: {connector.get('observed_thread_count', 0)}",
            f"  最后连接: {connector.get('seen_at')}",
        ]
    cached = diagnostics.get("cached_installs", [])
    cache_lines = ["缓存安装: 无"]
    if isinstance(cached, list) and cached:
        cache_lines = ["缓存安装:"] + [
            (
                f"  {item.get('marketplace')} / {item.get('version')}"
                f" / build={item.get('build_id') or '(未标记)'}"
            )
            for item in cached
            if isinstance(item, dict)
        ]
    shortcut_values = diagnostics.get("shortcuts", [])
    shortcut_lines = ["快捷方式: 未发现"]
    if isinstance(shortcut_values, list) and shortcut_values:
        shortcut_lines = ["快捷方式:"] + [f"  {value}" for value in shortcut_values]
    lines = [
        "Codex金手指 · 安装诊断（已脱敏）",
        "",
        f"Broker: {diagnostics.get('broker_version')} / {diagnostics.get('broker_build_id')}",
        f"协议: {diagnostics.get('protocol')}",
        f"运行模式: {diagnostics.get('runtime_mode')}",
        f"运行文件: {diagnostics.get('runtime_executable')}",
        f"管理员权限: {'是' if diagnostics.get('broker_elevated') else '否'}",
        f"Job Object: {'可用' if diagnostics.get('job_object_available') else '不可用'}",
        f"Endpoint: {diagnostics.get('endpoint_path')}",
        f"Codex 插件已启用: {diagnostics.get('plugin_enabled')}",
        f"当前版本缓存存在: {diagnostics.get('exact_version_cached')}",
        f"当前精确构建缓存存在: {diagnostics.get('exact_build_cached')}",
        f"连接器与窗口精确匹配: {diagnostics.get('component_match')}",
        *connector_lines,
        *cache_lines,
        *shortcut_lines,
    ]
    if diagnostics.get("plugin_config_error"):
        lines.append(f"配置读取错误: {diagnostics['plugin_config_error']}")
    lines.extend(["", str(diagnostics.get("security_note", ""))])
    return "\n".join(lines)


class RelayTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[RelayRequestHandler],
        *,
        token: str,
        store: RequestStore,
    ) -> None:
        self.token = token
        self.store = store
        self._connector_lock = threading.Lock()
        self._last_connector: dict[str, Any] | None = None
        self._connector_thread_ids: OrderedDict[str, None] = OrderedDict()
        super().__init__(address, handler)

    def note_connector(
        self,
        *,
        client_version: str,
        client_build_id: str,
        client_instance_id: Any,
        codex_thread_id: Any = None,
    ) -> None:
        fingerprint: str | None = None
        try:
            normalized = normalize_client_instance_id(client_instance_id)
        except RelayError:
            normalized = None
        if normalized is not None:
            fingerprint = canonical_hash(normalized)[:12]
        snapshot = {
            "seen_at": iso_timestamp(),
            "connector_version": client_version,
            "connector_build_id": client_build_id,
            "connector_fingerprint": fingerprint,
        }
        normalized_thread_id = normalize_codex_thread_id(codex_thread_id)
        with self._connector_lock:
            if normalized_thread_id is not None:
                self._connector_thread_ids.pop(normalized_thread_id, None)
                self._connector_thread_ids[normalized_thread_id] = None
                while len(self._connector_thread_ids) > 16:
                    self._connector_thread_ids.popitem(last=False)
            snapshot["observed_thread_count"] = len(self._connector_thread_ids)
            self._last_connector = snapshot

    def connector_snapshot(self) -> dict[str, Any] | None:
        with self._connector_lock:
            return (
                dict(self._last_connector)
                if self._last_connector is not None
                else None
            )

    def connector_thread_ids(self) -> tuple[str, ...]:
        with self._connector_lock:
            return tuple(reversed(self._connector_thread_ids))


class RelayWindow:
    def __init__(
        self,
        root: tk.Tk,
        *,
        store: RequestStore,
        server: RelayTCPServer,
        token: str,
        settings: dict[str, Any],
    ) -> None:
        self.root = root
        self.store = store
        self.server = server
        self.token = token
        self.closing = False
        self.current_request_id: str | None = None
        self.current_item_id: str | None = None
        self._detail_request_id: str | None = None
        self._detail_text: str | None = None
        self._detail_expanded = False
        self._policy_expanded = False
        self._task_filter = "active"
        self._codex_tasks: dict[str, CodexTask] = {}
        self._request_rows: dict[str, dict[str, Any]] = {}
        self._restart_in_progress = False
        self._restart_cancel = threading.Event()
        self._diagnostic_window: tk.Toplevel | None = None
        self._known_request_ids: set[str] = set()
        self._attention_active = False
        self._activation_job: str | None = None
        self._policy_update_job: str | None = None
        self._hit_shield_sync_job: str | None = None
        self._hit_shield_window: tk.Toplevel | None = None
        self._hit_shield_ready = False
        self._next_theme_probe = 0.0
        self._theme_preference = "system"
        self._effective_theme = "light"
        self._backdrop_enabled = False
        self.task_monitor = CodexTaskMonitor()
        self.palette = THEME_PALETTES["light"]
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._ui_settings = dict(settings)
        self.sound_enabled = tk.BooleanVar(
            master=root,
            value=self._ui_settings["sound_enabled"],
        )
        self.poll_wait_seconds = tk.StringVar(
            master=root,
            value=str(self._ui_settings["poll_wait_seconds"]),
        )
        self.max_pending_polls = tk.StringVar(
            master=root,
            value=str(self._ui_settings["max_pending_polls"]),
        )

        root.title("Codex金手指")
        root.geometry(f"{WINDOW_DEFAULT_WIDTH}x{WINDOW_DEFAULT_HEIGHT}")
        root.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1, minsize=WINDOW_MIN_CONTENT_HEIGHT)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.attributes("-alpha", WINDOW_CONTENT_OPACITY)
        except tk.TclError:
            pass

        self._build_ui()
        self._create_hit_shield()
        self._refresh_theme(force=True)
        self._apply_safe_minimum_size()
        self.task_monitor.start()
        self.refresh()

    def _create_hit_shield(self) -> None:
        """Create a nearly invisible window that catches color-key pass-through clicks."""

        if os.name != "nt":
            return
        shield: tk.Toplevel | None = None
        try:
            shield = tk.Toplevel(self.root)
            shield.withdraw()
            shield.overrideredirect(True)
            shield.configure(background=self.palette.window, takefocus=False)
            shield.attributes("-alpha", WINDOW_HIT_SHIELD_OPACITY)
            try:
                shield.attributes("-toolwindow", True)
            except tk.TclError:
                pass
            shield.bind("<ButtonPress>", self._on_hit_shield_pointer)
            self._hit_shield_window = shield
            self.root.bind(
                "<Configure>", self._schedule_hit_shield_sync, add="+"
            )
            self.root.bind("<Map>", self._schedule_hit_shield_sync, add="+")
            self.root.bind("<Unmap>", self._hide_hit_shield, add="+")
        except tk.TclError:
            if shield is not None:
                try:
                    shield.destroy()
                except tk.TclError:
                    pass
            self._hit_shield_window = None
            self._hit_shield_ready = False

    def _schedule_hit_shield_sync(self, _event: tk.Event[Any] | None = None) -> None:
        if self.closing or self._hit_shield_window is None:
            return
        if self._hit_shield_sync_job is not None:
            try:
                self.root.after_cancel(self._hit_shield_sync_job)
            except tk.TclError:
                pass
        try:
            self._hit_shield_sync_job = self.root.after_idle(self._sync_hit_shield)
        except tk.TclError:
            self._hit_shield_sync_job = None

    def _hide_hit_shield(self, _event: tk.Event[Any] | None = None) -> None:
        shield = self._hit_shield_window
        self._hit_shield_ready = False
        if shield is None:
            return
        try:
            shield.withdraw()
        except tk.TclError:
            pass

    def _sync_hit_shield(self) -> bool:
        self._hit_shield_sync_job = None
        shield = self._hit_shield_window
        if self.closing or shield is None:
            return False
        try:
            if self.root.state() in {"iconic", "withdrawn"}:
                shield.withdraw()
                self._hit_shield_ready = False
                return False
            self.root.update_idletasks()
            width = max(1, self.root.winfo_width())
            height = max(1, self.root.winfo_height())
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            shield.configure(background=self.palette.window)
            shield.geometry(f"{width}x{height}+{x}+{y}")
            shield.attributes("-alpha", WINDOW_HIT_SHIELD_OPACITY)
            shield.attributes("-topmost", self._attention_active)
            shield.deiconify()
            shield.update_idletasks()
            ready = place_native_window_behind(self.root, shield)
            if not ready:
                shield.withdraw()
            self._hit_shield_ready = ready
            if not ready and self._backdrop_enabled:
                self._fall_back_to_opaque_client()
            return ready
        except tk.TclError:
            self._hit_shield_ready = False
            if self._backdrop_enabled:
                self._fall_back_to_opaque_client()
            return False

    def _on_hit_shield_pointer(self, _event: tk.Event[Any]) -> str:
        try:
            self.root.lift()
        except tk.TclError:
            return "break"
        activate_native_window(self.root)
        self._schedule_hit_shield_sync()
        return "break"

    def _build_ui(self) -> None:
        header = ttk.Frame(
            self.root,
            padding=(16, 12, 16, 10),
            style="HAR.TFrame",
        )
        self.header = header
        header.grid(row=0, column=0, sticky="ew")
        title_row = ttk.Frame(header, style="HAR.TFrame")
        title_row.pack(fill=tk.X)
        title_row.columnconfigure(0, weight=1)
        ttk.Label(title_row, text="Codex金手指", style="Title.HAR.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.connection_label = ttk.Label(
            title_row,
            text="● 本地中继已连接",
            style="Connection.HAR.TLabel",
        )
        self.connection_label.grid(row=0, column=1, sticky="w", padx=(12, 0))
        preferences = ttk.Frame(title_row, style="HAR.TFrame")
        preferences.grid(row=0, column=2, sticky="e")
        self.policy_toggle_button = ttk.Button(
            preferences,
            text="等待策略",
            command=self._toggle_policy,
            style="Quiet.HAR.TButton",
            takefocus=True,
        )
        self.policy_toggle_button.pack(side=tk.LEFT, padx=(0, 10))
        self.theme_label = ttk.Label(
            preferences,
            text="外观：系统",
            style="Meta.HAR.TLabel",
        )
        self.theme_label.pack(side=tk.LEFT, padx=(0, 12))
        self.sound_toggle = ttk.Checkbutton(
            preferences,
            text="提示音",
            variable=self.sound_enabled,
            command=self._on_sound_toggle,
            style="HAR.TCheckbutton",
            takefocus=True,
        )
        self.sound_toggle.pack(side=tk.LEFT)
        self.diagnostics_button = ttk.Button(
            preferences,
            text="诊断",
            command=self.show_diagnostics,
            style="Diag.HAR.TButton",
            takefocus=True,
        )
        self.diagnostics_button.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(
            header,
            text="人机授权中继 · 每次仅授权当前冻结请求",
            style="Subtitle.HAR.TLabel",
        ).pack(anchor=tk.W, pady=(1, 0))

        policy = ttk.Frame(
            header,
            padding=(12, 10),
            style="PolicyCard.HAR.TFrame",
        )
        self.policy_panel = policy
        policy.pack(fill=tk.X, pady=(9, 0))
        policy.columnconfigure(8, weight=1)
        ttk.Label(
            policy,
            text="等待策略",
            style="PolicyTitle.HAR.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 14))
        ttk.Label(
            policy,
            text="轮询",
            style="PolicyLabel.HAR.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(0, 6))
        validate_integer = (
            self.root.register(lambda value: not value or value.isdigit()),
            "%P",
        )
        self.poll_wait_spinbox = ttk.Spinbox(
            policy,
            from_=MIN_POLL_WAIT_SECONDS,
            to=MAX_POLL_WAIT_SECONDS,
            width=6,
            textvariable=self.poll_wait_seconds,
            style="HAR.TSpinbox",
            command=self._schedule_policy_update,
            validate="key",
            validatecommand=validate_integer,
        )
        self.poll_wait_spinbox.grid(row=0, column=2, sticky="w")
        ttk.Label(
            policy,
            text="秒 ×",
            style="PolicyUnit.HAR.TLabel",
        ).grid(row=0, column=3, sticky="w", padx=(6, 12))
        ttk.Label(
            policy,
            text="上限",
            style="PolicyLabel.HAR.TLabel",
        ).grid(row=0, column=4, sticky="w", padx=(0, 6))
        self.max_pending_polls_spinbox = ttk.Spinbox(
            policy,
            from_=MIN_PENDING_POLL_CYCLES,
            to=MAX_PENDING_POLL_CYCLES,
            width=6,
            textvariable=self.max_pending_polls,
            style="HAR.TSpinbox",
            command=self._schedule_policy_update,
            validate="key",
            validatecommand=validate_integer,
        )
        self.max_pending_polls_spinbox.grid(row=0, column=5, sticky="w")
        ttk.Label(
            policy,
            text="次 = 总上限",
            style="PolicyUnit.HAR.TLabel",
        ).grid(row=0, column=6, sticky="w", padx=(6, 10))
        self.policy_budget_value_label = ttk.Label(
            policy,
            text="",
            style="BudgetValue.HAR.TLabel",
        )
        self.policy_budget_value_label.grid(row=0, column=7, sticky="w")

        self.policy_status_label = ttk.Label(
            policy,
            text="",
            style="PolicyStatus.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self.policy_status_label.grid(
            row=1,
            column=1,
            columnspan=8,
            sticky="w",
            pady=(3, 0),
        )
        for spinbox in (
            self.poll_wait_spinbox,
            self.max_pending_polls_spinbox,
        ):
            spinbox.bind("<FocusOut>", self._schedule_policy_update)
        self.poll_wait_seconds.trace_add("write", self._schedule_policy_update)
        self.max_pending_polls.trace_add("write", self._schedule_policy_update)
        policy.pack_forget()

        main = ttk.Frame(
            self.root,
            padding=(16, 0, 16, 14),
            style="HAR.TFrame",
        )
        self.main = main
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=5, minsize=340)
        main.columnconfigure(1, weight=3, minsize=310)
        main.columnconfigure(2, weight=0, minsize=0)
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main, padding=(14, 13), style="Card.HAR.TFrame")
        self.task_panel = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        left_header = ttk.Frame(left, style="Card.HAR.TFrame")
        left_header.grid(row=0, column=0, sticky="ew")
        left_header.columnconfigure(0, weight=1)
        ttk.Label(
            left_header,
            text="进行中的任务",
            style="CardSection.HAR.TLabel",
        ).grid(row=0, column=0, sticky="w")
        filters = ttk.Frame(left_header, style="Card.HAR.TFrame")
        filters.grid(row=0, column=1, sticky="e")
        self.active_filter_button = ttk.Button(
            filters,
            text="进行中",
            command=lambda: self._set_task_filter("active"),
            style="FilterActive.HAR.TButton",
            takefocus=True,
        )
        self.active_filter_button.pack(side=tk.LEFT)
        self.all_filter_button = ttk.Button(
            filters,
            text="全部",
            command=lambda: self._set_task_filter("all"),
            style="Filter.HAR.TButton",
            takefocus=True,
        )
        self.all_filter_button.pack(side=tk.LEFT, padx=(4, 0))

        self.inventory_label = ttk.Label(
            left,
            text="等待 Codex 任务状态…",
            style="CardMeta.HAR.TLabel",
        )
        self.inventory_label.grid(row=1, column=0, sticky="ew", pady=(5, 8))

        tree_frame = ttk.Frame(left, style="Card.HAR.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("state", "source"),
            show="tree headings",
            selectmode="browse",
            height=8,
            style="HAR.Treeview",
        )
        self.tree.heading("#0", text="任务 / 请求")
        self.tree.heading("state", text="状态")
        self.tree.heading("source", text="来源")
        self.tree.column("#0", width=210, minwidth=130, stretch=True)
        self.tree.column("state", width=68, minwidth=60, stretch=False, anchor=tk.CENTER)
        self.tree.column("source", width=92, minwidth=72, stretch=False)
        scrollbar = ttk.Scrollbar(
            tree_frame,
            orient=tk.VERTICAL,
            command=self.tree.yview,
            style="HAR.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        restart_panel = ttk.Frame(
            left,
            padding=(11, 10),
            style="Inset.HAR.TFrame",
        )
        restart_panel.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        restart_panel.columnconfigure(0, weight=1)
        ttk.Label(
            restart_panel,
            text="插件更新与安全重启",
            style="InsetTitle.HAR.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.restart_status_label = ttk.Label(
            restart_panel,
            text="正在核对任务状态",
            style="InsetMeta.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self.restart_status_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.restart_button = ttk.Button(
            restart_panel,
            text="安全重启 Codex",
            command=self.request_safe_restart,
            style="Restart.HAR.TButton",
            state=tk.DISABLED,
            takefocus=True,
        )
        self.restart_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 0))
        self.status_label = ttk.Label(
            left,
            text="等待请求…",
            style="CardMeta.HAR.TLabel",
        )
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        center = ttk.Frame(main, padding=(16, 13), style="Card.HAR.TFrame")
        self.decision_panel = center
        center.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(6, weight=1)
        ttk.Label(
            center,
            text="授权确认",
            style="CardSection.HAR.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.source_label = ttk.Label(
            center,
            text="尚未选择授权请求",
            style="CardMeta.HAR.TLabel",
        )
        self.source_label.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        self.action_label = ttk.Label(
            center,
            text="等待一个明确、单次、可核对的动作",
            style="DecisionTitle.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.action_label.grid(row=2, column=0, sticky="ew", pady=(5, 0))
        self.target_label = ttk.Label(
            center,
            text="",
            style="DecisionTarget.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.target_label.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        self.risk_label = ttk.Label(
            center,
            text="",
            style="Risk.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.risk_label.grid(row=4, column=0, sticky="ew", pady=(7, 0))
        self.detail_toggle_button = ttk.Button(
            center,
            text="详细参数 ›",
            command=self.toggle_detail,
            style="Detail.HAR.TButton",
            state=tk.DISABLED,
            takefocus=True,
        )
        self.detail_toggle_button.grid(row=5, column=0, sticky="w", pady=(9, 0))

        self.decision_stage = ttk.Frame(center, style="Card.HAR.TFrame", height=250)
        self.decision_stage.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        self.decision_stage.grid_propagate(False)
        self.decision_stage.bind("<Configure>", self._position_decision_buttons)
        self.approve_button = CircularButton(
            self.decision_stage,
            text="同意\n本次",
            command=self.approve_selected,
            variant="primary",
            diameter=170,
        )
        self.approve_button.set_state(tk.DISABLED)
        self.reject_button = CircularButton(
            self.decision_stage,
            text="拒",
            command=self.reject_selected,
            variant="danger",
            diameter=52,
        )
        self.reject_button.set_state(tk.DISABLED)
        self.approve_button.place(x=70, y=38)
        self.reject_button.place(x=205, y=35)
        self.decision_state_label = ttk.Label(
            center,
            text="未选择待确认请求",
            style="DecisionState.HAR.TLabel",
            anchor=tk.CENTER,
        )
        self.decision_state_label.grid(row=7, column=0, sticky="ew", pady=(3, 0))
        ttk.Label(
            center,
            text="仅授权当前冻结请求",
            style="Guard.HAR.TLabel",
            anchor=tk.CENTER,
        ).grid(row=8, column=0, sticky="ew", pady=(3, 0))

        detail_panel = ttk.Frame(main, padding=(14, 13), style="Card.HAR.TFrame")
        self.detail_panel = detail_panel
        detail_panel.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        detail_panel.columnconfigure(0, weight=1)
        detail_panel.rowconfigure(1, weight=1)
        detail_header = ttk.Frame(detail_panel, style="Card.HAR.TFrame")
        detail_header.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        detail_header.columnconfigure(0, weight=1)
        ttk.Label(
            detail_header,
            text="详细参数",
            style="CardSection.HAR.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            detail_header,
            text="收起 ×",
            command=self.toggle_detail,
            style="Quiet.HAR.TButton",
            takefocus=True,
        ).grid(row=0, column=1, sticky="e")
        detail_frame = ttk.Frame(detail_panel, style="Card.HAR.TFrame")
        detail_frame.grid(row=1, column=0, sticky="nsew")
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)
        self.detail = tk.Text(
            detail_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            width=48,
            height=12,
            font=("Cascadia Mono", 10),
            padx=12,
            pady=12,
            borderwidth=0,
            highlightthickness=1,
            relief=tk.FLAT,
        )
        detail_scroll = ttk.Scrollbar(
            detail_frame,
            orient=tk.VERTICAL,
            command=self.detail.yview,
            style="HAR.Vertical.TScrollbar",
        )
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail.bind("<MouseWheel>", self._on_detail_mousewheel)
        detail_panel.grid_remove()
        self._update_policy_preview()
        self._update_policy_toggle_summary()

    def _apply_safe_minimum_size(self) -> None:
        """Keep the compact decision lane usable across supported window sizes."""

        self.root.update_idletasks()
        minimum_width = max(
            WINDOW_MIN_WIDTH,
            self.header.winfo_reqwidth(),
        )
        minimum_height = max(
            WINDOW_MIN_HEIGHT,
            self.header.winfo_reqheight()
            + WINDOW_MIN_CONTENT_HEIGHT,
        )
        self.root.minsize(minimum_width, minimum_height)

    def _toggle_policy(self) -> None:
        self._policy_expanded = not self._policy_expanded
        if self._policy_expanded:
            self.policy_panel.pack(fill=tk.X, pady=(9, 0))
        else:
            self.policy_panel.pack_forget()
        self._update_policy_toggle_summary()

    def _update_policy_toggle_summary(self) -> None:
        wait_value = self.poll_wait_seconds.get().strip() or "—"
        poll_value = self.max_pending_polls.get().strip() or "—"
        marker = "▴" if self._policy_expanded else "▾"
        self.policy_toggle_button.configure(
            text=f"等待 {wait_value}秒 × {poll_value}次 {marker}"
        )

    def _set_task_filter(self, value: str) -> None:
        if value not in {"active", "all"} or value == self._task_filter:
            return
        self._task_filter = value
        self.active_filter_button.configure(
            style="FilterActive.HAR.TButton" if value == "active" else "Filter.HAR.TButton"
        )
        self.all_filter_button.configure(
            style="FilterActive.HAR.TButton" if value == "all" else "Filter.HAR.TButton"
        )
        self._refresh_task_tree()

    def _position_decision_buttons(self, _event: tk.Event[Any] | None = None) -> None:
        if self.closing:
            return
        width = max(1, self.decision_stage.winfo_width())
        height = max(1, self.decision_stage.winfo_height())
        approve_size = max(138, min(188, int(min(width * 0.58, height * 0.72))))
        reject_size = max(48, min(58, round(approve_size * 0.31)))
        approve_x = max(0, (width - approve_size) // 2)
        approve_y = max(4, (height - approve_size) // 2)
        reject_x = min(
            max(0, width - reject_size),
            approve_x + round(approve_size * 0.76),
        )
        reject_y = max(0, approve_y - round(reject_size * 0.18))
        self.approve_button.set_diameter(approve_size)
        self.reject_button.set_diameter(reject_size)
        self.approve_button.place(x=approve_x, y=approve_y)
        self.reject_button.place(x=reject_x, y=reject_y)
        wrap = max(220, width - 12)
        self.action_label.configure(wraplength=wrap)
        self.target_label.configure(wraplength=wrap)
        self.risk_label.configure(wraplength=wrap)

    def toggle_detail(self) -> None:
        if not self._detail_expanded and self.current_request_id is None:
            return
        self.root.update_idletasks()
        current_width = max(WINDOW_MIN_WIDTH, self.root.winfo_width())
        current_height = max(WINDOW_MIN_HEIGHT, self.root.winfo_height())
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        screen_width = max(WINDOW_MIN_WIDTH, self.root.winfo_screenwidth())
        available_width = max(WINDOW_MIN_WIDTH, screen_width - 32)
        if not self._detail_expanded:
            self._detail_expanded = True
            self.main.columnconfigure(2, weight=0, minsize=WINDOW_DETAIL_WIDTH)
            self.detail_panel.grid()
            target_width = min(
                available_width,
                max(WINDOW_EXPANDED_MIN_WIDTH, current_width + WINDOW_DETAIL_WIDTH),
            )
            expanded_minimum = min(WINDOW_EXPANDED_MIN_WIDTH, available_width)
            self.root.minsize(expanded_minimum, WINDOW_MIN_HEIGHT)
            if x + target_width > screen_width - 16:
                x = max(0, screen_width - target_width - 16)
            self.detail_toggle_button.configure(text="详细参数 ‹")
        else:
            self._detail_expanded = False
            self.detail_panel.grid_remove()
            self.main.columnconfigure(2, weight=0, minsize=0)
            self.root.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
            target_width = max(
                WINDOW_MIN_WIDTH,
                min(WINDOW_DEFAULT_WIDTH, current_width - WINDOW_DETAIL_WIDTH),
            )
            self.detail_toggle_button.configure(text="详细参数 ›")
        self.root.geometry(f"{target_width}x{current_height}+{x}+{y}")
        self._schedule_hit_shield_sync()

    def _configure_styles(
        self,
        palette: ThemePalette,
        *,
        window_background: str,
    ) -> None:
        style = self.style
        style.configure("HAR.TFrame", background=window_background)
        style.configure("Card.HAR.TFrame", background=palette.surface)
        style.configure("Inset.HAR.TFrame", background=palette.surface_alt)
        style.configure("PolicyCard.HAR.TFrame", background=palette.surface_alt)
        style.configure(
            "HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Title.HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Subtitle.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Meta.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Connection.HAR.TLabel",
            background=window_background,
            foreground=palette.success_text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "CardSection.HAR.TLabel",
            background=palette.surface,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "CardMeta.HAR.TLabel",
            background=palette.surface,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "DecisionTitle.HAR.TLabel",
            background=palette.surface,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        style.configure(
            "DecisionTarget.HAR.TLabel",
            background=palette.surface,
            foreground=palette.muted,
            font=("Cascadia Mono", 9),
        )
        style.configure(
            "Risk.HAR.TLabel",
            background=palette.surface,
            foreground="#8A4B08" if palette.name == "light" else "#F3C779",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "DecisionState.HAR.TLabel",
            background=palette.surface,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Guard.HAR.TLabel",
            background=palette.surface,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "InsetTitle.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "InsetMeta.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 8),
        )
        style.configure(
            "Section.HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "PolicyTitle.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "PolicyLabel.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyUnit.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyStatus.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicySuccess.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.success_text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyError.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.danger_text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "BudgetValue.HAR.TLabel",
            background=palette.surface_alt,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Status.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "HAR.TSpinbox",
            fieldbackground=palette.surface,
            foreground=palette.text,
            arrowcolor=palette.muted,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            insertcolor=palette.text,
            padding=(5, 4),
        )
        style.map(
            "HAR.TSpinbox",
            bordercolor=[("focus", palette.accent)],
            arrowcolor=[("active", palette.text)],
        )
        style.configure(
            "HAR.TCheckbutton",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 9),
            focuscolor=palette.accent,
            indicatorcolor=palette.surface_alt,
        )
        style.map(
            "HAR.TCheckbutton",
            background=[("active", window_background)],
            foreground=[("disabled", palette.disabled_text)],
            indicatorcolor=[
                ("selected", palette.accent),
                ("active", palette.secondary_hover),
            ],
        )
        style.configure(
            "Diag.HAR.TButton",
            background=palette.surface_alt,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            focuscolor=palette.accent,
            font=("Microsoft YaHei UI", 9),
            padding=(10, 5),
            relief=tk.FLAT,
        )
        for button_style, background, foreground in (
            ("Quiet.HAR.TButton", palette.surface_alt, palette.muted),
            ("Filter.HAR.TButton", palette.surface_alt, palette.muted),
            ("FilterActive.HAR.TButton", palette.selection, palette.text),
            ("Detail.HAR.TButton", palette.surface, palette.accent),
        ):
            style.configure(
                button_style,
                background=background,
                foreground=foreground,
                bordercolor=background,
                lightcolor=background,
                darkcolor=background,
                focuscolor=palette.accent,
                font=("Microsoft YaHei UI", 9),
                padding=(8, 4),
                relief=tk.FLAT,
            )
            style.map(
                button_style,
                background=[("pressed", palette.secondary_hover), ("active", palette.secondary_hover)],
                foreground=[("disabled", palette.disabled_text)],
            )
        style.configure(
            "Restart.HAR.TButton",
            background=palette.accent,
            foreground=palette.accent_text,
            bordercolor=palette.accent,
            lightcolor=palette.accent,
            darkcolor=palette.accent,
            focuscolor=palette.text,
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(10, 7),
            relief=tk.FLAT,
        )
        style.map(
            "Restart.HAR.TButton",
            background=[
                ("disabled", palette.disabled_background),
                ("pressed", palette.accent_pressed),
                ("active", palette.accent_hover),
            ],
            foreground=[("disabled", palette.disabled_text)],
        )
        style.map(
            "Diag.HAR.TButton",
            background=[
                ("pressed", palette.accent_pressed),
                ("active", palette.secondary_hover),
            ],
            foreground=[("disabled", palette.disabled_text)],
        )
        style.configure(
            "HAR.Treeview",
            background=palette.surface,
            fieldbackground=palette.surface,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            borderwidth=1,
            relief=tk.FLAT,
            rowheight=30,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "HAR.Treeview.Heading",
            background=palette.surface_alt,
            foreground=palette.text,
            bordercolor=palette.border,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(7, 7),
        )
        style.map(
            "HAR.Treeview",
            background=[("selected", palette.selection)],
            foreground=[("selected", palette.text)],
        )
        style.map(
            "HAR.Treeview.Heading",
            background=[("active", palette.secondary_hover)],
        )
        style.configure(
            "HAR.Vertical.TScrollbar",
            background=palette.surface_alt,
            troughcolor=window_background,
            bordercolor=window_background,
            arrowcolor=palette.muted,
            lightcolor=palette.surface_alt,
            darkcolor=palette.surface_alt,
        )
        style.map(
            "HAR.Vertical.TScrollbar",
            background=[("active", palette.secondary_hover)],
        )
        style.configure(
            "HAR.TPanedwindow",
            background=window_background,
            sashwidth=5,
        )

    def _theme_summary(self, *, backdrop_enabled: bool) -> str:
        preference_labels = {
            "light": "浅色",
            "dark": "深色",
            "system": "系统",
        }
        effective_label = "浅色" if self._effective_theme == "light" else "深色"
        preference_label = preference_labels.get(self._theme_preference, "系统")
        if self._theme_preference == "system":
            preference_label = f"系统 · {effective_label}"
        backdrop_label = (
            f"背景 {round(WINDOW_BACKGROUND_OPACITY * 100)}% / 文字 100%"
            if backdrop_enabled
            else "不透明背景"
        )
        return f"Codex {preference_label} · {backdrop_label}"

    def _fall_back_to_opaque_client(self) -> None:
        self._backdrop_enabled = False
        self._hide_hit_shield()
        try:
            self.root.attributes("-transparentcolor", "")
        except tk.TclError:
            pass
        self.root.configure(background=self.palette.window)
        self._configure_styles(self.palette, window_background=self.palette.window)
        self.approve_button.apply_theme(
            self.palette,
            parent_background=self.palette.surface,
        )
        self.reject_button.apply_theme(
            self.palette,
            parent_background=self.palette.surface,
        )
        self.theme_label.configure(text=self._theme_summary(backdrop_enabled=False))

    def _apply_theme(self) -> None:
        palette = self.palette
        native_backdrop_enabled = apply_native_window_appearance(
            self.root,
            dark=self._effective_theme == "dark",
            backdrop_color=palette.window,
            backdrop_opacity=WINDOW_BACKGROUND_OPACITY,
        )
        hit_shield_ready = (
            self._sync_hit_shield() if native_backdrop_enabled else False
        )
        backdrop_enabled = safe_transparent_client(
            backdrop_enabled=native_backdrop_enabled,
            hit_shield_ready=hit_shield_ready,
        )
        try:
            if backdrop_enabled:
                self.root.attributes("-transparentcolor", WINDOW_TRANSPARENT_COLOR)
            elif self._backdrop_enabled:
                self.root.attributes("-transparentcolor", "")
        except tk.TclError:
            backdrop_enabled = False
        if not backdrop_enabled:
            self._hide_hit_shield()
        self._backdrop_enabled = backdrop_enabled
        window_background = (
            WINDOW_TRANSPARENT_COLOR if self._backdrop_enabled else palette.window
        )
        self.root.configure(background=window_background)
        self._configure_styles(palette, window_background=window_background)
        self.detail.configure(
            background=palette.code_background,
            foreground=palette.text,
            insertbackground=palette.text,
            selectbackground=palette.selection,
            selectforeground=palette.text,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
        )
        self.approve_button.apply_theme(
            palette,
            parent_background=palette.surface,
        )
        self.reject_button.apply_theme(
            palette,
            parent_background=palette.surface,
        )
        self.tree.tag_configure("pending", foreground=palette.accent)
        self.tree.tag_configure("running", foreground=palette.accent)
        self.tree.tag_configure("failed", foreground=palette.danger_text)
        self.tree.tag_configure("timed_out", foreground=palette.danger_text)
        self.tree.tag_configure("active", foreground=palette.accent)
        self.tree.tag_configure("systemError", foreground=palette.danger_text)
        self.theme_label.configure(
            text=self._theme_summary(backdrop_enabled=self._backdrop_enabled)
        )

    def _refresh_theme(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_theme_probe:
            return
        self._next_theme_probe = now + THEME_REFRESH_SECONDS
        preference = read_codex_theme_preference()
        effective = resolve_theme(preference)
        if (
            force
            or preference != self._theme_preference
            or effective != self._effective_theme
        ):
            self._theme_preference = preference
            self._effective_theme = effective
            self.palette = THEME_PALETTES[effective]
            self._apply_theme()

    def _on_sound_toggle(self) -> None:
        self._ui_settings["sound_enabled"] = bool(self.sound_enabled.get())
        try:
            save_ui_settings(self._ui_settings)
        except OSError as exc:
            messagebox.showwarning(
                "无法保存提示音设置",
                f"本窗口已应用该选择，但无法保存到下次启动：{exc}",
                parent=self.root,
            )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds % 3_600 == 0:
            return f"{seconds // 3_600} 小时"
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        if seconds >= 60:
            minutes, remainder = divmod(seconds, 60)
            return f"{minutes} 分 {remainder} 秒"
        return f"{seconds} 秒"

    def _policy_form_settings(self) -> dict[str, Any]:
        try:
            poll_wait_seconds = int(self.poll_wait_seconds.get().strip())
            max_pending_polls = int(self.max_pending_polls.get().strip())
        except ValueError as exc:
            raise ValueError("两个设置都必须填写整数") from exc
        if not MIN_POLL_WAIT_SECONDS <= poll_wait_seconds <= MAX_POLL_WAIT_SECONDS:
            raise ValueError(
                f"单次轮询应为 {MIN_POLL_WAIT_SECONDS}–{MAX_POLL_WAIT_SECONDS} 秒"
            )
        if not MIN_PENDING_POLL_CYCLES <= max_pending_polls <= MAX_PENDING_POLL_CYCLES:
            raise ValueError(
                "等待确认轮询次数应为 "
                f"{MIN_PENDING_POLL_CYCLES}–{MAX_PENDING_POLL_CYCLES} 次"
            )
        pending_budget = poll_wait_seconds * max_pending_polls
        if not MIN_APPROVAL_EXPIRY_SECONDS <= pending_budget <= MAX_APPROVAL_EXPIRY_SECONDS:
            raise ValueError(
                "单次轮询 × 次数应介于 "
                f"{MIN_APPROVAL_EXPIRY_SECONDS}–{MAX_APPROVAL_EXPIRY_SECONDS} 秒"
            )
        return {
            "sound_enabled": bool(self.sound_enabled.get()),
            "poll_wait_seconds": poll_wait_seconds,
            "max_pending_polls": max_pending_polls,
        }

    def _schedule_policy_update(self, *_args: object) -> None:
        if self.closing:
            return
        self._update_policy_preview(pending=True)
        if self._policy_update_job is not None:
            try:
                self.root.after_cancel(self._policy_update_job)
            except tk.TclError:
                pass
        try:
            self._policy_update_job = self.root.after(
                POLICY_APPLY_DEBOUNCE_MS,
                self._apply_poll_policy,
            )
        except tk.TclError:
            self._policy_update_job = None

    def _update_policy_preview(self, *, pending: bool = False) -> None:
        self._update_policy_toggle_summary()
        try:
            policy = normalize_poll_policy(self._policy_form_settings())
        except ValueError as exc:
            self.policy_budget_value_label.configure(text="—")
            self.policy_status_label.configure(
                text=f"未应用 · {exc}",
                style="PolicyError.HAR.TLabel",
            )
            return
        budget = int(policy["pending_wait_budget_seconds"])
        formatted_budget = self._format_duration(budget)
        budget_label = (
            f"{budget} 秒"
            if formatted_budget == f"{budget} 秒"
            else f"{budget} 秒 · {formatted_budget}"
        )
        self.policy_budget_value_label.configure(text=budget_label)
        self.policy_status_label.configure(
            text=(
                "自动应用中… · 仅影响新请求"
                if pending
                else "已生效 · 仅影响新请求"
            ),
            style="PolicyStatus.HAR.TLabel",
        )

    def _apply_poll_policy(self) -> None:
        self._policy_update_job = None
        if self.closing:
            return
        try:
            settings = self._policy_form_settings()
            policy = self.store.update_poll_policy(settings)
        except (RelayError, ValueError) as exc:
            message = exc.message if isinstance(exc, RelayError) else str(exc)
            self.policy_status_label.configure(
                text=f"尚未应用：{message}",
                style="PolicyError.HAR.TLabel",
            )
            return
        settings["command_timeout_seconds"] = policy["command_timeout_seconds"]
        self._ui_settings.update(settings)
        try:
            save_ui_settings(self._ui_settings)
        except OSError as exc:
            self.policy_status_label.configure(
                text=f"本次运行已应用，但无法写入下次启动：{exc}",
                style="PolicyError.HAR.TLabel",
            )
            return
        self._update_policy_preview()
        self.policy_status_label.configure(
            text="已应用 · 仅影响新请求",
            style="PolicySuccess.HAR.TLabel",
        )

    def _play_notification_sound(self) -> None:
        if not self.sound_enabled.get():
            return
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, OSError, RuntimeError):
            try:
                self.root.bell()
            except tk.TclError:
                pass

    def _set_pending_attention(self, active: bool) -> None:
        if active == self._attention_active:
            return
        self._attention_active = active
        try:
            self.root.attributes("-topmost", active)
        except tk.TclError:
            pass
        if self._hit_shield_window is not None:
            try:
                self._hit_shield_window.attributes("-topmost", active)
            except tk.TclError:
                pass
            self._schedule_hit_shield_sync()

    def _notify_new_request(self) -> None:
        try:
            if self.root.state() in {"iconic", "withdrawn"}:
                self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass
        self._set_pending_attention(True)
        if self._activation_job is not None:
            try:
                self.root.after_cancel(self._activation_job)
            except tk.TclError:
                pass
        self._activation_job = self.root.after(50, self._activate_new_request_window)
        self._play_notification_sound()

    def _activate_new_request_window(self) -> None:
        self._activation_job = None
        if self.closing:
            return
        if activate_native_window(self.root):
            try:
                # Put keyboard focus on a non-actionable control. This activates the
                # top-level without making Enter or Space approve a request.
                self.tree.focus_set()
            except tk.TclError:
                pass

    @staticmethod
    def _state_label(state: str) -> str:
        labels = {
            "pending": "待确认",
            "running": "执行中",
            "approved": "已同意",
            "rejected": "已拒绝",
            "cancelled": "已取消",
            "expired": "已过期",
            "completed": "已完成",
            "failed": "失败",
            "timed_out": "超时",
        }
        return labels.get(state, state)

    @staticmethod
    def _kind_label(kind: str) -> str:
        return "命令" if kind == "command" else "文本"

    @staticmethod
    def _one_line(value: object, *, fallback: str = "—", limit: int = 160) -> str:
        if not isinstance(value, str):
            return fallback
        cleaned = " ".join(value.replace("\x00", " ").split())
        if not cleaned:
            return fallback
        return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"

    @staticmethod
    def _codex_state_label(state: str) -> str:
        return {
            "active": "进行中",
            "idle": "空闲",
            "notLoaded": "未载入",
            "systemError": "异常",
        }.get(state, state)

    @staticmethod
    def _request_item_id(request_id: str) -> str:
        return "request:" + request_id

    @staticmethod
    def _codex_item_id(task_id: str) -> str:
        return "codex:" + task_id

    @staticmethod
    def _compact_source(value: str) -> str:
        normalized = value.replace("/", "\\").rstrip("\\")
        return normalized.rsplit("\\", 1)[-1] if normalized else "Codex"

    def _refresh_task_tree(self) -> None:
        selected_before = self.current_item_id
        entries: list[tuple[str, str, tuple[str, str], str]] = []
        for row in self._request_rows.values():
            if self._task_filter == "active" and row["state"] not in ACTIVE_STATES:
                continue
            entries.append(
                (
                    self._request_item_id(str(row["request_id"])),
                    self._one_line(row.get("exact_action"), fallback="授权请求", limit=96),
                    (
                        self._state_label(str(row["state"])),
                        self._one_line(
                            row.get("source_project") or row.get("source_label"),
                            fallback="金手指",
                            limit=32,
                        ),
                    ),
                    str(row["state"]),
                )
            )
        for task in self._codex_tasks.values():
            if self._task_filter == "active" and not task.active:
                continue
            entries.append(
                (
                    self._codex_item_id(task.task_id),
                    self._one_line(task.title, fallback="未命名 Codex 任务", limit=96),
                    (
                        self._codex_state_label(task.status),
                        self._one_line(
                            self._compact_source(task.cwd),
                            fallback="Codex",
                            limit=32,
                        ),
                    ),
                    task.status,
                )
            )

        existing = set(self.tree.get_children())
        incoming = {entry[0] for entry in entries}
        for stale in existing - incoming:
            self.tree.delete(stale)
        for index, (item_id, text, values, tag) in enumerate(entries):
            if self.tree.exists(item_id):
                self.tree.item(item_id, text=text, values=values, tags=(tag,))
            else:
                self.tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    text=text,
                    values=values,
                    tags=(tag,),
                )
            self.tree.move(item_id, "", index)

        if selected_before and self.tree.exists(selected_before):
            self.tree.selection_set(selected_before)
            self.tree.focus(selected_before)

    def _select_item(self, item_id: str) -> None:
        if not self.tree.exists(item_id):
            return
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.see(item_id)
        self.current_item_id = item_id
        if item_id.startswith("request:"):
            request_id = item_id.removeprefix("request:")
            self.current_request_id = request_id
            self._render_detail(request_id)
        elif item_id.startswith("codex:"):
            self.current_request_id = None
            self._render_codex_task(item_id.removeprefix("codex:"))

    def _render_empty_state(self) -> None:
        self.current_request_id = None
        self.source_label.configure(text="尚未选择授权请求")
        self.action_label.configure(text="等待一个明确、单次、可核对的动作")
        self.target_label.configure(text="")
        self.risk_label.configure(text="")
        self.decision_state_label.configure(text="未选择待确认请求")
        self.detail_toggle_button.configure(state=tk.DISABLED, text="详细参数 ›")
        self.approve_button.set_state(tk.DISABLED)
        self.reject_button.set_state(tk.DISABLED)
        self._detail_request_id = None
        self._set_detail(
            "等待 Codex 提交一个明确、单次、可核对的授权请求。",
            reset_scroll=True,
        )

    def _render_codex_task(self, task_id: str) -> None:
        task = self._codex_tasks.get(task_id)
        if task is None:
            self._render_empty_state()
            return
        self.source_label.configure(text="Codex 任务")
        self.action_label.configure(text=task.title)
        self.target_label.configure(text=self._compact_source(task.cwd))
        self.risk_label.configure(text="任务状态仅用于安全重启判断，不会触发任何操作。")
        self.decision_state_label.configure(
            text=f"状态 · {self._codex_state_label(task.status)}"
        )
        self.detail_toggle_button.configure(state=tk.DISABLED, text="详细参数 ›")
        self.approve_button.set_state(tk.DISABLED)
        self.reject_button.set_state(tk.DISABLED)
        self._set_detail(
            "\n".join(
                (
                    f"任务: {task.title}",
                    f"状态: {self._codex_state_label(task.status)}",
                    f"项目: {task.cwd}",
                    f"类型: {task.kind}",
                    "",
                    "该信息来自正在运行的 Codex 本地只读任务清单，仅用于显示和安全重启门禁。",
                )
            ),
            reset_scroll=True,
        )
        self._detail_request_id = None

    def _update_inventory_and_restart(
        self,
        inventory: CodexTaskInventory,
        *,
        pending_count: int,
        running_count: int,
    ) -> None:
        relay_active = pending_count + running_count
        active_codex = len(inventory.active_tasks) if inventory.available else 0
        error_codex = (
            sum(task.status == "systemError" for task in inventory.tasks)
            if inventory.available
            else 0
        )
        if inventory.available:
            self.inventory_label.configure(
                text=f"Codex 进行中 {active_codex} · 授权待确认 {pending_count} · 执行中 {running_count}"
            )
        elif inventory.error_code == "awaiting_thread_metadata":
            self.inventory_label.configure(text="等待任一 Codex 任务连接金手指…")
        else:
            self.inventory_label.configure(text="Codex 任务状态暂不可用 · 已禁止重启")

        enabled = False
        if self._restart_in_progress or self.store.restart_is_pending():
            restart_text = "正在执行安全重启…"
        elif not inventory.available:
            restart_text = "任务清单不可用，保持禁用"
        elif not inventory.is_fresh():
            restart_text = "任务状态已过期，保持禁用"
        elif error_codex:
            restart_text = "任务清单含异常状态，保持禁用"
        elif active_codex:
            restart_text = f"仍有 {active_codex} 个 Codex 任务进行中"
        elif relay_active:
            restart_text = f"仍有 {relay_active} 个授权请求未结束"
        else:
            restart_text = "当前无进行中任务，可安全重启"
            enabled = True
        self.restart_status_label.configure(text=restart_text)
        self.restart_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def refresh(self) -> None:
        if self.closing:
            return
        self._refresh_theme()
        self.task_monitor.register_thread_ids(self.server.connector_thread_ids())
        inventory = self.task_monitor.snapshot()
        rows = self.store.rows_for_gui()
        incoming_request_ids = {str(row["request_id"]) for row in rows}
        new_request_ids = incoming_request_ids - self._known_request_ids
        self._known_request_ids = incoming_request_ids
        self._request_rows = {str(row["request_id"]): row for row in rows}
        self._codex_tasks = {task.task_id: task for task in inventory.tasks}
        self._refresh_task_tree()

        newest_request = next(
            (
                row
                for row in rows
                if row["request_id"] in new_request_ids and row["state"] == "pending"
            ),
            None,
        )
        if newest_request is not None:
            request_id = str(newest_request["request_id"])
            if self._task_filter != "active":
                self._set_task_filter("active")
            self._select_item(self._request_item_id(request_id))
            self._notify_new_request()
        elif (
            self.current_request_id
            and self.current_request_id in self._request_rows
            and self.tree.exists(self._request_item_id(self.current_request_id))
        ):
            self._render_detail(self.current_request_id)
        elif self.current_item_id and self.tree.exists(self.current_item_id):
            if self.current_item_id.startswith("codex:"):
                self._render_codex_task(self.current_item_id.removeprefix("codex:"))
        else:
            children = self.tree.get_children()
            pending = next((row for row in rows if row["state"] == "pending"), None)
            if pending is not None:
                self._select_item(self._request_item_id(str(pending["request_id"])))
            elif children:
                self._select_item(str(children[0]))
            else:
                self.current_item_id = None
                self._render_empty_state()

        pending_count, running_count = self.store.active_counts()
        self._set_pending_attention(pending_count > 0)
        self._update_inventory_and_restart(
            inventory,
            pending_count=pending_count,
            running_count=running_count,
        )
        self.status_label.configure(
            text=f"待确认 {pending_count} · 执行中 {running_count} · 本地端口 {self.server.server_address[1]}"
        )
        self.root.after(250, self.refresh)

    def on_select(self, _event: tk.Event[Any]) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._select_item(str(selected[0]))

    def _on_detail_mousewheel(self, event: tk.Event[Any]) -> str | None:
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return None
        units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        self.detail.yview_scroll(units, tk.UNITS)
        return "break"

    def _set_detail(self, text: str, *, reset_scroll: bool = False) -> None:
        if text == self._detail_text and not reset_scroll:
            return
        previous_fraction = self.detail.yview()[0] if self._detail_text is not None else 0.0
        self.detail.configure(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert("1.0", text)
        self.detail.configure(state=tk.DISABLED)
        self.detail.yview_moveto(0.0 if reset_scroll else previous_fraction)
        self._detail_text = text

    def _render_detail(self, request_id: str) -> None:
        try:
            detail = self.store.detail_for_gui(request_id)
        except RelayError:
            return
        frozen = detail["frozen"]
        poll_policy = detail["poll_policy"]
        source_project = self._one_line(
            frozen.get("source_project"), fallback="未标注项目", limit=46
        )
        source_task = self._one_line(
            frozen.get("source_task"), fallback="未标注任务", limit=46
        )
        self.source_label.configure(text=f"{source_project} · {source_task}")
        self.action_label.configure(
            text=self._one_line(frozen.get("exact_action"), fallback="授权请求", limit=140)
        )
        self.target_label.configure(
            text="目标 · "
            + self._one_line(frozen.get("target"), fallback="未标注", limit=120)
        )
        self.risk_label.configure(
            text="风险 · "
            + self._one_line(frozen.get("risk"), fallback="未标注", limit=110)
        )
        parameter_count = 6
        if frozen.get("kind") == "command":
            arguments = frozen.get("arguments")
            parameter_count = 8 + (len(arguments) if isinstance(arguments, list) else 0)
        marker = "‹" if self._detail_expanded else "›"
        self.detail_toggle_button.configure(
            state=tk.NORMAL,
            text=f"详细参数 {parameter_count}项 {marker}",
        )
        reset_scroll = request_id != self._detail_request_id
        permission_line = execution_mode_label(
            str(frozen["kind"]), bool(detail["broker_elevated"])
        )
        lines = [
            f"状态: {self._state_label(detail['state'])}",
            f"请求 ID: {detail['request_id']}",
            f"认领指纹: {detail['claim_fingerprint']}",
            f"请求哈希: {detail['request_hash']}",
            f"来源标签: {frozen['source_label']}",
            f"来源项目: {frozen['source_project']}",
            f"来源任务: {frozen['source_task']}",
            f"连接器指纹: {frozen['connector_fingerprint']}",
            f"创建时间: {detail['created_at']}",
            f"到期时间: {detail['expires_at']}",
            f"授权有效期: {frozen['expires_in_seconds']} 秒",
            f"单次轮询等待: {poll_policy['wait_seconds']} 秒",
            (
                "等待确认轮询: "
                f"已用 {poll_policy['pending_polls_used']} / "
                f"上限 {poll_policy['max_pending_polls']} 次"
            ),
            (
                "等待确认预算: "
                f"{poll_policy['pending_wait_budget_seconds']} 秒"
            ),
            f"策略版本: {poll_policy['policy_revision']}",
            permission_line,
            "",
            "唯一动作:",
            str(frozen["exact_action"]),
            "",
            "精确目标:",
            str(frozen["target"]),
            "",
            "风险与中断影响:",
            str(frozen["risk"]),
            "",
            "恢复路径:",
            str(frozen["recovery"]),
        ]
        if frozen["kind"] == "consent":
            lines.extend(
                [
                    "",
                    "点击后返回的精确文本:",
                    str(frozen["approved_reply"]),
                ]
            )
        else:
            command = [frozen["executable"], *frozen["arguments"]]
            lines.extend(
                [
                    "",
                    "可执行文件:",
                    str(frozen["executable"]),
                    f"大小: {frozen['executable_size']} bytes",
                    f"SHA-256: {frozen['executable_sha256']}",
                    "",
                    "参数数组:",
                ]
            )
            if frozen["arguments"]:
                lines.extend(
                    f"  [{index}] {argument!r}"
                    for index, argument in enumerate(frozen["arguments"])
                )
            else:
                lines.append("  (空)")
            dependencies = list(frozen.get("script_dependencies", []))
            lines.extend(["", "冻结的直接脚本依赖:"])
            if dependencies:
                for dependency in dependencies:
                    lines.extend(
                        [
                            f"  参数 [{dependency['argument_index']}] {dependency['path']}",
                            f"    大小: {dependency['size']} bytes",
                            f"    SHA-256: {dependency['sha256']}",
                        ]
                    )
            else:
                lines.append("  (无直接脚本文件参数；内联命令和脚本内部加载项不在此列表)")
            lines.extend(
                [
                    "",
                    "Windows 命令行预览（仅展示，实际使用 shell=False）:",
                    subprocess.list2cmdline(command),
                    "",
                    "工作目录:",
                    str(frozen["working_directory"]),
                    "",
                    f"执行超时: {frozen['timeout_seconds']} 秒",
                    "环境: 继承此窗口进程；stdin 关闭；stdout/stderr 各最多保留 256 KiB。",
                ]
            )
        if detail.get("receipt"):
            lines.extend(["", "一次性决策回执:", json.dumps(detail["receipt"], ensure_ascii=False, indent=2)])
        if detail.get("result"):
            lines.extend(["", "结果:", json.dumps(detail["result"], ensure_ascii=False, indent=2)])
        self._set_detail("\n".join(lines), reset_scroll=reset_scroll)
        self._detail_request_id = request_id

        pending = detail["state"] == "pending"
        self.approve_button.set_state(tk.NORMAL if pending else tk.DISABLED)
        self.reject_button.set_state(tk.NORMAL if pending else tk.DISABLED)
        state_text = self._state_label(str(detail["state"]))
        if pending:
            try:
                expiry = datetime.fromisoformat(
                    str(detail["expires_at"]).replace("Z", "+00:00")
                )
                remaining = max(0, int((expiry - utc_now()).total_seconds()))
                minutes, seconds = divmod(remaining, 60)
                state_text = (
                    f"待确认 · 剩余 {minutes:02d}:{seconds:02d} · "
                    f"轮询 {poll_policy['pending_polls_used']}/{poll_policy['max_pending_polls']}"
                )
            except (TypeError, ValueError):
                state_text = "待确认"
        elif detail["state"] == "running":
            state_text = "命令执行中 · 请勿关闭窗口"
        self.decision_state_label.configure(text=state_text)

    def approve_selected(self) -> None:
        request_id = self.current_request_id
        if not request_id:
            return
        try:
            self.store.approve_from_gui(request_id)
        except RelayError as exc:
            messagebox.showerror("无法批准", exc.message, parent=self.root)
        self._render_detail(request_id)
        self.tree.focus_set()

    def reject_selected(self) -> None:
        request_id = self.current_request_id
        if not request_id:
            return
        try:
            self.store.reject_from_gui(request_id)
        except RelayError as exc:
            messagebox.showerror("无法拒绝", exc.message, parent=self.root)
        self._render_detail(request_id)
        self.tree.focus_set()

    def request_safe_restart(self) -> None:
        if self._restart_in_progress or self.closing:
            return
        acquired, reason = self.store.try_begin_codex_restart()
        if not acquired:
            messagebox.showwarning(
                "暂不能重启 Codex",
                {
                    "relay_requests_active": "仍有授权请求未结束。",
                    "restart_already_pending": "安全重启已经在进行中。",
                    "broker_stopping": "金手指窗口正在关闭。",
                }.get(reason, "当前状态不允许安全重启。"),
                parent=self.root,
            )
            return

        self._restart_in_progress = True
        self._restart_cancel.clear()
        self.restart_button.configure(state=tk.DISABLED)
        self.restart_status_label.configure(text="正在重新核对全部任务…")

        def worker() -> None:
            inventory = self.task_monitor.probe_now()
            if self._restart_cancel.is_set():
                result = RestartResult(
                    False,
                    "restart_cancelled",
                    "金手指窗口正在关闭；已取消尚未开始的 Codex 重启。",
                )
            elif not inventory.is_fresh():
                result = RestartResult(
                    False,
                    inventory.error_code or "inventory_unavailable",
                    "无法取得完整且最新的 Codex 任务清单；已取消重启。",
                )
            elif any(task.status == "systemError" for task in inventory.tasks):
                result = RestartResult(
                    False,
                    "inventory_contains_errors",
                    "Codex 任务清单含异常状态；已取消重启。",
                )
            elif inventory.active_tasks:
                result = RestartResult(
                    False,
                    "codex_tasks_active",
                    f"仍有 {len(inventory.active_tasks)} 个 Codex 任务进行中；已取消重启。",
                )
            else:
                pending, running = self.store.active_counts()
                if pending or running:
                    result = RestartResult(
                        False,
                        "relay_requests_active",
                        "核对期间出现了未结束的授权请求；已取消重启。",
                    )
                else:
                    result = (
                        RestartResult(
                            False,
                            "restart_cancelled",
                            "金手指窗口正在关闭；已取消尚未开始的 Codex 重启。",
                        )
                        if self._restart_cancel.is_set()
                        else graceful_restart_codex()
                    )
            try:
                self.root.after(0, lambda: self._finish_safe_restart(result))
            except tk.TclError:
                self.store.end_codex_restart()

        threading.Thread(
            target=worker,
            name="golden-finger-safe-codex-restart",
            daemon=True,
        ).start()

    def _finish_safe_restart(self, result: RestartResult) -> None:
        self.store.end_codex_restart()
        self._restart_in_progress = False
        if self.closing:
            return
        if result.ok:
            self.restart_status_label.configure(text="Codex 已重新打开")
            messagebox.showinfo("安全重启完成", result.message, parent=self.root)
        else:
            self.restart_status_label.configure(text=result.message)
            messagebox.showwarning("未执行重启", result.message, parent=self.root)

    def show_diagnostics(self) -> None:
        existing = self._diagnostic_window
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except tk.TclError:
                pass
        rendered = format_install_diagnostics(
            collect_install_diagnostics(self.server)
        )
        window = tk.Toplevel(self.root)
        self._diagnostic_window = window
        window.title("Codex金手指 · 安装诊断")
        window.geometry("760x560")
        window.minsize(620, 420)
        window.transient(self.root)
        window.configure(background=self.palette.window)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        content = ttk.Frame(window, padding=(14, 14, 14, 8), style="HAR.TFrame")
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        text_widget = tk.Text(
            content,
            wrap=tk.WORD,
            font=("Cascadia Mono", 10),
            padx=12,
            pady=12,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.palette.border,
            highlightcolor=self.palette.accent,
            background=self.palette.code_background,
            foreground=self.palette.text,
            insertbackground=self.palette.text,
        )
        scrollbar = ttk.Scrollbar(
            content,
            orient=tk.VERTICAL,
            command=text_widget.yview,
            style="HAR.Vertical.TScrollbar",
        )
        text_widget.configure(yscrollcommand=scrollbar.set)
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        text_widget.insert("1.0", rendered)
        text_widget.configure(state=tk.DISABLED)

        actions = ttk.Frame(window, padding=(14, 6, 14, 14), style="HAR.TFrame")
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        def copy_snapshot() -> None:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(rendered)
                self.root.update_idletasks()
            except tk.TclError:
                pass

        ttk.Button(
            actions,
            text="复制诊断",
            command=copy_snapshot,
            style="Diag.HAR.TButton",
        ).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(
            actions,
            text="关闭",
            command=window.destroy,
            style="Diag.HAR.TButton",
        ).grid(row=0, column=2)

        def forget_window(_event: tk.Event[Any] | None = None) -> None:
            if self._diagnostic_window is window:
                self._diagnostic_window = None

        window.bind("<Destroy>", forget_window, add="+")
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.lift()

    def on_close(self) -> None:
        pending, running = self.store.active_counts()
        if (pending or running) and not messagebox.askyesno(
            "关闭 Codex金手指",
            f"仍有 {pending} 个待确认、{running} 个执行中请求。\n"
            "关闭后，待确认请求会取消；执行中请求的 Windows Job Object 进程树会被终止。\n"
            "已发生的外部副作用无法撤销，Codex 端也必须把未完整返回的结果视为不确定。\n\n"
            "确定关闭吗？",
            parent=self.root,
        ):
            return
        self.closing = True
        self._restart_cancel.set()
        for job in (
            self._policy_update_job,
            self._hit_shield_sync_job,
            self._activation_job,
        ):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._hide_hit_shield()
        self.task_monitor.stop()
        self.store.shutdown()
        self.server.shutdown()
        self.server.server_close()
        remove_endpoint_if_owned(os.getpid(), self.token)
        self.root.destroy()


def write_broker_self_test(output_path: Path) -> dict[str, Any]:
    """Write a startup-free runtime probe for release and clean-machine checks."""

    job_active = False
    job_error: dict[str, Any] | None = None
    job: WindowsJob | None = None
    try:
        job = WindowsJob.create()
        job_active = job.active
    except RelayError as exc:
        job_error = exc.as_dict()["error"]
    finally:
        if job is not None:
            job.close()
    result = {
        "ok": job_error is None,
        "product": "Codex金手指",
        "protocol": PROTOCOL_VERSION,
        "broker_version": BROKER_VERSION,
        "broker_build_id": BROKER_BUILD_ID,
        "runtime_mode": (
            "self-contained-exe" if getattr(sys, "frozen", False) else "python-source"
        ),
        "job_object_active": job_active,
        "job_object_error": job_error,
    }
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex金手指 relay window")
    parser.add_argument(
        "--self-test-output",
        type=Path,
        help="Write a sanitized runtime self-test JSON file without opening the window.",
    )
    args = parser.parse_args(argv)
    if args.self_test_output is not None:
        result = write_broker_self_test(args.self_test_output)
        return 0 if result["ok"] else 4

    instance_lock = SingleInstanceLock()
    token = ""
    server: RelayTCPServer | None = None
    server_started = False
    store: RequestStore | None = None
    try:
        instance_lock.acquire()
        if broker_is_reachable():
            raise RelayError(
                "broker_already_running",
                "Another Codex金手指 window is already running.",
            )
        elevated = is_elevated()
        settings = load_ui_settings()
        store = RequestStore(elevated=elevated, settings=settings)
        token = new_bearer_token()
        server = RelayTCPServer(
            ("127.0.0.1", 0),
            RelayRequestHandler,
            token=token,
            store=store,
        )
        started_at = iso_timestamp()
        write_endpoint(
            {
                "protocol": PROTOCOL_VERSION,
                "host": "127.0.0.1",
                "port": int(server.server_address[1]),
                "token": token,
                "pid": os.getpid(),
                "started_at": started_at,
                "broker_version": BROKER_VERSION,
                "broker_build_id": BROKER_BUILD_ID,
                "broker_elevated": elevated,
            }
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="relay-loopback-server",
            daemon=True,
        )
        server_thread.start()
        server_started = True
        root = tk.Tk()
        RelayWindow(
            root,
            store=store,
            server=server,
            token=token,
            settings=settings,
        )
        root.mainloop()
        return 0
    except RelayError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except tk.TclError as exc:
        print(f"gui_unavailable: {exc}", file=sys.stderr)
        return 3
    finally:
        if store is not None:
            store.shutdown()
        if server is not None:
            try:
                if server_started:
                    server.shutdown()
                server.server_close()
            except OSError:
                pass
        if token:
            remove_endpoint_if_owned(os.getpid(), token)
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
