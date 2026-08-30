from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from typing import Any, Callable

from forgeloop.client import ModelError
from forgeloop.subagents import ReadOnlySubagentPool
from forgeloop.tools import ToolError, ToolRegistry, Workspace


READ_ONLY_TOOL_NAMES = {"list_files", "read_file", "search_files"}


def final_report(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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


def schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {
        str(schema.get("function", {}).get("name", ""))
        for schema in schemas
    }


def decode_result(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise AssertionError("subagent result must be a JSON object")
    return parsed


def result_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    subtasks = payload.get("subtasks")
    if not isinstance(subtasks, list):
        raise AssertionError("subagent result must contain a subtasks array")
    return {str(item["id"]): item for item in subtasks}


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    raise AssertionError("subagent request did not contain its objective")


def assigned_objective(messages: list[dict[str, Any]]) -> str:
    """Extract the public objective from the pool's bounded child-task wrapper."""

    text = last_user_text(messages)
    prefix, separator, objective = text.partition(": ")
    if separator and prefix.startswith("Read-only subtask "):
        return objective
    return text


class RecordingFactory:
    def __init__(self, builder: Callable[[], Any]) -> None:
        self.builder = builder
        self.clients: list[Any] = []

    def __call__(self) -> Any:
        client = self.builder()
        self.clients.append(client)
        return client


class FinalClient:
    def __init__(self, report: str = "analysis complete") -> None:
        self.report = report
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_schemas: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.requests.append(deepcopy(messages))
        self.tool_schemas.append(deepcopy(tools))
        return final_report(f"{self.report}: {assigned_objective(messages)}")


class ConcurrentClient:
    def __init__(
        self,
        barrier: threading.Barrier,
        release_first: threading.Event,
        second_completed: threading.Event,
    ) -> None:
        self.barrier = barrier
        self.release_first = release_first
        self.second_completed = second_completed
        self.requests: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.requests.append(deepcopy(messages))
        objective = assigned_objective(messages)
        self.barrier.wait(timeout=3)
        if objective == "first objective":
            if not self.release_first.wait(timeout=3):
                raise AssertionError("test did not release the first subagent")
        else:
            self.second_completed.set()
        return final_report(f"report for {objective}")


class PartialFailureClient:
    def complete(self, messages, tools):
        objective = assigned_objective(messages)
        if objective == "fail this analysis":
            raise ModelError("synthetic child failure")
        return final_report(f"successful evidence for {objective}")


class ForbiddenToolClient:
    def __init__(self) -> None:
        self.calls = 0
        self.objective = ""
        self.seen_schema_names: set[str] = set()
        self.denial: dict[str, Any] | None = None

    def complete(self, messages, tools):
        self.calls += 1
        self.seen_schema_names = schema_names(tools)
        self.objective = assigned_objective(messages)
        if self.calls == 1:
            if self.objective == "try write":
                return tool_call(
                    "forbidden_write",
                    "write_file",
                    {"path": "created.txt", "content": "not allowed"},
                )
            if self.objective == "try replace":
                return tool_call(
                    "forbidden_replace",
                    "replace_in_file",
                    {
                        "path": "seed.txt",
                        "old": "original",
                        "new": "changed",
                    },
                )
            return tool_call(
                "forbidden_command",
                "run_command",
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('command.txt').write_text('x')",
                    ]
                },
            )
        self.denial = json.loads(str(messages[-1].get("content") or "{}"))
        return final_report(f"forbidden capability was denied for {self.objective}")


class BlockingFinalClient:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def complete(self, messages, tools):
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("test did not release the blocking subagent")
        return final_report("read-only evidence")


class ToolBudgetClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return tool_call(f"read_{self.calls}", "list_files", {})


class ReadOnlyRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_and_dispatcher_both_enforce_read_only_capabilities(self) -> None:
        (self.root / "seed.txt").write_text("original", encoding="utf-8")
        full_registry = ToolRegistry(self.workspace)
        read_only = ToolRegistry(self.workspace, read_only=True)

        self.assertEqual(schema_names(read_only.schemas), READ_ONLY_TOOL_NAMES)
        self.assertIn("write_file", schema_names(full_registry.schemas))
        self.assertIn("run_command", schema_names(full_registry.schemas))

        forbidden_calls = [
            tool_call(
                "write",
                "write_file",
                {"path": "created.txt", "content": "not allowed"},
            )["tool_calls"][0],
            tool_call(
                "replace",
                "replace_in_file",
                {"path": "seed.txt", "old": "original", "new": "changed"},
            )["tool_calls"][0],
            tool_call(
                "command",
                "run_command",
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('command.txt').write_text('x')",
                    ]
                },
            )["tool_calls"][0],
            tool_call(
                "recursive",
                "delegate_readonly",
                {"tasks": [{"id": "nested", "objective": "recurse"}]},
            )["tool_calls"][0],
        ]

        for call in forbidden_calls:
            with self.subTest(tool=call["function"]["name"]):
                result = read_only.execute(call)
                self.assertFalse(result["ok"])
                self.assertIn("tool", result)

        self.assertEqual(
            (self.root / "seed.txt").read_text(encoding="utf-8"),
            "original",
        )
        self.assertFalse((self.root / "created.txt").exists())
        self.assertFalse((self.root / "command.txt").exists())


class ReadOnlySubagentPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _delegate_in_thread(
        self,
        pool: ReadOnlySubagentPool,
        tasks: list[dict[str, str]],
    ) -> tuple[threading.Thread, dict[str, Any]]:
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                outcome["value"] = pool.delegate_tasks(tasks=tasks)
            except BaseException as exc:  # Preserve the worker failure for the test thread.
                outcome["error"] = exc

        worker = threading.Thread(target=invoke, name="test-delegate-call")
        worker.start()
        return worker, outcome

    def _finish_thread(
        self,
        worker: threading.Thread,
        outcome: dict[str, Any],
    ) -> str:
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive(), "delegate_tasks did not return")
        if "error" in outcome:
            raise outcome["error"]
        return str(outcome["value"])

    def test_subagents_really_overlap_but_results_keep_input_order(self) -> None:
        barrier = threading.Barrier(2)
        release_first = threading.Event()
        second_completed = threading.Event()
        factory = RecordingFactory(
            lambda: ConcurrentClient(barrier, release_first, second_completed)
        )
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=2)
        pool.start_run()

        worker, outcome = self._delegate_in_thread(
            pool,
            [
                {"id": "first", "objective": "first objective"},
                {"id": "second", "objective": "second objective"},
            ],
        )
        self.assertTrue(
            second_completed.wait(timeout=3),
            "both clients did not reach the barrier concurrently",
        )
        release_first.set()
        payload = decode_result(self._finish_thread(worker, outcome))

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["workspace_stable"])
        self.assertEqual(payload["batch"], 1)
        self.assertEqual(
            [item["id"] for item in payload["subtasks"]],
            ["first", "second"],
        )
        self.assertEqual(len(factory.clients), 2)
        self.assertIsNot(factory.clients[0], factory.clients[1])
        for client in factory.clients:
            request_text = json.dumps(client.requests, ensure_ascii=False)
            own_objective = assigned_objective(client.requests[0])
            other_objective = (
                "second objective"
                if own_objective == "first objective"
                else "first objective"
            )
            self.assertNotIn(other_objective, request_text)

    def test_partial_failure_preserves_successful_report(self) -> None:
        factory = RecordingFactory(PartialFailureClient)
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=2)
        pool.start_run()

        payload = decode_result(
            pool.delegate_tasks(
                tasks=[
                    {"id": "good", "objective": "inspect source"},
                    {"id": "bad", "objective": "fail this analysis"},
                ]
            )
        )
        records = result_by_id(payload)

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["workspace_stable"])
        self.assertEqual(records["good"]["status"], "completed")
        self.assertIn("successful evidence", records["good"]["report"])
        self.assertEqual(records["bad"]["status"], "error")
        self.assertIn("synthetic child failure", records["bad"]["report"])
        self.assertEqual([item["id"] for item in payload["subtasks"]], ["good", "bad"])

    def test_only_one_batch_is_allowed_per_run_and_start_run_resets_it(self) -> None:
        factory = RecordingFactory(FinalClient)
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=1)
        task = [{"id": "one", "objective": "inspect"}]

        pool.start_run()
        first = decode_result(pool.delegate_tasks(tasks=task))
        self.assertEqual(first["batch"], 1)
        with self.assertRaises(ToolError):
            pool.delegate_tasks(tasks=task)
        self.assertEqual(len(factory.clients), 1)

        pool.start_run()
        reset = decode_result(pool.delegate_tasks(tasks=task))
        self.assertEqual(reset["batch"], 1)
        self.assertEqual(len(factory.clients), 2)

    def test_invalid_batches_are_rejected_before_creating_clients(self) -> None:
        factory = RecordingFactory(FinalClient)
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=2)
        invalid_batches: list[Any] = [
            [],
            "not-a-list",
            [{}],
            [{"id": "", "objective": "inspect"}],
            [{"id": "bad id", "objective": "inspect"}],
            [{"id": "one", "objective": ""}],
            [{"id": "one", "objective": 123}],
            [
                {"id": "same", "objective": "first"},
                {"id": "same", "objective": "second"},
            ],
            [{"id": "one", "objective": "inspect", "unexpected": True}],
            [
                {"id": f"task-{index}", "objective": "inspect"}
                for index in range(4)
            ],
            [{"id": "huge", "objective": "x" * 50_000}],
        ]

        for tasks in invalid_batches:
            with self.subTest(tasks=repr(tasks)[:120]):
                pool.start_run()
                with self.assertRaises(ToolError):
                    pool.delegate_tasks(tasks=tasks)
        self.assertEqual(factory.clients, [])

        with self.assertRaises(ValueError):
            ReadOnlySubagentPool(factory, self.workspace, max_workers=0)

    def test_malicious_children_cannot_write_run_commands_or_recurse(self) -> None:
        (self.root / "seed.txt").write_text("original", encoding="utf-8")
        factory = RecordingFactory(ForbiddenToolClient)
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=3)
        pool.start_run()

        payload = decode_result(
            pool.delegate_tasks(
                tasks=[
                    {"id": "write", "objective": "try write"},
                    {"id": "replace", "objective": "try replace"},
                    {"id": "command", "objective": "try command"},
                ]
            )
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(
            (self.root / "seed.txt").read_text(encoding="utf-8"),
            "original",
        )
        self.assertFalse((self.root / "created.txt").exists())
        self.assertFalse((self.root / "command.txt").exists())
        for client in factory.clients:
            self.assertEqual(client.seen_schema_names, READ_ONLY_TOOL_NAMES)
            self.assertIsNotNone(client.denial)
            self.assertFalse(client.denial["ok"])
            self.assertNotIn("delegate_readonly", client.seen_schema_names)
            self.assertNotIn("delegate_tasks", client.seen_schema_names)

    def test_long_child_report_is_bounded_before_returning_to_parent(self) -> None:
        long_report = "evidence-" + ("x" * 20_000)
        factory = RecordingFactory(lambda: FinalClient(long_report))
        pool = ReadOnlySubagentPool(
            factory,
            self.workspace,
            max_workers=1,
            max_context_chars=10_000,
        )
        pool.start_run()

        payload = decode_result(
            pool.delegate_tasks(
                tasks=[{"id": "long", "objective": "inspect everything"}]
            )
        )
        report = result_by_id(payload)["long"]["report"]

        self.assertLess(len(report), len(long_report))
        self.assertLessEqual(len(report), 4_000)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 6_000)

    def test_workspace_drift_fails_closed_with_diagnostic_json(self) -> None:
        target = self.root / "observed.txt"
        target.write_text("before", encoding="utf-8")
        entered = threading.Event()
        release = threading.Event()
        factory = RecordingFactory(lambda: BlockingFinalClient(entered, release))
        pool = ReadOnlySubagentPool(factory, self.workspace, max_workers=1)
        pool.start_run()

        worker, outcome = self._delegate_in_thread(
            pool,
            [{"id": "reader", "objective": "inspect observed.txt"}],
        )
        self.assertTrue(entered.wait(timeout=3), "subagent did not start")
        target.write_text("changed outside the pool", encoding="utf-8")
        release.set()
        worker.join(timeout=4)
        self.assertFalse(worker.is_alive(), "delegate_tasks did not return")
        self.assertIsInstance(outcome.get("error"), ToolError)
        payload = decode_result(str(outcome["error"]))

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["workspace_stable"])
        self.assertIn("error", payload)
        self.assertTrue(str(payload["error"]).strip())

    def test_child_tool_budget_and_lifecycle_events_are_enforced(self) -> None:
        events: list[dict[str, Any]] = []
        factory = RecordingFactory(ToolBudgetClient)
        pool = ReadOnlySubagentPool(
            factory,
            self.workspace,
            max_workers=1,
            max_steps=6,
            max_tool_calls=2,
            on_event=events.append,
        )

        with self.assertRaises(ToolError) as raised:
            pool.delegate_tasks(
                tasks=[{"id": "budget", "objective": "keep reading forever"}]
            )
        payload = decode_result(str(raised.exception))

        record = result_by_id(payload)["budget"]
        self.assertEqual(record["status"], "tool_call_limit")
        self.assertEqual(record["steps"], 3)
        self.assertEqual(factory.clients[0].calls, 3)
        self.assertEqual(events[0]["type"], "delegation_started")
        self.assertEqual(events[1]["type"], "subtask_started")
        self.assertEqual(events[-2]["type"], "subtask_completed")
        self.assertEqual(events[-1]["type"], "delegation_completed")
        self.assertEqual(events[-1]["completed"], 0)
        self.assertEqual(events[-1]["failed"], 1)

    def test_default_subagent_budgets_are_unlimited(self) -> None:
        pool = ReadOnlySubagentPool(
            RecordingFactory(FinalClient), self.workspace, max_workers=1
        )
        self.assertIsNone(pool.max_steps)
        self.assertIsNone(pool.max_tool_calls)
        self.assertIsNone(pool.max_runtime_seconds)
        with self.assertRaises(ValueError):
            ReadOnlySubagentPool(
                RecordingFactory(FinalClient),
                self.workspace,
                max_workers=1,
                max_steps=0,
            )

    def test_aggregate_output_honors_the_configured_global_cap(self) -> None:
        factory = RecordingFactory(lambda: FinalClient("x" * 10_000))
        pool = ReadOnlySubagentPool(
            factory,
            self.workspace,
            max_workers=4,
            max_output_chars=500,
        )

        raw = pool.delegate_tasks(
            tasks=[
                {"id": f"task-{index}", "objective": "inspect"}
                for index in range(4)
            ]
        )
        payload = decode_result(raw)

        self.assertLessEqual(len(raw), 500)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            [item["id"] for item in payload["subtasks"]],
            ["task-0", "task-1", "task-2", "task-3"],
        )


if __name__ == "__main__":
    unittest.main()
