"""Isolated release MCP roundtrip; synthetic question answers, no GUI or commands."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=Path("dist/golden-finger-0.4.0"))
    parser.add_argument("--output", type=Path, default=Path("build/exe-integration-040-result.json"))
    options = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    build_root = (workspace / "build").resolve()
    package = options.package.resolve()
    output = options.output.resolve()
    require(output.is_relative_to(build_root), "Result must stay inside the build directory")
    executable = package / "bin" / "golden-finger-mcp.exe"
    sources = package / "scripts"
    require(executable.is_file(), "Release MCP EXE is not ready")
    require((sources / "relay_protocol.py").is_file(), "Stamped release sources are missing")
    sys.path.insert(0, str(sources))
    import relay_protocol as protocol
    import relay_broker as broker
    import install_release as installer

    require(protocol.RUNTIME_BUILD_ID.startswith("rel-"), "Fixture must use the stamped release build")
    report = {
        "passed": False,
        "fixture": "headless loopback broker from stamped release sources",
        "mcp_executable": str(executable),
        "package_version": protocol.PACKAGE_VERSION,
        "build_id": protocol.RUNTIME_BUILD_ID,
        "protocol": protocol.PROTOCOL_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "business_commands_executed": 0,
        "gui_opened": False,
        "approval_performed": False,
        "synthetic_question_answers": 0,
    }
    original_localappdata = os.environ.get("LOCALAPPDATA")
    process = None
    server = None
    serving = None
    store = None
    stderr_chunks = []
    frames = queue.Queue()
    pending_frames = {}
    progress_frames = []
    exit_code = 1
    temporary = tempfile.TemporaryDirectory(prefix="exe-integration-", dir=build_root)
    temporary_path = Path(temporary.name).resolve()
    require(temporary_path.is_relative_to(build_root), "Temporary cleanup target escaped build directory")

    def send(message):
        require(process is not None and process.poll() is None, "MCP EXE exited before request")
        process.stdin.write((json.dumps(message, ensure_ascii=True) + "\n").encode("ascii"))
        process.stdin.flush()

    def receive(rpc_id, seconds=12):
        if rpc_id in pending_frames:
            return pending_frames.pop(rpc_id)
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"No MCP response for test request {rpc_id}")
            try:
                value = frames.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"No MCP response for test request {rpc_id}") from None
            if not isinstance(value, dict):
                raise RuntimeError("MCP EXE closed or produced an invalid frame")
            if value.get("id") == rpc_id:
                return value
            if value.get("method") == "notifications/progress":
                progress_frames.append(value["params"])
                continue
            pending_frames[value.get("id")] = value

    def read_stdout():
        try:
            for line in process.stdout:
                if line.strip():
                    frames.put(json.loads(line))
        except Exception:
            frames.put(None)
        finally:
            frames.put(None)

    def read_stderr():
        # Retain only a byte count. No raw diagnostic content or capabilities
        # are persisted in the result file.
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            stderr_chunks.append(len(chunk))

    def structured(response):
        require("error" not in response, "MCP returned a JSON-RPC error")
        value = response.get("result", {}).get("structuredContent")
        require(isinstance(value, dict), "Tool response lacks structuredContent")
        return value

    try:
        os.environ["LOCALAPPDATA"] = str(temporary_path)
        require(protocol.endpoint_path().resolve().is_relative_to(temporary_path), "Endpoint was not isolated")
        store = broker.RequestStore(elevated=False)
        token = protocol.new_bearer_token()
        server = broker.RelayTCPServer(
            ("127.0.0.1", 0), broker.RelayRequestHandler, token=token, store=store,
        )
        endpoint = protocol.write_endpoint({
            "protocol": protocol.PROTOCOL_VERSION,
            "host": "127.0.0.1",
            "port": server.server_address[1],
            "token": token,
            "pid": os.getpid(),
            "started_at": broker.iso_timestamp(),
            "broker_version": protocol.PACKAGE_VERSION,
            "broker_build_id": protocol.RUNTIME_BUILD_ID,
            "broker_elevated": False,
        })
        require(endpoint.resolve().is_relative_to(temporary_path), "Fixture wrote a non-isolated endpoint")
        serving = threading.Thread(target=server.serve_forever, daemon=True, name="release-test-broker")
        serving.start()
        ready = installer.probe_broker({"version": protocol.PACKAGE_VERSION, "build_id": protocol.RUNTIME_BUILD_ID}, protocol.PROTOCOL_VERSION)
        require(ready is not None and ready["pid"] == os.getpid(), "Launcher's exact-build health probe failed")
        require(server.connector_snapshot() is None, "Readiness probe incorrectly claimed an MCP connection")
        report["checks"].append("launcher_readiness_does_not_fake_mcp_connection")
        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["CODEX_THREAD_ID"] = "10101010-2020-4030-8040-505050505050"
        process = subprocess.Popen(
            [str(executable)], cwd=str(package), env=child_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        reader = threading.Thread(target=read_stdout, daemon=True, name="release-test-stdout")
        errors = threading.Thread(target=read_stderr, daemon=True, name="release-test-stderr")
        reader.start()
        errors.start()
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "isolated-release-test", "version": "1"}},
        })
        initialized = receive(1)
        require(initialized.get("result", {}).get("serverInfo", {}).get("version") == protocol.PACKAGE_VERSION,
                "Packaged initialize returned a different version")
        report["checks"].append("initialize_version_matches")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "relay_health", "arguments": {}}})
        health = structured(receive(2))
        require(health.get("reachable") is True, "Packaged connector cannot reach isolated fixture")
        require(health.get("connector_build_id") == protocol.RUNTIME_BUILD_ID == health.get("broker_build_id"),
                "Connector EXE and fixture build IDs differ")
        report["checks"].append("health_exact_release_build_matches")
        send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "request_consent", "_meta": {"progressToken": "isolated-progress"}, "arguments": {
                "exact_action": "Test one isolated text-only consent request.",
                "target": "Temporary release test fixture",
                "risk": "No external action, command, or approval is performed.",
                "recovery": "Cancel the pending test request and remove the fixture.",
                "idempotency_key": "release-exe-consent-cancellation",
                "source_label": "isolated-release-test",
                "source_project": "temporary-fixture",
                "source_task": "MCP EXE cancellation regression",
                "approved_reply": "This reply must never be approved during this test.",
                "wait_seconds": 900,
            }},
        })
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            _, rows, pending, running = store.gui_snapshot()
            if rows and rows[0]["state"] == "pending" and rows[0]["poll_policy"]["pending_polls_used"] >= 1:
                break
            time.sleep(0.02)
        else:
            raise TimeoutError("Text consent did not enter its blocking wait")
        require(len(rows) == 1 and pending == 1 and running == 0, "Unexpected fixture request or execution")
        report["checks"].append("text_consent_reaches_pending_long_wait")
        progress_deadline = time.monotonic() + 3
        while not progress_frames and time.monotonic() < progress_deadline:
            frame = frames.get(timeout=max(0.01, progress_deadline - time.monotonic()))
            require(isinstance(frame, dict), "Connector exited while waiting for progress")
            if frame.get("method") == "notifications/progress":
                progress_frames.append(frame["params"])
            else:
                pending_frames[frame.get("id")] = frame
        cancelled_at = time.monotonic()
        send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3, "reason": "isolated release test"}})
        cancelled_response = structured(receive(3))
        if cancelled_response.get("ok") is True:
            require(cancelled_response.get("request", {}).get("state") == "cancelled", "MCP returned a non-cancelled request")
            cancellation_mode = "terminal_snapshot"
        else:
            error = cancelled_response.get("error", {})
            details = error.get("details", {})
            require(error.get("code") == "request_cancelled", "MCP returned an unrelated cancellation error")
            require(details.get("broker_cancellation") == "acknowledged" and details.get("state") == "cancelled",
                    "MCP cancellation lacks broker acknowledgment")
            cancellation_mode = "acknowledged_error"
        with store._condition:
            records = list(store._records.values())
            require(len(records) == 1, "Fixture created duplicate requests")
            require(records[0].state == "cancelled" and records[0].kind == "consent", "Fixture did not cancel text consent")
            require(records[0].receipt is None and records[0].process is None, "An approval or process unexpectedly occurred")
        report["cancellation_response"] = cancellation_mode
        report["cancellation_seconds"] = round(time.monotonic() - cancelled_at, 3)
        report["checks"].append("cancellation_confirmed_by_exe_and_broker")
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "relay_health", "arguments": {}}})
        final_health = structured(receive(4))
        require(final_health.get("reachable") is True, "Connector did not remain healthy after cancellation")
        report["checks"].append("health_after_cancellation")
        require(any(frame.get("progressToken") == "isolated-progress" for frame in progress_frames),
                "Active long wait did not emit requested progress")
        values = [frame["progress"] for frame in progress_frames if frame.get("progressToken") == "isolated-progress"]
        require(all(a < b for a, b in zip(values, values[1:])), "Progress values are not strictly increasing")
        report["checks"].append("active_progress_is_monotonic")

        consent_payload = {
            "exact_action": "Recover this isolated text-only pending receipt after connector restart.",
            "target": "Temporary fixture", "risk": "No execution", "recovery": "Cancel fixture",
            "idempotency_key": "release-exe-recovery", "approved_reply": "Never approved", "wait_seconds": 0,
            "source_label": "release-test", "source_project": "isolated-fixture", "source_task": "receipt recovery",
        }
        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "request_consent", "arguments": consent_payload}})
        pending_receipt = structured(receive(5))
        require(pending_receipt.get("request", {}).get("state") == "pending", "Recovery fixture did not submit")
        require(pending_receipt["polling"]["next_arguments"]["wait_seconds"] == 0, "Immediate polling preference was not retained")
        process.stdin.close()
        process.wait(timeout=8)
        reader.join(2)
        errors.join(2)
        require(process.returncode == 0, "First connector did not close cleanly")
        process.stdout.close()
        process.stderr.close()
        while not frames.empty():
            frames.get_nowait()
        pending_frames.clear()
        process = subprocess.Popen([str(executable)], cwd=str(package), env=child_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reader = threading.Thread(target=read_stdout, daemon=True)
        errors = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        errors.start()
        send({"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "isolated-restart-test", "version": "1"}}})
        require("result" in receive(6), "Restarted connector did not initialize")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "request_consent", "arguments": consent_payload}})
        recovered = structured(receive(7))
        require(recovered.get("idempotent_replay") is True, "Connector restart did not recover its journal")
        require(recovered["request"]["request_id"] == pending_receipt["request"]["request_id"], "Recovery created a duplicate request")
        require(len(store._records) == 2, "Unexpected duplicate request after connector restart")
        report["checks"].append("dpapi_receipt_recovers_across_real_connector_restart")
        send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "recover_authorization", "arguments": pending_receipt["recovery"]}})
        require(structured(receive(8))["request"]["request_id"] == recovered["request"]["request_id"], "Explicit recovery failed")
        send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "cancel_authorization", "arguments": {"request_id": recovered["request"]["request_id"], "claim_token": recovered["claim_token"]}}})
        require(structured(receive(9))["request"]["state"] == "cancelled", "Recovered request did not cancel")
        report["checks"].append("explicit_recovery_and_cancellation")

        preflight_args = {"executable": sys.executable, "arguments": ["-c", "raise RuntimeError('MUST-NOT-EXECUTE')"],
                          "working_directory": str(temporary_path), "environment": {"GF_PREFLIGHT_SECRET": "DO-NOT-ECHO"}, "output_encoding": "utf-8"}
        send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "preflight_command", "arguments": preflight_args}})
        checked = structured(receive(10))
        require(checked.get("ok") is True and checked.get("executed") is False, "EXE preflight failed")
        require("DO-NOT-ECHO" not in json.dumps(checked), "Preflight disclosed environment values")
        report["checks"].append("preflight_read_only_and_redacted")

        decision_base = {
            "exact_action": "Synthetic question: should prompting stop?",
            "target": "Isolated in-memory preference", "risk": "No action or command execution",
            "recovery": "Discard test fixture", "source_label": "release-test",
            "source_project": "isolated-fixture", "source_task": "typed question receipt",
            "wait_seconds": 0,
        }
        decision_cases = [
            ({"decision_type": "boolean", "true_label": "Stop prompting", "false_label": "Keep prompting"},
             "reject", "answered", {"type": "boolean", "value": False}),
            ({"decision_type": "single_select", "options": [
                {"id": "first", "label": "Keep existing"},
                {"id": "second", "label": "Change (Recommended)", "recommended": True}]},
             "approve", "answered", {"type": "single_select", "selected_index": 0,
                                      "option_id": "first", "label": "Keep existing"}),
            ({"decision_type": "single_select", "options": [
                {"id": "first", "label": "Keep existing"}, {"id": "second", "label": "Change"}]},
             "reject", "rejected", None),
            ({"decision_type": "boolean"}, "stop", "cancelled", None),
        ]
        for index, (variant, action, terminal, expected_answer) in enumerate(decision_cases):
            rpc_id = 20 + index * 2
            args = {**decision_base, **variant, "idempotency_key": f"release-decision-{index}"}
            send({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                  "params": {"name": "request_decision", "arguments": args}})
            pending_decision = structured(receive(rpc_id))
            require(pending_decision.get("request", {}).get("state") == "pending", "Decision EXE submission failed")
            request_id = pending_decision["request"]["request_id"]
            # The harness decides only its own synthetic, side-effect-free test
            # question in its isolated store. No user approval is simulated.
            {"approve": store.approve_from_gui, "reject": store.reject_from_gui,
             "stop": store.cancel_from_gui}[action](request_id)
            send({"jsonrpc": "2.0", "id": rpc_id + 1, "method": "tools/call",
                  "params": {"name": "wait_for_authorization",
                             "arguments": pending_decision["polling"]["next_arguments"]}})
            final_decision = structured(receive(rpc_id + 1))
            require(final_decision["request"]["state"] == terminal, "Decision state changed in MCP transport")
            require(final_decision["request"]["result"].get("answer") == expected_answer, "Decision answer lost meaning or option order")
            require(final_decision["polling"]["terminal"] is True, "Decision answer was not treated as terminal")
            require(store.detail_for_gui(request_id).get("process") is None, "Question unexpectedly spawned a process")
            report["synthetic_question_answers"] += 1
        report["checks"].append("semantic_boolean_false_first_option_reject_stop_roundtrip")
        require(all(record.process is None for record in store._records.values()), "Fixture spawned an unexpected process")
        written_files = {path.relative_to(temporary_path).as_posix() for path in temporary_path.rglob("*") if path.is_file()}
        require("GoldenFinger/endpoint.json" in written_files, "Fixture endpoint is missing")
        require(all(name == "GoldenFinger/endpoint.json" or (name.startswith("GoldenFinger/recovery/") and name.endswith((".sealed", ".lock"))) for name in written_files), "Fixture unexpectedly wrote settings or other state")
        require(any(name.endswith(".sealed") for name in written_files), "Protected receipt was not created")
        report["checks"].append("no_settings_or_user_endpoint_writes")
        report["passed"] = True
        exit_code = 0
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if store is not None:
            store.shutdown()
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            report["mcp_exit_code"] = process.returncode
            if process.returncode != 0:
                report["passed"] = False
                exit_code = 1
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        if server is not None:
            if serving is not None and serving.is_alive():
                server.shutdown()
            server.server_close()
        if serving is not None:
            serving.join(2)
        if original_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = original_localappdata
        temporary.cleanup()
        report["temporary_fixture_removed"] = not temporary_path.exists()
        report["stderr_bytes"] = sum(stderr_chunks)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "result_file": str(output), "checks": len(report["checks"])}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
