import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_aurion.contract import (TaskContract, contract_hash,  # noqa: E402
                                 load_contract, render_for_planner,
                                 resource_in_scope, scope_denials)
from mini_aurion.receipts import ReceiptStore  # noqa: E402

import json
import os
import tempfile

CONTRACT_PATH = (Path(__file__).resolve().parent.parent / "eval"
                 / "contract-code-qa-v1.json")


def contract(**kw):
    base = {"contract_id": "t", "version": 1, "goal": "g",
            "deliverable": "d"}
    base.update(kw)
    return TaskContract(**base)


class ContractTests(unittest.TestCase):
    def test_shipped_template_loads(self):
        loaded = load_contract(CONTRACT_PATH, "goal here")
        self.assertEqual(loaded.contract_id, "code-qa.v1")
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.goal, "goal here")
        self.assertEqual(loaded.criterion_ids(), frozenset({"A1", "A2"}))
        self.assertEqual(loaded.spend_usd, "3.00")
        self.assertEqual(loaded.messages, 10)
        self.assertFalse(loaded.decompose)

    def test_decompose_defaults_off(self):
        self.assertFalse(contract().decompose)
        self.assertNotEqual(contract_hash(contract()),
                            contract_hash(contract(decompose=True)))
        with tempfile.TemporaryDirectory(prefix="ma-ct-") as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"contract_id": "t", "version": 1,
                           "decompose": True}, fh)
            self.assertTrue(load_contract(path, "g").decompose)
            with open(path, "w") as fh:
                json.dump({"contract_id": "t", "version": 1}, fh)
            self.assertFalse(load_contract(path, "g").decompose)

    def test_hash_stable_and_goal_sensitive(self):
        self.assertEqual(contract_hash(contract()),
                         contract_hash(contract()))
        self.assertNotEqual(contract_hash(contract()),
                            contract_hash(contract(goal="other")))

    def test_empty_scope_allows_tree_but_not_escapes(self):
        self.assertTrue(resource_in_scope(contract(), "a/b.txt"))
        self.assertFalse(resource_in_scope(contract(), "../x"))
        self.assertFalse(resource_in_scope(contract(), "a/../../x"))

    def test_out_of_scope_wins_over_in_scope(self):
        scoped = contract(in_scope_paths=("docs/",),
                          out_of_scope_paths=("docs/secret.md",))
        self.assertTrue(resource_in_scope(scoped, "docs/a.md"))
        self.assertFalse(resource_in_scope(scoped, "docs/secret.md"))
        self.assertFalse(resource_in_scope(scoped, "other/a.md"))

    def test_scope_matches_files_and_dirs(self):
        scoped = contract(in_scope_paths=("OPERATIONS.md", "mini_aurion/"))
        self.assertTrue(resource_in_scope(scoped, "OPERATIONS.md"))
        self.assertTrue(resource_in_scope(scoped, "mini_aurion/tools.py"))
        self.assertTrue(resource_in_scope(scoped, "./mini_aurion/tools.py"))
        self.assertTrue(resource_in_scope(scoped, "mini_aurion\\tools.py"))
        self.assertFalse(resource_in_scope(scoped, "mini_aurionX/y.py"))
        self.assertFalse(resource_in_scope(scoped, "tests/t.py"))

    def test_render_names_criteria_and_specialists(self):
        loaded = load_contract(CONTRACT_PATH, "q")
        text = render_for_planner(loaded, ["spec"])
        self.assertIn("A1", text)
        self.assertIn("A2", text)
        self.assertIn("spec", text)

    def test_scope_denials_counts_only_scope(self):
        with tempfile.TemporaryDirectory(prefix="ma-ct-") as tmp:
            receipts = ReceiptStore(os.path.join(tmp, "r.jsonl"))
            receipts.append(agent_id="s", task_id="t", action="denied",
                            detail={"reason": "SCOPE_EXCLUDED: x"},
                            outcome="denied")
            receipts.append(agent_id="s", task_id="t", action="denied",
                            detail={"reason": "POLICY_DENIED"},
                            outcome="denied")
            self.assertEqual(scope_denials(receipts), 1)


if __name__ == "__main__":
    unittest.main()
