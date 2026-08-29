from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from forgeloop.agent import CodingAgent
from forgeloop.tools import ToolRegistry, Workspace


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

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
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
        result = CodingAgent(RepeatingClient(), self.registry, max_steps=8).run(
            "Keep listing files forever"
        )
        self.assertEqual(result.status, "repetition_limit")
        self.assertEqual(result.steps, 4)

    def test_step_limit_is_explicit(self) -> None:
        result = CodingAgent(RepeatingClient(), self.registry, max_steps=2).run(
            "List files"
        )
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.steps, 2)

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
            max_steps=2,
            max_runtime_seconds=0.01,
        ).run("Slow response")
        self.assertEqual(result.status, "runtime_limit")
        self.assertEqual(registry.count, 0)
        self.assertEqual(result.messages[-1]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
