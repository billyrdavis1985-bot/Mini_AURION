import unittest

from mini_aurion.policy import decide


class PolicyTests(unittest.TestCase):
    def test_read_is_allowed_at_every_stage(self):
        for stage in ("experimental", "gated", "trusted"):
            verdict, _ = decide(stage, "READ")
            self.assertEqual(verdict, "allow")

    def test_experimental_denies_write_and_external(self):
        self.assertEqual(decide("experimental", "WRITE"),
                         ("deny", "STAGE_FORBIDS"))
        self.assertEqual(decide("experimental", "EXTERNAL"),
                         ("deny", "STAGE_FORBIDS"))

    def test_gated_needs_approval_for_write_and_external(self):
        self.assertEqual(decide("gated", "WRITE"),
                         ("approve", "NEEDS_APPROVAL"))
        self.assertEqual(decide("gated", "EXTERNAL"),
                         ("approve", "NEEDS_APPROVAL"))

    def test_human_approval_allows_in_gated(self):
        self.assertEqual(decide("gated", "WRITE", approved=True),
                         ("allow", "HUMAN_APPROVED"))
        self.assertEqual(decide("gated", "EXTERNAL", approved=True),
                         ("allow", "HUMAN_APPROVED"))

    def test_approval_does_not_unlock_experimental(self):
        self.assertEqual(decide("experimental", "WRITE", approved=True)[0],
                         "deny")
        self.assertEqual(decide("experimental", "EXTERNAL", approved=True)[0],
                         "deny")

    def test_trusted_allows_write_freely(self):
        verdict, _ = decide("trusted", "WRITE")
        self.assertEqual(verdict, "allow")

    def test_trusted_external_needs_allowlist(self):
        self.assertEqual(decide("trusted", "EXTERNAL"),
                         ("approve", "NEEDS_APPROVAL"))
        self.assertEqual(decide("trusted", "EXTERNAL", allowlisted=True),
                         ("allow", "TRUSTED_ALLOWLISTED"))

    def test_allowlist_does_not_unlock_lower_stages(self):
        self.assertEqual(decide("gated", "EXTERNAL", allowlisted=True)[0],
                         "approve")
        self.assertEqual(decide("experimental", "EXTERNAL",
                                allowlisted=True)[0], "deny")

    def test_unknown_stage_and_class_are_denied(self):
        self.assertEqual(decide("nope", "READ"), ("deny", "UNKNOWN_STAGE"))
        self.assertEqual(decide("gated", "DELETE"), ("deny", "UNKNOWN_CLASS"))

    def test_decide_is_deterministic(self):
        first = decide("gated", "EXTERNAL")
        for _ in range(100):
            self.assertEqual(decide("gated", "EXTERNAL"), first)


if __name__ == "__main__":
    unittest.main()
