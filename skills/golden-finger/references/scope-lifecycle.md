# Scope and window lifecycle

Golden Finger scope is a routing preference: it says when Codex should prefer this Skill for a future eligible request. It is never standing consent, never changes sandbox or native approval policy, and never authorizes a command or natural-language action. The Broker does not persist or authenticate scope.

The plugin runtime and the routing scope are separate layers. Golden Finger must be installed and enabled in the user's personal/global Codex plugin environment before `project` or `global` scope can work across projects. A repository checkout or a project-local plugin copy is development source only. An `AGENTS.md` marker never installs or embeds the Skill; it only tells a task that already has access to the installed plugin when to use it. If the runtime is not globally available, explain this limitation and do not claim that a project/global marker makes it available elsewhere.

## First-use choice

If the current Codex task has no explicit choice and no applicable marked block in the active Codex instruction file, ask in ordinary conversation before calling any relay tool:

> 首次使用 Codex金手指。请选择引用范围：仅本次 / 当前任务（推荐） / 当前项目 `<absolute-root>` / 所有项目 / 暂不使用。这个选择只决定以后何时调用金手指，不代表同意任何操作；每笔动作仍需在窗口中单独点击。

Normalize the answer as:

- `once`: only the current eligible request; it ends at that request's terminal outcome or when the user cancels it.
- `task`: the current Codex task; it ends when the task ends or the user cancels it.
- `project`: the displayed normalized project root and its future tasks. Offer it only when the root is known, and persist the marked block below in that root's active instruction file.
- `global`: future tasks for the current user. Persist the marked block below in the active instruction file under the user's Codex home.
- `cancelled`: do not route through Golden Finger in the proposed scope. This is not rejection of a pending action.

If the user's invoking message already selects an unambiguous scope, confirm the normalized choice and do not ask twice. Do not silently expand an unanswered or ambiguous choice. `task` is only a recommendation, not an automatic default.

## Persistent AGENTS block

For `project` or `global`, first resolve the target level: the normalized project root or the user's Codex home. If `AGENTS.override.md` already exists at that level, it is the active file and must receive the block; otherwise use `AGENTS.md`. Never write an inactive sibling `AGENTS.md` behind an existing override. Disclose that exact destination and effect before writing. If the active destination cannot be inspected or safely updated, do not claim persistence; keep the choice task-local or ask the user how to proceed. Preserve all unrelated instructions and add or update only this marked block:

```md
<!-- golden-finger-scope:start -->
## Codex金手指使用作用域

- The user selected `$golden-finger` as a routing preference for this AGENTS.md scope. For each eligible action, follow the installed Skill, require a separate one-time window decision, and never treat this scope as action authorization.
- If the independent window is unavailable, fail closed and ask whether to start it and keep this scope or remove this marked scope block. Do not use the relay for its own lifecycle or as a substitute for native Codex approval.
<!-- golden-finger-scope:end -->
```

Do not copy the whole Skill into an instruction file. On scope cancellation, remove only this exact marked block from the same disclosed active file under the applicable filesystem/native approval rules. A scope-file edit is relay bootstrap/configuration and must not be routed through the relay itself.

## Window health and recovery

Call `relay_health` when the scope starts and immediately before every new relay request. A health preflight does not eliminate the race that the user may close the window a moment later, so request and wait errors must use the same recovery flow.

The connector is pull-only. It can notice loss only during an active Codex turn—through `relay_health`, a request call, or `wait_for_authorization`. It cannot watch in the background, wake an ended turn, or ask anything at the instant the user closes the window. On the next active use:

- Say **window closed** only when the result carries `reason_code=relay_window_closed`.
- For `broker_stopping`, say **window closing** and offer the same start-or-cancel recovery; it is the brief shutdown race before the endpoint disappears.
- For `broker_unavailable` or `reachable=false` without a close/closing reason, say **window unavailable**; do not claim the user closed it.
- Do not misclassify `protocol_mismatch`, an invalid endpoint, or another compatibility error as a closed window.

When `scope_recovery.must_ask_user=true`, ask exactly one choice:

1. Start the independent Golden Finger window and keep the agreed scope.
2. Cancel the agreed Golden Finger scope.

For start:

- Explain that Codex cannot start the independent authorization window automatically.
- Prefer the user-created “Codex金手指” desktop or Start-menu shortcut.
- For a self-contained Windows release, guide the user to open `bin\GoldenFinger.exe` from its stable installed directory. It must not require Python or PATH.
- For a source/development install, guide the user to open `scripts\launch_relay.pyw`; they may instead run `python scripts\relay_broker.py` from an ordinary unelevated terminal.
- After the user reports it open, call `relay_health` again. Do not infer success from the message alone.
- A restarted Broker cannot resume an old in-memory request. Re-evaluate the exact action before creating any fresh card. Never replay a last-known `running` command; report its outcome as uncertain until independently resolved.

For cancel:

- Mark `once` or `task` inactive in the current task. For `project` or `global`, remove only the marked block from the same disclosed active `AGENTS.override.md` or `AGENTS.md` path.
- If a request is still known to be `pending` and reachable, cancel it with its exact claim. If the Broker is unavailable, abandon it locally and do not replay it.
- Never claim that cancelling scope revoked an `approved` or `running` action. Scope cancellation is neither approval nor rejection of the current action; leave that action unapproved and stop unless the user chooses a fresh ordinary path.
