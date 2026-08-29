from __future__ import annotations

from copy import deepcopy
import unittest

from forgeloop.context import ContextManager, message_size


class ContextManagerTests(unittest.TestCase):
    def test_compaction_preserves_invariants_and_complete_tool_groups(self) -> None:
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "original task"},
        ]
        for index in range(12):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{index}",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": f'{{"path":"{index}.txt"}}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": f"call_{index}",
                        "content": "x" * 600,
                    },
                ]
            )
        original = deepcopy(messages)
        prepared = ContextManager(3_000).prepare(messages)

        self.assertEqual(messages, original, "compaction must not mutate full history")
        self.assertEqual(prepared[0]["role"], "system")
        self.assertTrue(any(item.get("content") == "original task" for item in prepared))
        self.assertLessEqual(message_size(prepared), 3_000)
        for index, message in enumerate(prepared):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                expected = len(message["tool_calls"])
                following = 0
                cursor = index + 1
                while cursor < len(prepared) and prepared[cursor].get("role") == "tool":
                    following += 1
                    cursor += 1
                self.assertEqual(following, expected)

    def test_short_history_is_copied_without_compaction(self) -> None:
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "task"},
        ]
        prepared = ContextManager(2_000).prepare(messages)
        self.assertEqual(prepared, messages)
        self.assertIsNot(prepared, messages)


if __name__ == "__main__":
    unittest.main()

