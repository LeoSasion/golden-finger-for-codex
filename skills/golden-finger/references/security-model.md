# Security model

The relay separates three roles:

1. Codex proposes one bounded request through the plugin MCP server.
2. The independently launched broker freezes and displays the request.
3. The window decides the exact request through a human click or its configured automatic-approval timer.

The MCP-facing process has no approve operation. Approval exists only as a GUI event in the broker process, including its local automatic timer. The authorization channel never launches or controls Codex and never injects text into another task. A separate, GUI-only lifecycle coordinator may read the running Codex Desktop host's `list_threads` result and perform a fail-closed graceful restart after a direct user click; it is not callable through the relay protocol.

## Guarantees

- Loopback-only IPC with a per-launch random bearer secret.
- Per-request random claim capabilities: `request_id` is only a locator, and the matching `claim_token` is required to read, wait for, cancel, or explicitly terminate a request.
- Random namespaces isolate idempotency keys; a protected journal may retain one across connector restarts within the same host-provided thread partition. Without a stable host hint, partitions are process-local.
- Exact package version and runtime build IDs are checked both in endpoint metadata and on every authenticated request. A source/installed or stale-window mismatch fails closed.
- Public snapshots and the GUI expose only a short claim fingerprint; they never expose the claim token or connector instance ID.
- Cards also show caller-supplied project/task/source labels and a derived connector fingerprint. These are human correlation evidence, not authenticated identity.
- In-memory request payloads and results; the endpoint file contains connection metadata only.
- Stable SHA-256 hash over the frozen request.
- SHA-256 fingerprinting of the resolved executable at display time and again immediately before process creation.
- SHA-256 fingerprinting and pre-launch re-verification of directly named `.ps1`, `.py`, `.pyw`, `.cmd`, `.bat`, `.js`, `.mjs`, and `.cjs` argument files, plus explicitly declared response files.
- On Windows, every approved process is created suspended, assigned to a kill-on-close Job Object, and then resumed; timeout, explicit termination, and Broker shutdown terminate the contained process tree.
- One decision per request and a frozen, hashed polling policy with an expiry time.
- Program plus argument-array execution with \`shell=False\`.
- Bounded runtime and bounded stdout/stderr retention.
- Automatic approval defaults to a five-second per-card wait, configurable in the window from 0 to 3600 whole seconds; zero disables it. It covers every pending card, including commands. It does not add an MCP approval operation or permit native Codex approval bypasses.
- Automatic receipts have `approval_mode=automatic` and `auto_approve_wait_seconds`, without `human_action`. Manual approval has `approval_mode=manual` and `human_action=approved_once`. A timer receipt must never be presented as proof of a human click or used where applicable rules require explicit human confirmation.
- Routing scope is kept in the Codex instruction layer, never in Broker request state; it grants no approval and is not included in request hashes, claim capabilities, or decision receipts.
- Within one connector-instance namespace, a reused idempotency key must carry the identical Codex proposal. A settings change between retries returns the already-frozen request and the same claim capability rather than applying the new policy or creating a conflict. The same key in another connector namespace creates an independent request.
- Codex task inventory is read only from the already-running desktop app-tools bridge. The connector forwards a host-provided task ID as a weak lookup hint; the Broker dynamically discovers the exact `list_threads` tool, requires the reviewed schema version, includes pinned tasks, rejects partial/truncated/unknown-status responses, and verifies that the named-pipe server process is an allowed Codex Desktop executable.
- Safe restart acquires an atomic Broker intake gate, refreshes the task inventory immediately, and remains disabled unless every Codex/ChatGPT task is non-active and every relay request is terminal. It sends only the normal window-close message, waits a bounded interval, never force-kills Codex, and relaunches the exact validated executable path.

## Non-guarantees

- This is not a replacement for Codex native approvals or sandboxing.
- The loopback secret is same-user isolation, not protection from another process already running as the same Windows user.
- The prototype does not cryptographically prove which physical person clicked.
- A host-provided Codex task ID is a weak lifecycle and local-journal lookup hint, not authenticated request identity or project ownership. A connector instance ID is only a random namespace; claim and recovery capabilities govern access.
- The claim token is a bearer capability. A same-user process or another task that obtains the exact token can observe, cancel, or terminate that request; the broker cannot distinguish legitimate transfer from theft.
- The token necessarily travels through MCP tool results and later tool arguments, so Codex task context or diagnostic logs may retain it. The Broker keeps it in memory and never places it in public snapshots or the GUI. The connector may retain the minimal claim/recovery credentials in its protected recovery journal; on Windows this is sealed with current-user DPAPI.
- A connector restart can recover the protected journal when the same host-provided thread partition is available. Otherwise its namespace changes. Recovery never creates a request; an unavailable record is an unknown outcome and must not trigger automatic execution replay.
- Commands inherit the broker process account, privilege level, and filesystem/network access. Environment variables inherit from the broker with explicit frozen overrides; complete override values are visible in local command details, while preflight and sanitized diagnostics omit their values.
- Only direct supported script arguments are snapshotted. Imports, modules, dot-sourced files, configuration, data, and other content loaded by the program are not recursively attested.
- The suspended-start sequence prevents user code running before Job Object assignment. Assignment or resume failure kills the newly created process and fails closed. File hashing does not make external files immutable after verification.
- Command output is returned to the current Codex context and may be retained there even though the broker itself keeps no persistent history.
- The broker is pull-only. It cannot wake a Codex turn that ended before the connector retrieved a terminal result.
- The connector cannot monitor window liveness in the background. It can detect unavailability only when an active Codex turn calls health, submits, or waits.
- MCP cancellation is processed concurrently with long waits, but the cancellation request may still fail or race with approval. The connector reports the Broker acknowledgement or failure; stopping a socket wait does not prove the pending card was cancelled.
- The window's **停止** control immediately changes only a pending card to `cancelled`, notifies the Broker wait condition, and expresses neither approval nor rejection. An approved command instead exposes **终止**, which requires a separate confirmation. MCP `terminate_command` is also a separate, claim-protected operation; cancellation never implicitly invokes it.
- Legacy saved poll values are ignored. Total pending validity and command timeout remain frozen; individual waits may be short, and duplicate/concurrent polls cannot expire a request before its monotonic deadline.
- The broker cannot observe commands or output from an independently launched terminal, test supervisor, listener, or other service. This observation limit does not prohibit an otherwise allowed bounded supervisor from being the exact command the broker starts.
- Job Object termination does not reverse filesystem, network, registry, or other side effects already performed. If the MCP result is interrupted, Codex must still treat the external outcome as uncertain and must not replay it automatically.
- Task titles, paths, kinds, and status fields returned by Codex are untrusted display data. Sanitization and schema checks reduce accidental misuse but do not turn them into authorization evidence.
- A graceful restart cannot guarantee that Codex will accept the close request, finish closing within the bound, restore every UI detail, or relaunch successfully. Failure never escalates to forced termination; if closing succeeded but relaunch failed, the user must reopen Codex normally.

The window's installation diagnostics are read-only and sanitized. They may show component versions/build IDs, cache paths, plugin enabled state, shortcut paths, elevation, runtime mode, and a derived connector fingerprint; they must never include the endpoint bearer secret, a claim token, request bodies, command output, or a raw connector instance ID.

Run the broker unelevated by default. The window header shows the current privilege level. If it is intentionally launched elevated, command request details also state that an approved command inherits elevation.

An explicit **导出脱敏操作摘要** action writes only a whitelisted summary of the current bounded in-memory history: request IDs, hashes, state, timing, exit status, and derived project hashes. It excludes command bodies, paths, output, source labels, tokens, and raw connector IDs. This is an optional local export, not automatic command-history persistence.

## Explicitly unsupported

- Executing \`codex.exe\` or \`codex app-server\`.
- Codex task routing or mutation, database edits, rollout/session inspection, deep links, authorization-driven named-pipe control, or UI automation. The sole named-pipe exception is the reviewed read-only `list_threads` lifecycle inventory described above.
- Force-killing Codex, restarting while the inventory is unavailable/stale/partial, or exposing restart through MCP.
- Approval through IPC, command line, environment variables, files, or any token. The model-visible claim token grants read/wait/cancel/terminate access and never approval.
- Raw shell fragments.
- Persistent command history in the broker.
- Persistent task, project, or global routing scope in the broker.

Any future feature that weakens one of these boundaries requires a new threat-model review.

Recovery credentials are capabilities, not approval. Do not expose them in diagnostic exports. Recovery cannot restore a Broker record lost on process restart and does not assert that external side effects were absent. Environment overrides are part of the exact command proposal; inherited environment values are not copied into public snapshots or recovery files.
