# Security model

The relay separates three roles:

1. Codex proposes one bounded request through the plugin MCP server.
2. The independently launched broker freezes and displays the request.
3. A human either approves that exact request once or rejects it.

The MCP-facing process has no approve operation. Approval exists only as a GUI event in the broker process. The authorization channel never launches or controls Codex and never injects text into another task. A separate, GUI-only lifecycle coordinator may read the running Codex Desktop host's `list_threads` result and perform a fail-closed graceful restart after a direct user click; it is not callable through the relay protocol.

## Guarantees

- Loopback-only IPC with a per-launch random bearer secret.
- Per-request random claim capabilities: `request_id` is only a locator, and the matching `claim_token` is required to read, wait for, or cancel a request.
- Connector-process random namespaces prevent accidental idempotency-key reuse across independently started connector processes.
- Exact package version and runtime build IDs are checked both in endpoint metadata and on every authenticated request. A source/installed or stale-window mismatch fails closed.
- Public snapshots and the GUI expose only a short claim fingerprint; they never expose the claim token or connector instance ID.
- Cards also show caller-supplied project/task/source labels and a derived connector fingerprint. These are human correlation evidence, not authenticated identity.
- In-memory request payloads and results; the endpoint file contains connection metadata only.
- Stable SHA-256 hash over the frozen request.
- SHA-256 fingerprinting of the resolved executable at display time and again immediately before process creation.
- SHA-256 fingerprinting and pre-launch re-verification of directly named `.ps1`, `.py`, `.pyw`, `.cmd`, and `.bat` argument files.
- On Windows, every approved process is immediately assigned to a kill-on-close Job Object; timeout and Broker shutdown terminate the contained process tree.
- One decision per request and a frozen, hashed polling policy with an expiry time.
- Program plus argument-array execution with \`shell=False\`.
- Bounded runtime and bounded stdout/stderr retention.
- No “always allow” control.
- Routing scope is kept in the Codex instruction layer, never in Broker request state; it grants no approval and is not included in request hashes, claim capabilities, or decision receipts.
- Within one connector-instance namespace, a reused idempotency key must carry the identical Codex proposal. A settings change between retries returns the already-frozen request and the same claim capability rather than applying the new policy or creating a conflict. The same key in another connector namespace creates an independent request.
- Codex task inventory is read only from the already-running desktop app-tools bridge. The connector forwards a host-provided task ID as a weak lookup hint; the Broker dynamically discovers the exact `list_threads` tool, requires the reviewed schema version, includes pinned tasks, rejects partial/truncated/unknown-status responses, and verifies that the named-pipe server process is an allowed Codex Desktop executable.
- Safe restart acquires an atomic Broker intake gate, refreshes the task inventory immediately, and remains disabled unless every Codex/ChatGPT task is non-active and every relay request is terminal. It sends only the normal window-close message, waits a bounded interval, never force-kills Codex, and relaunches the exact validated executable path.

## Non-guarantees

- This is not a replacement for Codex native approvals or sandboxing.
- The loopback secret is same-user isolation, not protection from another process already running as the same Windows user.
- The prototype does not cryptographically prove which physical person clicked.
- A host-provided Codex task ID is a weak lifecycle lookup hint, not authenticated request identity or project ownership. A connector instance ID is still only a random process namespace, and authorization isolation still comes solely from the per-request claim capability.
- The claim token is a bearer capability. A same-user process or another task that obtains the exact token can observe or cancel that request; the broker cannot distinguish legitimate transfer from theft.
- The token necessarily travels through MCP tool results and later tool arguments, so Codex task context or diagnostic logs may retain it. The Broker itself keeps it only in memory and never places it in public snapshots or the GUI.
- A connector restart changes its idempotency namespace. While the Broker and in-memory record still exist, an existing request can be continued with its original claim token; resubmitting the same key after restart is not guaranteed to replay the old card.
- Commands inherit the broker process account, privilege level, environment, and filesystem/network access.
- Only direct supported script arguments are snapshotted. Imports, modules, dot-sourced files, configuration, data, and other content loaded by the program are not recursively attested.
- There is a small operating-system scheduling interval between process creation and Job Object assignment. Assignment failure kills the new process and fails closed, but side effects that occurred in that interval cannot be rolled back.
- Command output is returned to the current Codex context and may be retained there even though the broker itself keeps no persistent history.
- The broker is pull-only. It cannot wake a Codex turn that ended before the connector retrieved a terminal result.
- The connector cannot monitor window liveness in the background. It can detect unavailability only when an active Codex turn calls health, submits, or waits.
- MCP cancellation is delayed by the connector's synchronous bounded wait. Once the notification is read, cancellation is best effort and applies only while the card remains pending.
- Valid window polling changes are automatically saved and apply only to later requests. Every existing request retains the exact policy revision, wait duration, pending-poll budget, and command timeout derived and shown at creation.
- The broker cannot observe commands or output from an independently launched terminal, test supervisor, listener, or other service. This observation limit does not prohibit an otherwise allowed bounded supervisor from being the exact command the broker starts.
- Job Object termination does not reverse filesystem, network, registry, or other side effects already performed. If the MCP result is interrupted, Codex must still treat the external outcome as uncertain and must not replay it automatically.
- Task titles, paths, kinds, and status fields returned by Codex are untrusted display data. Sanitization and schema checks reduce accidental misuse but do not turn them into authorization evidence.
- A graceful restart cannot guarantee that Codex will accept the close request, finish closing within the bound, restore every UI detail, or relaunch successfully. Failure never escalates to forced termination; if closing succeeded but relaunch failed, the user must reopen Codex normally.

The window's installation diagnostics are read-only and sanitized. They may show component versions/build IDs, cache paths, plugin enabled state, shortcut paths, elevation, runtime mode, and a derived connector fingerprint; they must never include the endpoint bearer secret, a claim token, request bodies, command output, or a raw connector instance ID.

Run the broker unelevated by default. If it is intentionally launched elevated, every command request detail displays that fact and states that an approved command inherits elevation; the window does not repeat it in a separate banner.

## Explicitly unsupported

- Executing \`codex.exe\` or \`codex app-server\`.
- Codex task routing or mutation, database edits, rollout/session inspection, deep links, authorization-driven named-pipe control, or UI automation. The sole named-pipe exception is the reviewed read-only `list_threads` lifecycle inventory described above.
- Force-killing Codex, restarting while the inventory is unavailable/stale/partial, or exposing restart through MCP.
- Approval through IPC, command line, environment variables, files, or any token. The model-visible claim token grants only read/wait/cancel access and never approval.
- Raw shell fragments.
- Persistent command history in the broker.
- Persistent task, project, or global routing scope in the broker.

Any future feature that weakens one of these boundaries requires a new threat-model review.
