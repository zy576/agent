from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from forgeloop.tools import ToolError, ToolRegistry, Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root, max_output_chars=2_000)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_write_read_replace_search_and_list(self) -> None:
        result = self.workspace.write_file("src/app.py", "value = 1\nprint(value)\n")
        self.assertIn("wrote", result)
        read = self.workspace.read_file("src/app.py")
        self.assertIn("1 | value = 1", read)
        self.assertIn("2 | print(value)", read)

        replaced = self.workspace.replace_in_file(
            "src/app.py", "value = 1", "value = 2"
        )
        self.assertIn("replaced 1", replaced)
        self.assertIn("value = 2", (self.root / "src" / "app.py").read_text())
        self.assertIn("src/app.py:1:value = 2", self.workspace.search_files("value"))
        self.assertIn("src/app.py", self.workspace.list_files())

    def test_read_line_range(self) -> None:
        self.workspace.write_file("numbers.txt", "one\ntwo\nthree\n")
        output = self.workspace.read_file("numbers.txt", start_line=2, end_line=2)
        self.assertIn("2 | two", output)
        self.assertNotIn("one", output)

    def test_rejects_path_escape_absolute_outside_and_git_internals(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.read_file("../outside.txt")
        outside = str((self.root.parent / "outside.txt").resolve())
        with self.assertRaises(ToolError):
            self.workspace.write_file(outside, "no")
        with self.assertRaises(ToolError):
            self.workspace.write_file(".git/config", "no")

    def test_rejects_likely_credential_files(self) -> None:
        for name in (".env", ".env.local", "private.pem", "id_rsa"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.workspace.write_file(name, "secret")

    def test_replace_requires_exact_occurrence_count(self) -> None:
        self.workspace.write_file("a.txt", "x x")
        with self.assertRaises(ToolError):
            self.workspace.replace_in_file("a.txt", "x", "y")
        self.assertEqual((self.root / "a.txt").read_text(), "x x")

    def test_run_command_uses_argv_without_shell_interpretation(self) -> None:
        output = self.workspace.run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "a&echo HACK"]
        )
        self.assertIn("exit_code=0", output)
        self.assertIn("a&echo HACK", output)
        self.assertEqual(output.count("HACK"), 1)

    def test_run_command_supports_workspace_relative_cwd(self) -> None:
        (self.root / "nested").mkdir()
        output = self.workspace.run_command(
            [sys.executable, "-c", "from pathlib import Path; print(Path.cwd().name)"],
            cwd="nested",
        )
        self.assertIn("nested", output)

    def test_run_command_strips_secret_environment_variables(self) -> None:
        variable = "FORGELOOP_CANARY_API_KEY"
        previous = os.environ.get(variable)
        os.environ[variable] = "must-not-leak"
        try:
            output = self.workspace.run_command(
                [
                    sys.executable,
                    "-c",
                    f"import os; print(os.getenv('{variable}', 'missing'))",
                ]
            )
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous
        self.assertIn("missing", output)
        self.assertNotIn("must-not-leak", output)

    def test_run_command_timeout_is_recoverable_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.workspace.run_command(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=0.1,
            )
        self.assertIn("timed out", str(caught.exception))

    def test_destructive_git_command_is_blocked(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.run_command(["git", "reset", "--hard"])


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = ToolRegistry(Workspace(Path(self.temporary.name)))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_malformed_json_becomes_structured_error(self) -> None:
        result = self.registry.execute(
            {
                "function": {
                    "name": "write_file",
                    "arguments": "{not-json",
                }
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["tool"], "write_file")

    def test_unknown_tool_becomes_structured_error(self) -> None:
        result = self.registry.execute(
            {"function": {"name": "launch_missiles", "arguments": "{}"}}
        )
        self.assertFalse(result["ok"])
        self.assertIn("unknown tool", result["error"])

    def test_extra_argument_is_rejected(self) -> None:
        result = self.registry.execute(
            {
                "function": {
                    "name": "list_files",
                    "arguments": json.dumps({"unexpected": True}),
                }
            }
        )
        self.assertFalse(result["ok"])
        self.assertIn("unexpected", result["error"])


if __name__ == "__main__":
    unittest.main()

