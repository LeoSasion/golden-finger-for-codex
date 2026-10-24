"""Read-only installation evidence and human-readable connection guidance.

No configuration mutation, approval, credential collection or process launch.
"""
from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Any


def read_plugin_configuration(config_path: Path) -> tuple[bool | None, list[str], str | None]:
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        return None, [], None
    except (OSError, tomllib.TOMLDecodeError):
        return None, [], "配置无法读取或格式无效；请在 Codex 设置中核对插件状态。"
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        return None, [], "插件配置格式无法识别。"
    entries = {name: value for name, value in plugins.items()
               if name == "golden-finger" or name.startswith("golden-finger@")}
    values = [entry.get("enabled") for entry in entries.values() if isinstance(entry, dict)]
    # Mixed marketplaces and missing flags are evidence of ambiguity, not enablement.
    enabled = values[0] if values and len(values) == len(entries) and all(
        isinstance(value, bool) and value == values[0] for value in values
    ) else None
    return enabled, sorted(entries), None


def connection_steps(snapshot: dict[str, Any]) -> list[tuple[str, str, str]]:
    enabled = snapshot.get("plugin_enabled")
    installed = bool(snapshot.get("cached_installs"))
    connector = snapshot.get("last_connector")
    matched = snapshot.get("component_match") is True
    return [
        ("窗口", "已运行", "此报告来自正在运行的窗口。"),
        ("MCP", "最近握手匹配" if matched else "构建不匹配" if connector else "尚未观察到连接",
         "握手仅证明连接器能够连接窗口。" if matched else
         "让窗口与 MCP 使用同一完整发行包，再从目标聊天调用 relay_health。"),
        ("插件", "已检测到启用配置" if enabled is True else "已检测到禁用配置" if enabled is False else "启用状态尚未确认",
         "找到缓存安装；配置状态仍应以 Codex 插件设置为准。" if installed else
         "未发现缓存安装；直接启动 EXE 不代表插件已经安装。请从 Codex 支持的插件入口接入完整发行目录。"),
        ("当前聊天", "需在目标聊天验证", "在目标聊天调用 relay_health，再提交一条仅返回文字的测试请求并检查回执。任务线索或旧连接记录不能证明本次聊天已接入。"),
    ]


def format_diagnostics(snapshot: dict[str, Any]) -> str:
    def yes_no(value: Any) -> str:
        return "是" if value is True else "否" if value is False else "未检测"

    lines = ["Codex金手指 · 安装诊断与接入检查（已脱敏）", ""]
    for index, (name, status, next_step) in enumerate(connection_steps(snapshot), 1):
        lines.extend([f"{index}. {name}：{status}", f"   {next_step}"])
    job = snapshot.get("job_object_available")
    lines.extend([
        "", f"Broker: {snapshot.get('broker_version')} / {snapshot.get('broker_build_id')}",
        f"协议: {snapshot.get('protocol')}",
        f"运行模式: {snapshot.get('runtime_mode')}",
        f"运行文件: {snapshot.get('runtime_executable')}",
        f"管理员权限: {yes_no(snapshot.get('broker_elevated'))}（命令继承窗口权限）",
        f"Job Object 实测: {'可用' if job is True else '不可用' if job is False else '未检测'}",
        f"Endpoint: {snapshot.get('endpoint_path')}",
        f"当前版本缓存存在: {yes_no(snapshot.get('exact_version_cached'))}",
        f"当前精确构建缓存存在: {yes_no(snapshot.get('exact_build_cached'))}",
        "命中的插件配置: " + (", ".join(snapshot.get("plugin_config_entries", [])) or "未发现"),
    ])
    if snapshot.get("job_object_error"):
        lines.append("Job Object 检测结果: " + str(snapshot["job_object_error"]))
    if snapshot.get("plugin_config_error"):
        lines.append("配置读取错误: " + str(snapshot["plugin_config_error"]))
    connector = snapshot.get("last_connector")
    if isinstance(connector, dict):
        lines.extend(["最近连接器:",
            f"  版本/构建: {connector.get('connector_version')} / {connector.get('connector_build_id')}",
            f"  指纹: {connector.get('connector_fingerprint') or '(未提供)'}",
            f"  已观察任务线索: {connector.get('observed_thread_count', 0)}",
            f"  最后连接: {connector.get('seen_at')}"])
    else:
        lines.append("最近连接器: 尚未观察到")
    caches = snapshot.get("cached_installs", [])
    lines.append("缓存安装: " + ("" if caches else "未发现"))
    for item in caches:
        lines.append(f"  {item.get('marketplace')} / {item.get('version')} / build={item.get('build_id') or '(未标记)'}")
    for warning in snapshot.get("cache_scan_errors", []):
        lines.append("缓存检查提示: " + str(warning))
    shortcuts = snapshot.get("shortcuts", [])
    lines.append("快捷方式: " + ("" if shortcuts else "未发现（不影响运行）"))
    lines.extend(f"  {value}" for value in shortcuts)
    lines.extend(["", str(snapshot.get("security_note", ""))])
    return "\n".join(lines)
