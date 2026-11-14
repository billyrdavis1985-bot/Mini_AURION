"""Reviewer finding on F12 (an outage scored as a model result), kept as
an expected-failure test.

When the fix lands, unittest reports "unexpected success" and CI fails
until the marker is deleted; the test then stays as a regression guard,
per the review-findings contract.

No network beyond 127.0.0.1, no API key, $0.
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.anthropic import (AnthropicConfig, AnthropicError,  # noqa: E402
                                   AnthropicModel)
from mini_aurion.google import GoogleConfig, GoogleError, GoogleModel  # noqa: E402
from mini_aurion.muse import MuseConfig, MuseError, MuseModel  # noqa: E402

# A provider that is up but refusing work, answering 200 with an error
# object and no answer. Shapes vary by vendor; the common part is a
# top-level "error" and no choices / content / candidates.
OVERLOADED = {"error": {"type": "service_overloaded", "code": "overloaded",
                        "message": "the service is temporarily busy"}}


class _Overloaded(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.dumps(OVERLOADED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class OutageIsNotAModelAnswer(unittest.TestCase):

    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Overloaded)
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _models(self):
        yield "muse", MuseError, MuseModel(MuseConfig(
            api_key="k", base_url=self.base + "/v1"))
        yield "anthropic", AnthropicError, AnthropicModel(AnthropicConfig(
            api_key="k", base_url=self.base, model="m",
            input_usd_per_m="1.0", output_usd_per_m="2.0"))
        yield "google", GoogleError, GoogleModel(GoogleConfig(
            api_key="k", base_url=self.base, model="m",
            input_usd_per_m="1.0", output_usd_per_m="2.0"))

    @unittest.expectedFailure
    def test_L1_a_200_error_body_raises_not_answers(self):
        """F12 records a full eval that "scored 1/5 under a
        service_overloaded outage". Since H1, provider errors are
        invalid runs, never scores, and a 503 or 529 is handled that way
        (the adapter raises, the orchestrator marks cause=error). But a
        provider that answers HTTP 200 with an `error` object and no
        answer is parsed as an empty reply. The specialist reads empty
        text as the model declining, so the run is graded as a VALID
        model miss, and the call settles at worst-case cost. All three
        adapters do this.

        Fix (one place): in provider_http.post_json, or each adapter's
        reply parser, raise the adapter's error with code
        PROVIDER_ERROR when the decoded body has a top-level `error` and
        no answer field. Keep the message identifiers-only, like
        short_http_error."""
        for name, error, model in self._models():
            with self.subTest(adapter=name):
                with self.assertRaises(error):
                    model.complete([{"role": "user", "text": "q"}], [])


if __name__ == "__main__":
    unittest.main()
