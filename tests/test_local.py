import http.server
import json
import threading
import unittest
from unittest import mock

from mini_aurion.local import (LOCAL_BASE_URL_ENV_VAR, LOCAL_MODEL_ENV_VAR,
                               LocalConfig, LocalModel)
from mini_aurion.muse import MuseError


class LocalHandler(http.server.BaseHTTPRequestHandler):
    script = []
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        LocalHandler.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"),
             "body": json.loads(raw.decode("utf-8"))})
        status, payload = LocalHandler.script.pop(0)
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def answer(content, usage=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"choices": [{"message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body


class LocalTierTests(unittest.TestCase):
    def setUp(self):
        LocalHandler.script = []
        LocalHandler.requests = []
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), LocalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = LocalConfig(
            base_url=f"http://127.0.0.1:{port}/v1", model="test-local")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_model_name_is_required(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(MuseError) as ctx:
                LocalConfig.from_env(environ={})
        self.assertEqual(ctx.exception.code, "NO_LOCAL_MODEL")

    def test_from_env_honors_overrides(self):
        config = LocalConfig.from_env(
            environ={LOCAL_BASE_URL_ENV_VAR: "http://127.0.0.1:9999/v1",
                     LOCAL_MODEL_ENV_VAR: "env-model"})
        self.assertEqual(config.model, "env-model")
        self.assertEqual(config.base_url, "http://127.0.0.1:9999/v1")

    def test_loopback_and_private_lan_http_allowed(self):
        for url in ("http://127.0.0.1:11434/v1",
                    "http://localhost:11434/v1",
                    "http://192.168.1.50:11434/v1",
                    "http://10.0.0.5:11434/v1",
                    "https://models.example.com/v1"):
            LocalConfig(base_url=url, model="m")

    def test_public_http_refused(self):
        with self.assertRaises(MuseError):
            LocalConfig(base_url="http://8.8.8.8:11434/v1", model="m")
        with self.assertRaises(MuseError):
            LocalConfig(base_url="http://models.example.com/v1", model="m")

    def test_local_tokens_price_zero(self):
        model = LocalModel(self.config)
        self.assertEqual(model.worst_case_nano([{"role": "user"}]), 0)

    def test_chat_completions_round_trip(self):
        LocalHandler.script.append(
            (200, answer(json.dumps({"claim": "hi",
                                     "evidence": [{"kind": "file",
                                                   "ref": "a.txt:1",
                                                   "quote": "hello world!"}]}),
                         usage={"prompt_tokens": 10,
                                "completion_tokens": 5})))
        model = LocalModel(self.config)
        reply = model.complete([{"role": "user", "text": "hi"}], [])
        self.assertEqual(reply["text"], "hi")
        self.assertEqual(reply["evidence"][0]["quote"], "hello world!")
        self.assertEqual(model.last_cost_nano, 0)
        req = LocalHandler.requests[0]
        self.assertTrue(req["path"].endswith("/v1/chat/completions"))
        self.assertEqual(req["auth"], "Bearer local")

    def test_tool_calls_parse(self):
        LocalHandler.script.append((200, answer(
            "", tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": '{"path": "a"}'}}])))
        reply = LocalModel(self.config).complete(
            [{"role": "user", "text": "read"}],
            [{"name": "read_file", "description": "read",
              "parameters": {"type": "object"}}])
        self.assertEqual(reply["tool_calls"][0]["tool"], "read_file")


if __name__ == "__main__":
    unittest.main()
