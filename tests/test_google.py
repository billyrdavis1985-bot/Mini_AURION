import http.server
import json
import threading
import unittest

from mini_aurion.google import GoogleConfig, GoogleError, GoogleModel


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
             "key": self.headers.get("x-goog-api-key"),
             "body": json.loads(raw.decode("utf-8"))})
        status, payload = ScriptedHandler.script.pop(0)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def answer(text=None, usage=None, calls=None):
    parts = []
    if text is not None:
        parts.append({"text": text})
    for name, args in calls or []:
        parts.append({"functionCall": {"name": name, "args": args}})
    body = {"candidates": [{"content": {"parts": parts,
                                        "role": "model"}}]}
    if usage is not None:
        body["usageMetadata"] = usage
    return body


class GoogleTests(unittest.TestCase):
    def setUp(self):
        ScriptedHandler.script = []
        ScriptedHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), ScriptedHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = GoogleConfig(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}",
            model="m", input_usd_per_m="1.0", output_usd_per_m="2.0")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_wire_shape_is_generate_content(self):
        ScriptedHandler.script.append((200, answer("CANNOT_VERIFY")))
        GoogleModel(self.config).complete(
            [{"role": "user", "text": "hi"}], [],
            system_prompt="PLAN")
        seen = ScriptedHandler.requests[0]
        self.assertEqual(seen["path"], "/v1beta/models/m:generateContent")
        self.assertEqual(seen["key"], "test-key")
        body = seen["body"]
        self.assertEqual(body["system_instruction"],
                         {"parts": [{"text": "PLAN"}]})
        self.assertEqual(body["contents"],
                         [{"role": "user",
                           "parts": [{"text": "hi"}]}])
        self.assertEqual(body["generationConfig"]["temperature"], 0.0)
        self.assertIn("maxOutputTokens", body["generationConfig"])

    def test_model_id_cannot_escape_the_path(self):
        config = GoogleConfig(
            api_key="k", base_url="http://127.0.0.1:1",
            model="../x", input_usd_per_m="1", output_usd_per_m="1")
        self.assertIn("/v1beta/models/..%2Fx:generateContent",
                      GoogleModel(config).url)

    def test_tools_convert_to_function_declarations(self):
        ScriptedHandler.script.append(
            (200, answer(calls=[("read_file", {"path": "a"})],
                         usage={"promptTokenCount": 1,
                                "candidatesTokenCount": 1})))
        reply = GoogleModel(self.config).complete(
            [{"role": "user", "text": "go"}],
            [{"name": "read_file", "description": "Read.",
              "parameters": {"type": "object"}}])
        body = ScriptedHandler.requests[0]["body"]
        self.assertEqual(
            body["tools"],
            [{"functionDeclarations":
              [{"name": "read_file", "description": "Read.",
                "parameters": {"type": "object"}}]}])
        self.assertEqual(reply["tool_calls"],
                         [{"tool": "read_file", "args": {"path": "a"}}])

    def test_usage_metadata_prices_the_call(self):
        ScriptedHandler.script.append(
            (200, answer("CANNOT_VERIFY",
                         usage={"promptTokenCount": 1_000_000,
                                "candidatesTokenCount": 1_000_000})))
        model = GoogleModel(self.config)
        model.complete([{"role": "user", "text": "hi"}], [])
        self.assertEqual(model.last_cost_nano, 3_000_000_000)

    def test_missing_usage_settles_at_worst_case(self):
        ScriptedHandler.script.append((200, answer("CANNOT_VERIFY")))
        model = GoogleModel(self.config)
        model.complete([{"role": "user", "text": "hi"}], [])
        self.assertGreater(model.last_cost_nano, 0)

    def test_blocked_content_is_an_evidence_free_reply(self):
        ScriptedHandler.script.append((200, {"promptFeedback": {}}))
        reply = GoogleModel(self.config).complete(
            [{"role": "user", "text": "go"}], [])
        self.assertEqual(reply["text"], "")
        self.assertEqual(reply["evidence"], [])

    def test_claim_json_parses(self):
        ScriptedHandler.script.append(
            (200, answer('{"claim": "it", "evidence": [],'
                         ' "confidence": 0.5}')))
        reply = GoogleModel(self.config).complete(
            [{"role": "user", "text": "go"}], [])
        self.assertEqual(reply["text"], "it")
        self.assertEqual(reply["confidence"], 0.5)

    def test_http_error_keeps_the_status(self):
        ScriptedHandler.script.append(
            (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}))
        with self.assertRaises(GoogleError) as ctx:
            GoogleModel(self.config).complete(
                [{"role": "user", "text": "go"}], [])
        self.assertEqual(ctx.exception.code, "HTTP_ERROR")
        self.assertIn("INVALID_ARGUMENT", str(ctx.exception))

    def test_dead_server_raises_transport(self):
        config = GoogleConfig(
            api_key="k", base_url="http://127.0.0.1:1", model="m",
            input_usd_per_m="1", output_usd_per_m="1")
        with self.assertRaises(GoogleError) as ctx:
            GoogleModel(config).complete(
                [{"role": "user", "text": "go"}], [])
        self.assertEqual(ctx.exception.code, "TRANSPORT_FAILED")

    def test_missing_key_model_prices_refuse(self):
        with self.assertRaises(GoogleError) as ctx:
            GoogleConfig.from_env(environ={})
        self.assertEqual(ctx.exception.code, "NO_KEY")
        env = {"GOOGLE_API_KEY": "k",
               "MINI_AURION_GOOGLE_PRICE_IN": "1",
               "MINI_AURION_GOOGLE_PRICE_OUT": "1"}
        with self.assertRaises(GoogleError) as ctx:
            GoogleConfig.from_env(environ=env)
        self.assertEqual(ctx.exception.code, "NO_MODEL")
        env["MINI_AURION_GOOGLE_MODEL"] = "m"
        del env["MINI_AURION_GOOGLE_PRICE_IN"]
        with self.assertRaises(GoogleError) as ctx:
            GoogleConfig.from_env(environ=env)
        self.assertEqual(ctx.exception.code, "NO_PRICES")

    def test_model_conforms_to_the_protocol(self):
        model = GoogleModel(self.config)
        self.assertEqual(model.provider, "google")
        self.assertEqual(model.model, "m")
        self.assertEqual(model.calls, 0)
        self.assertEqual(model.last_cost_nano, 0)
        self.assertTrue(callable(model.complete))
        self.assertTrue(callable(model.worst_case_nano))


if __name__ == "__main__":
    unittest.main()
