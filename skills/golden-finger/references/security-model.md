# Security model

The relay separates three roles:

1. Codex proposes one bounded request through the plugin MCP server.
2. The independently launched broker freezes and displays the request.
3. A human either approves that exact request once or rejects it.

The MCP-facing process has no approve operation. Approval exists only as a GUI event in the broker process. The broker never launches Codex, calls an App Server, reads Codex state, or injects text into another task.

## Guarantees

- Loopback-only IPC with a per-launch random bearer secret.
- In-memory request payloads and results; the endpoint file contains connection metadata only.
- Stable SHA-256 hash over the frozen request.
- SHA-256 fingerprinting of the resolved executable at display time and again immediately before process creation.
- One decision per request and a frozen, hashed polling policy with an expiry time.
- Program plus argument-array execution with \`shell=False\`.
- Bounded runtime and bounded stdout/stderr retention.
- No “always allow” control.
- A reused idempotency key must carry the identical Codex proposal. A settings change between retries returns the already-frozen request rather than applying the new policy or creating a conflict.

## Non-guarantees

- This is not a replacement for Codex native approvals or sandboxing.
- The loopback secret is same-user isolation, not protection from another process already running as the same Windows user.
- The prototype does not cryptographically prove which physical person clicked.
- The connector cannot securely infer a Codex task ID.
- Commands inherit the broker process account, privilege level, environment, and filesystem/network access.
- Arguments can name scripts or data files whose contents are not snapshotted; the executable fingerprint does not attest those files.
- Command output is returned to the current Codex context and may be retained there even though the broker itself keeps no persistent history.
- The broker is pull-only. It cannot wake a Codex turn that ended before the connector retrieved a terminal result.
- Valid window polling changes are automatically saved and apply only to later requests. Every existing request retains the exact policy revision, wait duration, pending-poll budget, and command timeout derived and shown at creation.
- The broker cannot observe commands or output from an independently launched terminal, test supervisor, listener, or other service. This observation limit does not prohibit an otherwise allowed bounded supervisor from being the exact command the broker starts.
- Closing the window can make a running child outcome uncertain if the operating system cannot terminate it cleanly.

Run the broker unelevated by default. If it is intentionally launched elevated, every command request detail displays that fact and states that an approved command inherits elevation; the window does not repeat it in a separate banner.

## Explicitly unsupported

- Executing \`codex.exe\` or \`codex app-server\`.
- Codex task routing, task mutation, database edits, rollout inspection, deep links, named-pipe task control, or UI automation.
- Approval through IPC, command line, environment variables, files, or a model-supplied token.
- Raw shell fragments.
- Persistent command history in the broker.

Any future feature that weakens one of these boundaries requires a new threat-model review.
