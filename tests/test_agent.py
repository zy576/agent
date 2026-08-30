from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from forgeloop.agent import SYSTEM_PROMPT, CodingAgent
from forgeloop.client import ModelError
from forgeloop.tools import ToolError, ToolRegistry, Workspace


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


class ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[list[dict]] = []
        self.tool_requests: list[list[dict]] = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        self.tool_requests.append(deepcopy(tools))
        if not self.responses:
            raise AssertionError("scripted client ran out of responses")
        return deepcopy(self.responses.pop(0))


class RepeatingClient:
    def __init__(self) -> None:
        self.count = 0

    def complete(self, messages, tools):
        self.count += 1
        return tool_call(f"call_{self.count}", "list_files", {})


class CountingRegistry:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.schemas = []
        self.count = 0

    def execute(self, call):
        self.count += 1
        return {"ok": True, "tool": "list_files", "output": "same"}


class SlowToolClient:
    def complete(self, messages, tools):
        time.sleep(0.03)
        return tool_call("slow_1", "list_files", {})


class FailingFinalizationClient:
    def __init__(self) -> None:
        self.count = 0

    def complete(self, messages, tools):
        self.count += 1
        if self.count == 1:
            return tool_call("inspect_1", "list_files", {})
        raise ModelError("report endpoint unavailable")


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = ToolRegistry(Workspace(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_full_write_verify_finish_loop(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "write_1",
                    "write_file",
                    {"path": "hello.py", "content": "print('hello')\n"},
                ),
                tool_call(
                    "test_1",
                    "run_command",
                    {"argv": [sys.executable, "hello.py"]},
                ),
                {
                    "role": "assistant",
                    "content": "Created hello.py and verified it prints hello.",
                },
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=5).run("Create hello.py")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.changed_files, ["hello.py"])
        self.assertTrue((self.root / "hello.py").is_file())
        self.assertIn("exit_code=0", client.requests[2][-1]["content"])
        self.assertEqual(len(result.verifications), 1)

    def test_default_unlimited_mode_runs_past_twenty_four_decisions(self) -> None:
        events = []
        responses = [
            tool_call(
                f"inspect_{index}",
                "list_files",
                {"path": f"unique_{index}"},
            )
            for index in range(30)
        ]
        responses.append(
            {
                "role": "assistant",
                "content": "Completed after more than twenty-four decisions.",
            }
        )
        client = ScriptedClient(responses)
        registry = CountingRegistry(Workspace(self.root))

        result = CodingAgent(
            client,
            registry,
            on_event=events.append,
        ).run("Keep working until the task is complete")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 31)
        self.assertEqual(registry.count, 30)
        self.assertEqual(len(client.requests), 31)
        self.assertNotIn(
            "finalization_request",
            [event["type"] for event in events],
        )

    def test_non_positive_step_limit_is_rejected(self) -> None:
        for invalid in (-1, 0):
            with self.subTest(max_steps=invalid), self.assertRaisesRegex(
                ValueError,
                "at least 1",
            ):
                CodingAgent(ScriptedClient([]), self.registry, max_steps=invalid)

    def test_premature_finish_after_write_triggers_verification_gate(self) -> None:
        events = []
        client = ScriptedClient(
            [
                tool_call(
                    "write_1",
                    "write_file",
                    {"path": "app.py", "content": "x = 1\n"},
                ),
                {"role": "assistant", "content": "Done."},
                tool_call(
                    "verify_1",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert Path('app.py').is_file()",
                        ]
                    },
                ),
                {"role": "assistant", "content": "Verified and complete."},
            ]
        )
        result = CodingAgent(
            client,
            self.registry,
            max_steps=6,
            on_event=events.append,
        ).run("Create app.py")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(client.requests), 4)
        self.assertTrue(any(event["type"] == "warning" for event in events))
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "Completion gate" in str(message.get("content"))
                for message in client.requests[2]
            )
        )

    def test_read_only_delegation_cannot_clear_main_verification_debt(self) -> None:
        pool_resets = []

        def delegate(tasks):
            return json.dumps(
                {
                    "ok": True,
                    "workspace_stable": True,
                    "subtasks": [
                        {
                            "id": tasks[0]["id"],
                            "status": "completed",
                            "steps": 1,
                            "report": "Looks correct, but this is not verification.",
                        }
                    ],
                }
            )

        registry = ToolRegistry(
            Workspace(self.root),
            delegate_handler=delegate,
            max_delegated_tasks=1,
            on_run_start=lambda: pool_resets.append(True),
        )
        client = ScriptedClient(
            [
                tool_call(
                    "write_1",
                    "write_file",
                    {"path": "app.py", "content": "x = 1\n"},
                ),
                tool_call(
                    "delegate_1",
                    "delegate_readonly",
                    {"tasks": [{"id": "review", "objective": "review app.py"}]},
                ),
                {"role": "assistant", "content": "The reviewer says it is done."},
                tool_call(
                    "verify_1",
                    "run_command",
                    {"argv": [sys.executable, "-m", "py_compile", "app.py"]},
                ),
                {"role": "assistant", "content": "Verified and complete."},
            ]
        )

        result = CodingAgent(client, registry, max_steps=7).run("Create app.py")

        self.assertEqual(result.status, "completed")
        self.assertEqual(pool_resets, [True])
        self.assertEqual(len(result.verifications), 1)
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "Completion gate" in str(message.get("content"))
                for message in client.requests[3]
            )
        )
        delegated_result = json.loads(client.requests[2][-1]["content"])
        self.assertEqual(delegated_result["tool"], "delegate_readonly")
        self.assertEqual(client.requests[2][-1]["tool_call_id"], "delegate_1")

    def test_custom_system_prompt_is_preserved_across_follow_up_turns(self) -> None:
        prompt = "You are a test-only read-only analyst."
        first_client = ScriptedClient(
            [{"role": "assistant", "content": "First report."}]
        )
        first = CodingAgent(
            first_client,
            self.registry,
            system_prompt=prompt,
        ).run("Inspect the project")
        second_client = ScriptedClient(
            [{"role": "assistant", "content": "Second report."}]
        )
        second = CodingAgent(
            second_client,
            self.registry,
            system_prompt=prompt,
        ).run("Inspect another file", history=first.messages)

        self.assertEqual(second.status, "completed")
        self.assertEqual(second.messages[0], {"role": "system", "content": prompt})

    def test_failed_delegation_requires_direct_evidence_before_completion(self) -> None:
        (self.root / "evidence.txt").write_text("fresh", encoding="utf-8")

        def fail_delegation(tasks):
            raise ToolError(
                json.dumps(
                    {
                        "ok": False,
                        "workspace_stable": False,
                        "error": "workspace changed during delegation",
                    }
                )
            )

        registry = ToolRegistry(
            Workspace(self.root),
            delegate_handler=fail_delegation,
            max_delegated_tasks=1,
        )
        client = ScriptedClient(
            [
                tool_call(
                    "delegate_1",
                    "delegate_readonly",
                    {"tasks": [{"id": "review", "objective": "inspect"}]},
                ),
                {"role": "assistant", "content": "Done without re-reading."},
                tool_call(
                    "read_1",
                    "read_file",
                    {"path": "evidence.txt"},
                ),
                {"role": "assistant", "content": "Re-read evidence; complete."},
            ]
        )

        result = CodingAgent(client, registry, max_steps=6).run("Review evidence")

        self.assertEqual(result.status, "completed")
        failed_result = json.loads(client.requests[1][-1]["content"])
        self.assertFalse(failed_result["ok"])
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "delegated investigation failed" in str(message.get("content"))
                for message in client.requests[2]
            )
        )

    def test_malformed_tool_arguments_return_to_model(self) -> None:
        client = ScriptedClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "bad_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": "{bad-json",
                            },
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": "The malformed call failed safely; no file was changed.",
                },
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=3).run("Try a tool")
        self.assertEqual(result.status, "completed")
        tool_result = json.loads(client.requests[1][-1]["content"])
        self.assertFalse(tool_result["ok"])

    def test_repetition_guard_stops_identical_loop(self) -> None:
        result = CodingAgent(RepeatingClient(), self.registry).run(
            "Keep listing files forever"
        )
        self.assertEqual(result.status, "repetition_limit")
        self.assertEqual(result.steps, 4)

    def test_unlimited_mode_still_honors_total_tool_call_budget(self) -> None:
        client = ScriptedClient(
            [
                tool_call("budget_1", "list_files", {"path": "one"}),
                tool_call("budget_2", "list_files", {"path": "two"}),
                tool_call("budget_3", "list_files", {"path": "three"}),
            ]
        )
        registry = CountingRegistry(Workspace(self.root))

        result = CodingAgent(client, registry, max_tool_calls=2).run(
            "Keep using tools"
        )

        self.assertEqual(result.status, "tool_call_limit")
        self.assertEqual(result.steps, 3)
        self.assertEqual(registry.count, 2)

    def test_step_limit_is_explicit(self) -> None:
        client = RepeatingClient()
        registry = CountingRegistry(Workspace(self.root))
        result = CodingAgent(client, registry, max_steps=2).run(
            "List files"
        )
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.steps, 2)
        self.assertEqual(client.count, 3)
        self.assertEqual(registry.count, 2)
        tool_messages = [
            message for message in result.messages if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 3)
        self.assertIn("report-only finalization", tool_messages[-1]["content"])

    def test_rejected_finalization_tool_call_keeps_history_resumable(self) -> None:
        client = ScriptedClient(
            [
                tool_call("inspect_before_finalize", "list_files", {}),
                tool_call(
                    "forbidden_finalize_write",
                    "write_file",
                    {"path": "forbidden.txt", "content": "must not be written"},
                ),
                {"role": "assistant", "content": "Continued safely."},
            ]
        )
        agent = CodingAgent(client, self.registry, max_steps=1)

        first = agent.run("Inspect once")
        second = agent.run("Continue safely", history=first.messages)

        self.assertEqual(first.status, "step_limit")
        self.assertFalse((self.root / "forbidden.txt").exists())
        self.assertEqual(second.status, "completed")
        self.assertIn(
            "report-only finalization disables tool use",
            client.requests[2][-2]["content"],
        )

    def test_last_budget_step_can_finalize_after_successful_verification(self) -> None:
        events = []
        client = ScriptedClient(
            [
                tool_call(
                    "write_last",
                    "write_file",
                    {"path": "done.py", "content": "value = 42\n"},
                ),
                tool_call(
                    "verify_last",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from done import value; assert value == 42",
                        ]
                    },
                ),
                {
                    "role": "assistant",
                    "content": (
                        "TASK_STATUS: COMPLETE\n\n"
                        "Created done.py and verified its value successfully."
                    ),
                },
            ]
        )

        result = CodingAgent(
            client,
            self.registry,
            max_steps=2,
            on_event=events.append,
        ).run("Create and verify done.py")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.changed_files, ["done.py"])
        self.assertFalse(result.verification_pending)
        self.assertEqual(len(result.verifications), 1)
        self.assertIn("exit_code=0", result.verifications[0])
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.tool_requests[-1], [])
        self.assertEqual(
            [event["type"] for event in events][-2:],
            ["finalization_request", "final"],
        )
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(result.messages[-1]["content"], result.summary)

    def test_failed_last_verification_cannot_be_finalized_as_success(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "write_before_failure",
                    "write_file",
                    {"path": "broken.py", "content": "value = 1\n"},
                ),
                tool_call(
                    "failed_verification",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(1)"]},
                ),
                {
                    "role": "assistant",
                    "content": "TASK_STATUS: COMPLETE\n\nEverything is complete.",
                },
            ]
        )

        result = CodingAgent(client, self.registry, max_steps=2).run(
            "Create and verify broken.py"
        )

        self.assertEqual(result.status, "step_limit")
        self.assertTrue(result.verification_pending)
        self.assertIn("exit_code=1", result.verifications[0])

    def test_failed_last_tool_cannot_be_finalized_as_success(self) -> None:
        client = ScriptedClient(
            [
                tool_call("unknown_last", "missing_tool", {}),
                {
                    "role": "assistant",
                    "content": "TASK_STATUS: COMPLETE\n\nEverything is complete.",
                },
            ]
        )

        result = CodingAgent(client, self.registry, max_steps=1).run(
            "Use a missing tool"
        )

        self.assertEqual(result.status, "step_limit")
        self.assertFalse(result.verification_pending)

    def test_failed_command_without_writes_cannot_be_finalized_as_success(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "failed_without_write",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(1)"]},
                ),
                {
                    "role": "assistant",
                    "content": "TASK_STATUS: COMPLETE\n\nEverything is complete.",
                },
            ]
        )

        result = CodingAgent(client, self.registry, max_steps=1).run(
            "Run the check"
        )

        self.assertEqual(result.status, "step_limit")
        self.assertTrue(result.verification_pending)
        self.assertIn("exit_code=1", result.verifications[0])

    def test_failed_to_start_command_records_verification_debt(self) -> None:
        final = {"role": "assistant", "content": "Validation passed; complete."}
        client = ScriptedClient(
            [
                tool_call(
                    "failed_start",
                    "run_command",
                    {"argv": ["forgeloop-command-that-does-not-exist"]},
                ),
                final,
                final,
                final,
            ]
        )

        result = CodingAgent(client, self.registry).run("Run the required check")

        self.assertEqual(result.status, "completed_with_verification_risk")
        self.assertTrue(result.verification_pending)
        self.assertEqual(len(result.verifications), 1)
        self.assertIn("tool_error=", result.verifications[0])

    def test_successful_command_clears_failed_start_verification_debt(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "failed_start",
                    "run_command",
                    {"argv": ["forgeloop-command-that-does-not-exist"]},
                ),
                tool_call(
                    "successful_retry",
                    "run_command",
                    {"argv": [sys.executable, "-c", "print('recovered')"]},
                ),
                {"role": "assistant", "content": "Validation now passes."},
            ]
        )

        result = CodingAgent(client, self.registry).run("Run the required check")

        self.assertEqual(result.status, "completed")
        self.assertFalse(result.verification_pending)
        self.assertEqual(len(result.verifications), 2)
        self.assertIn("tool_error=", result.verifications[0])
        self.assertIn("exit_code=0", result.verifications[-1])

    def test_finalization_without_required_status_marker_is_not_success(self) -> None:
        client = ScriptedClient(
            [
                tool_call("inspect_without_marker", "list_files", {}),
                {"role": "assistant", "content": "The inspection is complete."},
            ]
        )

        result = CodingAgent(client, self.registry, max_steps=1).run("Inspect")

        self.assertEqual(result.status, "step_limit")
        self.assertIn("required completion signal", result.summary)

    def test_finalization_model_error_does_not_poison_closed_history(self) -> None:
        events = []
        client = FailingFinalizationClient()

        result = CodingAgent(
            client,
            self.registry,
            max_steps=1,
            on_event=events.append,
        ).run("Inspect once")

        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.steps, 1)
        self.assertEqual(client.count, 2)
        self.assertEqual(result.messages[-1]["role"], "assistant")
        self.assertEqual(
            [event["type"] for event in events][-3:],
            ["finalization_request", "warning", "final"],
        )

    def test_report_only_finalization_cannot_clear_verification_debt(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "write_unverified",
                    "write_file",
                    {"path": "pending.py", "content": "value = 1\n"},
                ),
                {
                    "role": "assistant",
                    "content": "TASK_STATUS: COMPLETE\n\nEverything is complete.",
                },
            ]
        )

        result = CodingAgent(client, self.registry, max_steps=1).run(
            "Create pending.py"
        )

        self.assertEqual(result.status, "step_limit")
        self.assertTrue(result.verification_pending)
        self.assertEqual(client.tool_requests[-1], [])
        self.assertIn("did not mark this task complete", result.summary)
        self.assertEqual(result.messages[-1]["content"], result.summary)

    def test_runtime_limit_prevents_report_only_request_after_slow_last_tool(self) -> None:
        events = []
        client = ScriptedClient(
            [
                tool_call("slow_last", "list_files", {}),
                {"role": "assistant", "content": "Should not be requested."},
            ]
        )

        class SlowRegistry(CountingRegistry):
            def execute(self, call):
                time.sleep(0.03)
                return super().execute(call)

        registry = SlowRegistry(Workspace(self.root))
        result = CodingAgent(
            client,
            registry,
            max_steps=1,
            max_runtime_seconds=0.01,
            on_event=events.append,
        ).run("Inspect slowly")

        self.assertEqual(result.status, "runtime_limit")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(registry.count, 1)
        self.assertNotIn("finalization_request", [event["type"] for event in events])

    def test_empty_report_only_finalization_stops_once_with_closed_history(self) -> None:
        client = ScriptedClient(
            [
                tool_call("inspect_last", "list_files", {}),
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": "Continued safely."},
            ]
        )
        agent = CodingAgent(client, self.registry, max_steps=1)

        first = agent.run("Inspect once")
        second = agent.run("Continue", history=first.messages)

        self.assertEqual(first.status, "step_limit")
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(first.messages[-1]["role"], "assistant")
        self.assertIn("response was empty", first.messages[-1]["content"])
        self.assertEqual(second.status, "completed")

    def test_multi_tool_warning_is_appended_after_all_tool_results(self) -> None:
        calls = []
        for index in range(3):
            calls.append(
                {
                    "id": f"list_{index}",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                }
            )
        client = ScriptedClient(
            [
                {"role": "assistant", "content": None, "tool_calls": calls},
                {"role": "assistant", "content": "Stopped repeating and finished."},
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=3).run("Inspect files")
        self.assertEqual(result.status, "completed")
        roles = [message["role"] for message in client.requests[1]]
        self.assertEqual(roles[-4:], ["tool", "tool", "tool", "user"])
        tool_ids = [
            message["tool_call_id"]
            for message in client.requests[1]
            if message.get("role") == "tool"
        ]
        self.assertEqual(tool_ids[-3:], ["list_0", "list_1", "list_2"])

    def test_write_after_command_in_same_response_invalidates_verification(self) -> None:
        multi = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                tool_call(
                    "verify_early",
                    "run_command",
                    {"argv": [sys.executable, "-c", "print('ok')"]},
                )["tool_calls"][0],
                tool_call(
                    "write_late",
                    "write_file",
                    {"path": "late.txt", "content": "late\n"},
                )["tool_calls"][0],
            ],
        }
        client = ScriptedClient(
            [
                multi,
                {"role": "assistant", "content": "Done too early."},
                tool_call(
                    "verify_late",
                    "run_command",
                    {"argv": [sys.executable, "-c", "from pathlib import Path; assert Path('late.txt').is_file()"]},
                ),
                {"role": "assistant", "content": "Verified."},
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=6).run("Create late.txt")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(client.requests), 4)

    def test_command_side_file_change_requires_later_verification(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "command_write",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('generated.txt').write_text('x')",
                        ]
                    },
                ),
                {"role": "assistant", "content": "Generated."},
                tool_call(
                    "verify_generated",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert Path('generated.txt').read_text() == 'x'",
                        ]
                    },
                ),
                {"role": "assistant", "content": "Generated and verified."},
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=6).run("Generate a file")
        self.assertEqual(result.status, "completed")
        self.assertIn("generated.txt", result.changed_files)
        self.assertIn("workspace_changes", client.requests[1][-1]["content"])

    def test_remaining_calls_are_closed_but_not_executed_after_repetition_limit(self) -> None:
        calls = [
            tool_call(f"call_{index}", "list_files", {})["tool_calls"][0]
            for index in range(6)
        ]
        client = ScriptedClient(
            [{"role": "assistant", "content": None, "tool_calls": calls}]
        )
        registry = CountingRegistry(Workspace(self.root))
        result = CodingAgent(client, registry, max_steps=2).run("Repeat")
        self.assertEqual(result.status, "repetition_limit")
        self.assertEqual(registry.count, 4)
        tool_messages = [
            message for message in result.messages if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 6)
        self.assertIn("skipped", tool_messages[-1]["content"])

    def test_timed_out_command_partial_write_is_still_tracked(self) -> None:
        client = ScriptedClient(
            [
                tool_call(
                    "partial_write",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import time; "
                            "Path('partial.txt').write_text('x'); time.sleep(2)",
                        ],
                        "timeout_seconds": 0.1,
                    },
                ),
                {"role": "assistant", "content": "Done too early."},
                tool_call(
                    "verify_partial",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert Path('partial.txt').exists()",
                        ]
                    },
                ),
                {"role": "assistant", "content": "Tracked and verified."},
            ]
        )
        result = CodingAgent(client, self.registry, max_steps=6).run("Write then time out")
        self.assertEqual(result.status, "completed")
        self.assertIn("partial.txt", result.changed_files)
        self.assertEqual(len(client.requests), 4)

    def test_per_step_tool_call_budget_closes_skipped_results(self) -> None:
        calls = [
            tool_call(f"call_{index}", "list_files", {})["tool_calls"][0]
            for index in range(5)
        ]
        client = ScriptedClient(
            [{"role": "assistant", "content": None, "tool_calls": calls}]
        )
        registry = CountingRegistry(Workspace(self.root))
        result = CodingAgent(
            client,
            registry,
            max_steps=2,
            max_tool_calls_per_step=2,
        ).run("Too many calls")
        self.assertEqual(result.status, "tool_call_limit")
        self.assertEqual(registry.count, 2)
        tool_messages = [
            message for message in result.messages if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 5)
        self.assertIn("per-step", tool_messages[-1]["content"])

    def test_runtime_budget_can_stop_after_slow_model_response(self) -> None:
        registry = CountingRegistry(Workspace(self.root))
        result = CodingAgent(
            SlowToolClient(),
            registry,
            max_runtime_seconds=0.01,
        ).run("Slow response")
        self.assertEqual(result.status, "runtime_limit")
        self.assertEqual(registry.count, 0)
        self.assertEqual(result.messages[-1]["role"], "tool")

    def test_runtime_budget_rejects_slow_final_response_in_unlimited_mode(self) -> None:
        class SlowFinalClient:
            def complete(self, messages, tools):
                time.sleep(0.03)
                return {"role": "assistant", "content": "Finished too late."}

        result = CodingAgent(
            SlowFinalClient(),
            self.registry,
            max_runtime_seconds=0.01,
        ).run("Return a final report slowly")

        self.assertEqual(result.status, "runtime_limit")
        self.assertEqual(result.steps, 1)
        self.assertIn("runtime limit", result.summary)
        self.assertEqual(result.messages[-1]["content"], result.summary)

    def test_runtime_budget_rejects_slow_report_only_finalization(self) -> None:
        class SlowFinalizationClient:
            def __init__(self) -> None:
                self.count = 0

            def complete(self, messages, tools):
                self.count += 1
                if self.count == 1:
                    return tool_call("inspect_1", "list_files", {})
                time.sleep(0.03)
                return {
                    "role": "assistant",
                    "content": "TASK_STATUS: COMPLETE\n\nFinished too late.",
                }

        client = SlowFinalizationClient()
        registry = CountingRegistry(Workspace(self.root))
        result = CodingAgent(
            client,
            registry,
            max_steps=1,
            max_runtime_seconds=0.01,
        ).run("Inspect and report")

        self.assertEqual(result.status, "runtime_limit")
        self.assertEqual(result.steps, 1)
        self.assertEqual(client.count, 2)
        self.assertEqual(registry.count, 1)
        self.assertEqual(result.messages[-1]["content"], result.summary)

    def test_follow_up_turn_reuses_history_without_mutating_prior_result(self) -> None:
        client = ScriptedClient(
            [
                {"role": "assistant", "content": "First report."},
                {"role": "assistant", "content": "Follow-up report."},
            ]
        )
        agent = CodingAgent(client, self.registry, max_steps=2)

        first = agent.run("Inspect the project")
        first_snapshot = deepcopy(first.messages)
        second = agent.run("Now explain the result", history=first.messages)

        self.assertEqual(first.messages, first_snapshot)
        self.assertEqual(client.requests[1][:-1], first_snapshot)
        self.assertEqual(
            client.requests[1][-1],
            {"role": "user", "content": "Now explain the result"},
        )
        self.assertEqual(second.status, "completed")

    def test_resumed_turn_rejects_tampered_system_history(self) -> None:
        history = [
            {"role": "system", "content": "wrong policy"},
            {"role": "user", "content": "task"},
        ]
        client = ScriptedClient([])
        with self.assertRaisesRegex(ValueError, "system policy"):
            CodingAgent(client, self.registry).run("follow up", history=history)

    def test_resumed_turn_rejects_unclosed_tool_history(self) -> None:
        history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "task"},
            tool_call("open_1", "list_files", {}),
        ]
        with self.assertRaisesRegex(ValueError, "before all tool calls"):
            CodingAgent(ScriptedClient([]), self.registry).run(
                "follow up",
                history=history,
            )

    def test_verification_debt_can_carry_into_follow_up_turn(self) -> None:
        first_response = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                tool_call(
                    "write_1",
                    "write_file",
                    {"path": "pending.py", "content": "value = 1\n"},
                )["tool_calls"][0],
                tool_call("skipped_1", "list_files", {})["tool_calls"][0],
            ],
        }
        client = ScriptedClient(
            [
                first_response,
                {"role": "assistant", "content": "Already done."},
                tool_call(
                    "verify_1",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; "
                            "assert Path('pending.py').read_text() == 'value = 1\\n'",
                        ]
                    },
                ),
                {"role": "assistant", "content": "Verified now."},
            ]
        )
        agent = CodingAgent(
            client,
            self.registry,
            max_steps=3,
            max_tool_calls=1,
        )

        first = agent.run("Create pending.py")
        self.assertEqual(first.status, "tool_call_limit")
        self.assertTrue(first.verification_pending)
        second = agent.run(
            "Continue",
            history=first.messages,
            verification_pending=first.verification_pending,
        )

        self.assertEqual(second.status, "completed")
        self.assertFalse(second.verification_pending)
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "Completion gate" in str(message.get("content"))
                for message in client.requests[2]
            )
        )


if __name__ == "__main__":
    unittest.main()
