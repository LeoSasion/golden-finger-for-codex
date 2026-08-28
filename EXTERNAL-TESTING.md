# External testing

These checks are intentionally separated from Codex Desktop. Run them in a user-controlled terminal only after deciding to test this exact source tree.

## 1. Static and unit checks

From the plugin root:

    python scripts\static_validate.py
    python -m unittest discover -s tests -v

The unit suite does not launch the GUI, Codex, or a command child process.

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

The window must show the exact request and hash. Click “同意本次”. The terminal must finish with state \`approved\` and \`reply_text\` equal to \`同意\`. Repeat with the same request ID is not possible from this fixed client; transport still exposes no approve method.

## 4. Command smoke test

In a second terminal:

    python scripts\manual_smoke_client.py print

The window must display the resolved Python executable, SHA-256, exact \`-c\` argument, working directory, risk, and recovery path. Only after clicking “同意本次” may it run. The final state must be \`completed\`, exit code \`0\`, and stdout must contain:

    Golden Finger for Codex smoke OK

## 5. Rejection and expiry

- Submit either fixed smoke scenario and click “拒绝”; verify state \`rejected\` and that no command output exists.
- The fixed smoke client explicitly requests a five-minute expiry, but the window's frozen pending budget remains the authoritative cap. Submit it and do not click to verify the shorter applied bound.
- Close the window with a pending request; verify the client receives a terminal cancellation or a broker-unavailable error and does not infer approval.

## 6. Detail scrolling

- Submit a request whose action, risk, recovery, or arguments make the right-hand detail longer than the visible pane.
- Scroll down with both the mouse wheel and the visible scrollbar, then wait at least three seconds.
- The 250 ms refresh loop must not return the detail to the top. Selecting a different request should start that request at the top.

## 7. Theme and new-request attention

- In Codex Settings > Appearance, switch among Light, Dark, and System. The relay should follow within two seconds without restarting, including the current `[desktop].appearanceTheme` field, and should retain readable text, selection, focus, disabled, success, and danger states.
- On a supported Windows version, verify a 60% acrylic background while all text remains 100% opaque, plus rounded top-level corners and a matching light/dark title bar. Treat the blur radius as system-controlled; the current `ACCENT_POLICY` path exposes tint alpha but no radius setting. On an unsupported system, verify the explicit opaque-background fallback with 100% opaque text.
- Click every glass-background gap around the header, policy area, split panes, and footer while interactive controls from another application are directly underneath. No click may reach the lower window; a glass-gap click may only focus the relay. Temporarily force hit-shield placement to fail in a review build and verify the client background becomes opaque instead of remaining click-through.
- Resize repeatedly between the initial size and the effective minimum at Windows 100%, 150%, and 200% display scaling. The request panes must absorb the size change; the footer must remain visible, and “同意本次” must stay fully inside the lower-right corner with its complete hit target. Repeat the glass-gap click-through check after each resize.
- Submit two fixed requests in sequence. The second request must move to the first row, become selected, scroll into view, restore a minimized relay window, and keep it topmost while it remains pending.
- With another application focused, submit a new request and click “同意本次” once. The first complete press-and-release must activate the window and approve exactly once; repeat at different points in the 250 ms refresh interval to verify repainting cannot clear the pressed state. Keyboard focus should land on the non-actionable request list, so unrelated Enter or Space input cannot approve.
- Resolve or reject every pending request. The relay must release topmost state after the pending count reaches zero.
- With “新请求提示音” enabled, each newly created request should sound once; polling or repainting the same request must stay silent. Disable the option, submit another request, and verify silence. Restart the relay and verify that the sound choice persisted.

## 8. Polling-policy synchronization

- Verify the policy area is a compact formula rather than three separate cards. Set a 10-second poll and 1 pending poll; it must read `10 秒 × 1 次 = 总上限 10 秒`, apply without a save button, and remain after restarting the relay. Verify values below the 10-second lower bound are rejected without replacing the last valid policy.
- Start the broker once from an elevated user-controlled terminal only for this review. There must be no red privilege banner, but a command request detail must state `执行权限: 管理员（获批命令将继承此权限）`. Close it before continuing ordinary-privilege tests.
- Create one request, then change either window input. Verify the existing request retains its original `policy_revision`, wait, count, expiry, and derived command timeout while the next request receives the automatically applied values.
- Call `wait_for_authorization` with both a smaller and a larger positive value than the frozen per-call wait. Verify both calls use the exact frozen duration and the response's `polling.next_arguments` repeats that same value; only zero performs an immediate, non-consuming read.
- Use positive pending waits until the frozen count is exhausted and verify the request becomes `expired`; zero-wait reads must not consume the count.
- Approve a command before its pending count is exhausted. Verify later waits do not terminate it because of the pending counter and that its frozen command timeout equals the original poll-duration × poll-count product.

## 9. Codex plugin integration

Plugin installation, Codex restart, and live MCP integration are separate client-impacting actions. Do not perform them as part of the source/unit smoke tests. When explicitly authorized, install this plugin source, start the window manually, call \`relay_health\`, then test \`request_consent\` before testing the fixed print command. For the pull-loop check, use one separately reviewed fixed command that remains running longer than the configured per-call wait: Codex must reuse the same request ID and follow each response's exact `polling.next_arguments` until the terminal result, without asking the user to send an extra completion message.
