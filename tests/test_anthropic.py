import http.server
import json
import threading
import unittest

from mini_aurion.anthropic import (AnthropicConfig, AnthropicError,
                                   AnthropicModel)


class ScriptedHandler(http.server.BaseHTTPRequestHandler):
    script = []
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        ScriptedHandler.requests.append(
            {"path": self.path,
             "key": self.headers.get("x-api-key"),
             "version": self.headers.get("anthropic-version"),
             "body": json.loads(raw.decode("utf-8"))})
        status, payload = ScriptedHandler.script.pop(0)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def answer(text=None, usage=None, tool_uses=None):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for name, args in tool_uses or []:
        content.append({"type": "tool_use", "id": "u1", "name": name,
                        "input": args})
    body = {"content": content,
            "stop_reason": "tool_use" if tool_uses else "end_turn"}
    if usage is not None:
        body["usage"] = usage
    return body


class AnthropicTests(unittest.TestCase):
    def setUp(self):
        ScriptedHandler.script = []
        ScriptedHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), ScriptedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = AnthropicConfig(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}",
            model="m", input_usd_per_m="1.0", output_usd_per_m="2.0")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_wire_shape_is_the_messages_api(self):
        ScriptedHandler.script.append((200, answer("CANNOT_VERIFY")))
        AnthropicModel(self.config).complete(
            [{"role": "user", "text": "hi"}], [],
            system_prompt="PLAN")
        seen = ScriptedHandler.requests[0]
        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["key"], "test-key")
        self.assertEqual(seen["version"], "2023-06-01")
        body = seen["body"]
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["system"], "PLAN")
        self.assertEqual(body["messages"],
                         [{"role": "user", "content": "hi"}])
        self.assertIn("max_tokens", body)
        # This model 400s on the field (measured live), so it stays out.
        self.assertNotIn("temperature", body)

    def test_consecutive_user_turns_merge(self):
        ScriptedHandler.script.append((200, answer("CANNOT_VERIFY")))
        AnthropicModel(self.config).complete(
            [{"role": "brief", "text": "q"},
             {"role": "tool_result", "tool": "t", "args": {},
              "text": "r"}], [])
        body = ScriptedHandler.requests[0]["body"]
        self.assertEqual(len(body["messages"]), 1)
        self.assertIn("[brief] q", body["messages"][0]["content"])
        self.assertIn("[tool_result t {}]", body["messages"][0]["content"])

    def test_tools_convert_to_input_schema(self):
        ScriptedHandler.script.append(
            (200, answer(tool_uses=[("read_file", {"path": "a"})],
                         usage={"input_tokens": 1, "output_tokens": 1})))
        reply = AnthropicModel(self.config).complete(
            [{"role": "user", "text": "go"}],
            [{"name": "read_file", "description": "Read.",
              "parameters": {"type": "object"}}])
        body = ScriptedHandler.requests[0]["body"]
        self.assertEqual(body["tools"],
                         [{"name": "read_file", "description": "Read.",
                           "input_schema": {"type": "object"}}])
        self.assertEqual(reply["tool_calls"],
                         [{"tool": "read_file", "args": {"path": "a"}}])

    def test_non_object_tool_input_skips_the_call(self):
        ScriptedHandler.script.append(
            (200, answer(tool_uses=[("read_file", [1, 2])],
                         usage={"input_tokens": 1, "output_tokens": 1})))
        reply = AnthropicModel(self.config).complete(
            [{"role": "user", "text": "go"}], [])
        self.assertEqual(reply["tool_calls"], [])

    def test_usage_prices_the_call(self):
        ScriptedHandler.script.append(
            (200, answer("CANNOT_VERIFY",
                         usage={"input_tokens": 1_000_000,
                                "output_tokens": 1_000_000})))
        model = AnthropicModel(self.config)
        model.complete([{"role": "user", "text": "hi"}], [])
        self.assertEqual(model.last_cost_nano, 3_000_000_000)

    def test_missing_usage_settles_at_worst_case(self):
        ScriptedHandler.script.append((200, answer("CANNOT_VERIFY")))
        model = AnthropicModel(self.config)
        model.complete([{"role": "user", "text": "hi"}], [])
        self.assertGreater(model.last_cost_nano, 0)

    def test_claim_json_parses(self):
        ScriptedHandler.script.append(
            (200, answer('{"claim": "it", "evidence": [],'
                         ' "confidence": 0.5}')))
        reply = AnthropicModel(self.config).complete(
            [{"role": "user", "text": "go"}], [])
        self.assertEqual(reply["text"], "it")
        self.assertEqual(reply["confidence"], 0.5)

    def test_garbage_content_is_an_evidence_free_reply(self):
        ScriptedHandler.script.append((200, answer("just words")))
        reply = AnthropicModel(self.config).complete(
            [{"role": "user", "text": "go"}], [])
        self.assertEqual(reply["text"], "just words")
        self.assertEqual(reply["evidence"], [])

    def test_http_error_is_short_and_scrubbed(self):
        ScriptedHandler.script.append(
            (400, {"error": {"type": "invalid_request_error"}}))
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicModel(self.config).complete(
                [{"role": "user", "text": "go"}], [])
        self.assertEqual(ctx.exception.code, "HTTP_ERROR")
        self.assertIn("HTTP 400", str(ctx.exception))


    def test_dead_server_raises_transport(self):
        config = AnthropicConfig(
            api_key="k", base_url="http://127.0.0.1:1", model="m",
            input_usd_per_m="1", output_usd_per_m="1")
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicModel(config).complete(
                [{"role": "user", "text": "go"}], [])
        self.assertEqual(ctx.exception.code, "TRANSPORT_FAILED")

    def test_missing_key_model_prices_refuse(self):
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicConfig.from_env(environ={})
        self.assertEqual(ctx.exception.code, "NO_KEY")
        env = {"ANTHROPIC_API_KEY": "k",
               "MINI_AURION_ANTHROPIC_PRICE_IN": "1",
               "MINI_AURION_ANTHROPIC_PRICE_OUT": "1"}
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicConfig.from_env(environ=env)
        self.assertEqual(ctx.exception.code, "NO_MODEL")
        env["MINI_AURION_ANTHROPIC_MODEL"] = "m"
        del env["MINI_AURION_ANTHROPIC_PRICE_IN"]
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicConfig.from_env(environ=env)
        self.assertEqual(ctx.exception.code, "NO_PRICES")

    def test_public_http_is_refused(self):
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicConfig(api_key="k", base_url="http://8.8.8.8/v1",
                            model="m", input_usd_per_m="1",
                            output_usd_per_m="1")
        self.assertEqual(ctx.exception.code, "BAD_URL")

    def test_garbage_prices_refuse_at_config_time(self):
        env = {"ANTHROPIC_API_KEY": "k",
               "MINI_AURION_ANTHROPIC_MODEL": "m",
               "MINI_AURION_ANTHROPIC_PRICE_IN": "<billing>",
               "MINI_AURION_ANTHROPIC_PRICE_OUT": "1"}
        with self.assertRaises(AnthropicError) as ctx:
            AnthropicConfig.from_env(environ=env)
        self.assertEqual(ctx.exception.code, "BAD_PRICES")

    def test_model_conforms_to_the_protocol(self):
        model = AnthropicModel(self.config)
        self.assertEqual(model.provider, "anthropic")
        self.assertEqual(model.model, "m")
        self.assertEqual(model.calls, 0)
        self.assertEqual(model.last_cost_nano, 0)
        self.assertTrue(callable(model.complete))
        self.assertTrue(callable(model.worst_case_nano))


if __name__ == "__main__":
    unittest.main()
