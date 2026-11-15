"""Typed findings and the forecast register (D2, D4)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.messages import AgentMessage, MessageError  # noqa: E402
from mini_aurion.provider_http import parse_text_reply  # noqa: E402
from mini_aurion.receipts import ReceiptStore  # noqa: E402
from mini_aurion.research import (RESEARCH_INVESTIGATOR_PROMPT,  # noqa: E402
                                  RESEARCH_INVESTIGATOR_VERSION,
                                  register_forecasts, validate_finding)


def fact(**over):
    cite = {"type": "fact", "claim": "Rates rose.",
            "evidence": [{"kind": "url", "ref": "https://e.com/",
                          "snapshot": "s", "quote": "rates rose here"}]}
    cite.update(over)
    return cite


def forecast(**over):
    cite = {"type": "forecast", "claim": "Rates will rise.",
            "probability": 0.7, "resolves_at": "2027-06-01",
            "criterion": "index above 100",
            "evidence": [{"kind": "url", "ref": "https://e.com/",
                          "snapshot": "s", "quote": "rates rose here"}]}
    cite.update(over)
    return cite


class ValidateTests(unittest.TestCase):
    def test_sound_findings_have_no_reasons(self):
        self.assertEqual(validate_finding(fact()), [])
        self.assertEqual(validate_finding(forecast()), [])
        self.assertEqual(validate_finding(
            {"type": "assessment", "claim": "Bad trend.",
             "evidence": fact()["evidence"]}), [])

    def test_untyped_and_empty_claims_refused(self):
        self.assertIn("no known type",
                      " ".join(validate_finding({"claim": "x",
                                                "evidence": [{}]})))
        self.assertIn("no known type",
                      " ".join(validate_finding(
                          dict(fact(), type="hunch"))))
        self.assertIn("no claim",
                      " ".join(validate_finding(dict(fact(), claim=" "))))
        self.assertIn("not an object",
                      " ".join(validate_finding(["nope"])))

    def test_uncited_findings_refused(self):
        for bad in (dict(fact(), evidence=[]),
                    {"type": "assessment", "claim": "x"},
                    dict(forecast(), evidence="not-a-list")):
            self.assertIn("without citation",
                          " ".join(validate_finding(bad)))
        self.assertIn("must be objects", " ".join(
            validate_finding(dict(fact(), evidence=["x"]))))

    def test_forecast_fields_are_bounded(self):
        for prob in (None, "high", True, -0.1, 1.5):
            self.assertIn("probability", " ".join(validate_finding(
                dict(forecast(), probability=prob))), repr(prob))
        for ok in (0, 1, 0.0, 1.0, 0.5):
            self.assertEqual(validate_finding(
                dict(forecast(), probability=ok)), [], repr(ok))
        self.assertIn("resolution date", " ".join(
            validate_finding(dict(forecast(), resolves_at=" "))))
        self.assertIn("120 chars", " ".join(
            validate_finding(dict(forecast(), resolves_at="x" * 121))))
        self.assertIn("resolution criterion", " ".join(
            validate_finding(dict(forecast(), criterion=""))))
        self.assertIn("2000 chars", " ".join(
            validate_finding(dict(forecast(), criterion="x" * 2001))))


class RegisterTests(unittest.TestCase):
    def test_forecasts_append_with_receipts(self):
        tmp = tempfile.TemporaryDirectory(prefix="ma-pred-")
        self.addCleanup(tmp.cleanup)
        receipts = ReceiptStore(os.path.join(tmp.name, "r.jsonl"))
        path = os.path.join(tmp.name, "predictions.jsonl")
        out = register_forecasts(
            "t1", [forecast()], predictions_path=path,
            receipts=receipts, models=["meta/m1"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "forecast_registered")
        rows = Path(path).read_text().strip().split("\n")
        self.assertEqual(len(rows), 1)
        row = json.loads(rows[0])
        self.assertEqual(row["claim"], "Rates will rise.")
        self.assertEqual(row["probability"], 0.7)
        self.assertEqual(row["resolves_at"], "2027-06-01")
        self.assertEqual(row["criterion"], "index above 100")
        self.assertEqual(row["task_id"], "t1")
        self.assertEqual(row["models"], ["meta/m1"])
        self.assertEqual(row["snapshots"], ["s"])
        # Append-only: a second registration keeps the first.
        register_forecasts("t2", [forecast()], predictions_path=path,
                           receipts=receipts, models=["meta/m1"])
        rows = Path(path).read_text().strip().split("\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0])["task_id"], "t1")


class PromptTests(unittest.TestCase):
    def test_prompt_version_is_pinned(self):
        self.assertEqual(RESEARCH_INVESTIGATOR_VERSION,
                         "m0.mini-aurion-researcher.v1")

    def test_prompt_asks_for_typed_findings(self):
        for token in ("findings", '"type": "fact"', '"type": "assessment"',
                      '"type": "forecast"', "probability", "snapshot",
                      "CANNOT_VERIFY"):
            self.assertIn(token, RESEARCH_INVESTIGATOR_PROMPT)


class ProtocolTests(unittest.TestCase):
    def test_reply_carries_findings_through(self):
        reply = parse_text_reply(json.dumps(
            {"claim": "done", "evidence": [], "confidence": 0.9,
             "findings": [fact()]}))
        self.assertEqual(reply["findings"], [fact()])
        bare = parse_text_reply('{"claim": "x"}')
        self.assertEqual(bare["findings"], [])
        self.assertEqual(
            parse_text_reply("CANNOT_VERIFY")["findings"], [])

    def test_message_holds_findings(self):
        msg = AgentMessage(sender="s", task_id="t", claim="c",
                           evidence=[], findings=[fact()])
        self.assertEqual(msg.findings, [fact()])
        self.assertEqual(msg.to_dict()["findings"], [fact()])
        with self.assertRaises(MessageError):
            AgentMessage(sender="s", task_id="t", claim="c",
                         findings=["nope"])


if __name__ == "__main__":
    unittest.main()
