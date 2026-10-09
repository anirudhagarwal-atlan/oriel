"""Interrupted budget publication and post-charge dispatch, entirely offline."""
import json
import os
from unittest.mock import patch
import unittest

from contextbase.study_budget import StudyBudget
import test_study_controller as fixtures


class StudyControllerStorageTests(unittest.TestCase):
    # Reuse fixture construction without collecting the fixture's test methods
    # a second time. The configured source/model entry points remain blocked.
    setUpClass = classmethod(fixtures.StudyControllerTests.setUpClass.__func__)
    setUp = fixtures.StudyControllerTests.setUp
    rehash = fixtures.StudyControllerTests.rehash
    pair_factory = fixtures.StudyControllerTests.pair_factory
    run_controller = fixtures.StudyControllerTests.run_controller
    collection = fixtures.StudyControllerTests.collection

    def test_paid_reservation_overshoot_cannot_dispatch_requested_source_tools(self):
        class OverchargedModel(fixtures.AuthoredGoldModel):
            def complete(self, *args, **kwargs):
                response = super().complete(*args, **kwargs)
                response["cost_usd"] = .3
                return response

        starts, calls = [], []

        class ObservedTools(fixtures.FixtureTools):
            def __enter__(self):
                starts.append("started")
                return super().__enter__()

            def call(self, tool, arguments):
                calls.append((tool, arguments))
                return super().call(tool, arguments)

        model = OverchargedModel(self.output, behavior="two_turns")
        result = self.run_controller(model, tools_factory=lambda workspace, pair, arm: ObservedTools())
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(calls, [], "A globally blocked budget must prevent the already-requested source call")
        self.assertEqual(len(starts), 1, "No later trial may start MCP after the paid overshoot")
        self.assertEqual(self.pair.writes, [])
        self.assertIn("reservation_exceeded", result["budget"]["blocked_reasons"])
        packet = self.collection()["phases"][0]
        self.assertFalse(packet["execution"]["execution_complete"])
        self.assertAlmostEqual(packet["execution"]["known_cost_usd"], .3)
        first = packet["execution"]["trials"][0]
        self.assertEqual(first["tool_invocations"], [])
        self.assertEqual(first["inferences"][0]["cost_usd"], .3)
        self.assertFalse((self.output / "workspaces" / "batch_001" / "baseline-postflight-capture.json").exists())

    def test_failed_paid_settlement_publication_preserves_terminal_partial_collection(self):
        original_link = os.link
        model_reservations, failures = set(), []

        def fail_paid_publication(source, destination, *args, **kwargs):
            # Only fail publication of the first paid model settlement. Other
            # receipt storage remains usable, as with a localized/transient
            # ledger failure. No API, model process, or native source is used.
            if str(source).startswith(".pending-") and kwargs.get("src_dir_fd") is not None:
                descriptor = os.open(source, os.O_RDONLY, dir_fd=kwargs["src_dir_fd"])
                with os.fdopen(descriptor) as handle:
                    record = json.load(handle)
                payload = record["payload"]
                if record["event"] == "reserved" and payload["kind"] == "model_calls":
                    model_reservations.add(payload["reservation_id"])
                if (record["event"] == "settled" and payload["reservation_id"] in model_reservations
                        and not failures):
                    failures.append(payload["actual_cost_usd"])
                    raise OSError("Authored ledger publication interruption")
            return original_link(source, destination, *args, **kwargs)

        model = fixtures.AuthoredGoldModel(self.output)
        with patch("contextbase.study_budget.os.link", side_effect=fail_paid_publication):
            result = self.run_controller(model)
        self.assertEqual(failures, [.001])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.pair.writes, [])
        self.assertEqual(result["outcome"], "stopped")
        self.assertFalse(result["automatic_resume_allowed"])
        self.assertFalse(result["claim_eligible"])
        self.assertIn("receipt_storage_failed", result["budget"]["blocked_reasons"])
        self.assertGreaterEqual(result["budget"]["known_comparison_cost_usd"], .001)
        state = json.loads((self.output / "controller.json").read_text())
        self.assertEqual(state["outcome"], "stopped")
        self.assertFalse(state["automatic_resume_allowed"])
        execution = self.collection()["phases"][0]["execution"]
        self.assertEqual(execution["invocations"], 1)
        self.assertEqual(execution["trials"][0]["inferences"][0]["cost_usd"], .001)
        self.assertAlmostEqual(execution["known_cost_usd"], .001)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["statistical_acceptance"], "inconclusive")
        self.assertFalse(report["claim_eligible"])
        with StudyBudget.open(self.output / "budget", self.protocol) as reopened:
            status = reopened.status()
            self.assertIn("interrupted_reservation", status["blocked_reasons"])
            self.assertIn("interrupted_checkpoint", status["blocked_reasons"])
            self.assertEqual(sum(item["kind"] == "model_calls" for item in status["pending"]), 1)


if __name__ == "__main__":
    unittest.main()
