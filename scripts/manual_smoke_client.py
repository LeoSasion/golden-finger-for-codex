"""External-terminal smoke client for Golden Finger for Codex.

This helper exposes only two fixed test scenarios and has no approval operation.
It must not be used as a general command runner.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
import time

from relay_protocol import RelayClient, RelayError


TERMINAL_STATES = {
    "approved",
    "rejected",
    "cancelled",
    "expired",
    "completed",
    "failed",
    "timed_out",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit one fixed relay smoke request from an external terminal."
    )
    parser.add_argument(
        "scenario",
        choices=("consent", "print"),
        help="consent returns a button reply; print runs a fixed Python print after approval.",
    )
    return parser.parse_args()


def submit(scenario: str) -> dict:
    client = RelayClient()
    key = "manual-smoke-" + secrets.token_hex(12)
    common = {
        "target": "Golden Finger for Codex smoke-test state only",
        "risk": "This is a bounded local smoke test. It does not contact or control Codex.",
        "recovery": "Reject the request or close the test terminal; no persistent state is written.",
        "expires_in_seconds": 300,
        "idempotency_key": key,
        "source_label": "External manual smoke client",
    }
    if scenario == "consent":
        kind = "consent"
        payload = {
            **common,
            "exact_action": "Return the text 同意 to this fixed smoke client once.",
            "approved_reply": "同意",
        }
    else:
        kind = "command"
        payload = {
            **common,
            "exact_action": "Run this Python interpreter once and print one fixed smoke-test line.",
            "executable": sys.executable,
            "arguments": ["-c", "print('Golden Finger for Codex smoke OK')"],
            "working_directory": str(Path(__file__).resolve().parents[1]),
            "timeout_seconds": 15,
        }

    submitted = client.call("submit", kind=kind, payload=payload)
    snapshot = submitted["request"]
    print(
        json.dumps(
            {
                "submitted": True,
                "request_id": snapshot["request_id"],
                "request_hash": snapshot["request_hash"],
                "state": snapshot["state"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    deadline = time.monotonic() + 305
    while snapshot["state"] not in TERMINAL_STATES and time.monotonic() < deadline:
        policy = snapshot.get("poll_policy")
        wait_seconds = 30
        if isinstance(policy, dict) and isinstance(policy.get("wait_seconds"), int):
            wait_seconds = policy["wait_seconds"]
        response = client.call(
            "wait",
            request_id=snapshot["request_id"],
            wait_seconds=wait_seconds,
        )
        snapshot = response["request"]
        print(f"state={snapshot['state']}", flush=True)
    return snapshot


def main() -> int:
    arguments = parse_args()
    try:
        result = submit(arguments.scenario)
    except RelayError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("state") in {"approved", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
