from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forgeloop.agent import AgentResult
from forgeloop.client import ModelError
from forgeloop.cli import (
    EventPrinter,
    _run_interactive,
    _redact,
    _summarize_arguments,
    build_parser,
    main,
)


class CliTests(unittest.TestCase):
    def test_quiet_printer_does_not_duplicate_final_output(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            EventPrinter("secret", quiet=True)(
                {"type": "final", "status": "completed", "summary": "done"}
            )
        self.assertEqual(stream.getvalue(), "")

    def test_transcript_is_written_and_api_key_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.txt"
            printer = EventPrinter("super-secret", quiet=True, transcript=path)
            printer.header(Path(directory), "deepseek-v4-pro", 3)
            printer(
                {
                    "type": "warning",
                    "message": "never show super-secret",
                }
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("deepseek-v4-pro", content)
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("super-secret", content)

    def test_transcript_refuses_to_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.txt"
            path.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                EventPrinter("secret", quiet=True, transcript=path)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")

    def test_large_and_sensitive_arguments_are_summarized(self) -> None:
        rendered = _summarize_arguments(
            {"path": "app.py", "content": "x" * 100, "api_key": "secret"}
        )
        self.assertIn("<100 chars>", rendered)
        self.assertNotIn("secret", rendered)

    def test_token_shaped_text_is_redacted(self) -> None:
        self.assertEqual(_redact("sk-abcdefghijklmnop", ""), "[REDACTED]")
        sensitive = (
            "Bearer abcdefghijklmnop "
            "password=hunter2 "
            "AKIAABCDEFGHIJKLMNOP"
        )
        redacted = _redact(sensitive, "")
        self.assertNotIn("abcdefghijklmnop", redacted.lower())
        self.assertNotIn("hunter2", redacted)

    def test_parser_accepts_task_and_transcript(self) -> None:
        parsed = build_parser().parse_args(
            [
                "--transcript",
                "run.txt",
                "--max-tool-calls",
                "9",
                "--max-runtime-seconds",
                "30",
                "fix",
                "the",
                "bug",
            ]
        )
        self.assertEqual(parsed.task, ["fix", "the", "bug"])
        self.assertEqual(parsed.transcript, Path("run.txt"))
        self.assertEqual(parsed.max_tool_calls, 9)
        self.assertEqual(parsed.max_runtime_seconds, 30)

    def test_main_quiet_prints_final_exactly_once_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with (
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "configured"}, clear=True),
                patch("forgeloop.cli.CodingAgent") as agent_class,
                redirect_stdout(stdout),
            ):
                agent_class.return_value.run.return_value = AgentResult(
                    status="completed", summary="all done", steps=1
                )
                code = main(["--quiet", "--workspace", directory, "task"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "all done")

    def test_main_configuration_error_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
                code = main(["--workspace", directory, "task"])
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", stderr.getvalue())

    def test_main_reads_utf8_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_path = Path(directory) / "task.txt"
            task_path.write_text("修复这个问题", encoding="utf-8")
            with (
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "configured"}, clear=True),
                patch("forgeloop.cli.CodingAgent") as agent_class,
                redirect_stdout(io.StringIO()),
            ):
                agent_class.return_value.run.return_value = AgentResult(
                    status="completed", summary="done", steps=1
                )
                code = main(
                    ["--quiet", "--workspace", directory, "--task-file", str(task_path)]
                )
                received = agent_class.return_value.run.call_args.args[0]
        self.assertEqual(code, 0)
        self.assertEqual(received, "修复这个问题")

    def test_main_starts_interactive_mode_without_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "configured"}, clear=True),
                patch("forgeloop.cli._run_interactive", return_value=0) as interactive,
                redirect_stdout(io.StringIO()),
            ):
                code = main(["--interactive", "--workspace", directory])
        self.assertEqual(code, 0)
        self.assertEqual(interactive.call_args.args[1], "")

    def test_main_starts_local_web_workbench_without_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": "configured"}, clear=True),
                patch("forgeloop.web.serve_web", return_value=0) as serve_web,
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "--web",
                        "--no-open",
                        "--port",
                        "4321",
                        "--workspace",
                        directory,
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(serve_web.call_args.kwargs["port"], 4321)
        self.assertFalse(serve_web.call_args.kwargs["open_browser"])
        application = serve_web.call_args.args[0]
        self.assertEqual(application.snapshot()["workspace"], str(Path(directory).resolve()))

    def test_web_and_terminal_interactive_modes_are_mutually_exclusive(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--web", "--interactive"])

    def test_web_only_options_and_positional_task_are_rejected(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--web", "unexpected task"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--no-open", "task"])

    def test_interactive_session_reuses_history_and_ignores_blank_input(self) -> None:
        first_messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "first report"},
        ]
        second_messages = first_messages + [
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "second report"},
        ]
        scripted_results = iter(
            [
                AgentResult(
                    status="completed",
                    summary="first report",
                    steps=1,
                    messages=first_messages,
                ),
                AgentResult(
                    status="completed",
                    summary="second report",
                    steps=1,
                    messages=second_messages,
                ),
            ]
        )
        calls = []

        class FakeAgent:
            def run(self, task, *, history=None, verification_pending=False):
                calls.append((task, history, verification_pending))
                return next(scripted_results)

        entered = iter(["", "follow up", "/quit"])
        output = []
        errors = []
        code = _run_interactive(
            FakeAgent(),
            "first task",
            "secret",
            read_line=lambda prompt: next(entered),
            write_line=output.append,
            write_error=errors.append,
        )

        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("first task", None, False))
        self.assertEqual(calls[1], ("follow up", first_messages, False))
        self.assertFalse(errors)
        self.assertTrue(any("Interactive mode" in line for line in output))

    def test_interactive_session_carries_verification_debt(self) -> None:
        calls = []
        results = iter(
            [
                AgentResult(
                    status="step_limit",
                    summary="stopped",
                    steps=1,
                    verification_pending=True,
                    messages=[{"role": "system", "content": "policy"}],
                ),
                AgentResult(
                    status="completed",
                    summary="verified",
                    steps=1,
                    verification_pending=False,
                    messages=[{"role": "system", "content": "policy"}],
                ),
            ]
        )

        class FakeAgent:
            def run(self, task, *, history=None, verification_pending=False):
                calls.append(verification_pending)
                return next(results)

        entered = iter(["continue", "/quit"])
        code = _run_interactive(
            FakeAgent(),
            "start",
            "secret",
            read_line=lambda prompt: next(entered),
            write_line=lambda text: None,
            write_error=lambda text: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [False, True])

    def test_interactive_eof_and_keyboard_interrupt_exit_cleanly(self) -> None:
        class UnusedAgent:
            def run(self, task, *, history=None, verification_pending=False):
                raise AssertionError("agent should not run")

        def end_of_file(prompt):
            raise EOFError

        def interrupted(prompt):
            raise KeyboardInterrupt

        self.assertEqual(
            _run_interactive(
                UnusedAgent(),
                "",
                "secret",
                read_line=end_of_file,
                write_line=lambda text: None,
            ),
            0,
        )
        self.assertEqual(
            _run_interactive(
                UnusedAgent(),
                "",
                "secret",
                read_line=interrupted,
                write_line=lambda text: None,
            ),
            130,
        )

    def test_interactive_model_error_closes_session_safely(self) -> None:
        class FailingAgent:
            def run(self, task, *, history=None, verification_pending=False):
                raise ModelError("temporary failure")

        errors = []
        code = _run_interactive(
            FailingAgent(),
            "task",
            "secret",
            read_line=lambda prompt: "/quit",
            write_line=lambda text: None,
            write_error=errors.append,
        )
        self.assertEqual(code, 2)
        self.assertTrue(any("Session closed" in line for line in errors))


if __name__ == "__main__":
    unittest.main()
