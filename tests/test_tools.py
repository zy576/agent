from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_run_command_interrupt_terminates_process_tree(self) -> None:
        with (
            patch("forgeloop.tools.subprocess.Popen") as popen,
            patch("forgeloop.tools._terminate_process_tree") as terminate,
        ):
            process = popen.return_value
            process.wait.side_effect = [KeyboardInterrupt, 0]
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.run_command([sys.executable, "-c", "pass"])
        terminate.assert_called_once()
        self.assertIs(terminate.call_args.args[0], process)

    def test_run_command_interrupt_is_not_hidden_when_child_will_not_stop(self) -> None:
        with (
            patch("forgeloop.tools.subprocess.Popen") as popen,
            patch("forgeloop.tools._terminate_process_tree") as terminate,
        ):
            process = popen.return_value
            process.wait.side_effect = [
                KeyboardInterrupt,
                subprocess.TimeoutExpired(cmd="child", timeout=10),
            ]
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.run_command([sys.executable, "-c", "pass"])
        terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_run_command_interrupt_is_not_hidden_when_tree_cleanup_raises(self) -> None:
        with (
            patch("forgeloop.tools.subprocess.Popen") as popen,
            patch(
                "forgeloop.tools._terminate_process_tree",
                side_effect=OSError("kill denied"),
            ) as terminate,
        ):
            process = popen.return_value
            process.wait.side_effect = [KeyboardInterrupt, 0]
            with self.assertRaises(KeyboardInterrupt):
                self.workspace.run_command([sys.executable, "-c", "pass"])
        terminate.assert_called_once()
        process.kill.assert_called_once()

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


class WorkspaceSwitchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.scope = Path(self.temporary.name)
        (self.scope / "project-a").mkdir()
        (self.scope / "project-a" / "pyproject.toml").write_text(
            "[project]\n", encoding="utf-8"
        )
        (self.scope / "project-b").mkdir()
        (self.scope / "project-b" / "package.json").write_text("{}\n", encoding="utf-8")
        (self.scope / "plain").mkdir()
        (self.scope / "deep").mkdir()
        (self.scope / "deep" / "nested").mkdir()
        (self.scope / "deep" / "nested" / ".git").mkdir()
        (self.scope / "afile.txt").write_text("x", encoding="utf-8")
        (self.scope / ".ssh").mkdir()
        self.workspace = Workspace(self.scope)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_select_workspace_rebinds_root_inside_scope(self) -> None:
        result = self.workspace.select_workspace("project-a")
        self.assertIn("workspace switched", result)
        self.assertEqual(self.workspace.root, (self.scope / "project-a").resolve())
        self.workspace.write_file("hello.txt", "hi")
        self.assertTrue((self.scope / "project-a" / "hello.txt").is_file())

    def test_select_workspace_accepts_dot_for_scope_root(self) -> None:
        self.workspace.select_workspace("project-a")
        self.workspace.select_workspace(".")
        self.assertEqual(self.workspace.root, self.scope.resolve())

    def test_select_workspace_rejects_escape_missing_and_files(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.select_workspace("..")
        with self.assertRaises(ToolError):
            self.workspace.select_workspace(str(self.scope.parent))
        with self.assertRaises(ToolError):
            self.workspace.select_workspace("does-not-exist")
        with self.assertRaises(ToolError):
            self.workspace.select_workspace("afile.txt")

    def test_select_workspace_rejects_sensitive_directories(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.select_workspace(".ssh")

    def test_scope_root_can_be_wider_than_initial_root(self) -> None:
        nested = Workspace(self.scope / "project-a", scope_root=self.scope)
        nested.select_workspace("project-b")
        self.assertEqual(nested.root, (self.scope / "project-b").resolve())

    def test_workspace_outside_scope_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            Workspace(self.scope / "project-a", scope_root=self.scope / "project-b")

    def test_candidates_include_projects_and_scope_root(self) -> None:
        candidates = self.workspace.candidate_workspaces()
        paths = {entry["path"] for entry in candidates}
        self.assertIn(".", paths)
        self.assertIn("project-a", paths)
        self.assertIn("project-b", paths)
        self.assertIn("deep/nested", paths)
        self.assertNotIn("plain", paths)
        self.assertNotIn(".ssh", paths)
        by_path = {entry["path"]: entry for entry in candidates}
        self.assertTrue(by_path["."]["current"])

    def test_list_workspaces_marks_current(self) -> None:
        self.workspace.select_workspace("project-a")
        output = self.workspace.list_workspaces()
        self.assertIn("project-a  [current workspace]", output)
        self.assertIn("project-b", output)

    def test_rebind_any_accepts_directories_outside_the_scope(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="forgeloop-outside-"))
        try:
            result = self.workspace.rebind_any(str(outside))
            self.assertEqual(result, str(outside.resolve()))
            self.assertEqual(self.workspace.root, outside.resolve())
            self.assertEqual(self.workspace.scope_root, outside.resolve())
            self.workspace.write_file("inside.txt", "ok")
            self.assertTrue((outside / "inside.txt").is_file())
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_rebind_any_rejects_missing_files_and_sensitive_dirs(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.rebind_any("does-not-exist-anywhere")
        with self.assertRaises(ToolError):
            self.workspace.rebind_any("afile.txt")
        with self.assertRaises(ToolError):
            self.workspace.rebind_any(str(self.scope / ".ssh"))

    def test_list_directories_starts_at_home_and_flags_hidden(self) -> None:
        home_view = self.workspace.list_directories("")
        self.assertEqual(home_view["path"], str(Path.home().resolve()))
        self.assertEqual(home_view["home"], str(Path.home().resolve()))
        self.assertEqual(home_view["parent"], str(Path.home().resolve().parent))
        self.assertIsInstance(home_view["entries"], list)

        (self.scope / ".hidden-dir").mkdir()
        view = self.workspace.list_directories(str(self.scope))
        self.assertEqual(view["path"], str(self.scope.resolve()))
        self.assertEqual(view["parent"], str(self.scope.resolve().parent))
        self.assertEqual(view["home"], str(Path.home().resolve()))
        names = {entry["name"] for entry in view["entries"]}
        self.assertIn("project-a", names)
        self.assertIn("project-b", names)
        self.assertIn(".hidden-dir", names)
        self.assertNotIn(".ssh", names)
        by_name = {entry["name"]: entry for entry in view["entries"]}
        self.assertTrue(by_name[".hidden-dir"]["hidden"])
        self.assertFalse(by_name["project-a"]["hidden"])

    def test_list_directories_drives_view(self) -> None:
        view = self.workspace.list_directories("__drives__")
        self.assertEqual(view["path"], "")
        self.assertIsNone(view["parent"])
        self.assertEqual(view["home"], str(Path.home().resolve()))
        entry_paths = [entry["path"] for entry in view["entries"]]
        if os.name == "nt":
            self.assertTrue(any(path.endswith(":\\") for path in entry_paths))
        else:
            self.assertIn(str(Path(os.sep).resolve()), entry_paths)

    def test_list_directories_rejects_files_relative_and_sensitive_paths(self) -> None:
        with self.assertRaises(ToolError):
            self.workspace.list_directories(str(self.scope / "afile.txt"))
        with self.assertRaisesRegex(ToolError, "absolute"):
            self.workspace.list_directories("project-a")
        with self.assertRaisesRegex(ToolError, "sensitive"):
            self.workspace.list_directories(str(self.scope / ".ssh"))

    def test_create_directory_creates_and_validates(self) -> None:
        parent = str(self.scope / "project-a")
        created = self.workspace.create_directory(parent, "new-child")
        self.assertEqual(created, str((self.scope / "project-a" / "new-child").resolve()))
        self.assertTrue((self.scope / "project-a" / "new-child").is_dir())
        with self.assertRaisesRegex(ToolError, "exists"):
            self.workspace.create_directory(parent, "new-child")
        for bad_name in ("", "a/b", "a\\b", "..", "."):
            with self.subTest(bad_name=bad_name), self.assertRaises(ToolError):
                self.workspace.create_directory(parent, bad_name)
        with self.assertRaises(ToolError):
            self.workspace.create_directory(str(self.scope / "missing"), "child")
        with self.assertRaisesRegex(ToolError, "sensitive"):
            self.workspace.create_directory(str(self.scope), ".ssh")


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

    def test_workspace_tools_exposed_to_main_registry_only(self) -> None:
        names = {schema["function"]["name"] for schema in self.registry.schemas}
        self.assertIn("select_workspace", names)
        self.assertIn("list_workspaces", names)
        read_only = ToolRegistry(
            Workspace(Path(self.temporary.name)), read_only=True
        )
        read_only_names = {
            schema["function"]["name"] for schema in read_only.schemas
        }
        self.assertNotIn("select_workspace", read_only_names)
        self.assertNotIn("list_workspaces", read_only_names)

    def test_select_workspace_tool_executes(self) -> None:
        root = Path(self.temporary.name)
        (root / "sub").mkdir()
        result = self.registry.execute(
            {
                "function": {
                    "name": "select_workspace",
                    "arguments": json.dumps({"path": "sub"}),
                }
            }
        )
        self.assertTrue(result["ok"])
        self.assertIn("workspace switched", result["output"])
        self.assertEqual(self.registry.workspace.root, (root / "sub").resolve())


if __name__ == "__main__":
    unittest.main()
