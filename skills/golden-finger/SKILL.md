---
name: golden-finger
description: "Use Codex金手指 (Golden Finger for Codex), the Yes工程师人机授权中继, for semantic yes/no confirmations, approvals, permission to proceed, and ordered single-select questions, regardless of wording, language, keywords, or source parameter types; agreement selects the first original option. Also relay one exact allowed local command that needs the independent window. On first use agree a routing scope; scope never authorizes actions. Health-check the user-launched window before each request; if unavailable ask whether to start it or cancel the scope. Keep same-turn polling until terminal. Does not replace native Codex approvals, grant standing consent, manage this plugin's lifecycle, or answer free-text questions."
---

# Codex金手指

English name: Golden Finger for Codex. Functional descriptor: 人机授权中继. Search phrase: Yes工程师的金手指.

## Self-upgrade exception

- Never invoke this relay — including `relay_health`, request, cancel, or wait tools — for an installation, upgrade, enablement, repair, or removal of the `golden-finger` plugin itself. The action is self-referential and the requested version may not yet be runnable.
- Follow the current client and workspace's explicit approval workflow instead. Do not launch, listen for, or ask the user to open the relay merely to complete this exception.
- This exception is limited to this relay's own source, marketplace entry, cache, enabled state, or installed runtime. It is not a bypass for other client-impacting actions.

## Agree the usage scope

Before the first relay tool call in a Codex task, establish the Golden Finger **routing scope**. If no applicable persisted project/global scope in the active Codex instruction file and no explicit choice already exists in the current task, read [scope and window lifecycle](references/scope-lifecycle.md) and ask the user to choose `once`, `task`, `project`, `global`, or cancel. Recommend `task`; show the normalized absolute project root before offering `project`, and do not offer it when no project root is known.

This bootstrap choice belongs in ordinary conversation. Never send it through a relay request: selecting a scope establishes whether the relay should be used at all. Never infer standing consent from a scope. Every eligible action or question still requires its own frozen request and GUI decision.

For `project` or `global`, persist only the small marked block described in the reference, using `AGENTS.override.md` when it already exists at the target level and `AGENTS.md` otherwise. Disclose the exact active destination before writing. The block is the scope record; the Broker must not store or infer task/project scope. A project/global edit normally affects new Codex tasks, while the current task may honor the just-recorded choice immediately.

## Preconditions

- For cross-project use, the plugin runtime must be installed and enabled in the user's personal/global Codex plugin environment. A checkout or project-local source copy is only a development artifact; project/global scope markers select an already available Skill and cannot load the plugin into other projects.
- The user must launch the independent relay window themselves.
- Treat the window click as authorization for exactly the frozen request shown there, never as standing consent.
- The window now defaults to automatic approval after five seconds, configurable from 0 to 3600 seconds (0 disables). Check `receipt.approval_mode`: `automatic` is a timer result, not evidence of a human click. If applicable rules require explicit human confirmation, require manual approval; ask the user to disable automatic approval in the window and click the exact card instead of treating a timer reply as confirmation.
- Preserve any stricter workspace, system, Codex, or native tool approval rule. This skill cannot satisfy or suppress a native approval prompt.
- Never use `request_command_execution` or any authorization-transport side effect to launch or control Codex, Codex App Server, task routing, UI automation, local Codex databases, rollouts, or session files.
- That execution ban does not exclude `request_consent` when the card only collects one exact human decision about a later Codex administrative action. The click returns `reply_text` to the current tool call; it does not perform the later action, bypass a native prompt, or make an otherwise forbidden action permissible.
- The window's separate lifecycle panel may use a host-provided task ID to read the running Codex Desktop `list_threads` inventory and may expose a direct, fail-closed safe-restart button. This is a user-operated window feature, not a relay tool: never create a card to press it or treat it as an approval result. The user may click it after a separately authorized installation to refresh Codex, but the button neither authorizes nor performs installation, upgrade, enablement, repair, or removal, so the self-upgrade exception still applies to those actions.
- Do not claim authenticated task identity. The shared window has one global queue; isolation comes from a per-request capability. A host-provided task ID used for lifecycle inventory remains a weak lookup hint and is never request authentication or project ownership.
- Give every new card truthful human-readable `source_label`, `source_project`, and `source_task` values. Use the normalized absolute project root or `(projectless)` and the current user-facing task title when known. These labels help the user spot cross-project mistakes but remain weak, caller-supplied evidence; never treat them as authentication. The window derives a non-secret connector fingerprint from the process-random instance ID without displaying that raw ID.
- Every project/task must submit and retain its own request. Never adopt a `request_id`, `claim_token`, or idempotency key from another task. A request ID is only a weak locator; without its exact claim token it must not be readable, waitable, or cancellable.
- Judge command eligibility from the exact executable, arguments, target, risk,
  recovery path, and stricter instructions. Do not reject a command merely
  because it uses PowerShell or Python, runs tests, or acts as a bounded test
  supervisor. If a stricter instruction requires the user to launch that exact
  command in an independent terminal instead of through the broker, follow that
  narrower rule. The relay cannot observe a process it did not start.

Before creating each new relay request, call `relay_health`; a previous successful check is not durable. If the broker is unavailable, or an active wait reports `scope_recovery`, fail closed and follow [scope and window lifecycle](references/scope-lifecycle.md). Never launch it automatically. Ask whether the user wants to start the independent window and keep the agreed scope, or cancel the scope. Neither choice approves or rejects the interrupted action.

## Codex protocol version boundary

When maintaining code that reads, validates, snapshots, or generates against the Codex app-server protocol:

- Treat the protocol and its generated schemas as specific to the exact locally installed Codex build. Official protocol details can change quickly; documentation, repository snapshots, or schemas generated by another Codex version are not substitutes for the local build's output.
- Resolve the exact independent local Codex executable and version, the working directory, and the absolute output target before changing protocol-coupled code. After the separately required approval for that exact Codex invocation, generate the matching JSON Schema bundle with:

  ```text
  codex app-server generate-json-schema --out protocol
  ```

- `protocol` is relative to the declared working directory. Verify its resolved absolute path, preserve any existing generated bundle before replacement, and retain generator-version provenance with the artifacts so a version mismatch fails closed.
- Regenerate and revalidate downstream code, fixtures, and compatibility assumptions whenever the actual local Codex version changes. Never silently reuse the previous bundle.
- This command is not eligible for `request_command_execution`: it invokes Codex app-server tooling. The relay may only collect an otherwise permitted, exact `request_consent` decision; the proper execution surface must run the command under every stricter approval rule. Schema generation authorizes neither starting an app-server transport nor any other Codex action.

## Choose the request type

Interpret the complete question and its options before selecting a tool. Source cards do not need a boolean parameter. Do not use a keyword whitelist, require a particular language, or infer meaning from a recommendation marker. In the agreed scope, route eligible questions through the relay **before** invoking an ordinary host question-card tool or ending the turn to ask directly. An MCP tool cannot retroactively answer a host card that has already been displayed.

Use `request_decision` for an ordinary semantic yes/no confirmation or ordered single-select question. It collects an answer without running a program. Read [decision mapping](references/decision-mapping.md) when constructing these requests.

- `exact_action` is the complete original question. Preserve negation, conditions, scope, and consequences; never invert “是否不再提示” into “是否继续提示”.
- For a question whose answer is semantically yes/no, use internal `decision_type=boolean`, even if its source has no typed parameters. Give explicit `true_label` and `false_label` describing the two answers in the original question's meaning. “同意” returns `true`; “拒绝” returns `false`. A false answer is valid completion of that question, not a failed tool call.
- When the source presents an ordered option list, use `decision_type=single_select` and copy every original option in its original order, including stable IDs, labels, descriptions, and any explicit recommendation metadata. Do not collapse two arbitrary options to a boolean. “同意” always chooses the **first original option** (`selected_index=0`), even if no option is recommended or a later option is recommended. “拒绝” selects nothing; it does not mean the second option. “停止” cancels without answering either kind.
- A preference, design choice, or informational confirmation is eligible when it actually asks the user for a yes/no or single-select decision. A statement containing “确认” or “同意” is not itself a question. If the answer requires facts, free text, multiple selections, or resolving a genuinely ambiguous meaning, ask for the missing information through the appropriate ordinary tool; do not fabricate it or reduce it to agreement.
- Keep one request per original question and preserve `question_id` when present. Do not let one answer approve a batch of questions or later changed choices. A structured answer is scoped only to its frozen question and never substitutes for an independent action approval.
- Include the common `target`, `risk`, `recovery`, `idempotency_key`, `source_label`, `source_project`, and `source_task` fields. Describe what choosing the answer means; for a question with no external effect, say so directly.

Use \`request_consent\` when collecting authorization for one exact disclosed later action. Match its intent regardless of wording. The result authorizes only that frozen action. Include:

- \`exact_action\`: the single action the reply authorizes.
- \`target\`: exact process, file, task, or object.
- \`risk\`: concrete impact and interruption risk.
- \`recovery\`: practical rollback or recovery path.
- \`approved_reply\`: the exact short reply to return after manual or window-configured automatic approval.
- \`idempotency_key\`: a fresh opaque key for this request and this task. Reuse it only for an identical retry in the same connector instance.
- `source_label`, `source_project`, and `source_task`: concise, truthful human-readable origin evidence for the shared queue. They are not authenticated identity.

If you are about to ask for that authorization, create the consent card before ending the turn unless a stated exclusion applies. Do not fall back to direct typing merely because the later action concerns Codex. For example, “批准打开 Hook 审查终端” and “同意打开 Hook 审查终端” are both eligible for `request_consent`: the card authorizes only that later action, the authorization call itself performs no Codex action, and the current task must separately obey every applicable execution rule.

Never use an acknowledgement such as “收到” as authorization. A request for standing consent remains outside this flow. Native Codex permissions and the relay's own scope/lifecycle choices retain their existing separate workflow.

Use \`request_command_execution\` only when the window should execute a local program. Pass a program plus an argument array; never pass a shell-composed command string and never use \`shell=true\`. Resolve and disclose:

- exact executable;
- complete argument list;
- absolute working directory;
- exact target;
- risk and recovery path;
- bounded execution timeout and approval expiry.
- every directly named `.ps1`, `.py`, `.pyw`, `.cmd`, or `.bat` script argument that will be frozen and shown.

Never use `request_command_execution` with `codex.exe`, `codex app-server`, or another command that launches or controls Codex. Use `request_consent` only for the human-decision step when that step is otherwise permitted; perform any later authorized action through its proper execution surface.

The relay freezes a 9,000-second pending validity and command-execution cap. MCP waits default to 30 seconds and may be 0–900 seconds; zero reads immediately. Positive waits never consume an expiry count: only the monotonic pending deadline expires a card. Keep the original claim and follow polling.next_arguments after each active response.

PowerShell is just one possible executable. Python does not bypass authorization: invoking \`python.exe\` still requires the same exact one-time approval.

The Broker fingerprints the resolved executable and direct script-file arguments when it creates the card, then rechecks those bytes immediately before launch. This covers only scripts directly named in the approved argument vector; imports, modules, dot-sourced files, and data loaded later are not recursively frozen. If those indirect inputs matter, disclose and constrain them separately or do not use the command relay.

## Continue the current turn

The broker is pull-only and cannot wake a Codex turn after that turn ends. Every new request returns a per-request `claim_token`; every active response includes a structured `polling` directive derived from the request's frozen window policy. The connector instance ID only namespaces submissions and is not task identity. Interpret the state:

- \`approved\`: for consent, use only the returned \`reply_text\` and continue.
- `answered`: for a question, use `result.answer`. Boolean `false` is an answer; for single-select, use the returned original `option_id` and zero-based `selected_index`. Continue according to this answer without expanding its scope.
- \`completed\`: for commands, inspect exit code, stdout, stderr, and truncation flags before continuing.
- \`rejected\`, \`cancelled\`, \`expired\`, \`failed\`, \`timed_out\`, or \`terminated\`: stop the authorized action and report the terminal result.
- \`pending\` or \`running\`: this is not a stopping point. Keep the current turn open and immediately call the exact `polling.next_tool` with the exact `polling.next_arguments`. Those arguments include `request_id`, `claim_token`, and `wait_seconds`; preserve all three byte-for-byte and re-read them after every response rather than reconstructing a token or reusing a hard-coded wait. Pending requests expire only when their frozen monotonic deadline is reached; running commands continue under their separately frozen execution timeout.

The connector continues reading Desktop cancellation notifications during active waits. It interrupts its wait and attempts to cancel the still-pending card using the recorded claim; the response reports the Broker's cancellation outcome. If the Broker cannot confirm cancellation, do not claim that the card was cancelled. A command that has already been approved is never terminated by MCP cancellation or `cancel_authorization`.

The window's **停止** control cancels a still-pending card immediately without approval or rejection. For an approved command, the control becomes **终止** and asks for confirmation before ending that command's queue entry or process tree. When the user explicitly requests termination through Codex, call `terminate_command` with that command's original `request_id` and `claim_token`. Termination preserves captured output but does not undo completed side effects; never replay the action automatically.

Never return a final answer or ask the user to send “已完成” while the state is \`pending\` or \`running\`. Do not ask for another confirmation merely because one bounded poll returned active. Do not submit a duplicate with a new key. Never omit, guess, log for convenience, or transfer the claim token; if it is unavailable, fail closed instead of trying another task's request ID. Do not edit or reinterpret `poll_policy`, `pending_polls_remaining`, or `policy_revision`; they are broker-owned synchronization data. Send concise progress commentary about once per minute while continuing the same request.

Only output captured from a command started by \`request_command_execution\` can be returned. Independently launched commands and separate services are outside this pull loop and must be observed through their own authorized workflow. This observation boundary does not prohibit the broker from starting an explicitly allowed, bounded PowerShell, Python, test-suite, or test-supervisor command itself.

## Natural-language boundary

A button-generated “同意” is a structured callback result, not proof that the user approved a different action. Never reinterpret it as:

- approval for multiple steps;
- permission for a changed command, executable, target, risk, or recovery path;
- authorization to bypass sandboxing or native Codex policy;
- permission to send messages to arbitrary tasks.

For an eligible Codex-related consent card, an `approved` result is authorization for the exact disclosed later action, not evidence that the action already ran. Continue in the same turn only if that action is still allowed by every applicable rule; otherwise report the remaining blocker.

Read [security model](references/security-model.md) before proposing changes to the broker boundary. Read [tool contract](references/tool-contract.md) when debugging tool states or composing a request.

## Preflight, recovery and output

Use `preflight_command` to check an interpreter, argument vector, working directory, literal environment overrides and output encoding without executing or creating a card. `environment`, `output_encoding` and explicit `response_files` can accompany a command request. Direct response files must appear as exact @path arguments and are fingerprinted before approval. Templates in scripts/command_templates.py prepare Python, PowerShell, Node and npm invocation arrays.

If submission has an unknown outcome, use its exact recovery_token and payload_hash with `recover_authorization`. Recovery queries an existing request and never resubmits it. Keep recovery capabilities private, like claims. A missing record after Broker restart does not prove the action never occurred; report uncertainty and do not replay automatically.

Use `read_command_output` with the original request_id and claim_token for bounded byte-offset pages. Retained head/tail output can contain a gap; follow next_offset and do not treat omitted middle bytes as a complete log. Progress notifications are optional and stop with the active tool call; they cannot wake a finished Codex turn.
