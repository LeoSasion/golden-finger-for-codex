# Tool contract

## Request claiming

The broker keeps one global in-memory queue for the shared window, while each submitting MCP tool chain owns an independent request capability. A successful `request_consent` or `request_command_execution` response returns:

- `request.request_id`: a weak locator beginning with `gfr_`;
- `claim_token`: an unguessable per-request capability;
- `request.claim_fingerprint`: a 12-hex-character, non-secret correlation value shown in the window.

`request_id` alone is intentionally insufficient. Every read, wait, or cancel must carry the exact `claim_token` returned for that request. The token is not approval and cannot trigger a GUI decision; it only permits observing or cancelling its own request. Never copy it to another Codex task or reconstruct it from the fingerprint.

The connector also submits a process-random `client_instance_id` which namespaces idempotency keys. It is transport metadata, not a Codex project or task identity, and it is not part of later tool arguments. While the Broker and in-memory request still exist, the claim token remains authoritative if the connector process restarts.

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

The shipped policy fixes pending validity at 9,000 seconds from `900 seconds × 10 polls`. There are no mutable window polling inputs; legacy saved values are ignored. The broker freezes this policy when it creates the request and caps any shorter caller-supplied validity against it. A tool call may return \`pending\` before the validity ends; this is only a bounded poll result.

## request_command_execution

Creates one command card. Approval executes the frozen \`executable\` and \`arguments\` with \`shell=False\`.

Required fields: \`exact_action\`, \`executable\`, \`arguments\`, \`working_directory\`, \`target\`, \`risk\`, \`recovery\`, \`idempotency_key\`, `source_label`, `source_project`, and `source_task`.

The shipped policy produces a 9,000-second pending validity and the same 9,000-second post-approval execution timeout. Both are derived from the fixed 900-second duration multiplied by the 10-poll limit; there is no independent manual command-timeout field. The broker freezes these caps into each new request; explicit caller values may shorten but never exceed them.

Command eligibility is determined from the exact frozen executable, argument
array, target, risk, recovery path, and applicable higher-priority rules. The
relay has no blanket exclusion for PowerShell, Python, test suites, or bounded
test supervisors.

The broker resolves and fingerprints the executable plus directly named supported script arguments before display, verifies them again immediately before process creation, and rejects unsupported Codex-control targets. Indirect imports and data are not recursively frozen. On Windows, the approved process is immediately assigned to a kill-on-close Job Object; timeout and Broker shutdown terminate that process tree, without undoing side effects that already occurred.

## wait_for_authorization

Reads or waits for the state of an existing request. It cannot approve a request.

Required fields: `request_id` and the matching `claim_token`. Active responses repeat both fields in `polling.next_arguments`; callers must reuse that object exactly. A missing or incorrect token returns `claim_mismatch` without disclosing another request's result.

Zero performs an immediate read without consuming the count. Any positive wait uses the request's exact frozen `poll_policy.wait_seconds`, fixed at 900 seconds. A \`pending\` or \`running\` result includes a structured polling directive with the exact next tool and arguments. Codex must follow that directive rather than keep a separate polling configuration. The broker does not proactively wake a completed Codex turn.

Positive waits consume the frozen pending-poll budget only while the request remains \`pending\`; zero-wait reads do not. When the pending count or elapsed validity is exhausted before approval, the request becomes \`expired\`. Once a command is \`running\`, its separate frozen execution timeout remains authoritative so a monitoring counter never silently abandons or kills an approved process.

## cancel_authorization

Cancels a pending request. It requires the matching `request_id` and `claim_token`. It does not revoke an existing manual or automatic approval and cannot reliably undo a process that has started.

If Codex sends MCP `notifications/cancelled` for an active tool call, the synchronous connector can process it only after the current 900-second broker wait returns. It then makes a best-effort cancellation of a still-pending card with the recorded claim. It never terminates an already approved command.

The relay window's small **停止** control is the immediate request-side escape hatch. For a still-pending card it atomically sets `state=cancelled`, records `result.reason_code=stopped_by_user`, and notifies the Broker condition so an active wait returns immediately. It expresses neither approval nor rejection and is unavailable once a command is approved or running.

## State machine

\`pending -> approved | rejected | cancelled | expired\`

For command requests:

\`pending -> running -> completed | failed | timed_out\`

Terminal snapshots include the request ID, claim fingerprint, request hash, timestamps, and decision receipt. Approved snapshots distinguish `receipt.approval_mode=manual` (with `human_action=approved_once`) from `receipt.approval_mode=automatic` (with `auto_approve_wait_seconds` and no `human_action`). An automatic receipt is not proof of explicit human confirmation. Command snapshots additionally include exit code, stdout, stderr, and truncation flags. Public snapshots never contain the claim token or connector instance ID.

When the user closes the window while a card is pending, a graceful shutdown marks it `cancelled` with `result.reason_code=relay_window_closed`. The MCP response then includes `scope_recovery`. Restarting the Broker cannot resume that old in-memory request. A running command's Job Object is terminated, but the external outcome may still be uncertain because completed side effects cannot be rolled back; it must never be replayed automatically.

The relay returns only output from a command it started. It does not monitor commands launched in another terminal or output from unrelated services; this does not restrict the category of an otherwise allowed command started by the relay itself.
