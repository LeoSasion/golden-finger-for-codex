# Semantic question mapping

`request_decision` represents a question after the model has understood its meaning. The source need not declare a boolean or use any particular keyword. This is model routing before a host question card, not interception of a card already open in Codex.

| Original question | Representation | 同意 | 拒绝 |
| --- | --- | --- | --- |
| “继续使用当前目录吗？” | boolean; labels “使用当前目录” / “不使用当前目录” | true | false |
| “是否不再提示？” | boolean; labels “不再提示” / “保留提示” | true, stop prompting | false, keep prompting |
| “保留这些结果，可以吗？” | boolean; labels “保留结果” / “不保留结果” | true | false |
| “Which layout?” with options Compact, Detailed (Recommended) | single_select, preserve both options | Compact, index 0 | no selection |
| Options Continue / Cancel in that order | single_select, preserve both options | Continue, index 0 | no selection; do not select Cancel |

For an exact later action requiring authorization, use `request_consent`; the answer cannot replace a native permission. For an allowed command executed by the window, use `request_command_execution`.

## Frozen input

Use the common request fields and full original question in `exact_action`.

- `decision_type=boolean`: optional `true_label` and `false_label` default to 是/否. Prefer explicit unambiguous labels; they must differ. Do not supply `options`.
- `decision_type=single_select`: supply 1–20 original `options`, each with unique `id`, `label`, optional `description`, and optional boolean `recommended`. Do not supply boolean labels. When source IDs are absent, derive stable positional IDs for this question; do not invent option text. Preserve original order, including when a later option is recommended.
- Optional `question_id` is echoed in the answer. Submit each question separately with its own idempotency key and claim. An identical retry keeps the same key; changed question, labels, order, or options require a new request and decision.

## Terminal result

Agreement returns `state=answered` and `result.answer`:

```json
{"type":"boolean","value":true,"question_id":"q1"}
```

```json
{"type":"single_select","selected_index":0,"option_id":"compact","label":"Compact","question_id":"q2"}
```

Boolean rejection also returns `state=answered`, with `value=false`; it is a valid negative answer. Choice rejection returns `state=rejected` with no answer. Stop returns `state=cancelled` with no answer. Do not invent a fallback or resubmit after rejection/cancellation. All answer paths execute no command.

The window displays the mapping and full option list. Its existing automatic-approval setting applies unchanged: `receipt.approval_mode=automatic` is a timer answer, not a human click. A manual negative answer records `human_action=rejected_once`. Keep native explicit-human-confirmation requirements.

Free text, selecting multiple options, or a question whose meaning cannot be resolved is outside this one-button mapping. Ask only for the missing information using the appropriate question tool. Never discard options, infer a desired answer from arbitrary text, or silently reinterpret a negative question.
