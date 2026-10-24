# Tool contract

## Request claiming

The broker keeps one global in-memory queue for the shared window, while each submitting MCP tool chain owns an independent request capability. A successful `request_consent` or `request_command_execution` response returns:

- `request.request_id`: a weak locator beginning with `gfr_`;
- `claim_token`: an unguessable per-request capability;
- `request.claim_fingerprint`: a 12-hex-character, non-secret correlation value shown in the window.

`request_id` alone is intentionally insufficient. Every read, wait, cancel, or terminate must carry the exact `claim_token` returned for that request. The token is not approval and cannot trigger a GUI approval; it permits observing, cancelling, or explicitly terminating its own request. Never copy it to another Codex task or reconstruct it from the fingerprint.

The connector submits a random `client_instance_id` which namespaces idempotency keys. A protected journal retains the namespace within a host-provided thread partition; without that hint, a process-random partition is used. This is transport metadata, not authenticated project or task identity. The claim or recovery capability remains authoritative while the Broker retains the request.

When Codex supplies a valid host thread ID in MCP `_meta` or the connector's host environment, the connector may forward it as optional transport metadata for the window's read-only lifecycle inventory. It is not a tool argument, frozen request field, public snapshot field, request hash input, or claim. Missing or malformed metadata simply leaves lifecycle inventory unavailable and safe restart disabled; it must never weaken request isolation or be inferred from caller labels.

Every request additionally carries required human-readable `source_label`, `source_project`, and `source_task` fields. The Broker freezes and displays them with a short connector fingerprint derived from `client_instance_id`. All four values are correlation aids only; the per-request claim remains the isolation mechanism.

Routing scope is not a protocol field. `once`, `task`, `project`, or `global` is a Codex instruction-layer preference and must never be added to the frozen request, request hash, claim token, or approval receipt.

## relay_health

Checks the independently launched window without starting it or mutating a request. Codex calls it when a Golden Finger scope begins and immediately before each new request.

When the endpoint is absent or unreachable, the tool returns a successful diagnostic with `reachable=false`, `window_state=not_running_or_unreachable`, and `scope_recovery`. During the brief graceful-shutdown race, `broker_stopping` becomes `reachable=false`, `window_state=closing`, with the same recovery choices instead of a false healthy result. The recovery object requires Codex to ask whether the user wants to start the window and keep the agreed routing scope, or cancel that scope. Other errors such as protocol mismatch remain errors and must not be mislabeled as a user-closed window.

Health is pull-only and has no background subscription. A successful result does not guarantee the window remains open for the next call.

## request_consent

Creates one natural-language authorization card. A manual click or the window's configured automatic timer returns the exact configured \`approved_reply\`; it executes no program. Automatic approval defaults to five seconds; the user can change the window's wait or set it to zero to disable it. The timer is not the 900-second MCP polling wait.

Required fields: \`exact_action\`, \`target\`, \`risk\`, \`recovery\`, \`idempotency_key\`, `source_label`, `source_project`, and `source_task`.

The shipped policy caps pending validity at 9,000 seconds. There are no mutable window polling inputs; legacy saved values are ignored. The broker freezes this policy when it creates the request; caller-supplied validity may shorten it. A tool call may return \`pending\` before the monotonic deadline; neither repeated nor concurrent polls shorten that validity.

## request_decision

Collects one ordinary semantic yes/no or ordered single-select answer. The model normalizes the original question by meaning; the source need not have boolean parameters or particular keywords. `exact_action` preserves the full question. Use common request fields plus `decision_type=boolean` (optional distinct `true_label` / `false_label`) or `decision_type=single_select` (1–20 ordered `options` with unique IDs and labels). Optional `question_id` is echoed. See [decision mapping](decision-mapping.md).

Agreement returns `state=answered` and typed `result.answer`: boolean `value=true`, or the original first option's ID, label and zero-based `selected_index=0`. Recommendation metadata never reorders choices. Boolean rejection is `answered` with `value=false`; single-select rejection has no selected answer. Stop cancels without answering. No path executes a program or replaces native Codex permissions. Frozen question, labels and option order participate in the request hash and idempotency check.

The existing window timer is unchanged and applies to these cards; an automatic answer is disclosed in `receipt.approval_mode` and is not a human click. Already displayed host cards are not intercepted. Free text and multi-select require the appropriate ordinary host question flow.

## request_command_execution

Creates one command card. Approval executes the frozen \`executable\` and \`arguments\` with \`shell=False\`.

Required fields: \`exact_action\`, \`executable\`, \`arguments\`, \`working_directory\`, \`target\`, \`risk\`, \`recovery\`, \`idempotency_key\`, `source_label`, `source_project`, and `source_task`.

The shipped policy caps pending validity and execution timeout at 9,000 seconds each. Execution timeout starts after process startup; queue time is separate. The broker freezes these caps into each new request; explicit caller values may shorten but never exceed them. Poll count is informational.

Command eligibility is determined from the exact frozen executable, argument
array, target, risk, recovery path, and applicable higher-priority rules. The
relay has no blanket exclusion for PowerShell, Python, test suites, or bounded
test supervisors.

The broker resolves and fingerprints the executable plus directly named supported script arguments before display, verifies them again immediately before process creation, and rejects unsupported Codex-control targets. Indirect imports and data are not recursively frozen. On Windows, the approved process is created suspended, assigned to a kill-on-close Job Object, and only then resumed. Binding failure kills the suspended process before it executes. Timeout, explicit termination, and Broker shutdown terminate that process tree, without undoing side effects that already occurred.

Approved commands retain `state=running` while `execution_phase` distinguishes `queued`, `starting`, `running`, and `terminating`. At most four commands execute at once; commands in the same resolved Git repository run serially. Outside Git, the resolved working directory is the queue key. Caller-supplied project labels do not control execution isolation. The execution timeout begins at process startup, excluding queue time.

## wait_for_authorization

Reads or waits for the state of an existing request. It cannot approve a request.

Required fields: `request_id` and the matching `claim_token`. Active responses repeat both fields in `polling.next_arguments`; callers must reuse that object exactly. A missing or incorrect token returns `claim_mismatch` without disclosing another request's result.

Zero performs an immediate read. Positive MCP waits default to 30 seconds and accept 1–900 seconds, capped by the frozen upper bound. Active responses preserve the selected wait in polling.next_arguments. Concurrent or repeated waits do not consume an expiry budget; pending_polls_used is informational and pending_polls_remaining is null. expiry_basis=monotonic_deadline and remaining_seconds identify the actual lifetime. Running commands retain their independent execution timeout.


## cancel_authorization

Cancels a pending request. It requires the matching `request_id` and `claim_token`. It does not revoke an existing manual or automatic approval and cannot reliably undo a process that has started.

The connector reads MCP `notifications/cancelled` concurrently with active waits. It interrupts the wait, attempts pending cancellation with the recorded claim, and waits for that bounded attempt before replying. Cancellation during submission is reconciled when the claim arrives. The response's `broker_cancellation` outcome distinguishes acknowledged cancellation, an already-started action, and an unavailable or failed attempt; interruption alone is not evidence of cancellation. It never terminates an already approved command. Sixteen ordinary worker slots and two reserved control slots keep health, cancel, and terminate usable during long polls; notification cancellation uses two additional bounded workers.

The relay window's small **停止** control is the immediate request-side escape hatch. For a still-pending card it atomically sets `state=cancelled`, records `result.reason_code=stopped_by_user`, and notifies the Broker condition so an active wait returns immediately. It expresses neither approval nor rejection. For an approved command, this control becomes a separate **终止** action with a confirmation dialog.

## terminate_command

Explicitly terminates only the command identified by its matching `request_id` and `claim_token`. Use it when the user requests stopping that approved command, not as an automatic consequence of cancelling a wait. A queued command is removed before startup; a running command's contained process tree is terminated. The final snapshot has `state=terminated` and `result.reason_code=terminated_by_user`, retaining collected output. Repeated calls to a terminal command return its existing result. Completed external effects cannot be undone.

## State machine

\`pending -> approved | rejected | cancelled | expired\`

For command requests:

\`pending -> running -> completed | failed | timed_out | terminated\`

For decision questions:

`pending -> answered | rejected | cancelled | expired`

`answered` is terminal for both boolean true/false and the first-option selection. Its `result.answer` is an answer to the frozen question, not blanket action authorization.

Terminal snapshots include the request ID, claim fingerprint, request hash, timestamps, and decision receipt. Approved snapshots distinguish `receipt.approval_mode=manual` (with `human_action=approved_once`) from `receipt.approval_mode=automatic` (with `auto_approve_wait_seconds` and no `human_action`). An automatic receipt is not proof of explicit human confirmation. Command snapshots additionally include exit code, stdout, stderr, and truncation flags. Public snapshots never contain the claim token or connector instance ID.

When the user closes the window while a card is pending, a graceful shutdown marks it `cancelled` with `result.reason_code=relay_window_closed`. The MCP response then includes `scope_recovery`. Restarting the Broker cannot resume that old in-memory request. A running command's Job Object is terminated, but the external outcome may still be uncertain because completed side effects cannot be rolled back; it must never be replayed automatically.

The relay returns only output from a command it started. It does not monitor commands launched in another terminal or output from unrelated services; this does not restrict the category of an otherwise allowed command started by the relay itself.

Output retention and final serialized JSON size are both bounded. The Broker and MCP response (including its text and structured copies) apply a final byte budget, trimming stdout/stderr as needed while preserving terminal state, exit code, and truncation flags. A truncated response is not a failed command. read_command_output exposes bounded retained head/tail bytes by cursor, including explicit gaps. Use a command-owned output file when complete logs are required. The Broker retains at most 512 request records and accepts at most 32 concurrent TCP handlers, with at most 16 blocking waits so short control requests retain capacity. Excess waits return `wait_capacity`; retain the claim and retry the existing request rather than resubmitting the action.

## preflight_command

Read-only command validation without a request card or execution. Takes executable, arguments, working_directory and optional environment, output_encoding, response_files and timeout_seconds. Returns resolved executable and file hashes, effective override key names, command-line length and noninteractive capability. Command submission validates again. environment values are literal overrides frozen into the request; no shell activation occurs. Explicit @path response_files and direct Node scripts are fingerprinted and rechecked.

## recover_authorization

Takes the original recovery_token and payload_hash. It only looks up the existing Broker record and returns its original claim and state; it never starts, approves or resubmits an action. The submitting connector writes a minimal prepared recovery record before submission. Windows records use current-user DPAPI; no command body/output is stored. Stable host thread hints partition local records but do not authenticate identity. Without a stable hint, a process-random partition prevents automatic cross-process reuse. Unknown or missing recovery results must not lead to automatic replay.

## read_command_output and progress

read_command_output requires request_id and claim_token, with stream stdout/stderr, byte offset (default 0), and bounded limit (default 16384). Follow returned next_offset and gap metadata. Retention is bounded and may omit middle bytes. Progress notifications honor an active MCP progressToken, use actual elapsed time/state with no invented total, are rate-limited, and stop after the call completes. They cannot revive a completed host turn.

If a requested page cannot hold one complete character, the tool reports `output_page_limit_too_small` with `minimum_limit`; retry from the same offset with a larger limit. A still-running stream that has written only part of a character can temporarily return no text and the same cursor. MCP pages cap retained raw bytes at 16 KiB so the final escaped response fits without silently dropping text ahead of the returned cursor.
