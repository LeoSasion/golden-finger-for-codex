"""Side-effect-free source validation for the relay prototype."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from release_metadata import VERSION


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PYTHON_SOURCES = tuple(
    sorted(
        list((PLUGIN_ROOT / "scripts").glob("*.py"))
        + list((PLUGIN_ROOT / "scripts").glob("*.pyw"))
    )
)


class ValidationFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def load_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailure(f"{path} is not UTF-8: {exc}") from exc


def parse_json(path: Path) -> dict:
    try:
        value = json.loads(load_utf8(path))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"{path} is invalid JSON: {exc}") from exc
    require(isinstance(value, dict), f"{path} must contain a JSON object")
    return value


def validate_skill_metadata() -> None:
    skill_path = PLUGIN_ROOT / "skills" / "golden-finger" / "SKILL.md"
    source = load_utf8(skill_path)
    parts = source.split("---", 2)
    require(len(parts) == 3 and not parts[0].strip(), "SKILL frontmatter is missing")
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            value = json.loads(raw_value) if raw_value.startswith('"') else raw_value
        except json.JSONDecodeError as exc:
            raise ValidationFailure(f"SKILL frontmatter value {key!r} is invalid") from exc
        require(isinstance(value, str), f"SKILL frontmatter value {key!r} must be text")
        metadata[key] = value
    require(metadata.get("name") == "golden-finger", "wrong Skill name")
    description = metadata.get("description", "")
    require(
        "semantic yes/no" in description
        and "single-select" in description
        and "source parameter types" in description,
        "Skill must route semantic questions independently of source types",
    )
    require(
        "first original option" in description
        and "native Codex approvals" in description
        and "free-text questions" in description,
        "Skill decision mapping and native/free-text boundaries are missing",
    )
    require(
        "request_decision" in source
        and "references/decision-mapping.md" in source
        and (PLUGIN_ROOT / "skills/golden-finger/references/decision-mapping.md").is_file(),
        "Skill decision tool guidance is missing",
    )
    require("same-turn polling until terminal" in description, "Skill polling trigger is missing")
    require(
        "agree a routing scope" in description
        and "scope never authorizes actions" in description
        and "cancel the scope" in description,
        "Skill first-use scope and recovery trigger is incomplete",
    )
    require(
        "codex app-server generate-json-schema --out protocol" in source,
        "Skill is missing the version-matched Codex protocol schema command",
    )
    require(
        "This command is not eligible for `request_command_execution`" in source,
        "Skill is missing the Codex schema execution boundary",
    )
    require(
        all(field in source for field in ("source_label", "source_project", "source_task")),
        "Skill is missing cross-project source evidence guidance",
    )
    require("[TODO" not in source, "SKILL contains an unfinished placeholder")

    openai_yaml = load_utf8(
        PLUGIN_ROOT
        / "skills"
        / "golden-finger"
        / "agents"
        / "openai.yaml"
    )
    require('display_name: "Codex金手指"' in openai_yaml, "Skill display name drifted")
    require("Yes工程师" in openai_yaml, "Skill search phrase is missing")
    require("首次采用先约定使用作用域" in openai_yaml, "Skill scope prompt is missing")
    require("pending/running" in openai_yaml, "Skill default prompt lacks active polling")
    require(
        "allow_implicit_invocation: true" in openai_yaml,
        "Skill implicit invocation must remain enabled",
    )


def validate_manifest() -> None:
    manifest = parse_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    require(manifest.get("name") == "golden-finger", "wrong plugin name")
    version = manifest.get("version")
    require(
        isinstance(version, str)
        and (version == VERSION or version.startswith(VERSION + "+codex.")),
        "unexpected plugin version",
    )
    require(manifest.get("skills") == "./skills/", "skills path is missing")
    require(manifest.get("mcpServers") == "./.mcp.json", "MCP path is missing")
    interface = manifest.get("interface")
    require(isinstance(interface, dict), "plugin interface is missing")
    require(interface.get("displayName") == "Codex金手指", "product display name drifted")
    require(
        "人机授权中继" in interface.get("shortDescription", "")
        and "Yes工程师" in interface.get("shortDescription", ""),
        "searchable functional description drifted",
    )
    require(
        "Golden Finger for Codex" in interface.get("longDescription", ""),
        "English product name drifted",
    )
    default_prompts = interface.get("defaultPrompt")
    require(
        isinstance(default_prompts, list)
        and 1 <= len(default_prompts) <= 3
        and all(
            isinstance(prompt, str) and 0 < len(prompt) <= 128
            for prompt in default_prompts
        ),
        "interface.defaultPrompt must contain 1-3 non-empty strings of at most 128 characters",
    )

    mcp = parse_json(PLUGIN_ROOT / ".mcp.json")
    servers = mcp.get("mcpServers")
    require(isinstance(servers, dict), "mcpServers is missing")
    server = servers.get("golden_finger")
    require(isinstance(server, dict), "relay MCP server is missing")
    require(server.get("command") == "python", "MCP must use the Python connector")
    args = server.get("args")
    require(
        isinstance(args, list) and "./scripts/relay_mcp_server.py" in args,
        "MCP must launch only the connector, never the broker",
    )
    require(
        all("relay_broker" not in str(argument) for argument in args),
        "MCP configuration must not auto-launch the broker",
    )
    require(
        server.get("tool_timeout_sec") == 930,
        "MCP tool timeout must cover one 900-second broker wait",
    )


def string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def validate_python_sources() -> None:
    trees: dict[str, ast.Module] = {}
    for path in PYTHON_SOURCES:
        source = load_utf8(path)
        try:
            trees[path.name] = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ValidationFailure(f"{path} has invalid Python syntax: {exc}") from exc

    broker_tree = trees["relay_broker.py"]
    broker_attributes = {
        node.attr for node in ast.walk(broker_tree) if isinstance(node, ast.Attribute)
    }
    require("yview_moveto" in broker_attributes, "detail refresh must preserve scroll position")
    require("yview_scroll" in broker_attributes, "detail pane must support mouse-wheel scrolling")
    require("see" in broker_attributes, "newest request must be scrolled into view")
    require("move" in broker_attributes, "request rows must remain newest-first")
    operations: set[str] = set()
    for node in ast.walk(broker_tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left = node.left
        comparator = node.comparators[0]
        if (
            isinstance(left, ast.Name)
            and left.id == "operation"
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(comparator, ast.Constant)
            and isinstance(comparator.value, str)
        ):
            operations.add(comparator.value)
    require(
        operations == {"health", "submit", "status", "wait", "cancel", "terminate", "recover", "preflight", "read_output"},
        f"broker transport operation set drifted: {sorted(operations)}",
    )
    require("approve" not in operations, "transport must never expose approve")

    broker_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_broker.py")
    for appearance_marker in (
        "BROKER_VERSION = PACKAGE_VERSION",
        'root.title("Codex金手指")',
        "WINDOW_BACKGROUND_OPACITY = 0.60",
        "WINDOW_HIT_SHIELD_OPACITY = 0.01",
        'desktop.get("appearanceTheme")',
        "SetWindowCompositionAttribute",
        "safe_transparent_client",
        "place_native_window_behind",
        '"<Configure>"',
        'attributes("-transparentcolor", WINDOW_TRANSPARENT_COLOR)',
        'attributes("-topmost", active)',
        "SetForegroundWindow",
        "if enabled == self._enabled:",
        "diameter=170",
        '"expiry_basis": "monotonic_deadline"',
        "winsound.MessageBeep",
        "new_request_ids",
        'UI_SETTINGS_FILENAME = "ui-settings.json"',
        "WINDOW_MIN_WIDTH = 920",
        "WINDOW_SNAP_MIN_WIDTH = 640",
        "WINDOW_COMPACT_BREAKPOINT = 820",
        "WINDOW_TOUCH_TARGET = 40",
        "WINDOW_MIN_HEIGHT = 600",
        "WINDOW_MIN_CONTENT_HEIGHT = 260",
        'root.rowconfigure(1, weight=1, minsize=WINDOW_MIN_CONTENT_HEIGHT)',
        'main.grid(row=1, column=0, sticky="nsew"',
        "class CircularButton(tk.Button)",
        "_sync_native_accessible_name",
        "EnableWindow",
        "NotifyWinEvent",
        "SetWindowSubclass",
        "BUTTON_NATIVE_ACTIVE_DRAIN_MS = 100",
        "BUTTON_NATIVE_IDLE_DRAIN_MS = 500",
        "antialiased_circle_coverage",
        "circular_button_visual_state",
        "decision_button_layout",
        "contrast_ratio",
        "DECISION_LAYOUT_DEBOUNCE_MS = 80",
        "tk.PhotoImage",
        "Image.Resampling.LANCZOS",
        "ImageFilter.GaussianBlur",
        'self.bind("<Motion>", self._on_motion)',
        'text="任务与请求"',
        "Filter.HAR.TRadiobutton",
        'text="安全重启 Codex"',
        "detail_panel.grid_remove()",
        "responsive_window_layout",
        "relay_connection_summary",
        "empty_state_copy",
        'columnspan=int(layout["detail_columnspan"])',
        "RESPONSIVE_LAYOUT_DEBOUNCE_MS = 80",
        "GUI_TERMINAL_HISTORY_LIMIT = 200",
        "def gui_snapshot(",
        "_next_expiry_check_monotonic",
        "self.approve_button.place",
        "self.stop_button.place",
        "if item_id == self.current_item_id:",
        "CodexTaskMonitor",
        "try_begin_codex_restart",
        "graceful_restart_codex",
        'task.status == "systemError"',
        "_apply_safe_minimum_size",
        "winfo_reqheight",
        "获批命令将继承此权限",
        "command_timeout_seconds = pending_wait_budget_seconds",
        "cancel_from_gui",
        '"reason_code": "stopped_by_user"',
        "_get_claimed_locked",
        "claim_fingerprint",
        "secrets.compare_digest",
        "dict[tuple[str, str], str]",
        "verify_script_dependencies",
        "collect_install_diagnostics",
        "format_install_diagnostics",
        'text="接入检查"',
        "--self-test-output",
        "connector_fingerprint",
    ):
        require(
            appearance_marker in broker_source,
            f"appearance/attention contract is missing {appearance_marker!r}",
        )
    require('text="保存设置"' not in broker_source, "manual policy save button returned")
    execution_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_execution.py")
    for marker in ("class WindowsJob", "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", "CREATE_SUSPENDED", "resume_initial_thread"):
        require(marker in execution_source, f"contained execution is missing {marker!r}")
    require(
        "permission_label" not in broker_source,
        "the redundant privilege banner returned",
    )
    require(
        "警告：窗口当前以管理员权限运行" not in broker_source,
        "the removed administrator warning returned",
    )
    lifecycle_source = load_utf8(PLUGIN_ROOT / "scripts" / "codex_lifecycle.py")
    for lifecycle_marker in (
        "APP_TOOLS_SCHEMA_VERSION = 4",
        'tool.get("name") == "list_threads"',
        'payload.get("pinnedThreads")',
        '"inventory_truncated"',
        "GetNamedPipeServerProcessId",
        "is_codex_desktop_executable(server_executable)",
        "def is_fresh(",
        "PostMessageW(target.hwnd, 0x0010, 0, 0)",
        "ShellExecuteW",
        "未强制结束进程",
    ):
        require(
            lifecycle_marker in lifecycle_source,
            f"Codex lifecycle contract is missing {lifecycle_marker!r}",
        )
    lifecycle_lower = lifecycle_source.casefold()
    require("sqlite" not in lifecycle_lower, "lifecycle code must not read Codex databases")
    require("terminateprocess" not in lifecycle_lower, "Codex restart must never force-kill")
    mcp_source_for_lifecycle = load_utf8(
        PLUGIN_ROOT / "scripts" / "relay_mcp_server.py"
    )
    require(
        "extract_codex_thread_id(" in mcp_source_for_lifecycle
        and 'params.get("_meta")' in mcp_source_for_lifecycle
        and 'os.environ.get("CODEX_THREAD_ID")' in mcp_source_for_lifecycle,
        "MCP connector must pass host thread metadata to the lifecycle monitor",
    )
    render_detail = next(
        (
            node
            for node in ast.walk(broker_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_render_detail"
        ),
        None,
    )
    require(render_detail is not None, "GUI detail renderer is missing")
    render_detail_source = ast.get_source_segment(broker_source, render_detail) or ""
    require(
        "claim_fingerprint" in render_detail_source,
        "GUI must display the non-secret claim fingerprint",
    )
    require(
        "claim_token" not in render_detail_source
        and "client_instance_id" not in render_detail_source,
        "GUI must never display request claim capabilities or connector IDs",
    )

    popen_calls = [
        node
        for node in ast.walk(trees["relay_execution.py"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    require(len(popen_calls) == 1, "execution module must have exactly one reviewed Popen site")
    shell_keywords = [
        keyword for keyword in popen_calls[0].keywords if keyword.arg == "shell"
    ]
    require(len(shell_keywords) == 1, "Popen must set shell explicitly")
    require(
        isinstance(shell_keywords[0].value, ast.Constant)
        and shell_keywords[0].value.value is False,
        "Popen shell must be False",
    )

    launcher_tree = trees["launch_relay.pyw"]
    launcher_imports = {
        node.module for node in ast.walk(launcher_tree) if isinstance(node, ast.ImportFrom)
    }
    require(
        "relay_broker" in launcher_imports,
        "manual launcher must enter through relay_broker.main",
    )
    launcher_attributes = {
        node.attr for node in ast.walk(launcher_tree) if isinstance(node, ast.Attribute)
    }
    require(
        "showerror" in launcher_attributes,
        "manual launcher must surface startup errors without a console",
    )
    for forbidden_attribute in ("Popen", "run", "startfile"):
        require(
            forbidden_attribute not in launcher_attributes,
            f"manual launcher must not use {forbidden_attribute}",
        )
    launcher_names = {
        node.id for node in ast.walk(launcher_tree) if isinstance(node, ast.Name)
    }
    require("open" not in launcher_names, "manual launcher must not write launcher state")

    protocol_constants = string_constants(trees["relay_protocol.py"])
    for required_fragment in (
        "app-server",
        "state_5.sqlite",
        "session_index.jsonl",
        "relay_broker.py",
        "relay_mcp_server.py",
        "--dangerously-bypass-approvals-and-sandbox",
    ):
        require(
            required_fragment in protocol_constants,
            f"protected boundary is missing {required_fragment!r}",
        )

    mcp_constants = string_constants(trees["relay_mcp_server.py"])
    for tool_name in (
        "relay_health",
        "request_consent",
        "request_command_execution",
        "wait_for_authorization",
        "cancel_authorization",
        "terminate_command",
        "preflight_command",
        "read_command_output",
        "recover_authorization",
    ):
        require(tool_name in mcp_constants, f"MCP tool {tool_name!r} is missing")
    require("approve_authorization" not in mcp_constants, "MCP exposes an approval tool")
    for polling_marker in (
        "must_continue_current_turn",
        "do_not_wait_for_user_message",
        "wait_for_authorization",
        "remaining_seconds",
        "claim_token",
        "notifications/cancelled",
    ):
        require(
            polling_marker in mcp_constants,
            f"MCP polling directive is missing {polling_marker!r}",
        )
    mcp_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_mcp_server.py")
    require(
        "SERVER_VERSION = PACKAGE_VERSION" in mcp_source,
        "MCP server version must share the package version constant",
    )
    require(
        "CLIENT_INSTANCE_ID = new_client_instance_id()" in mcp_source,
        "MCP connector instance namespace is missing",
    )
    require(
        "polling.next_arguments" in mcp_source,
        "MCP continuation must direct Codex to the broker-owned next arguments",
    )
    require(
        all(field in mcp_source for field in ('"source_label"', '"source_project"', '"source_task"')),
        "MCP request schemas are missing source evidence fields",
    )
    for scope_marker in (
        "routing_preference_only",
        "start_window",
        "cancel_scope",
        "background_monitoring_supported",
        "relay_window_closed",
        "broker_stopping",
    ):
        require(
            scope_marker in mcp_constants,
            f"MCP scope recovery is missing {scope_marker!r}",
        )
    require(
        '"reason_code": "relay_window_closed"' in broker_source,
        "graceful window shutdown lacks a structured reason",
    )
    require(
        "if server.store.is_stopping()" in broker_source,
        "relay_health can falsely report healthy while the Broker is stopping",
    )

    protocol_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_protocol.py")
    require("PROTOCOL_VERSION = 8" in protocol_source, "relay protocol version must be 8")
    require(
        'PACKAGE_VERSION = VERSION' in protocol_source,
        "package version drifted",
    )
    for protocol_marker in (
        'PACKAGE_BUILD_ID = "source"',
        "RUNTIME_BUILD_ID = runtime_build_id()",
        '"client_version": PACKAGE_VERSION',
        '"client_build_id": RUNTIME_BUILD_ID',
        "freeze_script_dependencies",
        "verify_script_dependencies",
        '"source_project"',
        '"source_task"',
    ):
        require(
            protocol_marker in protocol_source,
            f"protocol contract is missing {protocol_marker!r}",
        )
    require(
        'REQUEST_ID_PREFIX = "gfr_"' in protocol_source
        and 'CLAIM_TOKEN_PREFIX = "gfcap_"' in protocol_source,
        "request claim prefixes drifted",
    )
    require(
        "har_" not in broker_source and "har_" not in mcp_source,
        "legacy request ID prefix returned",
    )
    require(
        "DEFAULT_APPROVAL_EXPIRY_SECONDS = 9_000" in protocol_source,
        "approval validity must default to 9,000 seconds",
    )
    require(
        "DEFAULT_COMMAND_TIMEOUT_SECONDS = 9_000" in protocol_source,
        "command timeout must default to 9,000 seconds",
    )
    require(
        "MAX_APPROVAL_EXPIRY_SECONDS = 36_000" in protocol_source
        and "MAX_COMMAND_TIMEOUT_SECONDS = 36_000" in protocol_source,
        "window-configured pending and command bounds must stay capped at 10 hours",
    )
    require(
        "MIN_POLL_WAIT_SECONDS = 900" in protocol_source
        and "DEFAULT_POLL_WAIT_SECONDS = 900" in protocol_source
        and "MAX_POLL_WAIT_SECONDS = 900" in protocol_source
        and "MIN_PENDING_POLL_CYCLES = 10" in protocol_source
        and "DEFAULT_PENDING_POLL_CYCLES = 10" in protocol_source
        and "MAX_PENDING_POLL_CYCLES = 10" in protocol_source,
        "polling policy must remain fixed at 900 seconds x 10 polls",
    )
    require(
        "min(float(MAX_POLL_WAIT_SECONDS), float(wait_seconds)) + 5.0" in protocol_source,
        "relay client socket timeout must cover the requested bounded wait",
    )
    require(
        "MIN_APPROVAL_EXPIRY_SECONDS = 10" in protocol_source,
        "derived approval lifetime must allow the 10-second minimum product",
    )

    readme = load_utf8(PLUGIN_ROOT / "README.md")
    hero_position = readme.find("assets/readme-hero.png")
    title_position = readme.find("# Codex金手指")
    require(
        readme.startswith("![")
        and 0 <= hero_position < title_position,
        "README must retain the fixed hero before the project title",
    )
    require(
        0 <= readme.find("```text") < readme.find("## 少打几个"),
        "README must lead with the copyable Agent installation prompt",
    )
    for marker in ("GoldenFingerSetup.exe", "SHA-256", "docs/LOCAL-GUIDE.md", "docs/TECHNICAL-REFERENCE.md"):
        require(marker in readme, f"README installation guidance is missing {marker!r}")
    technical_guide = load_utf8(PLUGIN_ROOT / "docs" / "TECHNICAL-REFERENCE.md")
    for launcher_marker in (
        "launch_relay.pyw",
        "pythonw.exe",
        "不会自动启动",
        "版本化缓存目录",
    ):
        require(
            launcher_marker in technical_guide,
            f"Technical launcher guidance is missing {launcher_marker!r}",
        )
    for ui_marker in (
        "appearanceTheme",
        "60%",
        "文字 100%",
        "自动选中",
        "新请求提示音",
        "等待策略",
        "不会穿透",
        "900 秒 × 10 次",
        "停止",
        "920 × 600",
        "640 × 600",
        "三列",
        "覆盖层",
        "UI Automation",
        "大圆形",
        "安全重启 Codex",
        "list_threads",
        "权限横幅已移除",
        "claim_token",
        "认领指纹",
        "协议版本 8",
        "Job Object",
        "直接命名",
        "右上角“接入检查”",
        "精确 build ID",
        "自包含 Windows 发行包",
        "使用作用域与窗口生命周期",
        "启动金手指窗口并继续作用域",
        "取消金手指作用域",
        "不能在 Codex 回合结束后后台监测",
    ):
        require(ui_marker in technical_guide, f"Technical UI guidance is missing {ui_marker!r}")

    build_source = load_utf8(PLUGIN_ROOT / "scripts" / "build_windows_release.py")
    verify_source = load_utf8(PLUGIN_ROOT / "scripts" / "verify_windows_release.py")
    workflow = load_utf8(
        PLUGIN_ROOT / ".github" / "workflows" / "windows-release.yml"
    )
    for marker in (
        "PyInstaller",
        "GoldenFinger",
        "golden-finger-mcp",
        "BUILD-ID.txt",
        "build-manifest.json",
        "ignore_python_cache",
        "__pycache__",
        ".pyc",
        "COLLECT(",
        '"PIL.AvifImagePlugin"',
        '"PIL.WebPImagePlugin"',
        '"pyinstaller-shared-onedir"',
        '"golden-finger.runtime"',
    ):
        require(marker in build_source, f"Windows release builder is missing {marker!r}")
    require('"--onefile"' not in build_source, "Windows release builder reverted to onefile")
    for marker in (
        "sha256_file",
        "initialize",
        "--self-test-output",
        "job_object_active",
        '"pyinstaller-shared-onedir"',
        '"golden-finger.runtime"',
    ):
        require(marker in verify_source, f"Windows release verifier is missing {marker!r}")
    require(
        "windows-latest" in workflow
        and "build_windows_release.py" in workflow
        and "verify_windows_release.py" in workflow,
        "clean Windows release workflow is incomplete",
    )

    scope_reference = load_utf8(
        PLUGIN_ROOT
        / "skills"
        / "golden-finger"
        / "references"
        / "scope-lifecycle.md"
    )
    for scope_marker in (
        "once",
        "task",
        "project",
        "global",
        "golden-finger-scope:start",
        "AGENTS.override.md",
        "personal/global Codex plugin environment",
        "scope_recovery.must_ask_user=true",
        "cannot resume an old in-memory request",
    ):
        require(
            scope_marker in scope_reference,
            f"scope lifecycle reference is missing {scope_marker!r}",
        )


def validate_utf8_tree() -> None:
    for path in PLUGIN_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".invalid-encoding"):
            continue
        if path.suffix.casefold() in {
            ".json",
            ".md",
            ".py",
            ".pyw",
            ".yaml",
            ".yml",
        }:
            load_utf8(path)


def main() -> int:
    try:
        validate_utf8_tree()
        validate_skill_metadata()
        validate_manifest()
        validate_python_sources()
    except ValidationFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "PASS: manifest, Skill metadata, UTF-8, Python syntax, polling contract, "
        "window-policy sync, request claiming, scope recovery, transport boundary, appearance/attention contract, "
        "manual launcher, and shell=False"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
