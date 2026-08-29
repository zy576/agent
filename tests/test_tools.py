from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
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
        for name in (
            ".env",
            ".env.local",
            ".NPMRC",
            "private.pem",
            "private.key",
            "id_rsa",
            ".AWS/credentials",
            ".GIT/config",
        ):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.workspace.write_file(name, "secret")

    def test_search_skips_likely_credential_files(self) -> None:
        (self.root / ".env").write_text("CANARY=must-not-leak", encoding="utf-8")
        (self.root / "private.pem").write_text("must-not-leak", encoding="utf-8")
        self.workspace.write_file("safe.txt", "must-not-leak")
        output = self.workspace.search_files("must-not-leak")
        self.assertIn("safe.txt", output)
        self.assertNotIn(".env", output)
        self.assertNotIn("private.pem", output)

    def test_search_does_not_follow_file_symlink_outside_workspace(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("outside-canary", encoding="utf-8")
        link = self.root / "linked.txt"
        try:
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            output = self.workspace.search_files("outside-canary")
            self.assertNotIn("outside-canary", output)
        finally:
            outside.unlink(missing_ok=True)

    def test_replace_requires_exact_occurrence_count(self) -> None:
        self.workspace.write_file("a.txt", "x x")
        with self.assertRaises(ToolError):
            self.workspace.replace_in_file("a.txt", "x", "y")
        self.assertEqual((self.root / "a.txt").read_text(), "x x")

    def test_replace_preserves_crlf_bom_and_missing_final_newline(self) -> None:
        target = self.root / "windows.txt"
        target.write_bytes(b"\xef\xbb\xbfa\r\nb")
        self.workspace.replace_in_file("windows.txt", "a", "x")
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfx\r\nb")

    def test_multiline_lf_arguments_edit_crlf_file_without_line_ending_churn(self) -> None:
        target = self.root / "module.py"
        target.write_bytes(b"\xef\xbb\xbfdef f():\r\n    return 1\r\n")
        self.workspace.replace_in_file(
            "module.py",
            "def f():\n    return 1",
            "def f():\n    return 2",
        )
        self.assertEqual(
            target.read_bytes(), b"\xef\xbb\xbfdef f():\r\n    return 2\r\n"
        )

    @unittest.skipIf(os.name == "nt", "POSIX executable mode")
    def test_atomic_overwrite_preserves_existing_file_mode(self) -> None:
        target = self.root / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        target.chmod(0o755)
        self.workspace.write_file("script.sh", "#!/bin/sh\necho new\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

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
        variables = {
            "FORGELOOP_CANARY_API_KEY": "must-not-leak-key",
            "DATABASE_URL": "must-not-leak-database",
            "AWS_ACCESS_KEY_ID": "must-not-leak-access",
            "GITHUB_PAT": "must-not-leak-github",
            "HTTPS_PROXY": "must-not-leak-proxy",
            "KUBECONFIG": "must-not-leak-kube",
            "PIP_INDEX_URL": "must-not-leak-index",
            "SSH_AUTH_SOCK": "must-not-leak-ssh",
        }
        previous = {name: os.environ.get(name) for name in variables}
        os.environ.update(variables)
        try:
            output = self.workspace.run_command(
                [
                    sys.executable,
                    "-c",
                    "import os; print('|'.join(os.getenv(name, 'missing') for name in "
                    + repr(list(variables))
                    + "))",
                ]
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertIn("|".join(["missing"] * len(variables)), output)
        self.assertNotIn("must-not-leak", output)

    def test_explicit_non_secret_environment_passthrough(self) -> None:
        variable = "FORGELOOP_BUILD_FLAVOR"
        previous = os.environ.get(variable)
        os.environ[variable] = "debug"
        try:
            workspace = Workspace(self.root, pass_env_names=(variable,))
            output = workspace.run_command(
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
        self.assertIn("debug", output)

    def test_explicit_secret_environment_passthrough_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Workspace(self.root, pass_env_names=("EXTRA_API_KEY",))

    def test_run_command_timeout_is_recoverable_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.workspace.run_command(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=0.1,
            )
        self.assertIn("timed out", str(caught.exception))

    def test_destructive_git_command_is_blocked(self) -> None:
        commands = [
            ["git", "reset", "--hard"],
            ["git.exe", "reset", "--hard"],
            [r"C:\Program Files\Git\cmd\git.exe", "reset", "--hard"],
            ["git", "clean", "-df"],
            ["git", "restore", "."],
            ["git", "checkout", "--", "."],
            ["cmd", "/c", "rd /s /q C:\\tmp"],
            ["pwsh", "-Command", "Remove-Item -Recurse C:\\tmp"],
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ToolError):
                self.workspace.run_command(command)

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm"), "npm.cmd unavailable")
    def test_windows_batch_wrapper_is_supported(self) -> None:
        output = self.workspace.run_command(["npm", "--version"], timeout_seconds=20)
        self.assertIn("exit_code=0", output)

    @unittest.skipUnless(os.name == "nt" and shutil.which("npm"), "npm.cmd unavailable")
    def test_windows_batch_wrapper_rejects_metacharacters(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.run_command(["npm", "a&echo injected"])

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_windows_batch_wrapper_supports_spaces_in_path_and_argument(self) -> None:
        directory = self.root / "tools with spaces"
        directory.mkdir()
        script = directory / "hello tool.cmd"
        script.write_text("@echo off\r\necho %~1\r\n", encoding="utf-8")
        output = self.workspace.run_command([str(script), "hello world"])
        self.assertIn("exit_code=0", output)
        self.assertIn("hello world", output)

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_windows_batch_wrapper_rejects_metacharacter_in_path(self) -> None:
        script = self.root / "hello&unsafe.cmd"
        script.write_text("@echo off\r\necho unsafe\r\n", encoding="utf-8")
        with self.assertRaises(ToolError):
            self.workspace.run_command([str(script)])

    def test_large_process_output_is_bounded(self) -> None:
        workspace = Workspace(self.root, max_output_chars=1_000)
        output = workspace.run_command(
            [sys.executable, "-c", "print('x' * 2_000_000)"]
        )
        self.assertLessEqual(len(output), 1_000)
        self.assertIn("omitted", output)

    def test_snapshot_ignores_test_cache_files(self) -> None:
        cache = self.root / ".pytest_cache"
        cache.mkdir()
        (cache / "state").write_text("one", encoding="utf-8")
        before = self.workspace.snapshot_files()
        (cache / "state").write_text("two", encoding="utf-8")
        after = self.workspace.snapshot_files()
        self.assertEqual(before, after)


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
