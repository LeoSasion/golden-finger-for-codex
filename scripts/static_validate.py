"""Side-effect-free source validation for the relay prototype."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys


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
    require("同意/授权/继续" in description, "Skill trigger description is incomplete")
    require("polling the same request" in description, "Skill polling trigger is missing")
    require(
        "codex app-server generate-json-schema --out protocol" in source,
        "Skill is missing the version-matched Codex protocol schema command",
    )
    require(
        "This command is not eligible for `request_command_execution`" in source,
        "Skill is missing the Codex schema execution boundary",
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
    require("pending/running" in openai_yaml, "Skill default prompt lacks active polling")


def validate_manifest() -> None:
    manifest = parse_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    require(manifest.get("name") == "golden-finger", "wrong plugin name")
    version = manifest.get("version")
    require(
        isinstance(version, str)
        and (version == "0.1.9" or version.startswith("0.1.9+codex.")),
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
        operations == {"health", "submit", "status", "wait", "cancel"},
        f"broker transport operation set drifted: {sorted(operations)}",
    )
    require("approve" not in operations, "transport must never expose approve")

    broker_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_broker.py")
    for appearance_marker in (
        'BROKER_VERSION = "0.1.9"',
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
        "height=44",
        "if requested_wait > 0",
        "winsound.MessageBeep",
        "new_request_ids",
        'UI_SETTINGS_FILENAME = "ui-settings.json"',
        "WINDOW_MIN_WIDTH = 920",
        "WINDOW_MIN_HEIGHT = 600",
        "WINDOW_MIN_CONTENT_HEIGHT = 260",
        'root.rowconfigure(1, weight=1, minsize=WINDOW_MIN_CONTENT_HEIGHT)',
        'main.grid(row=1, column=0, sticky="nsew"',
        'actions.grid(row=2, column=0, sticky="ew")',
        'self.approve_button.grid(row=0, column=2, sticky="e")',
        'text="次 = 总上限"',
        "_apply_safe_minimum_size",
        "winfo_reqheight",
        "获批命令将继承此权限",
        'trace_add("write", self._schedule_policy_update)',
        "command_timeout_seconds = pending_wait_budget_seconds",
    ):
        require(
            appearance_marker in broker_source,
            f"appearance/attention contract is missing {appearance_marker!r}",
        )
    require('text="保存设置"' not in broker_source, "manual policy save button returned")
    require(
        "permission_label" not in broker_source,
        "the redundant privilege banner returned",
    )
    require(
        "警告：窗口当前以管理员权限运行" not in broker_source,
        "the removed administrator warning returned",
    )

    popen_calls = [
        node
        for node in ast.walk(broker_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    require(len(popen_calls) == 1, "broker must have exactly one reviewed Popen site")
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
    ):
        require(tool_name in mcp_constants, f"MCP tool {tool_name!r} is missing")
    require("approve_authorization" not in mcp_constants, "MCP exposes an approval tool")
    for polling_marker in (
        "must_continue_current_turn",
        "do_not_wait_for_user_message",
        "wait_for_authorization",
        "pending_polls_remaining",
    ):
        require(
            polling_marker in mcp_constants,
            f"MCP polling directive is missing {polling_marker!r}",
        )
    mcp_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_mcp_server.py")
    require('SERVER_VERSION = "0.1.9"' in mcp_source, "MCP server version drifted")
    require(
        "polling.next_arguments" in mcp_source,
        "MCP continuation must direct Codex to the broker-owned next arguments",
    )

    protocol_source = load_utf8(PLUGIN_ROOT / "scripts" / "relay_protocol.py")
    require("PROTOCOL_VERSION = 2" in protocol_source, "relay protocol version must be 2")
    require(
        "DEFAULT_APPROVAL_EXPIRY_SECONDS = 3_600" in protocol_source,
        "approval validity must default to 60 minutes",
    )
    require(
        "DEFAULT_COMMAND_TIMEOUT_SECONDS = 3_600" in protocol_source,
        "command timeout must default to 60 minutes",
    )
    require(
        "MAX_APPROVAL_EXPIRY_SECONDS = 36_000" in protocol_source
        and "MAX_COMMAND_TIMEOUT_SECONDS = 36_000" in protocol_source,
        "window-configured pending and command bounds must stay capped at 10 hours",
    )
    require(
        "DEFAULT_POLL_WAIT_SECONDS = 50" in protocol_source
        and "DEFAULT_PENDING_POLL_CYCLES = 72" in protocol_source,
        "window polling defaults must remain 50 seconds x 72 polls",
    )
    require(
        "MIN_POLL_WAIT_SECONDS = 10" in protocol_source,
        "single poll wait must have a 10-second lower bound",
    )
    require(
        "MIN_APPROVAL_EXPIRY_SECONDS = 10" in protocol_source,
        "derived approval lifetime must allow the 10-second minimum product",
    )

    readme = load_utf8(PLUGIN_ROOT / "README.md")
    for launcher_marker in (
        "launch_relay.pyw",
        "pythonw.exe",
        "不会自动启动",
        "版本化缓存目录",
    ):
        require(
            launcher_marker in readme,
            f"README launcher guidance is missing {launcher_marker!r}",
        )
    for ui_marker in (
        "appearanceTheme",
        "60%",
        "文字 100%",
        "自动选中",
        "新请求提示音",
        "等待策略",
        "自动应用",
        "不会穿透",
        "只应用到随后创建的新请求",
        "920 × 600",
        "右下角",
        "极简公式",
        "权限横幅已移除",
    ):
        require(ui_marker in readme, f"README UI guidance is missing {ui_marker!r}")


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
        "window-policy sync, transport boundary, appearance/attention contract, "
        "manual launcher, and shell=False"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
