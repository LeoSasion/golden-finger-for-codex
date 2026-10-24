# External testing

## 0.3.0 execution, installation, and UI acceptance

The non-automatic-approval regression suites are `test_mcp_runtime.py`, `test_execution_runtime.py`, `test_release_workflow.py`, `test_diagnostics_ui.py`, and `test_tray_runtime.py`. Existing automatic-approval tests remain unchanged. Additional acceptance:

- Saturate ordinary MCP workers and Broker blocking waits; verify health, immediate read, cancellation and explicit termination still respond. Cancel during submission and after claim-cache eviction; report acknowledged, already-started, or unavailable outcomes without hanging or losing the claim.
- Execute bounded fixture commands in two different projects and in two subdirectories of the same Git workspace. Verify global concurrency four, serialization within the same workspace, queued termination without startup, and starting/running termination with retained output.
- Return large Chinese text, backslashes and quotes in both stdout and stderr with long RPC IDs. Check the final serialized frames stay within 1 MiB and preserve state/exit status and truncation flags.
- Disable automatic selection while keeping notifications enabled; new cards must still notify once without replacing the selected card. Open details and verify later cards do not replace the selection.
- Test tray creation failure, normal unexpected exit, explicit show, and exit with active requests. Never leave the window hidden after an unavailable tray. Restart Explorer and verify the icon or window remains recoverable.
- Import a complete ZIP into an isolated installation root; check status, stable launchers, upgrade, rollback and export. Reject modified inventories, extra files, path traversal, links, running installations and unexpected ownership. Verify the original README hero survives all copies.
- Check malformed cache markers and ambiguous plugin entries; the diagnostic must remain readable and provide qualified next steps. Export a summary and inspect it for absence of command text, output, raw source labels and tokens.

Source unit checks are not a substitute for manual validation on clean Windows 10/11, multiple monitors, 100%/150%/200% DPI, Narrator and restricted organization policies. Record unavailable environments and failed screenshot tools explicitly.

These checks are intentionally separated from Codex Desktop. Run them in a user-controlled terminal only after deciding to test this exact source tree.

## 1. Static and unit checks

From the plugin root:

    python scripts\static_validate.py
    python -m unittest discover -s tests -v

The unit suite does not launch Codex or any user-visible GUI. On Windows it creates hidden Tk controls for layout and native-button checks, plus bounded, harmless Python process trees and loopback servers to verify containment, cancellation, queueing, and termination. Tests own their temporary directories and process cleanup; they do not modify the user's Codex installation or plugin settings.

## 2. Start the independent window

In an ordinary, unelevated terminal:

    python scripts\relay_broker.py

Expected result: a window titled “Codex金手指” appears with the effective privilege level in the header. Starting a second copy must fail; each command request detail must also identify the effective execution privilege.

For the user-invoked no-console path, close the first window and double-click:

    scripts\launch_relay.pyw

Expected result: the same ordinary-privilege window appears without a terminal. Double-click the launcher again while the first window remains open; the second launch must show a visible error dialog and must not start another broker. Verify that no login-startup entry, scheduled task, service, or Codex hook was created.

## 3. Natural-language callback smoke test

For the existing manual-click, rejection, expiry, scope, and lifecycle checks below, first set “自动审批等待” to `0` and click “应用”. The default is now five seconds, so leaving it enabled would approve pending cards before these manual checks finish. Section 13 separately exercises the automatic path.

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
- At first open, verify the two primary lanes are visible and the third detail layer is collapsed. The large circular “同意 / 本次” control must be centered, with a small neutral “停止” on its left and a small red “拒绝” on its right; all three circles must share a horizontal centerline. Empty, filtered-empty, and read-only task views must keep all three controls visible but disabled and clear the selected authorization request. Click “详细参数” on a request and verify a scrollable detail layer covers both lanes without changing the outer window width; Escape and “收起” must restore focus to the detail trigger.
- Resize repeatedly between `980 × 720`, the comfortable `920 × 600` baseline, and the `640 × 600` Windows Snap minimum at 100%, 150%, and 200% display scaling. Below 820px the redundant wait-policy label should hide and the restart action should occupy its own row. The task list, authorization summary, three circular controls, and detail overlay must remain operable; no circular hit target may cross the window boundary. Repeat the glass-gap click-through check after each resize.
- Verify the left list defaults to active items and can switch to all items. Codex tasks and relay requests must use distinct rows; task titles and paths are display-only, while a new pending relay request still becomes the selected row automatically.
- Submit two fixed requests in sequence. The second request must move to the first row, become selected, scroll into view, restore a minimized relay window, and keep it topmost while it remains pending.
- With another application focused, submit a new request and click “同意本次” once. The first complete press-and-release must activate the window and approve exactly once; repeat at different points in the 250 ms refresh interval to verify repainting cannot clear the pressed state. Keyboard focus should land on the non-actionable request list, so unrelated Enter or Space input cannot approve.
- For “同意 / 本次”, “停止”, and “拒绝”, verify distinct default, hover, pressed, keyboard-focus, and disabled states with smooth antialiased edges in both themes. With Accessibility Insights or Narrator, verify each is exposed as a named Button with the correct enabled state and Invoke action; invoking a disabled control must fail, and invoking an enabled control must execute exactly once.
- Resolve or reject every pending request. The relay must release topmost state after the pending count reaches zero.
- With “新请求提示音” enabled, each newly created request should sound once; polling or repainting the same request must stay silent. Disable the option, submit another request, and verify silence. Restart the relay and verify that the sound choice persisted.
- Open “接入检查”. Verify the pane scrolls, refreshes, copies successfully, and reports the current Broker/connector version and exact build ID, runtime mode, protocol, plugin enabled state, cache/shortcut state, elevation, actual Job Object creation capability, and a derived connector fingerprint. Missing or conflicting config is unknown, never `None` or falsely disabled. A historical matching connector must not imply the current chat is integrated. It must not include the endpoint bearer value, a `claim_token`, request body/output, or raw connector instance ID.

## 8. Short polling, monotonic validity and immediate stop

- Verify the header shows the 9,000-second validity cap, with no editable poll-policy inputs. Put old poll values in `ui-settings.json`, restart, and verify they are ignored while the sound preference still persists.
- Start the broker once from an elevated user-controlled terminal only for this review. There must be no red privilege banner, but a command request detail must state `执行权限: 管理员（获批命令将继承此权限）`. Close it before continuing ordinary-privilege tests.
- Create a request and verify its frozen caps are 9,000 seconds pending validity and 9,000 seconds command execution time; explicit caller values may shorten them. Poll count is informational and `pending_polls_remaining` is null.
- Verify MCP waits default to 30 seconds and accept 0–900 seconds. The selected wait must appear unchanged in `polling.next_arguments`; zero reads immediately.
- Perform more than 10 repeated and concurrent short waits. The request must remain pending until its original monotonic deadline, with no deadline extension. Use an isolated test clock to verify expiry without a long-running wait.
- Approve a command before its pending deadline. Verify later polls do not terminate it based on their count; execution uses its independent frozen timeout after startup, excluding queue time.
- While one 900-second wait is active on a pending card, click the window's “停止”. The call must return promptly with `state=cancelled` and `reason_code=stopped_by_user`, without waiting for the 900-second deadline. Verify no approval reply and no rejection receipt is produced. An approved command must instead expose a separate “终止” action and confirmation dialog.

## 9. Concurrent-project claim isolation

- Start two `manual_smoke_client.py consent` runs from separate terminals before deciding either card. The shared window must display two independent `gfr_` request IDs with different claim fingerprints.
- Give the two clients distinct project/task labels and confirm the newest card automatically shows the right human-readable origin plus a different connector fingerprint. Treat those values as weak evidence only: the claim token must remain the enforced isolation boundary.
- Reject one card and approve the other. Each terminal must receive only the terminal state of its own request; neither may attach to the other card merely because both use the same shared Broker window.
- The unit suite must retain negative coverage proving that a request ID with no token or a wrong token gets `claim_mismatch`, that public snapshots contain no raw token/connector ID, and that the same idempotency key in two connector-instance namespaces creates two independent requests.
- Cancel one active MCP tool call from Desktop during a live integration run. The connector must read the notification during a long wait, stop waiting, and attempt cancellation using the original claim. Verify the reported Broker cancellation acknowledgement, rather than treating socket interruption as proof. Cancellation racing with approval must not kill an already approved/running command. A separate explicit `terminate_command` may terminate that request only with its matching claim.

## 10. Scope and window-lifecycle recovery

- Install and enable the plugin in the user's personal/global Codex plugin environment, then open a fresh task in an unrelated project that contains no Golden Finger source files. Verify the Skill and MCP are discoverable there. A project-local checkout or an `AGENTS.md` scope marker alone must not be treated as installing the plugin. Submit one card and verify the unrelated project's normalized root and task title appear in the window.
- In a fresh Codex task with no persisted scope marker, trigger the Skill four times independently and choose `once`, `task`, `project`, and `global`. The first question must state that scope is routing only and every action still needs its own frozen card; automatic receipts are not proof of a human click. `project` must display the normalized absolute project root; it must not be offered when no root is known.
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

Plugin installation, Codex restart, and live MCP integration are separate client-impacting actions. Do not perform them as part of the source/unit smoke tests. When explicitly authorized, install the protocol-7 client plugin and window runtime from the same exact build, start the window manually, call \`relay_health\`, then test \`request_consent\` before testing the fixed print command. Health must report matching connector/Broker versions and build IDs. For the pull-loop check, use one separately reviewed fixed command that remains running longer than the frozen per-call wait: Codex must follow each response's exact `polling.next_arguments`, including the same request ID and claim token, until the terminal result, without asking the user to send an extra completion message.

For the lifecycle panel, first verify that any active Codex/ChatGPT task or pending/running relay request disables “安全重启 Codex”. Simulate unsupported schema, partial sources, a full 50-row non-pinned result, an unknown status, stale inventory, and a non-Codex named-pipe owner; each case must keep restart disabled. Only under a separately explicit live-restart authorization, let every task become idle and click once: the window must acquire its intake gate, refresh inventory, send normal close, refuse force termination on timeout, and relaunch the exact validated Codex Desktop executable. The source/unit tests must mock or stop before the real close/relaunch step.

## 12. Self-contained and clean-machine release

In a fresh Windows checkout or disposable VM, with Python available only for the build step:

    python -m pip install -r requirements-build.txt
    python scripts\build_windows_release.py
    python scripts\verify_windows_release.py dist\golden-finger-0.4.0

The verifier must recalculate every manifest hash, require the shared PyInstaller onedir layout and sibling `bin\golden-finger.runtime` directory, initialize `bin\golden-finger-mcp.exe`, and run `bin\GoldenFinger.exe --self-test-output ...` without opening the GUI. Broker and MCP must report the version from `release_metadata.VERSION` and the identical stamped `rel-…` build ID in self-contained mode; the Broker probe must successfully create a Windows Job Object. The package must also contain `bin\GoldenFingerSetup.exe`, complete original README assets, and installation instructions. Tests on Python 3.13 and 3.14 precede one canonical Python 3.13 build; tag publishing uses that same verified artifact.

Copy the complete built package, including `bin\golden-finger.runtime`, to a clean Windows user account with no Python on PATH; copying either EXE by itself is unsupported. Launch `bin\GoldenFinger.exe` manually and verify the window appears. Point the package's `.mcp.json` at `bin\golden-finger-mcp.exe` through the separately authorized installation flow, restart/open a new Codex task as required, then repeat health and consent smoke checks. Confirm no build directory, source interpreter, developer checkout, or user-level Python dependency is required at runtime.

The GitHub Actions `Windows release verification` workflow must pass from a clean checkout and upload `golden-finger-0.4.0-windows-x64.zip` and its `.sha256` file. Publishing that artifact, installing it, changing shortcuts, or replacing a running window must stay within the user's authorized scope.

## 13. Automatic approval wait

- Set “自动审批等待” to `5` and apply. Submit the fixed consent smoke scenario without clicking. It must remain pending for the full five seconds from first GUI observation, then become approved at a subsequent 250 ms refresh; the reply is “同意”. Its receipt must say `approval_mode=automatic`, contain the applied wait, and contain no `human_action`.
- Under a separately reviewed command smoke run, leave the fixed print card untouched. After the wait it must enter running and complete using the same frozen-executable checks and Job Object as manual approval. An automatic receipt does not replace native Codex approval or any rule requiring explicit human confirmation.
- Change the wait to a different whole second value and apply. Every pending card starts a fresh complete wait; restart the window and verify the chosen wait persists independently of the fixed 900 × 10 polling policy. Set it to `0`, apply, and verify no untouched card is approved.
- Submit two consent cards a few seconds apart and switch selection. Each card retains its own wait; selection does not move or reset another card's deadline. Manually approve, reject, stop, or cancel one before its deadline; its terminal decision must never be overwritten. Expired cards must not be approved.
- Begin editing the parameter without applying it, open the detail overlay, hold a decision button pressed, or open the window-close confirmation. Automatic approval must pause and discard old timers; when interaction ends, every still-pending card receives a new complete wait. Invalid, fractional, negative, empty, or out-of-range input must not enable approval. A corrupt saved settings file disables automatic approval.
- Check the wait input, apply button, status warning, and countdown in both themes and at the Snap minimum width. Restore the desired wait after testing; do not silently change a real running broker's preferences from tests.

## 14. Isolated packaged regressions

After building, these checks use temporary installation roots, an isolated loopback Broker and current-user protected receipts. They never install into the user's existing root, modify Codex settings, authorize a real user action or execute a business command. The MCP check starts the real packaged connector twice to verify recovery across process restart and answers only synthetic, side-effect-free questions in its own isolated store.

```powershell
$env:GOLDEN_FINGER_RELEASE_PACKAGE = (Resolve-Path dist/golden-finger-0.4.0).Path
python -m unittest discover -s tests -p test_release_enhancements.py -v
python tests/verify_mcp_release.py --package "$env:GOLDEN_FINGER_RELEASE_PACKAGE"
Remove-Item Env:GOLDEN_FINGER_RELEASE_PACKAGE
```

The repair fixture damages only its own active Setup copy and active pointer, then verifies independent repair and recovery. The connector fixture checks exact-build readiness, progress during a pending call, cancellation, protected receipt recovery, and read-only preflight. Source unit tests cover bounded output paging, encodings, startup races and concurrent waits. Long-running soak tests remain deferred; these checks do not substitute for real multi-client, clean-machine or GUI interaction acceptance.
