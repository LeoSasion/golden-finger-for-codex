# Tool contract

## request_consent

Creates one natural-language authorization card. A click returns the exact configured \`approved_reply\`; it executes no program.

Required fields: \`exact_action\`, \`target\`, \`risk\`, \`recovery\`, and \`idempotency_key\`.

The shipped window defaults produce a 3,600-second pending validity from `50 seconds × 72 polls`. The two window inputs are the only policy source and valid changes apply automatically to later requests. The broker freezes the current window policy when it creates the request and caps any shorter caller-supplied validity against that policy. A tool call may return \`pending\` before the validity ends; this is only a bounded poll result.

## request_command_execution

Creates one command card. Approval executes the frozen \`executable\` and \`arguments\` with \`shell=False\`.

Required fields: \`exact_action\`, \`executable\`, \`arguments\`, \`working_directory\`, \`target\`, \`risk\`, \`recovery\`, and \`idempotency_key\`.

The shipped window defaults produce a 3,600-second pending validity and the same 3,600-second post-approval execution timeout. Both are derived from the poll duration multiplied by the pending-poll limit; there is no independent manual command-timeout field. The broker freezes the current window caps into each new request; explicit caller values may shorten but never exceed them.

Command eligibility is determined from the exact frozen executable, argument
array, target, risk, recovery path, and applicable higher-priority rules. The
relay has no blanket exclusion for PowerShell, Python, test suites, or bounded
test supervisors.

The broker resolves and fingerprints the executable before display, verifies it again immediately before process creation, and rejects unsupported Codex-control targets.

## wait_for_authorization

Reads or waits for the state of an existing request. It cannot approve a request.

Zero performs an immediate read without consuming the count. Any positive wait uses the request's exact frozen `poll_policy.wait_seconds`, whose shipped default and hard per-call ceiling are 50 seconds. A \`pending\` or \`running\` result includes a structured polling directive with the exact next tool and arguments. Codex must follow that directive rather than keep a separate polling configuration. The broker does not proactively wake a completed Codex turn.

Positive waits consume the frozen pending-poll budget only while the request remains \`pending\`; zero-wait reads do not. When the pending count or elapsed validity is exhausted before a click, the request becomes \`expired\`. Once a command is \`running\`, its separate frozen execution timeout remains authoritative so a monitoring counter never silently abandons or kills an approved process.

## cancel_authorization

Cancels a pending request. It does not revoke an already clicked approval and cannot reliably undo a process that has started.

## State machine

\`pending -> approved | rejected | cancelled | expired\`

For command requests:

\`pending -> running -> completed | failed | timed_out\`

Terminal snapshots include the request ID, request hash, timestamps, and decision receipt. Command snapshots additionally include exit code, stdout, stderr, and truncation flags.

The relay returns only output from a command it started. It does not monitor commands launched in another terminal or output from unrelated services; this does not restrict the category of an otherwise allowed command started by the relay itself.
