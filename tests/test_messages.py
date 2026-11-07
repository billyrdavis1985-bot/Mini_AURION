import unittest

from mini_aurion.messages import AgentMessage, MessageError


class MessageTests(unittest.TestCase):
    def test_valid_message_roundtrips(self):
        msg = AgentMessage(sender="spec", task_id="t1", claim="X holds",
                           evidence=[{"kind": "file", "ref": "a.txt"}],
                           confidence=0.8, cost_nano=100)
        d = msg.to_dict()
        self.assertEqual(d["sender"], "spec")
        self.assertEqual(d["evidence"], [{"kind": "file", "ref": "a.txt"}])
        self.assertEqual(d["escalated"], False)

    def test_evidence_free_claim_is_refused(self):
        msg = AgentMessage(sender="spec", task_id="t1", claim="trust me")
        with self.assertRaises(MessageError) as ctx:
            msg.require_evidence()
        self.assertEqual(ctx.exception.code, "NO_EVIDENCE")

    def test_escalation_passes_without_evidence(self):
        msg = AgentMessage(sender="spec", task_id="t1",
                           claim="CANNOT_VERIFY: source missing",
                           escalated=True)
        self.assertIs(msg.require_evidence(), msg)

    def test_bad_kind_is_refused_at_construction(self):
        with self.assertRaises(MessageError) as ctx:
            AgentMessage(sender="s", task_id="t", claim="c",
                         evidence=[{"kind": "vibes", "ref": "x"}])
        self.assertEqual(ctx.exception.code, "BAD_EVIDENCE")

    def test_evidence_without_ref_is_refused(self):
        with self.assertRaises(MessageError) as ctx:
            AgentMessage(sender="s", task_id="t", claim="c",
                         evidence=[{"kind": "file"}])
        self.assertEqual(ctx.exception.code, "BAD_EVIDENCE")

    def test_bad_fields_are_refused(self):
        with self.assertRaises(MessageError):
            AgentMessage(sender="", task_id="t", claim="c")
        with self.assertRaises(MessageError):
            AgentMessage(sender="s", task_id="t", claim="")
        with self.assertRaises(MessageError):
            AgentMessage(sender="s", task_id="t", claim="c", confidence=2.0)
        with self.assertRaises(MessageError):
            AgentMessage(sender="s", task_id="t", claim="c", confidence=-0.1)
        with self.assertRaises(MessageError):
            AgentMessage(sender="s", task_id="t", claim="c", cost_nano=-1)


if __name__ == "__main__":
    unittest.main()
