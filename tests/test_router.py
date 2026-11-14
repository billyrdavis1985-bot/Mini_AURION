import json
import os
import tempfile
import unittest
from pathlib import Path

from mini_aurion.router import (Router, RoutingError,
                                load_routing_table)

LOCAL_ENV = {"MINI_AURION_LOCAL_MODEL": "m"}
META_ENV = {"MODEL_API_KEY": "fake",
            "MINI_AURION_MUSE_BASE_URL": "https://127.0.0.1/v1"}


def table(**seats):
    out = dict(seats)
    out.setdefault("same_provider_ok", False)
    return out


class RoutingTableTests(unittest.TestCase):
    def load(self, raw):
        tmp = tempfile.TemporaryDirectory(prefix="ma-route-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "routing.json")
        with open(path, "w") as fh:
            fh.write(raw if isinstance(raw, str) else json.dumps(raw))
        return load_routing_table(path)

    def test_good_table_loads(self):
        parsed = self.load(table(
            specialist={"provider": "meta", "model": ""},
            orchestrator={"provider": "local", "model": "m"}))
        self.assertEqual(parsed["specialist"]["provider"], "meta")
        self.assertFalse(parsed["same_provider_ok"])

    def test_unknown_seat_is_refused(self):
        with self.assertRaises(RoutingError) as ctx:
            self.load(table(specialist={"provider": "meta"},
                            orchestrator={"provider": "local"},
                            janitor={"provider": "meta"}))
        self.assertEqual(ctx.exception.code, "BAD_TABLE")

    def test_missing_seat_is_refused(self):
        with self.assertRaises(RoutingError) as ctx:
            self.load(table(specialist={"provider": "meta"}))
        self.assertEqual(ctx.exception.code, "BAD_TABLE")

    def test_garbage_is_refused(self):
        with self.assertRaises(RoutingError) as ctx:
            self.load("{nope")
        self.assertEqual(ctx.exception.code, "BAD_TABLE")


class RouterTests(unittest.TestCase):
    def test_local_only_table_builds_with_no_keys(self):
        router = Router(table(
            specialist={"provider": "local", "model": "m"},
            orchestrator={"provider": "local", "model": "m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        self.assertEqual(router.model_for("specialist").provider,
                         "local")
        self.assertEqual(router.model_for("orchestrator").provider,
                         "local")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(RoutingError) as ctx:
            Router(table(specialist={"provider": "acme"},
                         orchestrator={"provider": "local"}),
                   environ=dict(LOCAL_ENV))
        self.assertEqual(ctx.exception.code, "BAD_PROVIDER")

    def test_missing_key_is_refused_before_any_call(self):
        with self.assertRaises(RoutingError) as ctx:
            Router(table(specialist={"provider": "meta"},
                         orchestrator={"provider": "local"}),
                   environ=dict(LOCAL_ENV))
        self.assertEqual(ctx.exception.code, "NO_MODEL")

    def test_same_provider_judge_is_refused(self):
        env = dict(LOCAL_ENV)
        env.update(META_ENV)
        with self.assertRaises(RoutingError) as ctx:
            Router(table(specialist={"provider": "meta"},
                         orchestrator={"provider": "meta"}),
                   environ=env)
        self.assertEqual(ctx.exception.code, "SAME_PROVIDER")

    def test_same_provider_ok_allows_it(self):
        env = dict(LOCAL_ENV)
        env.update(META_ENV)
        router = Router(table(specialist={"provider": "meta"},
                              orchestrator={"provider": "meta"},
                              same_provider_ok=True), environ=env)
        self.assertEqual(router.model_for("verifier").provider, "meta")

    def test_verifier_seat_overrides_the_judge(self):
        env = dict(LOCAL_ENV)
        env.update(META_ENV)
        router = Router(table(
            specialist={"provider": "local", "model": "m"},
            orchestrator={"provider": "local", "model": "m"},
            verifier={"provider": "meta", "model": ""},
            same_provider_ok=True), environ=env)
        self.assertEqual(router.model_for("verifier").provider, "meta")
        self.assertEqual(router.model_for("specialist").provider,
                         "local")

    def test_verifier_rule_uses_the_verifier_seat(self):
        env = dict(LOCAL_ENV)
        env.update(META_ENV)
        with self.assertRaises(RoutingError) as ctx:
            Router(table(
                specialist={"provider": "meta", "model": ""},
                orchestrator={"provider": "local", "model": "m"},
                verifier={"provider": "meta", "model": ""}),
                environ=env)
        self.assertEqual(ctx.exception.code, "SAME_PROVIDER")

    def test_unknown_seat_lookup_is_refused(self):
        router = Router(table(
            specialist={"provider": "local", "model": "m"},
            orchestrator={"provider": "local", "model": "m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        with self.assertRaises(RoutingError) as ctx:
            router.model_for("janitor")
        self.assertEqual(ctx.exception.code, "BAD_SEAT")

    def test_table_hash_is_stable_and_content_addressed(self):
        left = Router(table(
            specialist={"provider": "local", "model": "m"},
            orchestrator={"provider": "local", "model": "m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        right = Router(table(
            orchestrator={"provider": "local", "model": "m"},
            specialist={"provider": "local", "model": "m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        self.assertEqual(left.table_hash(), right.table_hash())
        changed = Router(table(
            specialist={"provider": "local", "model": "other"},
            orchestrator={"provider": "local", "model": "m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        self.assertNotEqual(left.table_hash(), changed.table_hash())


    def test_resolved_names_the_models_that_will_run(self):
        router = Router(table(
            specialist={"provider": "local", "model": "spec-m"},
            orchestrator={"provider": "local", "model": "orc-m"},
            same_provider_ok=True), environ=dict(LOCAL_ENV))
        self.assertEqual(router.resolved(), {
            "specialist": {"provider": "local", "model": "spec-m"},
            "orchestrator": {"provider": "local", "model": "orc-m"}})

    def test_hash_moves_when_only_the_environment_model_changes(self):
        # Same table text, different resolved model: the transcript
        # must tell them apart, or the Monitor shows the wrong seat.
        blank = {"provider": "local", "model": ""}
        left = Router(table(
            specialist=dict(blank), orchestrator=dict(blank),
            same_provider_ok=True),
            environ={"MINI_AURION_LOCAL_MODEL": "one"})
        right = Router(table(
            specialist=dict(blank), orchestrator=dict(blank),
            same_provider_ok=True),
            environ={"MINI_AURION_LOCAL_MODEL": "two"})
        self.assertEqual(left.table, right.table)
        self.assertNotEqual(left.table_hash(), right.table_hash())

    def test_native_seat_builds_from_env(self):
        env = {"ANTHROPIC_API_KEY": "fake",
               "MINI_AURION_ANTHROPIC_MODEL": "m",
               "MINI_AURION_ANTHROPIC_PRICE_IN": "1",
               "MINI_AURION_ANTHROPIC_PRICE_OUT": "2",
               "MINI_AURION_LOCAL_MODEL": "m"}
        router = Router(table(
            specialist={"provider": "anthropic", "model": ""},
            orchestrator={"provider": "local", "model": "m"}),
            environ=env)
        model = router.model_for("specialist")
        self.assertEqual(model.provider, "anthropic")
        self.assertEqual(model.model, "m")

    def test_cross_provider_rule_covers_native_seats(self):
        env = {"ANTHROPIC_API_KEY": "fake",
               "MINI_AURION_ANTHROPIC_MODEL": "m",
               "MINI_AURION_ANTHROPIC_PRICE_IN": "1",
               "MINI_AURION_ANTHROPIC_PRICE_OUT": "2"}
        with self.assertRaises(RoutingError) as ctx:
            Router(table(
                specialist={"provider": "anthropic", "model": ""},
                orchestrator={"provider": "anthropic", "model": ""}),
                environ=env)
        self.assertEqual(ctx.exception.code, "SAME_PROVIDER")

    def test_shipped_b1_table_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        parsed = load_routing_table(root / "eval" / "routing-b1.json")
        self.assertEqual(parsed["specialist"]["provider"], "meta")
        self.assertEqual(parsed["orchestrator"]["provider"], "deepseek")
        self.assertFalse(parsed["same_provider_ok"])

    def test_native_google_seat_builds_from_env(self):
        env = {"GOOGLE_API_KEY": "fake",
               "MINI_AURION_GOOGLE_MODEL": "m",
               "MINI_AURION_GOOGLE_PRICE_IN": "1",
               "MINI_AURION_GOOGLE_PRICE_OUT": "2",
               "MINI_AURION_LOCAL_MODEL": "m"}
        router = Router(table(
            specialist={"provider": "google", "model": ""},
            orchestrator={"provider": "local", "model": "m"}),
            environ=env)
        model = router.model_for("specialist")
        self.assertEqual(model.provider, "google")
        self.assertEqual(model.model, "m")

    def test_shipped_anthropic_table_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        parsed = load_routing_table(
            root / "eval" / "routing-anthropic.json")
        self.assertEqual(parsed["specialist"]["provider"], "anthropic")
        self.assertEqual(parsed["orchestrator"]["provider"], "meta")
        self.assertFalse(parsed["same_provider_ok"])

    def test_shipped_local_table_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        parsed = load_routing_table(
            root / "eval" / "routing-local.json")
        self.assertEqual(parsed["specialist"]["provider"], "local")
        self.assertEqual(parsed["orchestrator"]["provider"], "meta")
        self.assertFalse(parsed["same_provider_ok"])

    def test_shipped_openai_table_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        parsed = load_routing_table(
            root / "eval" / "routing-openai.json")
        self.assertEqual(parsed["specialist"]["provider"], "openai")
        self.assertEqual(parsed["orchestrator"]["provider"], "meta")
        self.assertFalse(parsed["same_provider_ok"])

    def test_shipped_google_table_is_valid(self):
        root = Path(__file__).resolve().parent.parent
        parsed = load_routing_table(
            root / "eval" / "routing-google.json")
        self.assertEqual(parsed["specialist"]["provider"], "google")
        self.assertEqual(parsed["orchestrator"]["provider"], "meta")
        self.assertFalse(parsed["same_provider_ok"])


if __name__ == "__main__":
    unittest.main()
