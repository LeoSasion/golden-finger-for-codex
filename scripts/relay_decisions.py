"""Frozen, typed answers for ordinary boolean and single-selection questions.

No keyword matching, host-card mutation, or command execution occurs here.
"""
from __future__ import annotations

from typing import Any

from relay_protocol import RelayError, _bounded_text, _reject_unknown_fields


def normalize_decision(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("decision_type")
    if not isinstance(kind, str) or kind not in {"boolean", "single_select"}:
        raise RelayError("invalid_decision", "decision_type must be boolean or single_select.")
    result: dict[str, Any] = {"decision_type": kind}
    if "question_id" in payload:
        result["question_id"] = _bounded_text(payload["question_id"], "question_id", maximum=128)
    if kind == "boolean":
        if "options" in payload:
            raise RelayError("invalid_decision", "Boolean questions cannot carry selection options.")
        result["true_label"] = _bounded_text(payload.get("true_label", "是"), "true_label", maximum=200)
        result["false_label"] = _bounded_text(payload.get("false_label", "否"), "false_label", maximum=200)
        if result["true_label"] == result["false_label"]:
            raise RelayError("invalid_decision", "Boolean answer labels must be distinct.")
        return result
    if "true_label" in payload or "false_label" in payload:
        raise RelayError("invalid_decision", "Single-selection questions cannot carry boolean labels.")
    options = payload.get("options")
    if not isinstance(options, list) or not 1 <= len(options) <= 20:
        raise RelayError("invalid_decision", "options must contain 1–20 ordered choices.")
    result["options"] = []
    identifiers: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise RelayError("invalid_decision", "Each option must be an object with id and label.")
        _reject_unknown_fields(option, {"id", "label", "description", "recommended"})
        identifier = _bounded_text(option.get("id"), "option.id", maximum=128)
        if identifier in identifiers:
            raise RelayError("invalid_decision", "Option ids must be unique.")
        identifiers.add(identifier)
        recommended = option.get("recommended", False)
        if type(recommended) is not bool:
            raise RelayError("invalid_decision", "recommended must be a boolean.")
        item = {
            "id": identifier,
            "label": _bounded_text(option.get("label"), "option.label", maximum=500),
            "recommended": recommended,
        }
        if "description" in option:
            item["description"] = _bounded_text(option["description"], "option.description", maximum=1000)
        result["options"].append(item)
    return result


def decision_result(frozen: dict[str, Any], affirmative: bool) -> dict[str, Any]:
    if frozen["decision_type"] == "boolean":
        answer = {"type": "boolean", "value": affirmative}
        reply = frozen["true_label"] if affirmative else frozen["false_label"]
    else:
        if not affirmative:
            raise ValueError("Rejecting a single-selection question does not choose an alternative")
        option = frozen["options"][0]
        answer = {"type": "single_select", "selected_index": 0,
                  "option_id": option["id"], "label": option["label"]}
        reply = option["label"]
    if "question_id" in frozen:
        answer["question_id"] = frozen["question_id"]
    return {"decision": "answered", "answer": answer, "reply_text": reply}


def decision_summary(frozen: dict[str, Any]) -> str:
    if frozen["decision_type"] == "boolean":
        return f"同意 = {frozen['true_label']} (true)；拒绝 = {frozen['false_label']} (false)；停止 = 不作答"
    return f"同意 = 第 1 项：{frozen['options'][0]['label']}；拒绝/停止 = 不选择任何项"


def decision_detail(frozen: dict[str, Any]) -> list[str]:
    lines = ["结构化询问（不执行命令）", decision_summary(frozen)]
    if frozen["decision_type"] == "single_select":
        lines.append("完整原始选项（保留顺序，推荐标记不改变选择位置）:")
        for index, option in enumerate(frozen["options"], 1):
            lines.append(f"  {index}. {option['label']} [id={option['id']}]")
            if option["recommended"]:
                lines.append("     源选项推荐标记: 是")
            if "description" in option:
                lines.append("     " + option["description"])
    return lines
