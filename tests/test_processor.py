"""Exercise the LLM wire protocol and reply-to-playback path without providers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from app import processor
from app.tasks import TaskCancelled, TaskStore


class FakeAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
        })
        self.send_response(self.server.status)
        self.send_header("Content-Type", self.server.content_type)
        self.end_headers()
        if self.server.prefix:
            self.wfile.write(self.server.prefix)
            self.wfile.flush()
            self.server.prefix_sent.set()
            self.server.release.wait(3)
        self.wfile.write(self.server.bodies.pop(0) if self.server.bodies else self.server.body)

    def log_message(self, *args):
        pass


class ProcessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        cls.thread.join()

    def setUp(self):
        history = patch.object(processor, "_history", processor.deque(maxlen=50))
        history.start()
        self.addCleanup(history.stop)
        self.api.requests = []
        self.api.bodies = []
        self.api.content_type = "application/json"
        self.api.prefix = b""
        self.api.prefix_sent = threading.Event()
        self.api.release = threading.Event()
        self.api.status = 200
        self.reply = "Привет! Как дела?"
        self.api.body = json.dumps({"choices": [{"message": {"content": self.reply}}]}).encode()
        env = patch.dict(os.environ, {
            "LLM_BASE_URL": f"http://127.0.0.1:{self.api.server_port}/v1/",
            "LLM_MODEL": "local-model",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_prompt_unicode_markup_and_optional_fields(self):
        self.assertEqual(processor.respond("  Привет  "), self.reply)
        request = self.api.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertIsNone(request["authorization"])
        self.assertEqual(request["body"], {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": processor.SYSTEM_PROMPT},
                {"role": "user", "content": "Привет"},
            ],
        })

    def test_authentication_and_reasoning_when_configured(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "test-key", "LLM_REASONING_EFFORT": "low"}):
            processor.respond("Hello")
        self.assertEqual(self.api.requests[0]["authorization"], "Bearer test-key")
        self.assertEqual(self.api.requests[0]["body"]["reasoning_effort"], "low")

    def test_follow_up_includes_previous_user_and_assistant(self):
        processor.respond("My name is Alex")
        processor.respond("What is my name?")
        self.assertEqual(self.api.requests[-1]["body"]["messages"], [
            {"role": "system", "content": processor.SYSTEM_PROMPT},
            {"role": "user", "content": "My name is Alex"},
            {"role": "assistant", "content": self.reply},
            {"role": "user", "content": "What is my name?"},
        ])

    def test_history_drops_oldest_messages_at_fifty(self):
        conversation = []
        for turn in range(30):
            text, reply = f"question {turn}", f"answer {turn}"
            self.api.body = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
            processor.respond(text)
            conversation.append({"role": "user", "content": text})
            self.assertEqual(self.api.requests[-1]["body"]["messages"], [
                {"role": "system", "content": processor.SYSTEM_PROMPT},
                *conversation[-50:],
            ])
            conversation.append({"role": "assistant", "content": reply})
            self.assertEqual(list(processor._history), conversation[-50:])

    def test_failed_and_cancelled_turns_leave_context_unchanged(self):
        processor.respond("Remember this")
        saved = list(processor._history)
        for failure in (TimeoutError(), TaskCancelled("interrupted")):
            with patch.object(processor, "urlopen", side_effect=failure):
                if isinstance(failure, TaskCancelled):
                    with self.assertRaises(TaskCancelled):
                        processor.respond("discard this")
                else:
                    processor.respond("discard this")
            self.assertEqual(list(processor._history), saved)
        processor.respond("Follow up")
        self.assertEqual(self.api.requests[-1]["body"]["messages"][1:-1], saved)

    def test_overlapping_requests_share_completed_context(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        requests = []

        def request(messages):
            requests.append(messages)
            if len(requests) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test request was not released")
            return "reply " + messages[-1]["content"]

        def second_turn():
            second_started.set()
            return processor.respond("second")

        with patch.object(processor, "_request_response", side_effect=request):
            first = threading.Thread(target=processor.respond, args=("first",))
            second = threading.Thread(target=second_turn)
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                second.start()
                self.assertTrue(second_started.wait(3))
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
        self.assertEqual(requests, [
            [{"role": "user", "content": "first"}],
            [{"role": "user", "content": "first"},
             {"role": "assistant", "content": "reply first"},
             {"role": "user", "content": "second"}],
        ])

    @staticmethod
    def tool_call(index, name="web_search", arguments='{"query":"current facts"}'):
        return {"id": f"call-{index}", "type": "function", "function": {
            "name": name, "arguments": arguments,
        }}

    def queue_messages(self, *messages):
        self.api.bodies = [json.dumps({"choices": [{"message": message}]}).encode()
                           for message in messages]

    def test_search_and_page_read_feed_final_reply_without_polluting_history(self):
        search = self.tool_call(1)
        read = self.tool_call(2, "read_web_page", '{"url":"https://example.com/news"}')
        self.queue_messages({"content": None, "tool_calls": [search]},
                            {"content": None, "tool_calls": [read]},
                            {"content": "Here is the verified answer."})
        with patch.dict(os.environ, {"KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "execute", side_effect=["search results", "page content"]) as execute:
            self.assertEqual(processor.respond("What happened today?"), "Here is the verified answer.")
        self.assertEqual(execute.call_args_list[0].args, ("web_search", search["function"]["arguments"]))
        self.assertEqual(execute.call_args_list[1].args, ("read_web_page", read["function"]["arguments"]))
        messages = self.api.requests[-1]["body"]["messages"]
        self.assertEqual([m["content"] for m in messages if m["role"] == "tool"],
                         ["search results", "page content"])
        self.assertEqual([t["function"]["name"] for t in self.api.requests[0]["body"]["tools"]],
                         ["web_search", "read_web_page"])
        self.assertEqual(list(processor._history), [
            {"role": "user", "content": "What happened today?"},
            {"role": "assistant", "content": "Here is the verified answer."},
        ])

    def test_tool_budget_caps_repeated_rounds_and_resets_for_next_response(self):
        rounds = [{"content": None, "tool_calls": [self.tool_call(i)]} for i in range(5)]
        self.queue_messages(*rounds, {"content": "Final answer"},
                            {"content": None, "tool_calls": [self.tool_call(6)]},
                            {"content": "Follow-up answer"})
        with patch.dict(os.environ, {"KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "execute", return_value="evidence") as execute:
            self.assertEqual(processor.respond("Research this"), "Final answer")
            self.assertEqual(execute.call_count, 5)
            final = self.api.requests[-1]["body"]
            self.assertEqual(final["tool_choice"], "none")
            self.assertIn("five-tool limit", final["messages"][-1]["content"])
            self.assertEqual(processor.respond("More details"), "Follow-up answer")
            self.assertEqual(execute.call_count, 6)
            self.assertEqual(self.api.requests[6]["body"]["tool_choice"], "auto")
            self.assertEqual(len(processor._history), 4)

    def test_tool_budget_counts_each_call_in_an_oversized_batch(self):
        self.queue_messages({"content": None, "tool_calls": [self.tool_call(i) for i in range(8)]},
                            {"content": "Answer from five searches"})
        with patch.dict(os.environ, {"KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "execute", return_value="evidence") as execute:
            self.assertEqual(processor.respond("Research"), "Answer from five searches")
            self.assertEqual(execute.call_count, 5)
        final = self.api.requests[-1]["body"]
        outputs = [m for m in final["messages"] if m["role"] == "tool"]
        self.assertEqual(len(outputs), 8)
        self.assertEqual(sum("Tool limit reached" in m["content"] for m in outputs), 3)
        self.assertEqual(final["tool_choice"], "none")

    def test_invalid_tool_arguments_use_budget_without_external_requests(self):
        self.queue_messages({"tool_calls": [self.tool_call(i, arguments='{"query":""}')
                                            for i in range(5)]}, {"content": "Could not verify"})
        with patch.dict(os.environ, {"KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "_request") as request:
            self.assertEqual(processor.respond("Research"), "Could not verify")
            request.assert_not_called()
        self.assertEqual(self.api.requests[-1]["body"]["tool_choice"], "none")

    def test_provider_ignoring_disabled_tools_cannot_run_more_tools(self):
        self.queue_messages(*[{"tool_calls": [self.tool_call(i)]} for i in range(6)])
        with patch.dict(os.environ, {"KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "execute", return_value="evidence") as execute:
            self.assertIn("tool use was disabled", processor.respond("Research"))
            self.assertEqual(execute.call_count, 5)
        self.assertEqual(len(self.api.requests), 6)
        self.assertEqual(list(processor._history), [])

    @staticmethod
    def sse(*deltas, finish="stop"):
        chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                  for delta in deltas]
        if finish:
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        return b"".join(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"
                        for chunk in chunks) + (b"data: [DONE]\n\n" if finish else b"")

    def test_stream_emits_first_line_before_remaining_text_arrives(self):
        self.api.content_type = "text/event-stream"
        self.api.prefix = self.sse({"content": "Привет."}, {"content": "\n"}, finish=None)
        self.api.body = self.sse({"content": "Как дела?\n"})
        heard = threading.Event()
        lines, result = [], []
        def sentence(text):
            lines.append(text)
            heard.set()
        with patch.dict(os.environ, {"LLM_STREAMING": "1"}):
            worker = threading.Thread(target=lambda: result.append(
                processor.respond("Привет", on_sentence=sentence)))
            worker.start()
            try:
                self.assertTrue(heard.wait(2), "first sentence waited for the full response")
                self.assertEqual(lines, ["Привет."])
                self.assertTrue(worker.is_alive())
                self.assertEqual(list(processor._history), [])
            finally:
                self.api.release.set()
                worker.join(3)
        self.assertEqual(result, ["Привет.\nКак дела?"])
        self.assertEqual(lines, ["Привет.", "Как дела?"])
        self.assertTrue(self.api.requests[0]["body"]["stream"])
        self.assertEqual(processor._history[-1]["content"], result[0])

    def test_stream_reassembles_fragmented_tools_and_keeps_five_call_cap(self):
        self.api.content_type = "text/event-stream"
        calls = []
        for i in range(6):
            calls.extend([
                {"tool_calls": [{"index": i, "id": f"call-{i}", "type": "function",
                                  "function": {"name": "web_", "arguments": '{"query":'}}]},
                {"tool_calls": [{"index": i, "function": {"name": "search", "arguments": '"facts"}'}}]},
            ])
        self.api.bodies = [self.sse(*calls, finish="tool_calls"), self.sse({"content": "Verified.\n"})]
        lines = []
        with patch.dict(os.environ, {"LLM_STREAMING": "1", "KEENABLE_API_KEY": "test"}), \
             patch.object(processor.web_tools, "execute", return_value="evidence") as execute:
            self.assertEqual(processor.respond("Research", on_sentence=lines.append), "Verified.")
        self.assertEqual(execute.call_count, 5)
        self.assertEqual(execute.call_args.args, ("web_search", '{"query":"facts"}'))
        self.assertEqual(lines, ["Verified."])
        self.assertEqual(self.api.requests[-1]["body"]["tool_choice"], "none")

    def test_broken_stream_does_not_commit_partial_history(self):
        self.api.content_type = "text/event-stream"
        self.api.body = self.sse({"content": "Unfinished text"}, finish=None)
        lines = []
        with patch.dict(os.environ, {"LLM_STREAMING": "1"}):
            result = processor.respond("Hello", on_sentence=lines.append)
        self.assertIn("stream ended before completion", result)
        self.assertEqual(lines, [result])
        self.assertEqual(list(processor._history), [])

    def test_stream_cancellation_does_not_commit_or_speak_error(self):
        self.api.content_type = "text/event-stream"
        self.api.body = self.sse({"content": "Hello.\n"})
        lines = []
        with patch.dict(os.environ, {"LLM_STREAMING": "1"}):
            with self.assertRaises(TaskCancelled):
                processor.respond("Hello", on_sentence=lines.append,
                                  check=Mock(side_effect=[None, None, TaskCancelled("interrupted")]))
        self.assertEqual(lines, [])
        self.assertEqual(list(processor._history), [])

    @staticmethod
    def response_result(text=None, output=None):
        return {"status": "completed", "output": output if output is not None else [
            {"type": "message", "role": "assistant", "id": "msg-test", "status": "completed",
             "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ]}

    def test_openai_selects_responses_and_other_providers_keep_chat(self):
        self.assertEqual(processor._api_style('https://api.openai.com/v1'), 'responses')
        self.assertEqual(processor._api_style('http://localhost:8000/v1'), 'chat_completions')
        with patch.dict(os.environ, {'LLM_API': 'chat_completions'}):
            self.assertEqual(processor._api_style('https://api.openai.com/v1'), 'chat_completions')

    def test_responses_wire_format_with_tools_and_reasoning(self):
        self.api.body = json.dumps(self.response_result('Verified answer.')).encode()
        with patch.dict(os.environ, {'LLM_API': 'responses', 'KEENABLE_API_KEY': 'test',
                                    'LLM_REASONING_EFFORT': 'low'}):
            self.assertEqual(processor.respond('Hello'), 'Verified answer.')
        request = self.api.requests[0]
        self.assertEqual(request['path'], '/v1/responses')
        body = request['body']
        self.assertEqual(body['reasoning'], {'effort': 'low'})
        self.assertFalse(body['store'])
        self.assertEqual(body['include'], ['reasoning.encrypted_content'])
        self.assertEqual(body['input'], [{'role': 'user', 'content': 'Hello'}])
        self.assertIn(processor.SYSTEM_PROMPT, body['instructions'])
        self.assertNotIn('messages', body)
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(body['tools'][0]['type'], 'function')
        self.assertEqual(body['tools'][0]['name'], 'web_search')

    def test_responses_preserves_reasoning_and_calls_and_caps_batch_at_five(self):
        reasoning = {'type': 'reasoning', 'id': 'rs-test', 'summary': [], 'encrypted_content': 'opaque-state'}
        calls = [{'type': 'function_call', 'id': f'fc-{i}', 'call_id': f'call-{i}',
                  'name': 'web_search', 'arguments': '{"query":"facts"}'} for i in range(7)]
        self.api.bodies = [json.dumps(self.response_result(output=[reasoning, *calls])).encode(),
                           json.dumps(self.response_result('Verified.')).encode()]
        with patch.dict(os.environ, {'LLM_API': 'responses', 'KEENABLE_API_KEY': 'test'}), \
             patch.object(processor.web_tools, 'execute', return_value='evidence') as execute:
            self.assertEqual(processor.respond('Research'), 'Verified.')
        self.assertEqual(execute.call_count, 5)
        continuation = self.api.requests[-1]['body']
        self.assertEqual(continuation['input'][1:9], [reasoning, *calls])
        outputs = continuation['input'][9:]
        self.assertEqual([item['call_id'] for item in outputs], [f'call-{i}' for i in range(7)])
        self.assertTrue(all(item['type'] == 'function_call_output' for item in outputs))
        self.assertIn('Tool limit reached', outputs[-1]['output'])
        self.assertEqual(continuation['tool_choice'], 'none')
        self.assertEqual(len(processor._history), 2)

    @staticmethod
    def response_events(*events):
        return b''.join(b'event: ' + event['type'].encode() + b'\ndata: '
                        + json.dumps(event, ensure_ascii=False).encode() + b'\n\n' for event in events)

    def test_responses_stream_emits_line_before_completed_event(self):
        self.api.content_type = 'text/event-stream'
        self.api.prefix = self.response_events({'type': 'response.output_text.delta', 'delta': 'First.\n'})
        self.api.body = self.response_events(
            {'type': 'response.output_text.delta', 'delta': 'Second!\n'},
            {'type': 'response.completed', 'response': self.response_result('First.\nSecond!\n')})
        heard = threading.Event()
        lines, replies = [], []
        def sentence(text):
            lines.append(text)
            heard.set()
        with patch.dict(os.environ, {'LLM_API': 'responses', 'LLM_STREAMING': '1'}):
            worker = threading.Thread(target=lambda: replies.append(processor.respond('Hello', on_sentence=sentence)))
            worker.start()
            try:
                self.assertTrue(heard.wait(2))
                self.assertEqual(lines, ['First.'])
                self.assertTrue(worker.is_alive())
            finally:
                self.api.release.set()
                worker.join(3)
        self.assertEqual(lines, ['First.', 'Second!'])
        self.assertEqual(replies, ['First.\nSecond!'])

    def test_responses_stream_tools_round_trip(self):
        self.api.content_type = 'text/event-stream'
        reasoning = {'type': 'reasoning', 'id': 'rs-test', 'summary': [], 'encrypted_content': 'opaque-state'}
        call = {'type': 'function_call', 'id': 'fc-test', 'call_id': 'call-test',
                'name': 'web_search', 'arguments': '{"query":"facts"}'}
        self.api.bodies = [self.response_events(
            {'type': 'response.output_item.added', 'item': call},
            {'type': 'response.function_call_arguments.delta', 'delta': '{"query":'},
            {'type': 'response.completed', 'response': self.response_result(output=[reasoning, call])}),
            self.response_events({'type': 'response.output_text.delta', 'delta': 'Verified.\n'},
                                 {'type': 'response.completed', 'response': self.response_result('Verified.\n')})]
        lines = []
        with patch.dict(os.environ, {'LLM_API': 'responses', 'LLM_STREAMING': '1', 'KEENABLE_API_KEY': 'test'}), \
             patch.object(processor.web_tools, 'execute', return_value='evidence') as execute:
            self.assertEqual(processor.respond('Research', on_sentence=lines.append), 'Verified.')
        execute.assert_called_once_with('web_search', '{"query":"facts"}')
        self.assertEqual(lines, ['Verified.'])
        self.assertEqual(self.api.requests[-1]['body']['input'][1:3], [reasoning, call])

    def test_responses_incomplete_output_is_not_committed(self):
        result = self.response_result('Unfinished')
        result['status'] = 'incomplete'
        self.api.body = json.dumps(result).encode()
        with patch.dict(os.environ, {'LLM_API': 'responses'}):
            self.assertIn('before it was complete', processor.respond('Hello'))
        self.assertEqual(list(processor._history), [])

    def test_prompt_asks_for_plain_speech_without_sound_tags(self):
        for backend in ("omnivoice",):
            with self.subTest(backend=backend), patch.dict(os.environ, {"TTS_BACKEND": backend}):
                processor.respond("Hello")
                prompt = self.api.requests[-1]["body"]["messages"][0]["content"]
                self.assertEqual(prompt, processor.SYSTEM_PROMPT)
                self.assertNotRegex(prompt, r"\[[A-Za-z-]+\]")  # no sound tags offered
                for legacy in ("<sound>", "(((", "# Voice", "# Sounds"):
                    self.assertNotIn(legacy, prompt)

    def test_empty_input_and_missing_configuration_do_not_call_api(self):
        with self.assertRaises(ValueError):
            processor.respond("  ")
        for name in ("LLM_BASE_URL", "LLM_MODEL"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                self.assertEqual(processor.respond("Hello"),
                                 "Не получилось сгенерировать ответ. Диктую ошибку: "
                                 "LLM_BASE_URL and LLM_MODEL must be configured")
        self.assertEqual(self.api.requests, [])

    def test_invalid_base_url(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "file:///tmp/llm"}):
            self.assertIn("Диктую ошибку: LLM_BASE_URL must be an HTTP(S) API base URL",
                          processor.respond("Hello"))

    def test_api_failure_does_not_expose_provider_body(self):
        self.api.status = 401
        self.api.body = b"private provider error"
        self.assertEqual(processor.respond("Hello"),
                         "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned HTTP 401")

    def test_invalid_and_empty_responses_become_spoken_errors(self):
        for body in (b"not json", b"{}", b"null", b'{"choices": []}',
                     b'{"choices": [{"message": {"content": null}}]}',
                     b'{"choices": [{"message": {"content": "  "}}]}'):
            with self.subTest(body=body):
                self.api.body = body
                self.assertTrue(processor.respond("Hello").startswith(
                    "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned"))

    def test_timeout_becomes_a_spoken_error(self):
        with patch.object(processor, "urlopen", side_effect=TimeoutError):
            self.assertEqual(processor.respond("Hello"),
                             "Не получилось сгенерировать ответ. Диктую ошибку: "
                             "LLM API request failed or timed out")

    def test_cancellation_is_not_spoken(self):
        with patch.object(processor, "urlopen", side_effect=TaskCancelled("task interrupted")):
            with self.assertRaises(TaskCancelled):
                processor.respond("Hello")

    def test_api_reply_reaches_tts_and_playback(self):
        self.assert_reply_reaches_playback(self.reply)

    def test_api_failure_reaches_tts_and_playback(self):
        self.api.status = 503
        self.api.body = b"private provider error"
        reply = "Не получилось сгенерировать ответ. Диктую ошибку: LLM API returned HTTP 503"
        for path in ("/answer", "/turn"):
            with self.subTest(path=path):
                self.assert_reply_reaches_playback(reply, path)

    def assert_reply_reaches_playback(self, reply, path="/answer"):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"DATA_DIR": temp}):
                from app import server

            class QuietHandler(server.Handler):
                def log_message(self, *args):
                    pass

            audio = Path(temp) / "reply.wav"
            audio.write_bytes(b"RIFF")
            player = Mock()
            player.play.return_value = {"played": True}
            store = TaskStore()
            with patch.object(server, "TASKS", store), \
                 patch.object(server, "DATA_DIR", Path(temp)), \
                 patch.object(server, "OUTPUT_MODE", "cast"), \
                 patch.object(server, "transcribe_audio", return_value="Привет"), \
                 patch.object(server, "synthesize", return_value=audio) as synthesize, \
                 patch.object(server, "PLAYER", player):
                http = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                try:
                    req = Request(f"http://127.0.0.1:{http.server_port}{path}?play=1",
                                  data=(b"recorded audio" if path == "/turn" else
                                        json.dumps({"text": "Привет", "play": True}).encode()),
                                  headers={"Content-Type": "application/json"})
                    with urlopen(req, timeout=3) as response:
                        result = json.load(response)
                finally:
                    http.shutdown()
                    http.server_close()
                    thread.join()
            self.assertEqual(result["response"], reply)
            synthesize.assert_called_once_with(reply, "auto", result["task_id"])
            player.play.assert_called_once_with(result["audio_url"], result["task_id"])
            self.assertEqual(store.get(result["task_id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
