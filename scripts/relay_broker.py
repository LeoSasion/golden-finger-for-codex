"""Independent approval window for Codex金手指 (Golden Finger for Codex).

This process is intentionally launched by the user, not by Codex or the MCP
connector. Approval transitions are reachable only from GUI callbacks.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ctypes
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
    PROTOCOL_VERSION,
    RelayError,
    broker_is_reachable,
    canonical_hash,
    endpoint_directory,
    fingerprint_executable,
    json_ascii,
    new_bearer_token,
    normalize_request,
    remove_endpoint_if_owned,
    write_endpoint,
)


BROKER_VERSION = "0.1.9"
TERMINAL_RETENTION_SECONDS = 3_600
WINDOW_BACKGROUND_OPACITY = 0.60
WINDOW_CONTENT_OPACITY = 1.0
WINDOW_TRANSPARENT_COLOR = "#010203"
WINDOW_HIT_SHIELD_OPACITY = 0.01
WINDOW_DEFAULT_WIDTH = 1_120
WINDOW_DEFAULT_HEIGHT = 720
WINDOW_MIN_WIDTH = 920
WINDOW_MIN_HEIGHT = 600
WINDOW_MIN_CONTENT_HEIGHT = 260
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
        accent_text="#07131F",
        selection="#D9ECFF",
        code_background="#F4F5F6",
        success_text="#205C2F",
        danger_text="#8F1D1D",
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
        accent_text="#07131F",
        selection="#163B59",
        code_background="#111111",
        success_text="#A8E4B8",
        danger_text="#FFB8B8",
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


class RoundedButton(tk.Canvas):
    """Keyboard-accessible, themeable rounded button for the two decisions."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        command: Callable[[], None],
        variant: str,
        width: int,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=44,
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="hand2",
        )
        self.button_text = text
        self.command = command
        self.variant = variant
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
        self.configure(takefocus=1 if self._enabled else 0)
        self.configure(cursor="hand2" if self._enabled else "")
        self._redraw()

    def _rounded_polygon(
        self,
        inset: int,
        *,
        fill: str,
        radius: int = 11,
    ) -> None:
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        left = inset
        top = inset
        right = width - inset
        bottom = height - inset
        points = (
            left + radius,
            top,
            right - radius,
            top,
            right,
            top,
            right,
            top + radius,
            right,
            bottom - radius,
            right,
            bottom,
            right - radius,
            bottom,
            left + radius,
            bottom,
            left,
            bottom,
            left,
            bottom - radius,
            left,
            top + radius,
            left,
            top,
        )
        self.create_polygon(points, fill=fill, outline="", smooth=True, splinesteps=24)

    def _redraw(self) -> None:
        self.delete("all")
        palette = self._palette
        if not self._enabled:
            fill = palette.disabled_background
            foreground = palette.disabled_text
        elif self.variant == "primary":
            if self._pressed:
                fill = palette.accent_pressed
            elif self._hovered:
                fill = palette.accent_hover
            else:
                fill = palette.accent
            foreground = palette.accent_text
        else:
            fill = palette.secondary_hover if self._hovered else palette.surface_alt
            foreground = palette.danger_text

        if self._focused and self._enabled:
            self._rounded_polygon(1, fill=palette.accent)
            inner_inset = 3
        elif self.variant == "secondary":
            self._rounded_polygon(1, fill=palette.border)
            inner_inset = 2
        else:
            inner_inset = 1
        self._rounded_polygon(inner_inset, fill=fill)
        self.create_text(
            int(self.cget("width")) // 2,
            int(self.cget("height")) // 2,
            text=self.button_text,
            fill=foreground,
            font=("Microsoft YaHei UI", 10, "bold"),
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
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        if was_pressed and 0 <= event.x <= width and 0 <= event.y <= height:
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


@dataclass
class RequestRecord:
    request_id: str
    frozen: dict[str, Any]
    created_at: str
    expires_at: str
    expires_monotonic: float
    elevated: bool
    state: str = "pending"
    updated_at: str = field(default_factory=iso_timestamp)
    terminal_monotonic: float | None = None
    receipt: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    pending_polls_used: int = 0
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)

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
        self._idempotency: dict[str, str] = {}
        self._shutting_down = False
        self._poll_policy = normalize_poll_policy(settings or default_ui_settings())

    def current_poll_policy(self) -> dict[str, Any]:
        with self._condition:
            return dict(self._poll_policy)

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
            key = str(record.frozen["idempotency_key"])
            if self._idempotency.get(key) == request_id:
                del self._idempotency[key]
        if removable:
            self._condition.notify_all()

    def submit(self, kind: Any, payload: Any) -> tuple[dict[str, Any], bool]:
        policy = self.current_poll_policy()
        frozen = self._freeze_with_policy(kind, payload, policy)
        with self._condition:
            if self._shutting_down:
                raise RelayError("broker_stopping", "The relay window is stopping.")
            self._expire_locked()
            idempotency_key = str(frozen["idempotency_key"])
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is None:
                    del self._idempotency[idempotency_key]
                elif existing.frozen.get("proposal_hash") != frozen["proposal_hash"]:
                    raise RelayError(
                        "idempotency_conflict",
                        "The idempotency key was reused for a different frozen request.",
                        details={"request_id": existing.request_id},
                    )
                else:
                    return existing.public_snapshot(), True

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
            request_id = "har_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")
            record = RequestRecord(
                request_id=request_id,
                frozen=frozen,
                created_at=iso_timestamp(created),
                expires_at=iso_timestamp(expires),
                expires_monotonic=time.monotonic() + frozen["expires_in_seconds"],
                elevated=self.elevated,
            )
            self._records[request_id] = record
            self._idempotency[idempotency_key] = request_id
            self._condition.notify_all()
            return record.public_snapshot(), False

    def _get_locked(self, request_id: Any) -> RequestRecord:
        if not isinstance(request_id, str) or not request_id.startswith("har_"):
            raise RelayError("invalid_request_id", "request_id is invalid.")
        self._expire_locked()
        record = self._records.get(request_id)
        if record is None:
            raise RelayError("request_not_found", "The relay request was not found.")
        return record

    def status(self, request_id: Any) -> dict[str, Any]:
        with self._condition:
            return self._get_locked(request_id).public_snapshot()

    def wait(self, request_id: Any, wait_seconds: Any) -> dict[str, Any]:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
            raise RelayError("invalid_wait", "wait_seconds must be a number.")
        with self._condition:
            record = self._get_locked(request_id)
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
                record = self._get_locked(request_id)
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

    def cancel(self, request_id: Any) -> dict[str, Any]:
        with self._condition:
            record = self._get_locked(request_id)
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
        with self._condition:
            record = self._records.get(request_id)
            if record is None:
                return
            record.state = state
            record.updated_at = iso_timestamp()
            record.terminal_monotonic = time.monotonic()
            record.process = None
            record.result = {
                "decision": "approved",
                "message": message,
                **(result or {}),
            }
            self._condition.notify_all()

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
                record.state = "failed"
                record.updated_at = iso_timestamp()
                record.terminal_monotonic = time.monotonic()
                record.result = {
                    "decision": "approved",
                    "message": f"Process creation failed: {type(exc).__name__}: {exc}",
                }
                self._condition.notify_all()
                return
            if self._shutting_down:
                try:
                    process.kill()
                except OSError:
                    pass
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

    def shutdown(self) -> None:
        with self._condition:
            self._shutting_down = True
            processes: list[subprocess.Popen[bytes]] = []
            for record in self._records.values():
                if record.state == "pending":
                    record.state = "cancelled"
                    record.updated_at = iso_timestamp()
                    record.terminal_monotonic = time.monotonic()
                    record.result = {
                        "decision": "cancelled",
                        "message": "The relay window closed before a decision.",
                    }
                elif record.state == "running" and record.process is not None:
                    processes.append(record.process)
            self._condition.notify_all()
        for process in processes:
            try:
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

            operation = request.get("operation")
            if operation == "health":
                response = {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "broker_version": BROKER_VERSION,
                    "broker_elevated": server.store.elevated,
                    "poll_policy": server.store.current_poll_policy(),
                    "pid": os.getpid(),
                }
            elif operation == "submit":
                snapshot, replay = server.store.submit(
                    request.get("kind"),
                    request.get("payload"),
                )
                response = {
                    "ok": True,
                    "request": snapshot,
                    "idempotent_replay": replay,
                }
            elif operation == "status":
                response = {
                    "ok": True,
                    "request": server.store.status(request.get("request_id")),
                }
            elif operation == "wait":
                response = {
                    "ok": True,
                    "request": server.store.wait(
                        request.get("request_id"),
                        request.get("wait_seconds", 0),
                    ),
                }
            elif operation == "cancel":
                response = {
                    "ok": True,
                    "request": server.store.cancel(request.get("request_id")),
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
        super().__init__(address, handler)


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
        self._detail_request_id: str | None = None
        self._detail_text: str | None = None
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
            padding=(16, 14, 16, 10),
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
        preferences = ttk.Frame(title_row, style="HAR.TFrame")
        preferences.grid(row=0, column=1, sticky="e")
        self.theme_label = ttk.Label(
            preferences,
            text="外观：系统",
            style="Meta.HAR.TLabel",
        )
        self.theme_label.pack(side=tk.LEFT, padx=(0, 14))
        self.sound_toggle = ttk.Checkbutton(
            preferences,
            text="新请求提示音",
            variable=self.sound_enabled,
            command=self._on_sound_toggle,
            style="HAR.TCheckbutton",
            takefocus=True,
        )
        self.sound_toggle.pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="人机授权中继 · 每次仅授权当前冻结请求",
            style="Subtitle.HAR.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        policy = ttk.Frame(
            header,
            style="HAR.TFrame",
        )
        policy.pack(fill=tk.X, pady=(10, 0))
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

        main = ttk.Panedwindow(
            self.root,
            orient=tk.HORIZONTAL,
            style="HAR.TPanedwindow",
        )
        self.main = main
        main.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))

        left = ttk.Frame(main, padding=(0, 0, 9, 0), style="HAR.TFrame")
        right = ttk.Frame(main, padding=(9, 0, 0, 0), style="HAR.TFrame")
        main.add(left, weight=2)
        main.add(right, weight=3)

        ttk.Label(left, text="请求队列", style="Section.HAR.TLabel").pack(
            anchor=tk.W,
            pady=(0, 7),
        )
        self.tree = ttk.Treeview(
            left,
            columns=("kind", "state", "action"),
            show="headings",
            selectmode="browse",
            height=8,
            style="HAR.Treeview",
        )
        self.tree.heading("kind", text="类型")
        self.tree.heading("state", text="状态")
        self.tree.heading("action", text="动作")
        self.tree.column("kind", width=64, minwidth=56, stretch=False)
        self.tree.column("state", width=78, minwidth=68, stretch=False)
        self.tree.column("action", width=280, minwidth=140)
        scrollbar = ttk.Scrollbar(
            left,
            orient=tk.VERTICAL,
            command=self.tree.yview,
            style="HAR.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        ttk.Label(right, text="冻结请求详情", style="Section.HAR.TLabel").pack(
            anchor=tk.W,
            pady=(0, 7),
        )
        detail_frame = ttk.Frame(right, style="HAR.TFrame")
        detail_frame.pack(fill=tk.BOTH, expand=True)
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
        self.detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail.bind("<MouseWheel>", self._on_detail_mousewheel)

        actions = ttk.Frame(
            self.root,
            padding=(16, 0, 16, 14),
            style="HAR.TFrame",
        )
        self.actions = actions
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.approve_button = RoundedButton(
            actions,
            text="同意本次",
            command=self.approve_selected,
            variant="primary",
            width=112,
        )
        self.approve_button.set_state(tk.DISABLED)
        self.approve_button.grid(row=0, column=2, sticky="e")
        self.reject_button = RoundedButton(
            actions,
            text="拒绝",
            command=self.reject_selected,
            variant="secondary",
            width=86,
        )
        self.reject_button.set_state(tk.DISABLED)
        self.reject_button.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.status_label = ttk.Label(
            actions,
            text="等待请求…",
            style="Status.HAR.TLabel",
        )
        self.status_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._update_policy_preview()

    def _apply_safe_minimum_size(self) -> None:
        """Keep fixed chrome visible while allowing only the content row to shrink."""

        self.root.update_idletasks()
        minimum_width = max(
            WINDOW_MIN_WIDTH,
            self.header.winfo_reqwidth(),
            self.main.winfo_reqwidth() + 32,
            self.actions.winfo_reqwidth(),
        )
        minimum_height = max(
            WINDOW_MIN_HEIGHT,
            self.header.winfo_reqheight()
            + self.actions.winfo_reqheight()
            + WINDOW_MIN_CONTENT_HEIGHT,
        )
        self.root.minsize(minimum_width, minimum_height)

    def _configure_styles(
        self,
        palette: ThemePalette,
        *,
        window_background: str,
    ) -> None:
        style = self.style
        style.configure("HAR.TFrame", background=window_background)
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
            "Section.HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "PolicyTitle.HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "PolicyLabel.HAR.TLabel",
            background=window_background,
            foreground=palette.text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyUnit.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyStatus.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicySuccess.HAR.TLabel",
            background=window_background,
            foreground=palette.success_text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "PolicyError.HAR.TLabel",
            background=window_background,
            foreground=palette.danger_text,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "BudgetValue.HAR.TLabel",
            background=window_background,
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
            parent_background=self.palette.window,
        )
        self.reject_button.apply_theme(
            self.palette,
            parent_background=self.palette.window,
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
            parent_background=window_background,
        )
        self.reject_button.apply_theme(
            palette,
            parent_background=window_background,
        )
        self.tree.tag_configure("pending", foreground=palette.accent)
        self.tree.tag_configure("running", foreground=palette.accent)
        self.tree.tag_configure("failed", foreground=palette.danger_text)
        self.tree.tag_configure("timed_out", foreground=palette.danger_text)
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

    def refresh(self) -> None:
        if self.closing:
            return
        self._refresh_theme()
        rows = self.store.rows_for_gui()
        existing = set(self.tree.get_children())
        incoming = {row["request_id"] for row in rows}
        new_request_ids = incoming - self._known_request_ids
        self._known_request_ids = incoming
        for stale in existing - incoming:
            self.tree.delete(stale)
        for index, row in enumerate(rows):
            values = (
                self._kind_label(row["kind"]),
                self._state_label(row["state"]),
                row["exact_action"].replace("\n", " ")[:120],
            )
            if self.tree.exists(row["request_id"]):
                self.tree.item(
                    row["request_id"],
                    values=values,
                    tags=(row["state"],),
                )
            else:
                self.tree.insert(
                    "",
                    tk.END,
                    iid=row["request_id"],
                    values=values,
                    tags=(row["state"],),
                )
            self.tree.move(row["request_id"], "", index)

        newest_request = next(
            (
                row
                for row in rows
                if row["request_id"] in new_request_ids and row["state"] == "pending"
            ),
            None,
        )
        if newest_request is not None:
            request_id = newest_request["request_id"]
            self.tree.selection_set(request_id)
            self.tree.focus(request_id)
            self.tree.see(request_id)
            self.current_request_id = request_id
            self._render_detail(request_id)
            self._notify_new_request()
        elif self.current_request_id and self.tree.exists(self.current_request_id):
            self._render_detail(self.current_request_id)
        elif rows:
            pending = next((row for row in rows if row["state"] == "pending"), rows[0])
            self.tree.selection_set(pending["request_id"])
            self.tree.focus(pending["request_id"])
            self.tree.see(pending["request_id"])
            self.current_request_id = pending["request_id"]
            self._render_detail(pending["request_id"])
        else:
            self.current_request_id = None
            self._detail_request_id = None
            self._set_detail(
                "等待 Codex 提交一个明确、单次、可核对的授权请求。",
                reset_scroll=True,
            )
            self.approve_button.set_state(tk.DISABLED)
            self.reject_button.set_state(tk.DISABLED)

        pending_count, running_count = self.store.active_counts()
        self._set_pending_attention(pending_count > 0)
        self.status_label.configure(
            text=f"待确认 {pending_count} · 执行中 {running_count} · "
            f"本地端口 {self.server.server_address[1]}"
        )
        self.root.after(250, self.refresh)

    def on_select(self, _event: tk.Event[Any]) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self.current_request_id = selected[0]
        self._render_detail(selected[0])

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
        reset_scroll = request_id != self._detail_request_id
        permission_line = execution_mode_label(
            str(frozen["kind"]), bool(detail["broker_elevated"])
        )
        lines = [
            f"状态: {self._state_label(detail['state'])}",
            f"请求 ID: {detail['request_id']}",
            f"请求哈希: {detail['request_hash']}",
            f"来源标签: {frozen['source_label']}",
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

    def on_close(self) -> None:
        pending, running = self.store.active_counts()
        if (pending or running) and not messagebox.askyesno(
            "关闭 Codex金手指",
            f"仍有 {pending} 个待确认、{running} 个执行中请求。\n"
            "关闭会取消待确认请求，并尝试终止正在执行的子进程。是否关闭？",
            parent=self.root,
        ):
            return
        self.closing = True
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
        self.store.shutdown()
        self.server.shutdown()
        self.server.server_close()
        remove_endpoint_if_owned(os.getpid(), self.token)
        self.root.destroy()


def main() -> int:
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
