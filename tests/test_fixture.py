import unittest

from mini_aurion.fixture import ESCALATE_TEXT, FakeModel, FakeTools, FixtureError


class FixtureTests(unittest.TestCase):
    def test_script_replies_in_order(self):
        model = FakeModel([{"text": "first", "tool_calls": [{"tool": "t"}]},
                           {"text": "second"}])
        r1 = model.complete([], [])
        r2 = model.complete([], [])
        self.assertEqual(r1["text"], "first")
        self.assertEqual(r1["tool_calls"], [{"tool": "t"}])
        self.assertEqual(r2["text"], "second")
        self.assertEqual(r2["tool_calls"], [])
        self.assertEqual(model.calls, 2)

    def test_empty_script_escalates_instead_of_guessing(self):
        model = FakeModel()
        reply = model.complete([], [])
        self.assertEqual(reply["text"], ESCALATE_TEXT)
        self.assertEqual(reply["tool_calls"], [])

    def test_exhausted_script_escalates(self):
        model = FakeModel([{"text": "only"}])
        model.complete([], [])
        reply = model.complete([], [])
        self.assertEqual(reply["text"], ESCALATE_TEXT)

    def test_model_cost_is_always_zero(self):
        model = FakeModel([{"text": "a"}, {"text": "b"}])
        model.complete([], [])
        model.complete([], [])
        model.complete([], [])
        self.assertEqual(model.total_cost_nano(), 0)

    def test_tools_serve_canned_outputs_and_log_calls(self):
        tools = FakeTools({"read": "file-bytes", "calc": lambda a: a["x"] * 2})
        self.assertEqual(tools.call("read", {}), "file-bytes")
        self.assertEqual(tools.call("calc", {"x": 21}), 42)
        self.assertEqual(tools.invocations,
                         [{"tool": "read", "args": {}},
                          {"tool": "calc", "args": {"x": 21}}])

    def test_unknown_tool_is_refused_but_logged(self):
        tools = FakeTools()
        with self.assertRaises(FixtureError) as ctx:
            tools.call("nope", {"a": 1})
        self.assertEqual(ctx.exception.code, "UNKNOWN_TOOL")
        self.assertEqual(tools.invocations, [{"tool": "nope", "args": {"a": 1}}])

    def test_tool_cost_is_always_zero(self):
        tools = FakeTools({"t": "v"})
        tools.call("t", {})
        self.assertEqual(tools.total_cost_nano(), 0)


if __name__ == "__main__":
    unittest.main()
