from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forgeloop.agent import AgentResult
from forgeloop.cli import (
    EventPrinter,
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


if __name__ == "__main__":
    unittest.main()
