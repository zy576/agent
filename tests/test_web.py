from __future__ import annotations

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

from forgeloop.agent import AgentResult
from forgeloop.client import ModelError
from forgeloop.web import (
    MAX_EVENTS_PER_RUN,
    RunState,
    WebApplication,
    WebBusyError,
    WebClosingError,
    WebPoisonedError,
    _loopback_authority,
    _loopback_origin,
    _safe_agent_event,
    create_server,
    serve_web,
)


def wait_for_run(state: RunState, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    with state.condition:
        while not state.done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("web run did not finish")
            state.condition.wait(timeout=remaining)


class RecordingFactory:
    def __init__(self, api_key: str = "test-deepseek-secret") -> None:
        self.api_key = api_key
        self.calls: list[tuple[str, list[dict] | None, bool]] = []

    def __call__(self, on_event):
        parent = self

        class FakeAgent:
            def run(self, task, *, history=None, verification_pending=False):
                parent.calls.append((task, history, verification_pending))
                turn = len(parent.calls)
                on_event({"type": "model_request", "step": 1, "message_count": 2})
                on_event(
                    {
                        "type": "tool_start",
                        "step": 1,
                        "call_id": f"call_{turn}",
                        "tool": "read_file",
                        "arguments": {"path": "app.py", "api_key": parent.api_key},
                    }
                )
                on_event(
                    {
                        "type": "tool_end",
                        "step": 1,
                        "call_id": f"call_{turn}",
                        "tool": "read_file",
                        "result": {
                            "ok": True,
                            "output": f"safe result {parent.api_key}",
                        },
                    }
                )
                summary = f"turn {turn} complete; never expose {parent.api_key}"
                on_event(
                    {
                        "type": "final",
                        "step": 1,
                        "status": "completed",
                        "summary": summary,
                    }
                )
                return AgentResult(
                    status="completed",
                    summary=summary,
                    steps=1,
                    changed_files=["app.py"],
                    verifications=["python -m unittest: exit_code=0"],
                    verification_pending=turn == 1,
                    messages=[{"role": "system", "content": f"history-{turn}"}],
                )

        return FakeAgent()


class WebApplicationTests(unittest.TestCase):
    def test_completed_status_with_verification_debt_is_fail_closed(self) -> None:
        factory = RecordingFactory()
        application = WebApplication(
            factory,
            api_key=factory.api_key,
            workspace="C:/workspace",
            model="deepseek-test",
        )

        first = application.start_turn("first task")
        wait_for_run(first)

        snapshot = application.snapshot()
        self.assertEqual(
            snapshot["latest_outcome"]["status"],
            "completed_with_verification_risk",
        )
        self.assertEqual(
            snapshot["conversation"][-1]["status"],
            "completed_with_verification_risk",
        )

    def test_session_carries_history_and_verification_debt(self) -> None:
        factory = RecordingFactory()
        application = WebApplication(
            factory,
            api_key=factory.api_key,
            workspace="C:/workspace",
            model="deepseek-test",
        )

        first = application.start_turn("first task")
        wait_for_run(first)
        second = application.start_turn("follow up")
        wait_for_run(second)

        self.assertEqual(factory.calls[0], ("first task", None, False))
        self.assertEqual(
            factory.calls[1],
            ("follow up", [{"role": "system", "content": "history-1"}], True),
        )
        snapshot = application.snapshot()
        self.assertEqual(snapshot["turn_count"], 2)
        self.assertFalse(snapshot["verification_pending"])
        self.assertEqual(snapshot["latest_outcome"]["status"], "completed")
        self.assertEqual(snapshot["latest_outcome"]["changed_files"], ["app.py"])
        self.assertEqual(
            snapshot["latest_outcome"]["verifications"],
            ["python -m unittest: exit_code=0"],
        )
        self.assertEqual([item["role"] for item in snapshot["conversation"]], [
            "user",
            "assistant",
            "user",
            "assistant",
        ])

    def test_browser_events_are_ordered_bounded_and_redacted(self) -> None:
        factory = RecordingFactory()
        application = WebApplication(
            factory,
            api_key=factory.api_key,
            workspace="C:/workspace",
            model="deepseek-test",
        )
        state = application.start_turn("inspect")
        wait_for_run(state)
        _, events, next_id, done = state.snapshot(0)

        self.assertTrue(done)
        self.assertEqual([item["id"] for item in events], list(range(next_id)))
        self.assertEqual(events[-1]["type"], "turn_complete")
        rendered = json.dumps(
            {"events": events, "status": application.snapshot()}, ensure_ascii=False
        )
        self.assertNotIn(factory.api_key, rendered)
        self.assertIn("[REDACTED]", rendered)

        for event_type in ("tool_start", "tool_end"):
            raw_event = {
                "type": event_type,
                "step": 1,
                "call_id": factory.api_key,
                "tool": factory.api_key,
                "arguments": {},
                "result": {"ok": True, "output": "safe"},
            }
            safe_event = _safe_agent_event(raw_event, factory.api_key)
            self.assertNotIn(factory.api_key, json.dumps(safe_event))

        self.assertEqual(
            _safe_agent_event(
                {
                    "type": "finalization_request",
                    "message_count": 17,
                },
                factory.api_key,
            ),
            {"type": "finalization_request", "message_count": 17},
        )

        bounded = RunState("run", "task", 1)
        for index in range(MAX_EVENTS_PER_RUN + 3):
            bounded.append({"type": "test", "value": index})
        base, records, event_id, _ = bounded.snapshot(0)
        self.assertEqual(base, 3)
        self.assertEqual(len(records), MAX_EVENTS_PER_RUN)
        self.assertEqual(event_id, MAX_EVENTS_PER_RUN + 3)

    def test_concurrent_turn_is_rejected_until_worker_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def factory(on_event):
            class BlockingAgent:
                def run(self, task, *, history=None, verification_pending=False):
                    started.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("test did not release worker")
                    return AgentResult(
                        status="completed",
                        summary="done",
                        steps=1,
                        messages=[{"role": "system", "content": "closed"}],
                    )

            return BlockingAgent()

        application = WebApplication(
            factory,
            api_key="secret",
            workspace="C:/workspace",
            model="model",
        )
        first = application.start_turn("first")
        self.assertTrue(started.wait(timeout=2))
        first.started_at -= 1.0
        active_snapshot = application.snapshot()
        self.assertTrue(active_snapshot["busy"])
        self.assertEqual(active_snapshot["active_run_id"], first.run_id)
        self.assertGreaterEqual(active_snapshot["active_elapsed_ms"], 900)
        with self.assertRaises(WebBusyError):
            application.start_turn("second")
        release.set()
        wait_for_run(first)
        follow_up = application.start_turn("second")
        wait_for_run(follow_up)
        self.assertEqual(application.snapshot()["active_elapsed_ms"], 0)

    def test_agent_exception_closes_session_and_redacts_secret(self) -> None:
        secret = "deepseek-private-value"

        def factory(on_event):
            class FailingAgent:
                def run(self, task, *, history=None, verification_pending=False):
                    raise ModelError(f"upstream rejected {secret}")

            return FailingAgent()

        application = WebApplication(
            factory,
            api_key=secret,
            workspace="C:/workspace",
            model="model",
        )
        state = application.start_turn("fail")
        wait_for_run(state)
        snapshot = application.snapshot()
        self.assertTrue(snapshot["poisoned"])
        self.assertNotIn(secret, json.dumps(snapshot))
        with self.assertRaises(WebPoisonedError):
            application.start_turn("unsafe continuation")

    def test_wait_for_workers_does_not_return_before_thread_finishes(self) -> None:
        finish_entered = threading.Event()
        finish_release = threading.Event()
        original_finish = RunState.finish

        def delayed_finish(state):
            finish_entered.set()
            if not finish_release.wait(timeout=3):
                raise AssertionError("test did not release finish")
            original_finish(state)

        def factory(on_event):
            class ImmediateAgent:
                def run(self, task, *, history=None, verification_pending=False):
                    return AgentResult(
                        status="completed",
                        summary="done",
                        steps=1,
                        messages=[{"role": "system", "content": "closed"}],
                    )

            return ImmediateAgent()

        application = WebApplication(
            factory, api_key="secret", workspace="C:/workspace", model="model"
        )
        with patch.object(RunState, "finish", delayed_finish):
            application.start_turn("task")
            self.assertTrue(finish_entered.wait(timeout=2))
            waiter = threading.Thread(target=application.wait_for_workers)
            waiter.start()
            waiter.join(timeout=0.1)
            self.assertTrue(waiter.is_alive())
            finish_release.set()
            waiter.join(timeout=2)
            self.assertFalse(waiter.is_alive())

    def test_default_http_port_uses_canonical_authority(self) -> None:
        self.assertEqual(_loopback_authority(80), "127.0.0.1")
        self.assertEqual(_loopback_origin(80), "http://127.0.0.1")
        self.assertEqual(_loopback_authority(8765), "127.0.0.1:8765")

    def test_shutdown_permanently_closes_new_turn_admission(self) -> None:
        factory = RecordingFactory()
        application = WebApplication(
            factory, api_key="secret", workspace="C:/workspace", model="model"
        )
        application.begin_shutdown()
        with self.assertRaises(WebClosingError):
            application.start_turn("too late")
        self.assertTrue(application.snapshot()["closing"])
        self.assertEqual(application.snapshot()["turn_count"], 0)


class WebHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = RecordingFactory()
        self.application = WebApplication(
            self.factory,
            api_key=self.factory.api_key,
            workspace="C:/workspace/demo",
            model="deepseek-test",
            token="fixed-browser-token",
        )
        self.server, self.url = create_server(self.application)
        self.port = int(self.server.server_address[1])
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.application.wait_for_workers()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def authorized_headers(self, *, json_body=False):
        headers = {"X-ForgeLoop-Token": self.application.token}
        if json_body:
            headers.update(
                {"Origin": self.origin, "Content-Type": "application/json"}
            )
        return headers

    def test_static_assets_and_status_have_security_headers(self) -> None:
        status, headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ForgeLoop", payload)
        self.assertIn(b"fixed-browser-token", payload)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, _, payload = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn(b"innerHTML", payload)

        status, _, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 403)
        status, _, payload = self.request(
            "GET", "/api/status", headers=self.authorized_headers()
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["workspace"], "C:/workspace/demo")

    def test_turn_endpoint_requires_origin_token_and_exact_json(self) -> None:
        body = json.dumps({"task": "inspect"}).encode()
        status, _, _ = self.request(
            "POST", "/api/turn", body=body, headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 403)

        headers = self.authorized_headers(json_body=True)
        headers["Origin"] = "http://evil.invalid"
        status, _, _ = self.request("POST", "/api/turn", body=body, headers=headers)
        self.assertEqual(status, 403)

        headers = self.authorized_headers(json_body=True)
        headers["Content-Type"] = "text/plain"
        status, _, _ = self.request("POST", "/api/turn", body=body, headers=headers)
        self.assertEqual(status, 403)

        status, _, _ = self.request(
            "POST",
            "/api/turn",
            body=b"not-json",
            headers=self.authorized_headers(json_body=True),
        )
        self.assertEqual(status, 400)

        status, _, _ = self.request("OPTIONS", "/api/turn")
        self.assertEqual(status, 405)

    def test_completed_events_replay_and_never_leak_api_key(self) -> None:
        body = json.dumps({"task": "inspect"}).encode()
        status, _, payload = self.request(
            "POST",
            "/api/turn",
            body=body,
            headers=self.authorized_headers(json_body=True),
        )
        self.assertEqual(status, 202)
        run_id = json.loads(payload)["run_id"]
        state = self.application.get_run(run_id)
        self.assertIsNotNone(state)
        wait_for_run(state)

        status, headers, payload = self.request(
            "GET",
            f"/api/events?run_id={run_id}&cursor=0",
            headers=self.authorized_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/x-ndjson; charset=utf-8")
        self.assertNotIn(self.factory.api_key.encode(), payload)
        events = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(events[-1]["type"], "turn_complete")
        self.assertEqual([event["id"] for event in events], list(range(len(events))))

    def test_host_path_and_body_limits_are_enforced(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.putrequest("GET", "/api/status", skip_host=True)
        connection.putheader("Host", "localhost")
        connection.putheader("X-ForgeLoop-Token", self.application.token)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        connection.close()

        status, _, _ = self.request("GET", "/assets/../web.py")
        self.assertEqual(status, 404)
        connection = HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.putrequest("POST", "/api/turn")
        for name, value in self.authorized_headers(json_body=True).items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(64 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()

    def test_rejected_post_cannot_desynchronize_keep_alive_connection(self) -> None:
        injected = (
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
        ).encode("ascii")
        request = (
            f"POST /api/turn HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Origin: http://evil.invalid\r\n"
            f"X-ForgeLoop-Token: {self.application.token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(injected)}\r\n\r\n"
        ).encode("ascii") + injected

        received = bytearray()
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as stream:
            stream.sendall(request)
            stream.settimeout(1)
            while True:
                try:
                    chunk = stream.recv(4096)
                except (ConnectionResetError, socket.timeout):
                    break
                if not chunk:
                    break
                received.extend(chunk)

        response = bytes(received)
        self.assertIn(b"403 Forbidden", response)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertNotIn(self.application.token.encode(), response)
        self.assertNotIn(b"AI Coding Workbench", response)

    def test_live_stream_flushes_before_completion_and_disconnect_is_harmless(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def factory(on_event):
            class BlockingAgent:
                def run(self, task, *, history=None, verification_pending=False):
                    on_event({"type": "model_request", "step": 1, "message_count": 2})
                    started.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("test did not release agent")
                    on_event(
                        {
                            "type": "final",
                            "step": 1,
                            "status": "completed",
                            "summary": "done",
                        }
                    )
                    return AgentResult(
                        status="completed",
                        summary="done",
                        steps=1,
                        messages=[{"role": "system", "content": "closed"}],
                    )

            return BlockingAgent()

        application = WebApplication(
            factory,
            api_key="secret",
            workspace="C:/workspace",
            model="model",
            token="stream-token",
        )
        server, _ = create_server(application)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            state = application.start_turn("first")
            self.assertTrue(started.wait(timeout=2))
            connection = HTTPConnection(
                "127.0.0.1", int(server.server_address[1]), timeout=3
            )
            connection.request(
                "GET",
                f"/api/events?run_id={state.run_id}&cursor=0",
                headers={"X-ForgeLoop-Token": application.token},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            first_event = json.loads(response.readline())
            self.assertEqual(first_event["type"], "run_started")
            connection.close()

            release.set()
            wait_for_run(state)
            self.assertFalse(application.snapshot()["busy"])
            follow_up = application.start_turn("second")
            wait_for_run(follow_up)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            application.wait_for_workers()
            thread.join(timeout=2)

    def test_post_already_reading_body_is_rejected_after_shutdown_begins(self) -> None:
        body = json.dumps({"task": "must not start"}).encode()
        request_headers = (
            f"POST /api/turn HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Origin: {self.origin}\r\n"
            f"X-ForgeLoop-Token: {self.application.token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")

        received = bytearray()
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as stream:
            stream.sendall(request_headers)
            time.sleep(0.05)
            self.application.begin_shutdown()
            stream.sendall(body)
            stream.settimeout(2)
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)

        self.assertIn(b"503 Service Unavailable", bytes(received))
        snapshot = self.application.snapshot()
        self.assertEqual(snapshot["turn_count"], 0)
        self.assertFalse(snapshot["busy"])


class WebStaticSourceTests(unittest.TestCase):
    def test_frontend_uses_safe_dom_rendering_and_has_no_remote_assets(self) -> None:
        root = Path(__file__).parents[1] / "src" / "forgeloop" / "web_static"
        javascript = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        for unsafe in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            ".style.",
        ):
            self.assertNotIn(unsafe, javascript)
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)
        self.assertIn("aria-live", html)
        self.assertIn('event.type === "finalization_request"', javascript)
        self.assertIn('ui.verificationIcon.textContent = "!"', javascript)
        self.assertIn('const status = String(event.status || "unknown")', javascript)
        self.assertIn("function classifyOutcome", javascript)
        self.assertIn("const MAX_TIMELINE_ITEMS = 600", javascript)
        self.assertIn("const MAX_CONVERSATION_ITEMS = 100", javascript)
        self.assertIn("const EVENT_BATCH_SIZE = 100", javascript)
        self.assertIn("oldest.remove()", javascript)
        self.assertIn("startElapsedClock(snapshot.active_elapsed_ms)", javascript)
        self.assertIn("await processEventLines(lines)", javascript)
        self.assertIn("verifications[verifications.length - 1]", javascript)
        self.assertIn('notice.setAttribute("aria-hidden", "true")', javascript)
        self.assertNotIn("scrollIntoView", javascript)
        self.assertNotIn(
            '<div class="verification-icon" aria-hidden="true">✓</div>',
            html,
        )


class WebServerLifecycleTests(unittest.TestCase):
    def test_browser_opener_runs_after_server_loop_has_started(self) -> None:
        serving = threading.Event()
        opened = threading.Event()
        ordering = []

        class FakeServer:
            def serve_forever(self, poll_interval):
                ordering.append("serving")
                serving.set()
                if not opened.wait(timeout=2):
                    raise AssertionError("browser opener did not run")

            def server_close(self):
                ordering.append("closed")

        class FakeApplication:
            def begin_shutdown(self):
                ordering.append("shutdown")

            def wait_for_workers(self):
                ordering.append("drained")

        def browser_open(url):
            self.assertTrue(serving.is_set())
            self.assertEqual(url, "http://127.0.0.1:4321/")
            ordering.append("opened")
            opened.set()
            return True

        with (
            patch(
                "forgeloop.web.create_server",
                return_value=(FakeServer(), "http://127.0.0.1:4321/"),
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = serve_web(
                FakeApplication(),
                open_browser=True,
                browser_open=browser_open,
            )

        self.assertEqual(result, 0)
        self.assertEqual(ordering[:2], ["serving", "opened"])
        self.assertEqual(ordering[-3:], ["shutdown", "closed", "drained"])


if __name__ == "__main__":
    unittest.main()
