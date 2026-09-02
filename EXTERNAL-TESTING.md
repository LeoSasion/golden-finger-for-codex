# External testing

These checks are intentionally separated from Codex Desktop. Run them in a user-controlled terminal only after deciding to test this exact source tree.

## 1. Static and unit checks

From the plugin root:

    python scripts\static_validate.py
    python -m unittest discover -s tests -v

The unit suite does not launch the GUI or Codex. On Windows it creates a Job Object and runs one bounded, harmless Python child solely to verify containment and termination; the test owns cleanup and has no persistent output.

## 2. Start the independent window

In an ordinary, unelevated terminal:

    python scripts\relay_broker.py

Expected result: a window titled “Codex金手指” appears without a redundant privilege banner. Starting a second copy must fail; each command request detail must still identify the effective execution privilege.

For the user-invoked no-console path, close the first window and double-click:

    scripts\launch_relay.pyw

Expected result: the same ordinary-privilege window appears without a terminal. Double-click the launcher again while the first window remains open; the second launch must show a visible error dialog and must not start another broker. Verify that no login-startup entry, scheduled task, service, or Codex hook was created.

## 3. Natural-language callback smoke test

In a second terminal:

    python scripts\manual_smoke_client.py consent

The window must show the exact request, request hash, and a 12-character claim fingerprint; the raw claim token must not appear. Click “同意本次”. The terminal must finish with state \`approved\` and \`reply_text\` equal to \`同意\`. Repeat with the same request ID is not possible from this fixed client; transport still exposes no approve method.

## 4. Command smoke test

In a second terminal:

    python scripts\manual_smoke_client.py print

The window must display the resolved Python executable, SHA-256, exact \`-c\` argument, working directory, source project/task labels, connector fingerprint, risk, and recovery path. Because this scenario uses `-c`, the direct-script list must explicitly say none. Only after clicking “同意本次” may it run. The final state must be \`completed\`, exit code \`0\`, and stdout must contain:

    Golden Finger for Codex smoke OK

The unit suite additionally creates a temporary direct `.py` argument, verifies its SHA-256 is frozen, changes the file, and proves pre-launch re-verification detects the mismatch. During a reviewed live run, make the same change after a direct-script card appears but before clicking: the Broker must return `failed` without starting it. Imports, modules, dot-sourced files, configuration, and data loaded from inside the script remain outside this direct-argument guarantee.

## 5. Rejection and expiry

- Submit either fixed smoke scenario and click “拒绝”; verify state \`rejected\` and that no command output exists.
- The fixed smoke client explicitly requests a five-minute expiry, but the window's frozen pending budget remains the authoritative cap. Submit it and do not click to verify the shorter applied bound.
- Close the window with a pending request; verify the client receives a terminal cancellation or a broker-unavailable error and does not infer approval.
- Close the window with a running reviewed test process. The close prompt must state that the Job Object process tree will be terminated and that completed external side effects cannot be undone. Verify the process exits, but still report an interrupted MCP outcome as uncertain and never replay it.

## 6. Detail scrolling

- Submit a request whose action, risk, recovery, or arguments make the right-hand detail longer than the visible pane.
- Scroll down with both the mouse wheel and the visible scrollbar, then wait at least three seconds.
- The 250 ms refresh loop must not return the detail to the top. Selecting a different request should start that request at the top.

## 7. Theme and new-request attention

- In Codex Settings > Appearance, switch among Light, Dark, and System. The relay should follow within two seconds without restarting, including the current `[desktop].appearanceTheme` field, and should retain readable text, selection, focus, disabled, success, and danger states.
- On a supported Windows version, verify a 60% acrylic background while all text remains 100% opaque, plus rounded top-level corners and a matching light/dark title bar. Treat the blur radius as system-controlled; the current `ACCENT_POLICY` path exposes tint alpha but no radius setting. On an unsupported system, verify the explicit opaque-background fallback with 100% opaque text.
- Click every glass-background gap around the header, fixed policy label, cards, and optional detail overlay while interactive controls from another application are directly underneath. No click may reach the lower window; a glass-gap click may only focus the relay. Temporarily force hit-shield placement to fail in a review build and verify the client background becomes opaque instead of remaining click-through.
- At first open, verify the two primary lanes are visible and the third detail layer is collapsed. The large circular “同意 / 本次” control must be centered, with a small neutral “停止” at roughly ten-to-eleven o'clock and a small red “拒绝” at roughly one-to-two o'clock. Click “详细参数” and verify a scrollable detail layer covers both lanes without changing the outer window width; Escape and “收起” must restore focus to the detail trigger.
- Resize repeatedly between `980 × 720`, the comfortable `920 × 600` baseline, and the `640 × 600` Windows Snap minimum at 100%, 150%, and 200% display scaling. Below 820px the redundant wait-policy label should hide and the restart action should occupy its own row. The task list, authorization summary, three circular controls, and detail overlay must remain operable; no circular hit target may cross the window boundary. Repeat the glass-gap click-through check after each resize.
- Verify the left list defaults to active items and can switch to all items. Codex tasks and relay requests must use distinct rows; task titles and paths are display-only, while a new pending relay request still becomes the selected row automatically.
- Submit two fixed requests in sequence. The second request must move to the first row, become selected, scroll into view, restore a minimized relay window, and keep it topmost while it remains pending.
- With another application focused, submit a new request and click “同意本次” once. The first complete press-and-release must activate the window and approve exactly once; repeat at different points in the 250 ms refresh interval to verify repainting cannot clear the pressed state. Keyboard focus should land on the non-actionable request list, so unrelated Enter or Space input cannot approve.
- For “同意 / 本次”, “停止”, and “拒绝”, verify distinct default, hover, pressed, keyboard-focus, and disabled states with smooth antialiased edges in both themes. With Accessibility Insights or Narrator, verify each is exposed as a named Button with the correct enabled state and Invoke action; invoking a disabled control must fail, and invoking an enabled control must execute exactly once.
- Resolve or reject every pending request. The relay must release topmost state after the pending count reaches zero.
- With “新请求提示音” enabled, each newly created request should sound once; polling or repainting the same request must stay silent. Disable the option, submit another request, and verify silence. Restart the relay and verify that the sound choice persisted.
- Open “诊断”. Verify the pane scrolls, copies successfully, reports the current Broker/connector version and exact build ID, runtime mode, protocol, plugin enabled state, cache/shortcut state, elevation, Job Object support, and a derived connector fingerprint. It must not include the endpoint bearer value, a `claim_token`, request body/output, or raw connector instance ID.

## 8. Fixed polling and immediate stop

- Verify the header shows `等待 900秒 × 10次`, with no editable poll inputs or save control. Put old poll values in `ui-settings.json`, restart, and verify they are ignored while the sound preference still persists.
- Start the broker once from an elevated user-controlled terminal only for this review. There must be no red privilege banner, but a command request detail must state `执行权限: 管理员（获批命令将继承此权限）`. Close it before continuing ordinary-privilege tests.
- Create a request and verify its frozen policy is exactly 900 seconds, 10 pending polls, 9,000 seconds validity, and a 9,000-second derived command timeout.
- Call `wait_for_authorization` with any positive value other than the frozen per-call wait. Verify the broker still uses 900 seconds and the response's `polling.next_arguments` repeats 900; only zero performs an immediate, non-consuming read.
- Use positive pending waits until the frozen count is exhausted and verify the request becomes `expired`; zero-wait reads must not consume the count.
- Approve a command before its pending count is exhausted. Verify later waits do not terminate it because of the pending counter and that its frozen command timeout equals the original poll-duration × poll-count product.
- While one 900-second wait is active on a pending card, click the window's “停止”. The call must return promptly with `state=cancelled` and `reason_code=stopped_by_user`, without waiting for the 900-second deadline. Verify no approval reply and no rejection receipt is produced. The control must be disabled for approved or running commands.

## 9. Concurrent-project claim isolation

- Start two `manual_smoke_client.py consent` runs from separate terminals before deciding either card. The shared window must display two independent `gfr_` request IDs with different claim fingerprints.
- Give the two clients distinct project/task labels and confirm the newest card automatically shows the right human-readable origin plus a different connector fingerprint. Treat those values as weak evidence only: the claim token must remain the enforced isolation boundary.
- Reject one card and approve the other. Each terminal must receive only the terminal state of its own request; neither may attach to the other card merely because both use the same shared Broker window.
- The unit suite must retain negative coverage proving that a request ID with no token or a wrong token gets `claim_mismatch`, that public snapshots contain no raw token/connector ID, and that the same idempotency key in two connector-instance namespaces creates two independent requests.
- Cancel one active MCP tool call from Desktop during a live integration run. Because the connector reads stdin synchronously, its cancellation notification may remain delayed for the full 900-second broker wait. Do not confuse this with the relay-window “停止” path above. If the notification is eventually read, a still-pending card becomes cancelled; an already approved/running command must not be killed.

## 10. Scope and window-lifecycle recovery

- Install and enable the plugin in the user's personal/global Codex plugin environment, then open a fresh task in an unrelated project that contains no Golden Finger source files. Verify the Skill and MCP are discoverable there. A project-local checkout or an `AGENTS.md` scope marker alone must not be treated as installing the plugin. Submit one card and verify the unrelated project's normalized root and task title appear in the window.
- In a fresh Codex task with no persisted scope marker, trigger the Skill four times independently and choose `once`, `task`, `project`, and `global`. The first question must state that scope is routing only and every action still needs its own click. `project` must display the normalized absolute project root; it must not be offered when no root is known.
- For `project` and `global`, verify that only the marked Golden Finger block is added to the disclosed active instruction file. With no override, this is `AGENTS.md`; repeat with an existing `AGENTS.override.md` and verify the block is written there instead of an inactive sibling. Unrelated instructions remain byte-for-byte intact. A new task should inherit that scope, while `task` must not. Cancel the persistent scope and verify that only the marked block is removed from the same file.
- With no window running, call `relay_health`. It must return `reachable=false`, `window_state=not_running_or_unreachable`, and two choices: start the window while keeping scope, or cancel scope. It must submit no card and must not start a process.
- Pause shutdown after the Broker sets its stopping flag but before its TCP endpoint disappears. `relay_health` must return `reachable=false`, `window_state=closing`, and the same two recovery choices; a new request returning `broker_stopping` must also carry them.
- Follow the launch guidance, start the window manually, and call `relay_health` again before submitting. A user message saying “已打开” alone must never be treated as a successful health check.
- Create a pending request and close the window through its close button. The response should be `cancelled` with `reason_code=relay_window_closed` when the graceful result is delivered, or a structured broker-unavailable recovery if the connection closes first. Ask whether to restart and keep the scope or cancel it. Neither choice may be reported as approval or rejection of the action.
- Choose restart. Verify the new window is healthy and that the old request is not resumed or automatically resubmitted. Choose cancel in a separate run; verify future routing stops for that scope and no old request is replayed.
- Close the window while a command is `running`. The result may be uncertain; restarting the window or cancelling scope must not resubmit, rerun, or claim to revoke that command.
- Simulate protocol mismatch, same-version/different-build mismatch, different-version mismatch, invalid endpoint metadata, and endpoint-unreadable errors. They must stay fail-closed errors and must not be described as proof that the user closed the window. A stale 0.1.x window must never accept a 0.2.0 connector.
- End the Codex turn, then close the window. Confirm that no background notification appears; the loss is detected only at the next active health/request/wait call.

## 11. Codex plugin integration

Plugin installation, Codex restart, and live MCP integration are separate client-impacting actions. Do not perform them as part of the source/unit smoke tests. When explicitly authorized, install the protocol-5 client plugin and window runtime from the same exact build, start the window manually, call \`relay_health\`, then test \`request_consent\` before testing the fixed print command. Health must report matching connector/Broker versions and build IDs. For the pull-loop check, use one separately reviewed fixed command that remains running longer than the frozen per-call wait: Codex must follow each response's exact `polling.next_arguments`, including the same request ID and claim token, until the terminal result, without asking the user to send an extra completion message.

For the lifecycle panel, first verify that any active Codex/ChatGPT task or pending/running relay request disables “安全重启 Codex”. Simulate unsupported schema, partial sources, a full 50-row non-pinned result, an unknown status, stale inventory, and a non-Codex named-pipe owner; each case must keep restart disabled. Only under a separately explicit live-restart authorization, let every task become idle and click once: the window must acquire its intake gate, refresh inventory, send normal close, refuse force termination on timeout, and relaunch the exact validated Codex Desktop executable. The source/unit tests must mock or stop before the real close/relaunch step.

## 12. Self-contained and clean-machine release

In a fresh Windows checkout or disposable VM, with Python available only for the build step:

    python -m pip install -r requirements-build.txt
    python scripts\build_windows_release.py
    python scripts\verify_windows_release.py dist\golden-finger-0.2.0

The verifier must recalculate every manifest hash, require the shared PyInstaller onedir layout and sibling `bin\golden-finger.runtime` directory, initialize `bin\golden-finger-mcp.exe`, and run `bin\GoldenFinger.exe --self-test-output ...` without opening the GUI. Both EXE paths remain unchanged; both EXEs must report version `0.2.0`, the identical stamped `rel-…` build ID, and self-contained mode; the Broker probe must successfully create a Windows Job Object.

Copy the complete built package, including `bin\golden-finger.runtime`, to a clean Windows user account with no Python on PATH; copying either EXE by itself is unsupported. Launch `bin\GoldenFinger.exe` manually and verify the window appears. Point the package's `.mcp.json` at `bin\golden-finger-mcp.exe` through the separately authorized installation flow, restart/open a new Codex task as required, then repeat health and consent smoke checks. Confirm no build directory, source interpreter, developer checkout, or user-level Python dependency is required at runtime.

The GitHub Actions `Windows release verification` workflow must pass from a clean checkout and upload only `golden-finger-0.2.0-windows-x64.zip`. Publishing that artifact, installing it, changing shortcuts, or replacing a running window remains a separate explicit action.
