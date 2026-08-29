from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()

