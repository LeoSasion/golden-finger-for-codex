import os
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from relay_protocol import (RelayError, normalize_request, preflight_command,
                            windows_command_line_units, merge_execution_environment,
                            normalize_environment, verify_script_dependencies)
from command_templates import command_template


class CommandPreflightTests(unittest.TestCase):
    def test_actual_quoted_windows_limit_and_surrogate_units(self):
        with self.assertRaises(RelayError) as caught:
            windows_command_line_units(sys.executable, ["x" * 8192] * 5)
        self.assertEqual(caught.exception.code, "command_line_too_long")
        self.assertGreater(windows_command_line_units("a.exe", ["\U0001f600"]), len("a.exe \U0001f600") + 1)
        with self.assertRaises(RelayError):
            windows_command_line_units("a.exe", ['"' * 8192] * 2)

    def test_preflight_has_no_execution_or_secret_echo(self):
        with tempfile.TemporaryDirectory() as folder, patch("subprocess.Popen") as spawn:
            result = preflight_command({"executable": sys.executable, "arguments": ["-c", "print(1)"],
                                        "working_directory": folder, "environment": {"API_TOKEN": "secret"},
                                        "output_encoding": "cp936"})
            spawn.assert_not_called()
            self.assertEqual(result["output_encoding"], "gbk")
            self.assertFalse(result["executed"])
            self.assertNotIn("secret", str(result))

    def test_environment_validation_and_inheritance(self):
        with patch.dict(os.environ, {"GF_INHERIT": "unchanged"}):
            env = merge_execution_environment({"GF_CUSTOM": "value"})
            self.assertEqual(env["GF_INHERIT"], "unchanged")
            self.assertEqual(env["GF_CUSTOM"], "value")
        for invalid in ({"A=B": "x"}, {"A": "\x00"}, {"A": 1}):
            with self.assertRaises(RelayError):
                normalize_environment(invalid)

    def test_response_file_is_explicit_frozen_and_rechecked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "args.js"
            path.write_text("original")
            frozen = normalize_request("command", {"exact_action": "test", "target": "fixture", "risk": "none",
                "recovery": "none", "idempotency_key": "response-test", "executable": sys.executable,
                "arguments": ["@args.js"], "working_directory": folder, "response_files": ["args.js"]})
            self.assertEqual(frozen["script_dependencies"][0]["kind"], "response_file")
            path.write_text("changed")
            self.assertTrue(verify_script_dependencies(frozen["script_dependencies"]))

    def test_templates_are_argument_vectors_and_freeze_node_scripts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.js"
            path.write_text("// fixture")
            result = command_template("npm", executable=sys.executable, script=str(path), directory=folder,
                                      arguments=["run", "test & literal"])
            self.assertEqual(result["command"]["arguments"], [str(path.resolve()), "run", "test & literal"])
            self.assertEqual(len(result["preflight"]["script_dependencies"]), 1)

    def test_relative_path_search_uses_project_directory_and_templates_resolve_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / "tools"
            scripts.mkdir()
            interpreter = scripts / "fixture.exe"
            interpreter.write_bytes(b"fixture only: never executed")
            entry = root / "-entry.py"
            entry.write_text("raise AssertionError('must not execute')")
            checked = preflight_command({"executable": "fixture.exe", "arguments": [],
                "working_directory": folder, "environment": {"PATH": "tools"}})
            self.assertEqual(checked["executable"], str(interpreter.resolve()))
            result = command_template("python", executable="tools/fixture.exe", script="-entry.py", directory=folder, arguments=[])
            self.assertEqual(result["command"]["executable"], str(interpreter.resolve()))
            self.assertEqual(result["command"]["arguments"][1], str(entry.resolve()))

    def test_template_cli_accepts_options_and_never_runs_script(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.py"
            marker = Path(folder) / "must-not-exist"
            path.write_text("raise AssertionError('preflight executed the script')")
            cli = Path(__file__).resolve().parents[1] / "scripts" / "command_templates.py"
            result = subprocess.run([sys.executable, "-B", str(cli), "python", "--executable", sys.executable,
                "--script", str(path), "--directory", folder, "--", "--literal", "x y"],
                capture_output=True, text=True, check=True, timeout=10)
            value = json.loads(result.stdout)
            self.assertEqual(value["command"]["arguments"][-2:], ["--literal", "x y"])
            self.assertFalse(value["preflight"]["executed"])
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
