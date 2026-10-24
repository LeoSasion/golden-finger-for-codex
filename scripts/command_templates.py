"""Prepare explicit noninteractive command arguments without executing a program."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from relay_protocol import RelayError, preflight_command


def command_template(kind: str, *, executable: str, script: str, directory: str,
                     arguments: list[str], environment: dict[str, str] | None = None,
                     output_encoding: str = "auto") -> dict:
    if kind not in {"python", "powershell", "node", "npm"}:
        raise ValueError("Unknown command template")
    base = Path(directory).resolve()
    script = str((base / script).resolve())
    executable_path = Path(executable)
    if not executable_path.is_absolute() and any(separator in executable for separator in ("/", "\\")):
        executable = str((base / executable_path).resolve())
    args = (["-u", script] if kind == "python" else
            ["-NoLogo", "-NoProfile", "-NonInteractive", "-File", script] if kind == "powershell" else [script])
    # npm uses node.exe + the explicit npm-cli.js, never a composed cmd shell string.
    command = {"executable": executable, "arguments": args + arguments,
               "working_directory": str(base), "environment": environment if environment is not None else {},
               "output_encoding": output_encoding}
    return {"command": command, "preflight": preflight_command(command),
            "next_step": "Fill exact_action, target, risk, recovery and a unique idempotency_key when requesting execution. This template has executed nothing."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", choices=["python", "powershell", "node", "npm"])
    parser.add_argument("--executable", required=True, help="Exact interpreter executable, e.g. .venv/Scripts/python.exe or node.exe.")
    parser.add_argument("--script", required=True, help="Direct script path; for npm, the explicit npm-cli.js.")
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    parser.add_argument("--environment-json", default="{}", help="Literal environment overrides as a JSON object.")
    parser.add_argument("--output-encoding", default="auto")
    raw = sys.argv[1:]
    separator = raw.index("--") if "--" in raw else len(raw)
    remaining = raw[separator + 1:] if separator < len(raw) else []
    args = parser.parse_args(raw[:separator])
    try:
        result = command_template(args.template, executable=args.executable, script=args.script,
                                  directory=str(args.directory.resolve()), arguments=remaining,
                                  environment=json.loads(args.environment_json), output_encoding=args.output_encoding)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (RelayError, ValueError) as exc:
        print(json.dumps(exc.as_dict() if isinstance(exc, RelayError) else {"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
