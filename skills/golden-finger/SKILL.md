---
name: golden-finger
description: "Use Codex金手指 (Golden Finger for Codex), the Yes工程师人机授权中继, when asking for one exact human approval or rejection regardless of wording (同意/授权/批准/确认/允许/继续/拒绝 or Yes/Approve/Confirm/Authorize/Allow/Proceed/Reject), or asking the user to paste one exact allowed local command into independent system PowerShell that its shell cannot run. On first use, agree a routing scope; scope never authorizes actions. Health-check the user-launched window before every request. If unavailable or closed, fail closed and ask whether to start it and keep the scope or cancel the scope. Keep same-turn polling until terminal. Judge the frozen action/command, not words. Exclude mere acknowledgement, informational confirmation, preference questions, native Codex approvals, standing consent, this plugin's lifecycle, already-launched commands, and authorization-channel control of Codex. request_consent may relay one exact decision about a later Codex administrative action only when the authorization channel performs no Codex action."
---

# Codex金手指

English name: Golden Finger for Codex. Functional descriptor: 人机授权中继. Search phrase: Yes工程师的金手指.

## Self-upgrade exception

- Never invoke this relay — including `relay_health`, request, cancel, or wait tools — for an installation, upgrade, enablement, repair, or removal of the `golden-finger` plugin itself. The action is self-referential and the requested version may not yet be runnable.
- Follow the current client and workspace's explicit approval workflow instead. Do not launch, listen for, or ask the user to open the relay merely to complete this exception.
- This exception is limited to this relay's own source, marketplace entry, cache, enabled state, or installed runtime. It is not a bypass for other client-impacting actions.

## Agree the usage scope

Before the first relay tool call in a Codex task, establish the Golden Finger **routing scope**. If no applicable persisted project/global scope in the active Codex instruction file and no explicit choice already exists in the current task, read [scope and window lifecycle](references/scope-lifecycle.md) and ask the user to choose `once`, `task`, `project`, `global`, or cancel. Recommend `task`; show the normalized absolute project root before offering `project`, and do not offer it when no project root is known.

This bootstrap choice belongs in ordinary conversation. Never send it through `request_consent`, because selecting a scope is a preference about when to use the relay, not authorization for any action. Never infer standing consent from a scope. Every eligible action still requires its own frozen request and GUI decision.

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

Use \`request_consent\` when the only requested response is a natural-language authorization choice. Match the intent, not one exact token. Recall examples include 同意, 授权, 批准, 确认, 允许, 继续, 拒绝, Yes, Approve, Confirm, Authorize, Allow, Proceed, and Reject. Phrases such as “可以执行吗”, “是否批准本次操作”, “确认后继续”, and “May I proceed?” qualify when the answer decides whether one exact disclosed action may proceed. Include:

- \`exact_action\`: the single action the reply authorizes.
- \`target\`: exact process, file, task, or object.
- \`risk\`: concrete impact and interruption risk.
- \`recovery\`: practical rollback or recovery path.
- \`approved_reply\`: the exact short reply to return after manual or window-configured automatic approval.
- \`idempotency_key\`: a fresh opaque key for this request and this task. Reuse it only for an identical retry in the same connector instance.
- `source_label`, `source_project`, and `source_task`: concise, truthful human-readable origin evidence for the shared queue. They are not authenticated identity.

If you are about to ask for that decision, create the consent card before ending the turn unless a stated exclusion applies—even when the proposed reply uses a synonym not listed above. Do not fall back to direct typing merely because the later action concerns Codex. For example, “批准打开 Hook 审查终端” and “同意打开 Hook 审查终端” are both eligible for `request_consent`: the card authorizes only that later action, the authorization call itself performs no Codex action, and the current task must separately obey every applicable execution rule.

Do not trigger merely because a sentence contains an approval-like word. A receipt acknowledgement such as “收到” or “知道了”, an informational check such as “请确认版本号”, a preference or design choice, and a request for standing consent are outside this flow unless the answer specifically authorizes or rejects one exact action. When the intent remains ambiguous, ask a narrow clarifying question instead of treating the word itself as approval.

Use \`request_command_execution\` only when the window should execute a local program. Pass a program plus an argument array; never pass a shell-composed command string and never use \`shell=true\`. Resolve and disclose:

- exact executable;
- complete argument list;
- absolute working directory;
- exact target;
- risk and recovery path;
- bounded execution timeout and approval expiry.
- every directly named `.ps1`, `.py`, `.pyw`, `.cmd`, or `.bat` script argument that will be frozen and shown.

Never use `request_command_execution` with `codex.exe`, `codex app-server`, or another command that launches or controls Codex. Use `request_consent` only for the human-decision step when that step is otherwise permitted; perform any later authorized action through its proper execution surface.

The relay uses one fixed policy: a 900-second positive wait, at most 10 pending polls, and their 9,000-second product as both pending validity and command-execution cap. The window has no mutable polling inputs and ignores legacy saved poll values. The broker freezes this policy into each request and caps any shorter caller-supplied approval expiry or command timeout; never treat one MCP poll as the request expiry or override the frozen policy with a second Codex-side configuration.

PowerShell is just one possible executable. Python does not bypass authorization: invoking \`python.exe\` still requires the same exact one-time approval.

The Broker fingerprints the resolved executable and direct script-file arguments when it creates the card, then rechecks those bytes immediately before launch. This covers only scripts directly named in the approved argument vector; imports, modules, dot-sourced files, and data loaded later are not recursively frozen. If those indirect inputs matter, disclose and constrain them separately or do not use the command relay.

## Continue the current turn

The broker is pull-only and cannot wake a Codex turn after that turn ends. Every new request returns a per-request `claim_token`; every active response includes a structured `polling` directive derived from the request's frozen window policy. The connector instance ID only namespaces submissions and is not task identity. Interpret the state:

- \`approved\`: for consent, use only the returned \`reply_text\` and continue.
- \`completed\`: for commands, inspect exit code, stdout, stderr, and truncation flags before continuing.
- \`rejected\`, \`cancelled\`, \`expired\`, \`failed\`, or \`timed_out\`: stop the authorized action and report the terminal result.
- \`pending\` or \`running\`: this is not a stopping point. Keep the current turn open and immediately call the exact `polling.next_tool` with the exact `polling.next_arguments`. Those arguments include `request_id`, `claim_token`, and `wait_seconds`; preserve all three byte-for-byte and re-read them after every response rather than reconstructing a token or reusing a hard-coded wait. Pending requests expire when their frozen time or poll-count budget is exhausted; running commands continue under their separately frozen execution timeout.

The connector reads Desktop cancellation notifications synchronously, so Desktop **Stop** can remain delayed until the active 900-second broker wait returns. If the user wants to end that wait immediately, direct them to the relay window's small **停止** control. It transitions only a still-pending request to `cancelled`, wakes the active broker wait, and means neither approval nor rejection. It does not cancel an approved or running command.

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
