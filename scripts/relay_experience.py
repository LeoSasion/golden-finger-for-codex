"""Product copy and side-effect-free practice, separate from authorization."""
from __future__ import annotations

import re
from typing import Any

CHAT_TEST_PROMPT = """请使用 Codex金手指完成一次无副作用的接入测试。
先检查 relay_health；按已约定作用域，通过 request_decision 询问“是否完成这次文字接入测试？”，
明确显示是/否答案，不运行任何命令、不修改文件。
收到终态后报告本题答案和连接器取回状态；pending 时保持本回合继续等待。
若工具不可用或窗口版本不匹配，请明确指出需要修复哪一步，不要声称测试已通过。"""


def question_presentation(frozen: dict[str, Any]) -> tuple[str, str, str]:
    """Keep the original question and first-option rule; promote its consequence."""
    kind = frozen.get("kind")
    if kind == "decision" and frozen.get("decision_type") == "boolean":
        return ("回答问题", "同意后回答：" + frozen["true_label"],
                "拒绝后回答：" + frozen["false_label"] + "；停止不作答")
    if kind == "decision":
        return ("选择方案", "同意后选择第 1 项：" + frozen["options"][0]["label"],
                "保留原始顺序；拒绝与停止都不选择其他项")
    if kind == "command":
        return "执行命令", "同意后执行这一条精确命令", "拒绝或停止：待批准的命令不会启动"
    return "确认本次动作", "同意后授权这一件事", "拒绝或停止：不授权本次动作"


def delivery_copy(snapshot: dict[str, Any]) -> str:
    if snapshot.get("state") in {"pending", "running"}:
        return "等待本次结果；答案取回后会在这里确认"
    delivered = snapshot.get("delivery") or {}
    if delivered.get("status") == "connector_received":
        return "连接器已取回本次结果 · 不代表 Agent 已执行后续动作"
    return "本次结果已生成 · 等待连接器取回"


def _identifier(value: Any) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9.+_-]{1,100}", text) else "未确认"


def repair_prompt(snapshot: dict[str, Any]) -> str:
    """An allowlist, never a copied diagnostic body, endpoint, label or token."""
    enabled = snapshot.get("plugin_enabled")
    enabled_text = "已检测到启用" if enabled is True else "已检测到禁用" if enabled is False else "尚未确认"
    matched = "匹配" if snapshot.get("component_match") is True else "尚未确认匹配"
    return (
        "请帮我修复这台 Windows 电脑上的 Codex金手指接入。\n"
        "项目：https://github.com/LeoSasion/golden-finger-for-codex\n"
        f"窗口版本：{_identifier(snapshot.get('broker_version'))}\n"
        f"构建：{_identifier(snapshot.get('broker_build_id'))}\n"
        f"协议：{_identifier(snapshot.get('protocol'))}\n"
        f"运行方式：{_identifier(snapshot.get('runtime_mode'))}\n"
        f"最近连接器与窗口：{matched}\n插件配置：{enabled_text}\n"
        f"本构建缓存：{'已发现' if snapshot.get('exact_build_cached') is True else '尚未发现'}\n"
        "请先阅读项目安装指南，定位安装文件、插件启用、MCP 连接或当前聊天验证的具体问题。\n"
        "可在本次修复范围内完成官方来源校验、安装修复和受支持配置；保留已有设置与其他插件。\n"
        "需要关闭窗口或刷新客户端时，先检查活动任务，明确交接，避免打断工作。\n"
        "不要修改自动审批参数，不要重放旧命令，不要编辑私有数据库或伪造安装缓存。\n"
        "修复后在目标聊天进行无副作用文字测试，以真实终态回执验收。\n"
        "以上只包含接入状态，不包含令牌、端点、项目路径、原始命令或输出。"
    )


class PracticeSession:
    """No Broker/store/transport access: practice can never authorize a request."""
    QUESTIONS = (
        {"title": "演示 1：有选项时，同意选择第一项", "question": "你希望先怎样处理这个演示方案？",
         "choices": ("保留现状", "调整方案（推荐）"), "yes": "选择第 1 项：保留现状", "no": "没有选择任何项"},
        {"title": "演示 2：保留原问题的否定关系", "question": "是否不再提示？（仅演示，不改变任何设置）",
         "choices": ("是：不再提示", "否：保留提示"), "yes": "回答是：不再提示", "no": "回答否：保留提示"},
    )

    def __init__(self) -> None:
        self.index = 0
        self.result: str | None = None

    @property
    def current(self) -> dict[str, Any]:
        return self.QUESTIONS[self.index]

    def answer(self, action: str) -> str:
        if action not in {"agree", "reject", "stop"}:
            raise ValueError("Unknown practice action")
        self.result = ("本题已取消，不作答" if action == "stop" else
                       self.current["yes" if action == "agree" else "no"])
        return "演示结果：" + self.result

    def advance(self) -> bool:
        if self.result is None:
            return False
        if self.index + 1 < len(self.QUESTIONS):
            self.index += 1
            self.result = None
            return True
        return False
