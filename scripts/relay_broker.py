"""Independent approval window for Codex金手指 (Golden Finger for Codex).

This process is intentionally launched by the user, not by Codex or the MCP
connector. Approval transitions are reachable only from GUI callbacks.
"""

from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import ctypes
from ctypes import wintypes
import json
import locale
import math
import os
from pathlib import Path
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, BinaryIO, Callable
import unicodedata
import zlib

import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
except ImportError:  # Source-mode fallback; release builds always include Pillow.
    Image = ImageDraw = ImageFilter = ImageTk = None  # type: ignore[assignment]

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
    MAX_OUTPUT_BYTES,
    MAX_PENDING_REQUESTS,
    MAX_WIRE_BYTES,
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
GUI_TERMINAL_HISTORY_LIMIT = 200
WINDOW_BACKGROUND_OPACITY = 0.60
WINDOW_CONTENT_OPACITY = 1.0
WINDOW_TRANSPARENT_COLOR = "#010203"
WINDOW_HIT_SHIELD_OPACITY = 0.01
WINDOW_DEFAULT_WIDTH = 980
WINDOW_DEFAULT_HEIGHT = 720
# The incumbent two-column composition is most comfortable at this width, but
# the actual resize floor is lower so a DPI-aware window still fits Windows Snap.
WINDOW_MIN_WIDTH = 920
WINDOW_SNAP_MIN_WIDTH = 640
WINDOW_COMPACT_BREAKPOINT = 820
WINDOW_MIN_HEIGHT = 600
WINDOW_MIN_CONTENT_HEIGHT = 260
WINDOW_TOUCH_TARGET = 40
WINDOW_DECISION_STAGE_MIN_HEIGHT = 168
THEME_REFRESH_SECONDS = 2.0
DECISION_LAYOUT_DEBOUNCE_MS = 80
RESPONSIVE_LAYOUT_DEBOUNCE_MS = 80
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
    selection_text: str
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
        accent="#006FD6",
        accent_hover="#0062C0",
        accent_pressed="#0056A8",
        accent_text="#FFFFFF",
        selection="#006FD6",
        selection_text="#FFFFFF",
        code_background="#F4F5F6",
        success_text="#205C2F",
        danger_text="#8F1D1D",
        danger="#D92D20",
        danger_hover="#C8271C",
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
        accent_text="#0D0D0D",
        selection="#1F6FA8",
        selection_text="#FFFFFF",
        code_background="#111111",
        success_text="#A8E4B8",
        danger_text="#FFB8B8",
        danger="#C9343A",
        danger_hover="#B82D33",
        danger_pressed="#A5252B",
        shadow="#0E0E0E",
        disabled_background="#303030",
        disabled_text="#777777",
        secondary_hover="#343434",
    ),
}


def antialiased_circle_coverage(distance: float, radius: float) -> float:
    """Return one-pixel edge coverage for a software-rasterized circle."""

    return max(0.0, min(1.0, radius + 0.5 - distance))


def circular_button_visual_state(
    *,
    enabled: bool,
    hovered: bool,
    pressed: bool,
) -> str:
    """Resolve a single explicit visual state for the decision controls."""

    if not enabled:
        return "disabled"
    if pressed:
        return "pressed"
    if hovered:
        return "hover"
    return "default"


def decision_button_layout(
    width: int,
    height: int,
) -> dict[str, tuple[int, int, int]]:
    """Lay out the three circular decisions without overlapping their canvases."""

    bounded_width = max(1, int(width))
    bounded_height = max(1, int(height))
    # Reserve breathing room above the status rows. At the 600 px Snap floor
    # the descriptive copy can leave this stage much shorter than its requested
    # 250 px; height must therefore be a real constraint, not merely a hint.
    usable_height = max(1, bounded_height - 16)
    minimum_approve_size = min(138, usable_height)
    raw_approve_size = max(
        minimum_approve_size,
        min(188, int(min(bounded_width * 0.58, usable_height))),
    )
    approve_size = max(
        1,
        min(186, usable_height, max(1, round(raw_approve_size / 6) * 6)),
    )
    auxiliary_floor = min(WINDOW_TOUCH_TARGET, usable_height)
    auxiliary_size = max(
        auxiliary_floor,
        min(58, usable_height, round(approve_size * 0.31)),
    )
    auxiliary_size = max(
        auxiliary_floor,
        min(58, usable_height, round(auxiliary_size / 2) * 2),
    )
    gap = max(8, round(auxiliary_size * 0.14))
    width_limited_approve = bounded_width - 2 * (auxiliary_size + gap)
    if width_limited_approve >= minimum_approve_size:
        approve_size = min(approve_size, width_limited_approve)
        approve_size = max(
            1,
            min(usable_height, max(1, approve_size // 6 * 6)),
        )
        auxiliary_size = max(
            auxiliary_floor,
            min(58, usable_height, round(approve_size * 0.31)),
        )
        auxiliary_size = max(
            auxiliary_floor,
            min(58, usable_height, round(auxiliary_size / 2) * 2),
        )
        gap = max(8, round(auxiliary_size * 0.14))

    approve_x = max(0, (bounded_width - approve_size) // 2)
    approve_y = max(4, (bounded_height - approve_size) // 2)
    auxiliary_y = max(0, (bounded_height - auxiliary_size) // 2)
    stop_x = max(0, approve_x - auxiliary_size - gap)
    reject_x = min(
        max(0, bounded_width - auxiliary_size),
        approve_x + approve_size + gap,
    )
    return {
        "stop": (stop_x, auxiliary_y, auxiliary_size),
        "approve": (approve_x, approve_y, approve_size),
        "reject": (reject_x, auxiliary_y, auxiliary_size),
    }


def responsive_window_layout(width: int) -> dict[str, Any]:
    """Return a deterministic Snap-safe layout without querying Tk state."""

    bounded_width = max(WINDOW_SNAP_MIN_WIDTH, int(width))
    compact = bounded_width < WINDOW_COMPACT_BREAKPOINT
    return {
        "compact": compact,
        "left_weight": 1 if compact else 4,
        "decision_weight": 1 if compact else 3,
        # At the 640 px Snap floor these two panes still leave 250 px inside
        # the decision card, enough for the three non-overlapping circles.
        "left_minsize": 298 if compact else 340,
        "decision_minsize": 282 if compact else 310,
        "tree_title_width": 120 if compact else 210,
        "tree_state_width": 58 if compact else 68,
        "tree_source_width": 64 if compact else 92,
        "show_wait_policy": not compact,
        # Detail is an in-window overlay, never a third sizing column.
        "detail_column": 0,
        "detail_columnspan": 2,
    }


def fit_summary_lines(
    text: str,
    *,
    measure: Callable[[str], int],
    width: int,
    max_lines: int,
) -> str:
    """Fit a preview to measured lines; the frozen detail remains unabridged."""

    remaining = " ".join(text.split())
    lines: list[str] = []
    available = max(1, width)
    for line_index in range(max(1, max_lines)):
        if not remaining:
            break
        if measure(remaining) <= available:
            lines.append(remaining)
            break
        last_line = line_index == max(1, max_lines) - 1
        suffix = "…" if last_line else ""
        low, high = 0, len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(remaining[:middle].rstrip() + suffix) <= available:
                low = middle
            else:
                high = middle - 1
        cut = max(1, low)
        if not last_line and remaining[cut : cut + 1] != " ":
            boundary = remaining.rfind(" ", 0, cut)
            if boundary >= cut // 2:
                cut = boundary
        lines.append(remaining[:cut].rstrip() + suffix)
        remaining = remaining[cut:].lstrip()
    return "\n".join(lines)


def relay_connection_summary(
    *,
    server_open: bool,
    connector_seen: bool,
) -> tuple[str, str]:
    """Return the semantic style and honest local-relay status copy."""

    if not server_open:
        return (
            "ConnectionError.HAR.TLabel",
            "● 本地中继不可用 · 请关闭并重新打开窗口",
        )
    if connector_seen:
        return (
            "Connection.HAR.TLabel",
            "● 本地中继就绪 · 最近已连接 Codex",
        )
    return (
        "ConnectionWaiting.HAR.TLabel",
        "● 本地中继就绪 · 等待 Codex",
    )


def empty_state_copy(
    *,
    task_filter: str,
    has_hidden_items: bool,
    inventory_available: bool,
    inventory_error_code: str | None,
) -> tuple[str, str, str, str]:
    """Distinguish filter-empty, first-use, and inventory-error states."""

    if not inventory_available and inventory_error_code not in {
        None,
        "awaiting_thread_metadata",
    }:
        reason = inventory_error_code or "unknown"
        return (
            "Codex 任务清单暂不可用",
            "安全重启已禁用 · 打开“诊断”查看恢复信息",
            "任务清单不可用",
            f"任务清单错误: {reason}\n打开右上角“诊断”核对连接、版本与安装状态。",
        )
    if task_filter == "active" and has_hidden_items:
        return (
            "当前没有进行中的任务或请求",
            "选择“全部”可查看历史请求与空闲任务",
            "当前筛选无结果",
            "当前筛选没有结果。选择“全部”可查看已结束请求与空闲任务。",
        )
    if not inventory_available and inventory_error_code == "awaiting_thread_metadata":
        return (
            "本地中继已就绪",
            "等待任一 Codex 任务连接并提交授权请求",
            "等待 Codex 连接",
            "尚未观察到 Codex 任务。保持本窗口开启；收到请求时会自动提醒。",
        )
    return (
        "当前没有授权请求",
        "Codex 提交明确动作后，金手指会在这里提醒你",
        "暂无待确认请求",
        "当前没有待确认请求。你可以继续使用 Codex，收到请求时本窗口会自动提醒。",
    )


def ellipsize_display_text(
    value: object,
    *,
    max_units: int,
    fallback: str = "—",
) -> str:
    """Fit mixed Chinese/Latin text to an approximate UI width with an ellipsis."""

    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.replace("\x00", " ").split())
    if not cleaned:
        return fallback
    budget = max(3, int(max_units))

    def units(character: str) -> int:
        return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1

    if sum(units(character) for character in cleaned) <= budget:
        return cleaned
    prefix: list[str] = []
    consumed = 0
    content_budget = budget - 2
    for character in cleaned:
        character_units = units(character)
        if consumed + character_units > content_budget:
            break
        prefix.append(character)
        consumed += character_units
    return "".join(prefix).rstrip() + "…"


def _hex_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB color, got {color!r}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG contrast ratio for two #RRGGBB colors."""

    def relative_luminance(color: str) -> float:
        channels = []
        for channel in _hex_rgb(color):
            normalized = channel / 255
            channels.append(
                normalized / 12.92
                if normalized <= 0.04045
                else ((normalized + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first_luminance = relative_luminance(first)
    second_luminance = relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _blend_rgb(
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
    opacity: float,
) -> tuple[int, int, int]:
    alpha = max(0.0, min(1.0, opacity))
    inverse = 1.0 - alpha
    return tuple(
        round(background[channel] * inverse + foreground[channel] * alpha)
        for channel in range(3)
    )  # type: ignore[return-value]


def _paint_antialiased_circle(
    pixels: bytearray,
    *,
    size: int,
    center_x: float,
    center_y: float,
    radius: float,
    color: tuple[int, int, int],
    opacity: float = 1.0,
) -> None:
    if radius <= 0 or opacity <= 0:
        return
    minimum_x = max(0, math.floor(center_x - radius - 1))
    maximum_x = min(size - 1, math.ceil(center_x + radius + 1))
    minimum_y = max(0, math.floor(center_y - radius - 1))
    maximum_y = min(size - 1, math.ceil(center_y + radius + 1))
    for y in range(minimum_y, maximum_y + 1):
        pixel_y = y + 0.5
        row_offset = y * size
        for x in range(minimum_x, maximum_x + 1):
            distance = math.hypot(x + 0.5 - center_x, pixel_y - center_y)
            coverage = antialiased_circle_coverage(distance, radius) * opacity
            if coverage <= 0:
                continue
            index = (row_offset + x) * 3
            inverse = 1.0 - coverage
            pixels[index] = round(pixels[index] * inverse + color[0] * coverage)
            pixels[index + 1] = round(
                pixels[index + 1] * inverse + color[1] * coverage
            )
            pixels[index + 2] = round(
                pixels[index + 2] * inverse + color[2] * coverage
            )


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def _rgb_png(pixels: bytearray, *, size: int) -> bytes:
    """Encode RGB pixels for Tk without a slow per-pixel Tcl color list."""

    stride = size * 3
    scanlines = b"".join(
        b"\x00" + bytes(pixels[offset : offset + stride])
        for offset in range(0, len(pixels), stride)
    )
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=1))
        + _png_chunk(b"IEND", b"")
    )


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


def normalize_poll_policy(_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the single shipped wait policy; legacy UI values are ignored."""

    poll_wait_seconds = DEFAULT_POLL_WAIT_SECONDS
    max_pending_polls = DEFAULT_PENDING_POLL_CYCLES
    pending_wait_budget_seconds = poll_wait_seconds * max_pending_polls
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
    return settings


def save_ui_settings(settings: dict[str, Any], path: Path | None = None) -> None:
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


# UI Automation invokes a native BM_CLICK which is bridged back onto Tk's
# thread. Poll quickly only while a decision is actionable; dormant windows
# should not wake dozens of times per second indefinitely.
BUTTON_NATIVE_ACTIVE_DRAIN_MS = 100
BUTTON_NATIVE_IDLE_DRAIN_MS = 500
BUTTON_MESSAGE_CLICK = 0x00F5  # BM_CLICK
BUTTON_MESSAGE_NCDESTROY = 0x0082  # WM_NCDESTROY
EVENT_OBJECT_STATECHANGE = 0x800A
EVENT_OBJECT_NAMECHANGE = 0x800C
ACCESSIBLE_OBJID_CLIENT = -4
ACCESSIBLE_CHILDID_SELF = 0

if os.name == "nt":
    ButtonSubclassProc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )
else:
    ButtonSubclassProc = None


class CircularButton(tk.Button):
    """Native semantic button with software-antialiased circular artwork."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        command: Callable[[], None],
        variant: str,
        diameter: int,
    ) -> None:
        placeholder = tk.PhotoImage(master=parent, width=diameter, height=diameter)
        super().__init__(
            parent,
            text=text,
            command=command,
            image=placeholder,
            compound=tk.CENTER,
            width=diameter,
            height=diameter,
            borderwidth=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            relief=tk.FLAT,
            overrelief=tk.FLAT,
            default=tk.DISABLED,
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
        self._parent_background = self._palette.surface
        self._button_image: Any = placeholder
        self._image_cache: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._native_hwnd: int | None = None
        self._native_subclass_id = 0
        self._native_subclass_callback: Any = None
        self._native_subclass_installed = False
        self._native_invoke_pending = False
        self._native_drain_owner: tk.Misc | None = None
        self._destroyed = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<KeyPress-space>", self._on_space_press)
        self.bind("<KeyRelease-space>", self._on_space_release)
        self.bind("<Return>", self._on_return)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._install_native_invoke_bridge()
        self._redraw()
        self._schedule_native_invoke_drain()

    def apply_theme(self, palette: ThemePalette, *, parent_background: str) -> None:
        if palette != self._palette or parent_background != self._parent_background:
            self._image_cache.clear()
        self._palette = palette
        self._parent_background = parent_background
        self.configure(
            background=parent_background,
            activebackground=parent_background,
            highlightbackground=parent_background,
            highlightcolor=parent_background,
        )
        self._redraw()

    def set_state(self, state: str) -> None:
        enabled = state not in {tk.DISABLED, "disabled"}
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._hovered = False
            self._pressed = False
        self.configure(
            takefocus=1 if enabled else 0,
            cursor="hand2" if enabled else "",
        )
        self._sync_native_enabled()
        if enabled:
            self._accelerate_native_invoke_drain()
        self._redraw()

    def set_diameter(self, diameter: int) -> None:
        bounded = max(42, int(diameter))
        if bounded == self._diameter:
            return
        self._diameter = bounded
        self._image_cache.clear()
        self.configure(width=bounded, height=bounded)
        self._redraw()

    def _inside_circle(self, x: float, y: float) -> bool:
        center = self._diameter / 2
        radius = max(1.0, center - max(5, round(self._diameter * 0.04)))
        return math.hypot(x - center, y - center) <= radius

    def _render_pillow_button_image(
        self,
        *,
        size: int,
        background_rgb: tuple[int, int, int],
        fill_rgb: tuple[int, int, int],
        shadow_rgb: tuple[int, int, int],
        focus_rgb: tuple[int, int, int],
        outline_rgb: tuple[int, int, int],
        center_x: float,
        center_y: float,
        radius: float,
        shadow_offset: int,
        shadow_opacity: float,
    ) -> Any:
        if Image is None or ImageDraw is None or ImageFilter is None or ImageTk is None:
            raise RuntimeError("Pillow renderer requested without Pillow")
        scale = 3
        rendered_size = size * scale
        canvas = Image.new("RGBA", (rendered_size, rendered_size), (*background_rgb, 255))

        def ellipse_box(circle_radius: float, *, y_offset: float = 0.0) -> tuple[float, ...]:
            return (
                (center_x - circle_radius) * scale,
                (center_y + y_offset - circle_radius) * scale,
                (center_x + circle_radius) * scale,
                (center_y + y_offset + circle_radius) * scale,
            )

        if shadow_opacity > 0:
            shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow_layer).ellipse(
                ellipse_box(radius + 0.8, y_offset=shadow_offset),
                fill=(*shadow_rgb, round(255 * shadow_opacity)),
            )
            blur_radius = max(1.4, size * 0.012) * scale
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_radius))
            canvas = Image.alpha_composite(canvas, shadow_layer)

        drawing = ImageDraw.Draw(canvas)
        if self._focused and self._enabled:
            drawing.ellipse(
                ellipse_box(radius + 2.0),
                fill=(*focus_rgb, 230),
            )
        drawing.ellipse(ellipse_box(radius), fill=(*outline_rgb, 255))
        drawing.ellipse(ellipse_box(radius - 1.25), fill=(*fill_rgb, 255))
        reduced = canvas.resize((size, size), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(reduced, master=self)

    def _render_button_image(self, visual_state: str) -> Any:
        size = self._diameter
        palette = self._palette
        small = self.variant in {"danger", "neutral"}
        cache_key = (
            size,
            visual_state,
            self._focused,
            palette,
            self._parent_background,
            self.variant,
        )
        cached = self._image_cache.get(cache_key)
        if cached is not None:
            self._image_cache.move_to_end(cache_key)
            return cached

        if visual_state == "disabled":
            fill = palette.disabled_background
        elif self.variant == "danger":
            fill = {
                "pressed": palette.danger_pressed,
                "hover": palette.danger_hover,
            }.get(visual_state, palette.danger)
        elif self.variant == "neutral":
            fill = {
                "pressed": palette.border,
                "hover": palette.secondary_hover,
            }.get(visual_state, palette.surface_alt)
        else:
            fill = {
                "pressed": palette.accent_pressed,
                "hover": palette.accent_hover,
            }.get(visual_state, palette.accent)

        background_rgb = _hex_rgb(self._parent_background)
        fill_rgb = _hex_rgb(fill)
        shadow_rgb = _hex_rgb(palette.shadow)
        focus_rgb = _hex_rgb(palette.text)
        outline_source = (
            palette.danger_hover
            if self.variant == "danger"
            else palette.border
            if self.variant == "neutral"
            else palette.accent_hover
        )
        outline_rgb = _blend_rgb(_hex_rgb(outline_source), focus_rgb, 0.18)
        margin = max(6, round(size * 0.045))
        radius = max(12.0, size / 2 - margin)
        center_x = size / 2
        center_y = size / 2
        if visual_state == "hover":
            center_y -= 1
            radius += 0.5
            shadow_offset = max(3, round(size * 0.030))
            shadow_opacity = 0.42
        elif visual_state == "pressed":
            center_y += 2
            radius -= 1
            shadow_offset = 1
            shadow_opacity = 0.12
        elif visual_state == "disabled":
            shadow_offset = 0
            shadow_opacity = 0.0
        else:
            shadow_offset = max(2, round(size * 0.022))
            shadow_opacity = 0.30

        if Image is not None and ImageDraw is not None and ImageFilter is not None and ImageTk is not None:
            image = self._render_pillow_button_image(
                size=size,
                background_rgb=background_rgb,
                fill_rgb=fill_rgb,
                shadow_rgb=shadow_rgb,
                focus_rgb=focus_rgb,
                outline_rgb=outline_rgb,
                center_x=center_x,
                center_y=center_y,
                radius=radius,
                shadow_offset=shadow_offset,
                shadow_opacity=shadow_opacity,
            )
        else:
            pixels = bytearray(background_rgb * (size * size))
            if shadow_opacity > 0:
                for spread, opacity_scale in ((2.4, 0.22), (1.2, 0.34), (0.0, 0.44)):
                    _paint_antialiased_circle(
                        pixels,
                        size=size,
                        center_x=center_x,
                        center_y=center_y + shadow_offset,
                        radius=radius + spread,
                        color=shadow_rgb,
                        opacity=shadow_opacity * opacity_scale,
                    )

            if self._focused and self._enabled:
                _paint_antialiased_circle(
                    pixels,
                    size=size,
                    center_x=center_x,
                    center_y=center_y,
                    radius=radius + 2.0,
                    color=focus_rgb,
                    opacity=0.90,
                )
            _paint_antialiased_circle(
                pixels,
                size=size,
                center_x=center_x,
                center_y=center_y,
                radius=radius,
                color=outline_rgb,
                opacity=1.0,
            )
            _paint_antialiased_circle(
                pixels,
                size=size,
                center_x=center_x,
                center_y=center_y,
                radius=radius - 1.25,
                color=fill_rgb,
                opacity=1.0,
            )
            image = tk.PhotoImage(
                master=self,
                data=base64.b64encode(_rgb_png(pixels, size=size)),
                format="png",
            )
        self._image_cache[cache_key] = image
        self._image_cache.move_to_end(cache_key)
        while len(self._image_cache) > 8:
            self._image_cache.popitem(last=False)
        return image

    def _redraw(self) -> None:
        size = self._diameter
        palette = self._palette
        small = self.variant in {"danger", "neutral"}
        visual_state = circular_button_visual_state(
            enabled=self._enabled,
            hovered=self._hovered,
            pressed=self._pressed,
        )
        foreground = (
            palette.disabled_text
            if visual_state == "disabled"
            else "#FFFFFF"
            if self.variant == "danger"
            else palette.text
            if self.variant == "neutral"
            else palette.accent_text
        )
        self._button_image = self._render_button_image(visual_state)
        font_size = max(9, round(size * (0.16 if small else 0.105)))
        self.configure(
            image=self._button_image,
            text=self.button_text,
            compound=tk.CENTER,
            foreground=foreground,
            activeforeground=foreground,
            disabledforeground=palette.disabled_text,
            background=self._parent_background,
            activebackground=self._parent_background,
            justify=tk.CENTER,
            font=("Microsoft YaHei UI", font_size, "bold"),
            wraplength=max(34, size - 12),
        )
        self._sync_native_accessible_name()

    def _sync_native_accessible_name(self) -> None:
        """Expose a stable Win32 Button name even when an image is displayed."""

        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = self.winfo_id()
            accessible_name = " ".join(self.button_text.split())
            length = max(0, int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))))
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, len(buffer))
            if buffer.value != accessible_name:
                user32.SetWindowTextW(ctypes.c_void_p(hwnd), accessible_name)
                user32.NotifyWinEvent(
                    ctypes.c_uint(EVENT_OBJECT_NAMECHANGE),
                    ctypes.c_void_p(hwnd),
                    ctypes.c_long(ACCESSIBLE_OBJID_CLIENT),
                    ctypes.c_long(ACCESSIBLE_CHILDID_SELF),
                )
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            pass

    def _sync_native_enabled(self) -> None:
        """Keep the underlying Win32 Button state aligned with the Tk state."""

        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = self.winfo_id()
            native_enabled = bool(user32.IsWindowEnabled(ctypes.c_void_p(hwnd)))
            if native_enabled == self._enabled:
                return
            user32.EnableWindow(
                ctypes.c_void_p(hwnd),
                ctypes.c_int(1 if self._enabled else 0),
            )
            user32.NotifyWinEvent(
                ctypes.c_uint(EVENT_OBJECT_STATECHANGE),
                ctypes.c_void_p(hwnd),
                ctypes.c_long(ACCESSIBLE_OBJID_CLIENT),
                ctypes.c_long(ACCESSIBLE_CHILDID_SELF),
            )
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            pass

    def _install_native_invoke_bridge(self) -> None:
        """Translate UI Automation BM_CLICK into one Tk-thread invocation."""

        if os.name != "nt" or ButtonSubclassProc is None:
            return
        try:
            comctl32 = ctypes.windll.comctl32
            comctl32.SetWindowSubclass.argtypes = (
                wintypes.HWND,
                ButtonSubclassProc,
                ctypes.c_size_t,
                ctypes.c_size_t,
            )
            comctl32.SetWindowSubclass.restype = wintypes.BOOL
            comctl32.DefSubclassProc.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
            comctl32.RemoveWindowSubclass.argtypes = (
                wintypes.HWND,
                ButtonSubclassProc,
                ctypes.c_size_t,
            )
            comctl32.RemoveWindowSubclass.restype = wintypes.BOOL

            hwnd = int(self.winfo_id())
            subclass_id = ctypes.c_size_t(id(self)).value or 1

            @ButtonSubclassProc
            def subclass_proc(
                native_hwnd: int,
                message: int,
                wparam: int,
                lparam: int,
                _subclass_id: int,
                _reference_data: int,
            ) -> int:
                # Never call Tk from this native callback. Tk's main loop releases
                # the GIL while waiting in user32, so re-entering Tk here can crash
                # CPython. Only publish a flag for the scheduled Tk-thread drain.
                try:
                    if message == BUTTON_MESSAGE_CLICK and self._enabled:
                        self._native_invoke_pending = True
                    elif message == BUTTON_MESSAGE_NCDESTROY:
                        self._native_subclass_installed = False
                except (AttributeError, RuntimeError):
                    pass
                return int(
                    comctl32.DefSubclassProc(
                        native_hwnd,
                        message,
                        wparam,
                        lparam,
                    )
                )

            if not comctl32.SetWindowSubclass(
                wintypes.HWND(hwnd),
                subclass_proc,
                ctypes.c_size_t(subclass_id),
                ctypes.c_size_t(0),
            ):
                return
            self._native_hwnd = hwnd
            self._native_subclass_id = subclass_id
            # The OS retains only a function pointer. Keep the ctypes callback
            # alive until RemoveWindowSubclass succeeds or the HWND is gone.
            self._native_subclass_callback = subclass_proc
            self._native_subclass_installed = True
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            self._native_hwnd = None
            self._native_subclass_id = 0
            self._native_subclass_callback = None
            self._native_subclass_installed = False

    def _schedule_native_invoke_drain(self) -> None:
        if self._destroyed or not self._native_subclass_installed:
            return
        try:
            owner = self.winfo_toplevel()
            members = getattr(owner, "_golden_finger_native_buttons", None)
            if members is None:
                members = set()
                setattr(owner, "_golden_finger_native_buttons", members)
            members.add(self)
            self._native_drain_owner = owner
            if getattr(owner, "_golden_finger_native_drain_job", None) is None:
                job = owner.after(
                    BUTTON_NATIVE_ACTIVE_DRAIN_MS,
                    lambda current_owner=owner: CircularButton._drain_native_invoke_group(
                        current_owner
                    ),
                )
                setattr(owner, "_golden_finger_native_drain_job", job)
        except tk.TclError:
            self._native_drain_owner = None

    def _accelerate_native_invoke_drain(self) -> None:
        """Replace a dormant 500 ms wake-up when a decision becomes actionable."""

        owner = self._native_drain_owner
        if owner is None or self._destroyed or not self._native_subclass_installed:
            return
        try:
            job = getattr(owner, "_golden_finger_native_drain_job", None)
            if job is not None:
                owner.after_cancel(job)
            job = owner.after(
                BUTTON_NATIVE_ACTIVE_DRAIN_MS,
                lambda current_owner=owner: CircularButton._drain_native_invoke_group(
                    current_owner
                ),
            )
            setattr(owner, "_golden_finger_native_drain_job", job)
        except tk.TclError:
            pass

    @staticmethod
    def _drain_native_invoke_group(owner: tk.Misc) -> None:
        try:
            setattr(owner, "_golden_finger_native_drain_job", None)
            members = getattr(owner, "_golden_finger_native_buttons", set())
            for button in tuple(members):
                if button._destroyed:
                    members.discard(button)
                else:
                    button._drain_native_invoke()
            if members and owner.winfo_exists():
                delay_ms = (
                    BUTTON_NATIVE_ACTIVE_DRAIN_MS
                    if any(button._enabled for button in members)
                    else BUTTON_NATIVE_IDLE_DRAIN_MS
                )
                job = owner.after(
                    delay_ms,
                    lambda current_owner=owner: CircularButton._drain_native_invoke_group(
                        current_owner
                    ),
                )
                setattr(owner, "_golden_finger_native_drain_job", job)
        except tk.TclError:
            try:
                setattr(owner, "_golden_finger_native_drain_job", None)
            except tk.TclError:
                pass

    def _drain_native_invoke(self) -> None:
        if self._destroyed:
            return
        should_invoke = self._native_invoke_pending
        self._native_invoke_pending = False
        if should_invoke and self._enabled:
            tk.Button.invoke(self)

    def _unschedule_native_invoke_drain(self) -> None:
        owner = self._native_drain_owner
        self._native_drain_owner = None
        if owner is None:
            return
        try:
            members = getattr(owner, "_golden_finger_native_buttons", set())
            members.discard(self)
            if members:
                return
            job = getattr(owner, "_golden_finger_native_drain_job", None)
            if job is not None:
                owner.after_cancel(job)
            setattr(owner, "_golden_finger_native_drain_job", None)
        except tk.TclError:
            pass

    def _remove_native_invoke_bridge(self) -> None:
        callback = self._native_subclass_callback
        hwnd = self._native_hwnd
        if (
            os.name == "nt"
            and self._native_subclass_installed
            and ButtonSubclassProc is not None
            and callback is not None
            and hwnd is not None
        ):
            try:
                removed = bool(
                    ctypes.windll.comctl32.RemoveWindowSubclass(
                        wintypes.HWND(hwnd),
                        callback,
                        ctypes.c_size_t(self._native_subclass_id),
                    )
                )
                if not removed and ctypes.windll.user32.IsWindow(
                    ctypes.c_void_p(hwnd)
                ):
                    # Keep the callback alive if the OS may still call it.
                    return
            except (AttributeError, OSError, TypeError, ValueError):
                return
        self._native_subclass_installed = False
        self._native_subclass_callback = None
        self._native_hwnd = None
        self._native_subclass_id = 0

    def _on_destroy(self, event: tk.Event[Any]) -> None:
        if event.widget is not self or self._destroyed:
            return
        self._destroyed = True
        self._native_invoke_pending = False
        self._unschedule_native_invoke_drain()
        self._remove_native_invoke_bridge()

    def invoke(self) -> Any:
        """Keep every programmatic path behind the same disabled-state guard."""

        if not self._enabled:
            return ""
        return tk.Button.invoke(self)

    def _on_enter(self, event: tk.Event[Any]) -> None:
        if self._enabled and self._inside_circle(event.x, event.y):
            self._hovered = True
            self.configure(cursor="hand2")
            self._redraw()

    def _on_motion(self, event: tk.Event[Any]) -> None:
        if not self._enabled:
            return
        hovered = self._inside_circle(event.x, event.y)
        if hovered == self._hovered:
            return
        self._hovered = hovered
        if not hovered:
            self._pressed = False
        self.configure(cursor="hand2" if hovered else "")
        self._redraw()

    def _on_leave(self, _event: tk.Event[Any]) -> None:
        self._hovered = False
        self._pressed = False
        self.configure(cursor="")
        self._redraw()

    def _on_press(self, event: tk.Event[Any]) -> str | None:
        if not self._enabled or not self._inside_circle(event.x, event.y):
            return "break"
        self.focus_set()
        self._pressed = True
        self._redraw()
        return None

    def _on_release(self, event: tk.Event[Any]) -> str | None:
        if not self._enabled:
            return "break"
        inside = self._inside_circle(event.x, event.y)
        was_pressed = self._pressed and inside
        self._pressed = False
        self._hovered = inside
        self.configure(cursor="hand2" if inside else "")
        self._redraw()
        return None if was_pressed else "break"

    def _on_focus_in(self, _event: tk.Event[Any]) -> None:
        self._focused = True
        self._redraw()

    def _on_focus_out(self, _event: tk.Event[Any]) -> None:
        self._focused = False
        self._pressed = False
        self._redraw()

    def _on_space_press(self, _event: tk.Event[Any]) -> str | None:
        if self._enabled:
            self._pressed = True
            self._redraw()
            return None
        return "break"

    def _on_space_release(self, _event: tk.Event[Any]) -> str | None:
        if self._enabled and self._pressed:
            self._pressed = False
            self._redraw()
            return None
        return "break"

    def _on_return(self, _event: tk.Event[Any]) -> str:
        if self._enabled:
            self.invoke()
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
        self._gui_revision = 0
        self._gui_cache_revision = -1
        self._gui_cache_rows: tuple[dict[str, Any], ...] = ()
        self._gui_cache_counts = (0, 0)
        self._next_expiry_check_monotonic = 0.0

    def current_poll_policy(self) -> dict[str, Any]:
        with self._condition:
            return dict(self._poll_policy)

    def is_stopping(self) -> bool:
        with self._condition:
            return self._shutting_down

    def _mark_gui_changed_locked(self, *, expiry_changed: bool = True) -> None:
        """Invalidate the bounded GUI snapshot and wake all protocol waiters."""

        self._gui_revision += 1
        if expiry_changed:
            self._next_expiry_check_monotonic = 0.0
        self._condition.notify_all()

    def _ensure_gui_cache_locked(self) -> None:
        if self._gui_cache_revision == self._gui_revision:
            return
        rows: list[dict[str, Any]] = []
        terminal_rows = 0
        pending = 0
        running = 0
        for record in reversed(self._records.values()):
            if record.state == "pending":
                pending += 1
            elif record.state == "running":
                running += 1
            is_active = record.state in ACTIVE_STATES
            if not is_active and terminal_rows >= GUI_TERMINAL_HISTORY_LIMIT:
                continue
            if not is_active:
                terminal_rows += 1
            rows.append(
                {
                    **record.public_snapshot(),
                    "risk": record.frozen["risk"],
                    "source_label": record.frozen["source_label"],
                    "source_project": record.frozen["source_project"],
                    "source_task": record.frozen["source_task"],
                    "codex_thread_id": record.codex_thread_id,
                }
            )
        self._gui_cache_rows = tuple(rows)
        self._gui_cache_counts = (pending, running)
        self._gui_cache_revision = self._gui_revision

    def gui_snapshot(
        self,
        changed_since: int | None = None,
    ) -> tuple[int, list[dict[str, Any]] | None, int, int]:
        """Return one bounded snapshot, omitting rows when nothing has changed."""

        with self._condition:
            self._expire_locked()
            self._ensure_gui_cache_locked()
            rows = (
                None
                if changed_since == self._gui_revision
                else list(self._gui_cache_rows)
            )
            pending, running = self._gui_cache_counts
            return self._gui_revision, rows, pending, running

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
        if now < self._next_expiry_check_monotonic:
            return
        changed = False
        next_check = float("inf")
        for record in self._records.values():
            if record.state == "pending" and now >= record.expires_monotonic:
                self._expire_pending_locked(
                    record,
                    message="The request expired before a human decision.",
                )
                changed = True
            if record.state == "pending":
                next_check = min(next_check, record.expires_monotonic)
            elif record.terminal_monotonic is not None:
                next_check = min(
                    next_check,
                    record.terminal_monotonic + TERMINAL_RETENTION_SECONDS,
                )
        removable = [
            request_id
            for request_id, record in self._records.items()
            if record.terminal_monotonic is not None
            and now - record.terminal_monotonic >= TERMINAL_RETENTION_SECONDS
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
            next_check = float("inf")
            for record in self._records.values():
                if record.state == "pending":
                    next_check = min(next_check, record.expires_monotonic)
                elif record.terminal_monotonic is not None:
                    next_check = min(
                        next_check,
                        record.terminal_monotonic + TERMINAL_RETENTION_SECONDS,
                    )
        if changed or removable:
            self._mark_gui_changed_locked(expiry_changed=False)
        self._next_expiry_check_monotonic = next_check

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
                    if (
                        existing.codex_thread_id is None
                        and normalized_codex_thread_id is not None
                    ):
                        existing.codex_thread_id = normalized_codex_thread_id
                        self._mark_gui_changed_locked()
                    return existing.public_snapshot(), True, existing.claim_token

            self._ensure_gui_cache_locked()
            active_count = sum(self._gui_cache_counts)
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
            self._mark_gui_changed_locked()
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
                    self._mark_gui_changed_locked()
                    return record.public_snapshot()
                record.pending_polls_used += 1
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
                return record.public_snapshot()
            if record.state == "running":
                raise RelayError(
                    "already_started",
                    "The command has already started; cancellation cannot revoke approval.",
                )
            return record.public_snapshot()

    def cancel_from_gui(self, request_id: str) -> dict[str, Any]:
        """Stop one pending wait without expressing approval or rejection."""

        with self._condition:
            record = self._get_locked(request_id)
            if record.state != "pending":
                raise RelayError(
                    "decision_already_made",
                    "This request is no longer pending.",
                )
            now = iso_timestamp()
            record.state = "cancelled"
            record.updated_at = now
            record.terminal_monotonic = time.monotonic()
            record.receipt = {
                "human_action": "stopped_waiting_once",
                "decided_at": now,
                "request_hash": record.request_hash,
            }
            record.result = {
                "decision": "cancelled",
                "reason_code": "stopped_by_user",
                "message": (
                    "The user stopped waiting without approving or rejecting "
                    "the request."
                ),
            }
            self._mark_gui_changed_locked()
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
            self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
                return record.public_snapshot()

            record.state = "running"
            self._mark_gui_changed_locked()
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
            self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
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
                self._mark_gui_changed_locked()
                return
            record.process = process
            record.process_job = process_job
            record.updated_at = iso_timestamp()
            self._mark_gui_changed_locked()

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
        _revision, rows, _pending, _running = self.gui_snapshot()
        return rows or []

    def detail_for_gui(self, request_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._get_locked(request_id)
            return {
                **record.public_snapshot(),
                "frozen": json.loads(json.dumps(record.frozen)),
            }

    def active_counts(self) -> tuple[int, int]:
        _revision, _rows, pending, running = self.gui_snapshot()
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
            self._ensure_gui_cache_locked()
            if sum(self._gui_cache_counts):
                return False, "relay_requests_active"
            self._restart_pending = True
            self._mark_gui_changed_locked()
            return True, "restart_gate_acquired"

    def end_codex_restart(self) -> None:
        with self._condition:
            self._restart_pending = False
            self._mark_gui_changed_locked()

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
            self._mark_gui_changed_locked()
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


def _computer_use_version_key(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if not all(part.isascii() and part.isdecimal() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _configured_computer_use_version(codex_home: Path) -> str | None:
    """Read the bundled catalog's current version without inspecting sessions."""

    try:
        with (codex_home / "config.toml").open("rb") as handle:
            config = tomllib.load(handle)
        marketplaces = config.get("marketplaces", {})
        bundled = (
            marketplaces.get("openai-bundled", {})
            if isinstance(marketplaces, dict)
            else {}
        )
        if not isinstance(bundled, dict) or bundled.get("source_type") != "local":
            return None
        source = bundled.get("source")
        if not isinstance(source, str) or not source.strip():
            return None
        marketplace_root = Path(source).expanduser()
        if not marketplace_root.is_absolute():
            marketplace_root = codex_home / marketplace_root
        catalog = json.loads(
            (marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        entries = catalog.get("plugins", []) if isinstance(catalog, dict) else []
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") != "computer-use":
                continue
            plugin_source = entry.get("source", {})
            if (
                not isinstance(plugin_source, dict)
                or plugin_source.get("source") != "local"
            ):
                continue
            plugin_path = plugin_source.get("path")
            if not isinstance(plugin_path, str) or not plugin_path.strip():
                continue
            manifest = json.loads(
                (
                    marketplace_root
                    / plugin_path
                    / ".codex-plugin"
                    / "plugin.json"
                ).read_text(encoding="utf-8")
            )
            if not isinstance(manifest, dict) or manifest.get("name") != "computer-use":
                continue
            version = manifest.get("version")
            if isinstance(version, str) and _computer_use_version_key(version) is not None:
                return version
    except (OSError, UnicodeError, ValueError):
        # A missing or stale catalog must not prevent opening an installed copy.
        pass
    return None


def resolve_computer_use_guidance(codex_home: Path | None = None) -> Path:
    """Resolve on every click so both the user profile and plugin version can change."""

    if codex_home is None:
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        codex_home = Path(configured_home) if configured_home else Path.home() / ".codex"
    codex_home = codex_home.expanduser().resolve()
    cache_root = codex_home / "plugins" / "cache" / "openai-bundled" / "computer-use"
    configured_version = _configured_computer_use_version(codex_home)
    if configured_version is not None:
        current = cache_root / configured_version / "docs" / "guidance.md"
        if current.is_file():
            return current

    candidates: list[tuple[tuple[int, ...], Path]] = []
    if cache_root.is_dir():
        for version_root in cache_root.iterdir():
            version_key = _computer_use_version_key(version_root.name)
            if version_key is None or not version_root.is_dir():
                continue
            guidance = version_root / "docs" / "guidance.md"
            if guidance.is_file():
                candidates.append((version_key, guidance))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    raise FileNotFoundError(
        "没有找到已安装 Computer Use 插件的 docs/guidance.md。\n\n"
        f"查找目录：{cache_root}\n\n"
        "请在 Codex 中安装或更新 Computer Use 插件后重试。"
    )


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
        self._task_tree_signature: tuple[Any, ...] | None = None
        self._content_render_signature: tuple[Any, ...] | None = None
        self._summary_values: tuple[str, str, str, str] | None = None
        self._summary_signature: tuple[Any, ...] | None = None
        self._summary_has_ellipsis = False
        self._tree_empty_text: str | None = None
        self._inventory_render_signature: tuple[Any, ...] | None = None
        self._status_text: str | None = None
        self._task_filter = "active"
        self._codex_tasks: dict[str, CodexTask] = {}
        self._request_rows: dict[str, dict[str, Any]] = {}
        self._restart_in_progress = False
        self._restart_cancel = threading.Event()
        self._diagnostic_window: tk.Toplevel | None = None
        self._diagnostic_text: tk.Text | None = None
        self._known_request_ids: set[str] = set()
        self._store_gui_revision: int | None = None
        self._attention_active = False
        self._activation_job: str | None = None
        self._decision_layout_job: str | None = None
        self._responsive_layout_job: str | None = None
        self._responsive_layout_signature: tuple[Any, ...] | None = None
        self._connection_render_signature: tuple[Any, ...] | None = None
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
        self.task_filter_value = tk.StringVar(master=root, value=self._task_filter)
        self._inventory_available = False
        self._inventory_error_code: str | None = "awaiting_thread_metadata"

        root.title("Codex金手指")
        root.geometry(f"{WINDOW_DEFAULT_WIDTH}x{WINDOW_DEFAULT_HEIGHT}")
        root.minsize(WINDOW_SNAP_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1, minsize=WINDOW_MIN_CONTENT_HEIGHT)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.attributes("-alpha", WINDOW_CONTENT_OPACITY)
        except tk.TclError:
            pass

        self._build_ui()
        root.bind("<Configure>", self._schedule_responsive_layout, add="+")
        root.bind("<Escape>", self._on_escape, add="+")
        self._create_hit_shield()
        self._refresh_theme(force=True)
        self._apply_safe_minimum_size()
        self._apply_responsive_layout(force=True)
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
            padding=(16, 12, 16, 12),
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
            text="● 本地中继就绪 · 等待 Codex",
            style="ConnectionWaiting.HAR.TLabel",
        )
        self.connection_label.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.header_subtitle = ttk.Label(
            header,
            text="人机授权中继 · 停止仅结束等待，不代表同意或拒绝",
            style="Subtitle.HAR.TLabel",
        )
        self.header_subtitle.pack(anchor=tk.W, pady=(4, 0))
        preferences = ttk.Frame(header, style="HAR.TFrame")
        self.preferences_row = preferences
        preferences.pack(fill=tk.X, pady=(8, 0))
        preferences.columnconfigure(0, weight=1)
        self.wait_policy_label = ttk.Label(
            preferences,
            text=(
                f"等待 {DEFAULT_POLL_WAIT_SECONDS}秒 × "
                f"{DEFAULT_PENDING_POLL_CYCLES}次"
            ),
            style="Meta.HAR.TLabel",
        )
        self.wait_policy_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.theme_label = ttk.Label(
            preferences,
            text="外观：系统",
            style="Meta.HAR.TLabel",
        )
        self.theme_label.grid(row=0, column=1, sticky="e", padx=(0, 12))
        self.sound_toggle = ttk.Checkbutton(
            preferences,
            text="提示音",
            variable=self.sound_enabled,
            command=self._on_sound_toggle,
            style="HAR.TCheckbutton",
            takefocus=True,
        )
        self.sound_toggle.grid(row=0, column=2, sticky="e")
        self.computer_use_guidance_button = ttk.Button(
            preferences,
            text="编辑 Computer Use 指南",
            command=self.open_computer_use_guidance,
            style="Diag.HAR.TButton",
            width=0,
            takefocus=True,
        )
        self.computer_use_guidance_button.grid(
            row=0, column=3, sticky="e", padx=(8, 0)
        )
        self.diagnostics_button = ttk.Button(
            preferences,
            text="诊断",
            command=self.show_diagnostics,
            style="Diag.HAR.TButton",
            width=0,
            takefocus=True,
        )
        self.diagnostics_button.grid(row=0, column=4, sticky="e", padx=(8, 0))
        main = ttk.Frame(
            self.root,
            padding=(16, 0, 16, 16),
            style="HAR.TFrame",
        )
        self.main = main
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=5, minsize=340)
        main.columnconfigure(1, weight=3, minsize=310)
        main.columnconfigure(2, weight=0, minsize=0)
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main, padding=16, style="Card.HAR.TFrame")
        self.task_panel = left
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        left_header = ttk.Frame(left, style="Card.HAR.TFrame")
        left_header.grid(row=0, column=0, sticky="ew")
        left_header.columnconfigure(0, weight=1)
        self.task_section_label = ttk.Label(
            left_header,
            text="任务与请求",
            style="CardSection.HAR.TLabel",
        )
        self.task_section_label.grid(row=0, column=0, sticky="w")
        filters = ttk.Frame(left_header, style="Card.HAR.TFrame")
        filters.grid(row=0, column=1, sticky="e")
        self.active_filter_button = ttk.Radiobutton(
            filters,
            text="进行中",
            command=lambda: self._set_task_filter("active"),
            variable=self.task_filter_value,
            value="active",
            style="Filter.HAR.TRadiobutton",
            takefocus=True,
        )
        self.active_filter_button.pack(side=tk.LEFT)
        self.all_filter_button = ttk.Radiobutton(
            filters,
            text="全部",
            command=lambda: self._set_task_filter("all"),
            variable=self.task_filter_value,
            value="all",
            style="Filter.HAR.TRadiobutton",
            takefocus=True,
        )
        self.all_filter_button.pack(side=tk.LEFT, padx=(4, 0))

        self.inventory_label = ttk.Label(
            left,
            text="等待 Codex 任务状态…",
            style="CardMeta.HAR.TLabel",
        )
        self.inventory_label.grid(row=1, column=0, sticky="ew", pady=(8, 12))

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
        self.tree_empty_label = ttk.Label(
            tree_frame,
            text="",
            style="CardMeta.HAR.TLabel",
            justify=tk.CENTER,
            anchor=tk.CENTER,
            takefocus=False,
        )

        restart_panel = ttk.Frame(
            left,
            padding=12,
            style="Inset.HAR.TFrame",
        )
        self.restart_panel = restart_panel
        restart_panel.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        restart_panel.columnconfigure(0, weight=1)
        self.restart_title_label = ttk.Label(
            restart_panel,
            text="插件更新与安全重启",
            style="InsetTitle.HAR.TLabel",
        )
        self.restart_title_label.grid(row=0, column=0, sticky="w")
        self.restart_status_label = ttk.Label(
            restart_panel,
            text="正在核对任务状态",
            style="InsetMeta.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self.restart_status_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.restart_button = ttk.Button(
            restart_panel,
            text="安全重启 Codex",
            command=self.request_safe_restart,
            style="Restart.HAR.TButton",
            state=tk.DISABLED,
            takefocus=True,
        )
        self.restart_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        self.status_label = ttk.Label(
            left,
            text="等待请求…",
            style="CardMeta.HAR.TLabel",
        )
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        center = ttk.Frame(main, padding=16, style="Card.HAR.TFrame")
        self.decision_panel = center
        center.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(6, weight=1, minsize=WINDOW_DECISION_STAGE_MIN_HEIGHT + 12)
        self.decision_heading_label = ttk.Label(
            center,
            text="授权确认",
            style="CardSection.HAR.TLabel",
        )
        self.decision_heading_label.grid(row=0, column=0, sticky="w")
        self.source_label = ttk.Label(
            center,
            text="尚未选择授权请求",
            style="CardMeta.HAR.TLabel",
        )
        self.source_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.action_label = ttk.Label(
            center,
            text="等待一个明确、单次、可核对的动作",
            style="DecisionTitle.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.action_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.target_label = ttk.Label(
            center,
            text="",
            style="DecisionTarget.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.target_label.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.risk_label = ttk.Label(
            center,
            text="",
            style="Risk.HAR.TLabel",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=310,
        )
        self.risk_label.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.detail_toggle_button = ttk.Button(
            center,
            text="详细参数 ›",
            command=self.toggle_detail,
            style="Detail.HAR.TButton",
            state=tk.DISABLED,
            takefocus=True,
        )
        self.detail_toggle_button.grid(row=5, column=0, sticky="w", pady=(8, 0))

        self.decision_stage = ttk.Frame(
            center, style="Card.HAR.TFrame", height=WINDOW_DECISION_STAGE_MIN_HEIGHT
        )
        self.decision_stage.grid(row=6, column=0, sticky="nsew", pady=(12, 0))
        self.decision_stage.grid_propagate(False)
        self.decision_stage.bind("<Configure>", self._schedule_decision_layout)
        self.stop_button = CircularButton(
            self.decision_stage,
            text="停止",
            command=self.stop_selected,
            variant="neutral",
            diameter=52,
        )
        self.stop_button.set_state(tk.DISABLED)
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
            text="拒绝",
            command=self.reject_selected,
            variant="danger",
            diameter=52,
        )
        self.reject_button.set_state(tk.DISABLED)
        self.approve_button.place(x=70, y=38)
        self.stop_button.place(x=50, y=35)
        self.reject_button.place(x=205, y=35)
        self.decision_state_label = ttk.Label(
            center,
            text="未选择待确认请求",
            style="DecisionState.HAR.TLabel",
            anchor=tk.CENTER,
        )
        self.decision_state_label.grid(row=7, column=0, sticky="ew", pady=(4, 0))
        self.guard_label = ttk.Label(
            center,
            text="仅授权当前冻结请求",
            style="Guard.HAR.TLabel",
            anchor=tk.CENTER,
        )
        self.guard_label.grid(row=8, column=0, sticky="ew", pady=(4, 0))

        detail_panel = ttk.Frame(main, padding=(14, 13), style="Card.HAR.TFrame")
        self.detail_panel = detail_panel
        detail_panel.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="nsew",
        )
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
        self.detail_close_button = ttk.Button(
            detail_header,
            text="返回授权确认",
            command=self.toggle_detail,
            style="Quiet.HAR.TButton",
            takefocus=True,
        )
        self.detail_close_button.grid(row=0, column=1, sticky="e")
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

    def _apply_safe_minimum_size(self) -> None:
        """Keep the window Snap-safe while retaining a usable vertical floor."""

        self.root.update_idletasks()
        minimum_height = max(
            WINDOW_MIN_HEIGHT,
            self.header.winfo_reqheight()
            + self.decision_panel.winfo_reqheight()
            + 16,
        )
        self.root.minsize(WINDOW_SNAP_MIN_WIDTH, minimum_height)

    def _schedule_responsive_layout(
        self,
        event: tk.Event[Any] | None = None,
    ) -> None:
        if self.closing or (event is not None and event.widget is not self.root):
            return
        if self._responsive_layout_job is not None:
            try:
                self.root.after_cancel(self._responsive_layout_job)
            except tk.TclError:
                pass
        try:
            self._responsive_layout_job = self.root.after(
                RESPONSIVE_LAYOUT_DEBOUNCE_MS,
                self._run_scheduled_responsive_layout,
            )
        except tk.TclError:
            self._responsive_layout_job = None

    def _run_scheduled_responsive_layout(self) -> None:
        self._responsive_layout_job = None
        self._apply_responsive_layout()

    def _apply_responsive_layout(self, *, force: bool = False) -> None:
        width = max(WINDOW_SNAP_MIN_WIDTH, self.root.winfo_width())
        layout = responsive_window_layout(width)
        signature = (width, *layout.values())
        if not force and signature == self._responsive_layout_signature:
            return

        self.main.columnconfigure(
            0,
            weight=int(layout["left_weight"]),
            minsize=int(layout["left_minsize"]),
            uniform="primary",
        )
        self.main.columnconfigure(
            1,
            weight=int(layout["decision_weight"]),
            minsize=int(layout["decision_minsize"]),
            uniform="primary",
        )
        self.main.columnconfigure(2, weight=0, minsize=0)
        self.tree.column(
            "#0",
            width=int(layout["tree_title_width"]),
            minwidth=100 if layout["compact"] else 130,
        )
        self.tree.column(
            "state",
            width=int(layout["tree_state_width"]),
            minwidth=54,
        )
        self.tree.column(
            "source",
            width=int(layout["tree_source_width"]),
            minwidth=60,
        )

        if layout["show_wait_policy"]:
            self.wait_policy_label.grid()
            self.theme_label.grid_configure(column=1, sticky="e")
            self.restart_button.grid_configure(
                row=0,
                column=1,
                rowspan=2,
                sticky="e",
                padx=(12, 0),
                pady=0,
            )
        else:
            self.wait_policy_label.grid_remove()
            self.theme_label.grid_configure(column=0, sticky="w")
            self.restart_button.grid_configure(
                row=2,
                column=0,
                rowspan=1,
                sticky="ew",
                padx=0,
                pady=(8, 0),
            )

        # Query the actual stretched cards, not their minimum widths. This also
        # keeps previews accurate at intermediate sizes and with native fonts.
        self.root.update_idletasks()
        content_width = max(220, width - 32)
        left_wrap = max(190, self.task_panel.winfo_width() - 32)
        decision_wrap = max(190, self.decision_panel.winfo_width() - 32)
        self.header_subtitle.configure(wraplength=content_width)
        self.inventory_label.configure(wraplength=left_wrap)
        restart_wrap = max(120, left_wrap - 24)
        if not layout["compact"]:
            restart_wrap = max(120, restart_wrap - self.restart_button.winfo_reqwidth() - 12)
        self.restart_status_label.configure(wraplength=restart_wrap)
        self.tree_empty_label.configure(wraplength=max(120, self.tree.winfo_width() - 24))
        self.source_label.configure(wraplength=decision_wrap)
        self.action_label.configure(wraplength=decision_wrap)
        self.target_label.configure(wraplength=decision_wrap)
        self.risk_label.configure(wraplength=decision_wrap)
        self.guard_label.configure(wraplength=decision_wrap)
        if self._fit_decision_summary():
            self._content_render_signature = None
        if self._detail_expanded:
            self.detail_panel.grid_configure(
                column=int(layout["detail_column"]),
                columnspan=int(layout["detail_columnspan"]),
            )
            self.detail_panel.tkraise()
        self.theme_label.configure(
            text=self._theme_summary(backdrop_enabled=self._backdrop_enabled)
        )
        self._responsive_layout_signature = signature
        # Recompute the ellipsized task title after the stretched Treeview
        # column settles at its new width.
        self._task_tree_signature = None
        self._apply_safe_minimum_size()
        self._schedule_decision_layout()

    def _set_decision_summary(
        self, source: str, action: str, target: str = "", risk: str = ""
    ) -> None:
        self._summary_values = (source, action, target, risk)
        if self._fit_decision_summary():
            self._apply_safe_minimum_size()

    def _fit_decision_summary(self) -> bool:
        if self._summary_values is None:
            return False
        compact = responsive_window_layout(self.root.winfo_width())["compact"]
        width = max(186, self.decision_panel.winfo_width() - 36)
        labels = (self.source_label, self.action_label, self.target_label, self.risk_label)
        fonts = tuple(self.style.lookup(str(label.cget("style")), "font") for label in labels)
        signature = (width, compact, fonts, self._summary_values)
        if signature == self._summary_signature:
            return False
        self._summary_has_ellipsis = False
        for label, value, font_spec, line_count in zip(
            labels, self._summary_values, fonts, (1, 2, 1 if compact else 2, 2)
        ):
            font = tkfont.Font(root=self.root, font=font_spec)
            preview = fit_summary_lines(
                value, measure=font.measure, width=width, max_lines=line_count
            )
            self._summary_has_ellipsis |= "…" in preview
            label.configure(text=preview, wraplength=width)
            if value:
                label.grid()
            else:
                label.grid_remove()
        self._summary_signature = signature
        return True

    def _set_task_filter(self, value: str) -> None:
        if value not in {"active", "all"}:
            return
        self.task_filter_value.set(value)
        if value == self._task_filter:
            return
        self._task_filter = value
        self.task_section_label.configure(text="任务与请求")
        self._task_tree_signature = None
        self._content_render_signature = None
        self._refresh_task_tree()

    def _position_decision_buttons(self, _event: tk.Event[Any] | None = None) -> None:
        if self.closing:
            return
        width = max(1, self.decision_stage.winfo_width())
        height = max(1, self.decision_stage.winfo_height())
        layout = decision_button_layout(width, height)
        stop_x, stop_y, auxiliary_size = layout["stop"]
        approve_x, approve_y, approve_size = layout["approve"]
        reject_x, reject_y, reject_size = layout["reject"]
        self.approve_button.set_diameter(approve_size)
        self.stop_button.set_diameter(auxiliary_size)
        self.reject_button.set_diameter(reject_size)
        self.approve_button.place(x=approve_x, y=approve_y)
        self.stop_button.place(x=stop_x, y=stop_y)
        self.reject_button.place(x=reject_x, y=reject_y)

    def _schedule_decision_layout(self, _event: tk.Event[Any] | None = None) -> None:
        if self.closing:
            return
        if self._decision_layout_job is not None:
            try:
                self.root.after_cancel(self._decision_layout_job)
            except tk.TclError:
                pass
        try:
            self._decision_layout_job = self.root.after(
                DECISION_LAYOUT_DEBOUNCE_MS,
                self._run_scheduled_decision_layout,
            )
        except tk.TclError:
            self._decision_layout_job = None

    def _run_scheduled_decision_layout(self) -> None:
        self._decision_layout_job = None
        self._position_decision_buttons()

    def _set_detail_expanded(
        self,
        expanded: bool,
        *,
        restore_focus: bool = False,
    ) -> None:
        if expanded == self._detail_expanded:
            return
        self._detail_expanded = expanded
        layout = responsive_window_layout(self.root.winfo_width())
        if expanded:
            self.detail_panel.grid(
                row=0,
                column=int(layout["detail_column"]),
                columnspan=int(layout["detail_columnspan"]),
                sticky="nsew",
            )
            self.detail_panel.tkraise()
            try:
                self.detail_panel.grab_set()
            except tk.TclError:
                pass
            self.detail_toggle_button.configure(text="详细参数 · 已打开")
            self.root.after_idle(self.detail_close_button.focus_set)
        else:
            try:
                if self.root.grab_current() is self.detail_panel:
                    self.detail_panel.grab_release()
            except tk.TclError:
                pass
            self.detail_panel.grid_remove()
            self.detail_toggle_button.configure(text="详细参数 ›")
            if restore_focus:
                self.root.after_idle(self.detail_toggle_button.focus_set)
        self._schedule_hit_shield_sync()
        self._content_render_signature = None

    def toggle_detail(self) -> None:
        if not self._detail_expanded and self.current_request_id is None:
            return
        self._set_detail_expanded(
            not self._detail_expanded,
            restore_focus=self._detail_expanded,
        )
        if self.current_request_id is not None:
            self._render_detail(self.current_request_id)

    def _on_escape(self, _event: tk.Event[Any]) -> str | None:
        if not self._detail_expanded:
            return None
        self._set_detail_expanded(False, restore_focus=True)
        if self.current_request_id is not None:
            self._render_detail(self.current_request_id)
        return "break"

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
            font=("Microsoft YaHei UI", 9),
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
            "ConnectionWaiting.HAR.TLabel",
            background=window_background,
            foreground=palette.muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "ConnectionError.HAR.TLabel",
            background=window_background,
            foreground=palette.danger_text,
            font=("Microsoft YaHei UI", 9, "bold"),
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
            padding=(8, 10, 8, 11),
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
            padding=(12, 8, 12, 9),
            relief=tk.FLAT,
        )
        for button_style, background, foreground in (
            ("Quiet.HAR.TButton", palette.surface_alt, palette.muted),
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
                padding=(8, 8, 8, 9),
                relief=tk.FLAT,
            )
            style.map(
                button_style,
                background=[("pressed", palette.secondary_hover), ("active", palette.secondary_hover)],
                foreground=[("disabled", palette.disabled_text)],
            )
        style.configure(
            "Filter.HAR.TRadiobutton",
            background=palette.surface_alt,
            foreground=palette.muted,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            focuscolor=palette.accent,
            indicatorcolor=palette.surface,
            font=("Microsoft YaHei UI", 9),
            padding=(8, 10, 8, 11),
            relief=tk.FLAT,
        )
        style.map(
            "Filter.HAR.TRadiobutton",
            background=[
                ("pressed", palette.accent_pressed),
                ("selected", palette.selection),
                ("active", palette.accent_hover),
            ],
            foreground=[
                ("disabled", palette.disabled_text),
                ("pressed", palette.accent_text),
                ("selected", palette.selection_text),
                ("active", palette.accent_text),
            ],
            indicatorcolor=[
                ("selected", palette.selection_text),
                ("active", palette.accent_text),
            ],
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
            padding=(12, 8, 12, 9),
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
            foreground=[
                ("disabled", palette.disabled_text),
                ("pressed", palette.accent_text),
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
            rowheight=WINDOW_TOUCH_TARGET,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "HAR.Treeview.Heading",
            background=palette.surface_alt,
            foreground=palette.text,
            bordercolor=palette.border,
            relief=tk.FLAT,
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(8, 10),
        )
        style.map(
            "HAR.Treeview",
            background=[("selected", palette.selection)],
            foreground=[("selected", palette.selection_text)],
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
            arrowsize=WINDOW_TOUCH_TARGET,
            width=WINDOW_TOUCH_TARGET,
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
        if responsive_window_layout(self.root.winfo_width())["compact"]:
            return f"外观 · {preference_label}"
        backdrop_label = (
            f"背景 {round(WINDOW_BACKGROUND_OPACITY * 100)}% / 文字 100%"
            if backdrop_enabled
            else "不透明背景"
        )
        return f"Codex {preference_label} · {backdrop_label}"

    def _apply_diagnostic_theme(self) -> None:
        window = self._diagnostic_window
        text_widget = self._diagnostic_text
        if window is None or text_widget is None:
            return
        try:
            if not window.winfo_exists():
                return
            window.configure(background=self.palette.window)
            text_widget.configure(
                background=self.palette.code_background,
                foreground=self.palette.text,
                insertbackground=self.palette.text,
                selectbackground=self.palette.selection,
                selectforeground=self.palette.selection_text,
                highlightbackground=self.palette.border,
                highlightcolor=self.palette.accent,
            )
        except tk.TclError:
            pass

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
        self.stop_button.apply_theme(
            self.palette,
            parent_background=self.palette.surface,
        )
        self.reject_button.apply_theme(
            self.palette,
            parent_background=self.palette.surface,
        )
        self._apply_diagnostic_theme()
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
            selectforeground=palette.selection_text,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
        )
        self.approve_button.apply_theme(
            palette,
            parent_background=palette.surface,
        )
        self.stop_button.apply_theme(
            palette,
            parent_background=palette.surface,
        )
        self.reject_button.apply_theme(
            palette,
            parent_background=palette.surface,
        )
        self._apply_diagnostic_theme()
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
        try:
            tree_title_width = int(self.tree.column("#0", "width"))
        except (tk.TclError, TypeError, ValueError):
            tree_title_width = 120
        # Treeview reserves leading space for its native tree affordance. Seven
        # pixels per display unit is a conservative YaHei UI approximation.
        title_units = max(8, (tree_title_width - 22) // 7)
        entries: list[tuple[str, str, tuple[str, str], str]] = []
        for row in self._request_rows.values():
            if self._task_filter == "active" and row["state"] not in ACTIVE_STATES:
                continue
            entries.append(
                (
                    self._request_item_id(str(row["request_id"])),
                    ellipsize_display_text(
                        row.get("exact_action"),
                        fallback="授权请求",
                        max_units=title_units,
                    ),
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
                    ellipsize_display_text(
                        task.title,
                        fallback="未命名 Codex 任务",
                        max_units=title_units,
                    ),
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

        entries_signature = tuple(entries)
        empty_text = (
            "当前筛选无结果\n切换“全部”查看历史"
            if self._task_filter == "active" and (self._request_rows or self._codex_tasks)
            else "暂无任务或授权请求\n收到请求后会出现在这里"
        )
        if entries:
            if self._tree_empty_text is not None:
                self.tree_empty_label.place_forget()
                self._tree_empty_text = None
        elif self._tree_empty_text != empty_text:
            self.tree_empty_label.configure(text=empty_text)
            self.tree_empty_label.place(in_=self.tree, relx=0.5, rely=0.5, anchor=tk.CENTER)
            self._tree_empty_text = empty_text
        if entries_signature == self._task_tree_signature:
            return

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
            if self.tree.selection() != (selected_before,):
                self.tree.selection_set(selected_before)
            if self.tree.focus() != selected_before:
                self.tree.focus(selected_before)
        self._task_tree_signature = entries_signature

    def _select_item(self, item_id: str) -> None:
        if not self.tree.exists(item_id):
            return
        if self.tree.selection() != (item_id,):
            self.tree.selection_set(item_id)
        if self.tree.focus() != item_id:
            self.tree.focus(item_id)
        if self.current_item_id == item_id:
            return
        self.tree.see(item_id)
        self.current_item_id = item_id
        if item_id.startswith("request:"):
            request_id = item_id.removeprefix("request:")
            self.current_request_id = request_id
            self._render_detail(request_id)
        elif item_id.startswith("codex:"):
            self.current_request_id = None
            self._render_codex_task(item_id.removeprefix("codex:"))

    def _set_decision_mode(self, mode: str) -> None:
        # Stable controls preserve the approval UI's spatial memory. Renderers
        # keep them disabled unless a pending frozen request is selected.
        if mode in {"empty", "task"}:
            self.current_request_id = None
            for button in (self.stop_button, self.approve_button, self.reject_button):
                button.set_state(tk.DISABLED)
        self.decision_stage.grid()
        self._schedule_decision_layout()
        if mode == "task":
            if self._detail_expanded:
                self._set_detail_expanded(False)
            self.decision_heading_label.configure(text="任务详情")
            self.guard_label.configure(text="仅用于安全重启判断，不会执行操作")
            return
        if mode == "empty":
            if self._detail_expanded:
                self._set_detail_expanded(False)
            self.decision_heading_label.configure(text="等待授权请求")
            self.guard_label.configure(text="收到请求后会显示可核对的单次动作")
            return
        self.decision_heading_label.configure(text="授权确认")
        self.guard_label.configure(text="仅授权当前冻结请求")

    def _render_empty_state(self) -> None:
        has_hidden_items = bool(self._request_rows or self._codex_tasks)
        render_signature = (
            "empty",
            self._task_filter,
            has_hidden_items,
            self._inventory_available,
            self._inventory_error_code,
        )
        if render_signature == self._content_render_signature:
            return
        self.current_request_id = None
        self._set_decision_mode("empty")
        source_text, action_text, state_text, empty_detail = empty_state_copy(
            task_filter=self._task_filter,
            has_hidden_items=has_hidden_items,
            inventory_available=self._inventory_available,
            inventory_error_code=self._inventory_error_code,
        )
        self._set_decision_summary(source_text, action_text)
        self.decision_state_label.configure(text=state_text)
        self.detail_toggle_button.configure(state=tk.DISABLED, text="详细参数 ›")
        self.approve_button.set_state(tk.DISABLED)
        self.stop_button.set_state(tk.DISABLED)
        self.reject_button.set_state(tk.DISABLED)
        self._detail_request_id = None
        self._set_detail(empty_detail, reset_scroll=True)
        self._content_render_signature = render_signature

    def _render_codex_task(self, task_id: str) -> None:
        task = self._codex_tasks.get(task_id)
        if task is None:
            self._render_empty_state()
            return
        render_signature = (
            "task",
            task.task_id,
            task.title,
            task.status,
            task.cwd,
            task.kind,
            task.updated_at,
        )
        if render_signature == self._content_render_signature:
            return
        self._set_decision_mode("task")
        self._set_decision_summary(
            "只读 Codex 任务", task.title,
            f"项目 · {self._compact_source(task.cwd)}",
            "这里只显示任务状态，不会授权或触发任何操作。",
        )
        self.decision_state_label.configure(
            text=f"状态 · {self._codex_state_label(task.status)}"
        )
        self.detail_toggle_button.configure(state=tk.DISABLED, text="详细参数 ›")
        self.approve_button.set_state(tk.DISABLED)
        self.stop_button.set_state(tk.DISABLED)
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
        self._content_render_signature = render_signature

    def _update_connection_status(self) -> None:
        try:
            server_open = self.server.socket.fileno() >= 0
        except (AttributeError, OSError):
            server_open = False
        connector_seen = self.server.connector_snapshot() is not None
        render_signature = (server_open, connector_seen)
        if render_signature == self._connection_render_signature:
            return
        style_name, status_text = relay_connection_summary(
            server_open=server_open,
            connector_seen=connector_seen,
        )
        self.connection_label.configure(text=status_text, style=style_name)
        self._connection_render_signature = render_signature

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
        inventory_fresh = inventory.is_fresh()
        restart_pending = self.store.restart_is_pending()
        render_signature = (
            inventory.available,
            inventory.error_code,
            inventory_fresh,
            tuple((task.task_id, task.status, task.updated_at) for task in inventory.tasks),
            pending_count,
            running_count,
            self._restart_in_progress,
            restart_pending,
        )
        if render_signature == self._inventory_render_signature:
            return
        if inventory.available:
            self.inventory_label.configure(
                text=f"Codex 进行中 {active_codex} · 授权待确认 {pending_count} · 执行中 {running_count}"
            )
        elif inventory.error_code == "awaiting_thread_metadata":
            self.inventory_label.configure(
                text="等待任一 Codex 任务连接金手指 · 收到请求后会自动提醒"
            )
        else:
            reason = inventory.error_code or "unknown"
            self.inventory_label.configure(
                text=f"Codex 任务状态暂不可用（{reason}）· 已禁止重启 · 请打开诊断"
            )

        enabled = False
        if self._restart_in_progress or restart_pending:
            restart_text = "正在执行安全重启…"
        elif not inventory.available:
            if inventory.error_code == "awaiting_thread_metadata":
                restart_text = "等待 Codex 任务连接，保持禁用"
            else:
                restart_text = "任务清单不可用；打开诊断后重试"
        elif not inventory_fresh:
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
        self._inventory_render_signature = render_signature

    def refresh(self) -> None:
        if self.closing:
            return
        self._refresh_theme()
        self._update_connection_status()
        self.task_monitor.register_thread_ids(self.server.connector_thread_ids())
        inventory = self.task_monitor.snapshot()
        self._inventory_available = inventory.available
        self._inventory_error_code = inventory.error_code
        (
            store_revision,
            changed_rows,
            pending_count,
            running_count,
        ) = self.store.gui_snapshot(self._store_gui_revision)
        self._store_gui_revision = store_revision
        if changed_rows is not None:
            rows = changed_rows
            incoming_request_ids = {str(row["request_id"]) for row in rows}
            new_request_ids = incoming_request_ids - self._known_request_ids
            self._known_request_ids = incoming_request_ids
            self._request_rows = {str(row["request_id"]): row for row in rows}
        else:
            rows = list(self._request_rows.values())
            new_request_ids = set()
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

        self._set_pending_attention(pending_count > 0)
        self._update_inventory_and_restart(
            inventory,
            pending_count=pending_count,
            running_count=running_count,
        )
        status_text = (
            f"待确认 {pending_count} · 执行中 {running_count} · "
            f"本地端口 {self.server.server_address[1]}"
        )
        if status_text != self._status_text:
            self.status_label.configure(text=status_text)
            self._status_text = status_text
        self.root.after(250, self.refresh)

    def on_select(self, _event: tk.Event[Any]) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        item_id = str(selected[0])
        if item_id == self.current_item_id:
            return
        self._select_item(item_id)

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
        remaining_seconds: int | None = None
        if detail["state"] == "pending":
            try:
                expiry = datetime.fromisoformat(
                    str(detail["expires_at"]).replace("Z", "+00:00")
                )
                remaining_seconds = max(0, int((expiry - utc_now()).total_seconds()))
            except (TypeError, ValueError):
                pass
        render_signature = (
            "request",
            request_id,
            detail["request_hash"],
            detail["state"],
            detail["updated_at"],
            poll_policy["pending_polls_used"],
            remaining_seconds,
            self._detail_expanded,
        )
        if render_signature == self._content_render_signature:
            return
        self._set_decision_mode("request")
        source_project = self._one_line(
            frozen.get("source_project"), fallback="未标注项目", limit=46
        )
        source_task = self._one_line(
            frozen.get("source_task"), fallback="未标注任务", limit=46
        )
        self._set_decision_summary(
            f"{source_project} · {source_task}",
            self._one_line(frozen.get("exact_action"), fallback="授权请求", limit=140),
            "目标 · " + self._one_line(frozen.get("target"), fallback="未标注", limit=120),
            "风险 · " + self._one_line(frozen.get("risk"), fallback="未标注", limit=110),
        )
        parameter_count = 6
        if frozen.get("kind") == "command":
            arguments = frozen.get("arguments")
            parameter_count = 8 + (len(arguments) if isinstance(arguments, list) else 0)
        marker = "‹" if self._detail_expanded else "›"
        preview_hint = " · 含省略" if self._summary_has_ellipsis else ""
        self.detail_toggle_button.configure(
            state=tk.NORMAL,
            text=f"详细参数 {parameter_count}项{preview_hint} {marker}",
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
        self.stop_button.set_state(tk.NORMAL if pending else tk.DISABLED)
        self.reject_button.set_state(tk.NORMAL if pending else tk.DISABLED)
        state_text = self._state_label(str(detail["state"]))
        if pending:
            if remaining_seconds is not None:
                minutes, seconds = divmod(remaining_seconds, 60)
                state_text = (
                    f"待确认 · 剩余 {minutes:02d}:{seconds:02d} · "
                    f"轮询 {poll_policy['pending_polls_used']}/{poll_policy['max_pending_polls']}"
                )
            else:
                state_text = "待确认"
        elif detail["state"] == "running":
            state_text = "命令执行中 · 请勿关闭窗口"
        elif (
            detail["state"] == "cancelled"
            and isinstance(detail.get("result"), dict)
            and detail["result"].get("reason_code") == "stopped_by_user"
        ):
            state_text = "已停止 · 未同意，也未拒绝"
        self.decision_state_label.configure(text=state_text)
        self._content_render_signature = render_signature

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

    def stop_selected(self) -> None:
        request_id = self.current_request_id
        if not request_id:
            return
        try:
            self.store.cancel_from_gui(request_id)
        except RelayError as exc:
            messagebox.showerror("无法停止", exc.message, parent=self.root)
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

    def open_computer_use_guidance(self) -> None:
        try:
            guidance = resolve_computer_use_guidance()
        except OSError as exc:
            messagebox.showerror("无法找到 Computer Use 指南", str(exc), parent=self.root)
            return
        try:
            # Default ShellExecute verb honors the user's .md file association.
            os.startfile(str(guidance))
        except OSError as exc:
            messagebox.showerror(
                "无法打开 Computer Use 指南",
                f"文件：{guidance}\n\n"
                "请检查文件权限，并为 .md 文件设置默认打开软件后重试。\n\n"
                f"系统提示：{exc}",
                parent=self.root,
            )

    def show_diagnostics(self) -> None:
        existing = self._diagnostic_window
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    if self._diagnostic_text is not None:
                        self._diagnostic_text.focus_set()
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
        self._diagnostic_text = text_widget
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

        copy_button = ttk.Button(
            actions,
            text="复制诊断",
            command=copy_snapshot,
            style="Diag.HAR.TButton",
        )
        copy_button.grid(row=0, column=1, padx=(0, 8))

        def close_diagnostics(_event: tk.Event[Any] | None = None) -> str:
            window.destroy()
            return "break"

        close_button = ttk.Button(
            actions,
            text="关闭",
            command=close_diagnostics,
            style="Diag.HAR.TButton",
        )
        close_button.grid(row=0, column=2)

        def forget_window(event: tk.Event[Any] | None = None) -> None:
            if event is not None and event.widget is not window:
                return
            if self._diagnostic_window is window:
                self._diagnostic_window = None
                self._diagnostic_text = None
                if not self.closing:
                    try:
                        self.diagnostics_button.focus_set()
                    except tk.TclError:
                        pass

        window.bind("<Destroy>", forget_window, add="+")
        window.bind("<Escape>", close_diagnostics)
        window.protocol("WM_DELETE_WINDOW", close_diagnostics)
        window.lift()
        window.after_idle(copy_button.focus_set)

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
            self._hit_shield_sync_job,
            self._activation_job,
            self._decision_layout_job,
            self._responsive_layout_job,
        ):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        try:
            if self.root.grab_current() is self.detail_panel:
                self.detail_panel.grab_release()
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
