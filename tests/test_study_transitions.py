"""Native-shaped transition requests against in-memory source doubles only."""
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from contextbase import study_transitions
from contextbase.rystic import PINS
from contextbase.study_binding import _hash
from contextbase.study_phases import prepare_phases
from contextbase.study_transitions import apply_transition, TransitionError, ISSUE_UPDATE
from test_study_binding import binding_fixture, source_rows


class Source:
    def __init__(self, pair, name, port):
        self.pair, self.name = pair, name
        self.url = "http://127.0.0.1:" + str(port)

    def request(self, method, path, *, query=None, form=None):
        pair = self.pair
        if method == "GET":
            pair.reads.append(path)
            if pair.fail_readback and pair.writes:
                raise ValueError("Synthetic secret-like failure details must stay omitted")
            assert query == {"limit": 100}
            return {"data": deepcopy(pair.rows["stripe." + path.rsplit("/", 1)[-1]]), "has_more": False}
        assert method == "POST" and query is None
        identity = path.rsplit("/", 1)[-1]
        customer = next(row for row in pair.rows["stripe.customers"] if row["id"] == identity)

        def effect():
            if set(form) == {"metadata[account_id]"}:
                customer["metadata"]["account_id"] = form["metadata[account_id]"]
            elif form == {"metadata[is_internal]": ""}:
                customer["metadata"].pop("is_internal", None)
            else:
                raise AssertionError("Unsupported fixture write")

        pair.mutate({"source": "stripe", "method": method, "path": path, "form": deepcopy(form)}, effect)
        return deepcopy(customer)

    def graphql(self, query, variables):
        pair = self.pair
        if "issueUpdate(" not in query:
            pair.reads.append("linear.issues")
            assert variables == {"after": None}
            return {"issues": {"nodes": deepcopy(pair.rows["linear.issues"]),
                               "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        assert query == ISSUE_UPDATE
        assert set(variables) == {"id", "input"} and set(variables["input"]) == {"stateId"}
        issue = next(row for row in pair.rows["linear.issues"] if row["id"] == variables["id"])

        def effect():
            issue["state"] = deepcopy(pair.states[variables["input"]["stateId"]])

        pair.mutate({"source": "linear", "query": query, "variables": deepcopy(variables)}, effect)
        return {"issueUpdate": {"success": True, "issue": {"id": issue["id"]}}}


class MutationPair:
    def __init__(self, plan, phase="baseline"):
        self.rows = source_rows(plan, phase)
        self.states = {issue["state_id"]: {"id": issue["state_id"], "name": issue["state_name"], "type": issue["state_type"]}
                       for phase in plan["phases"] for account in phase["intent_manifest"]["accounts"]
                       for issue in account["issues"]}
        self.stripe, self.linear = Source(self, "stripe", 18981), Source(self, "linear", 18982)
        self.identities = {name: {"product": product, "version": version} for name, (product, version) in PINS.items()}
        self.verifications, self.reads, self.writes = 0, [], []
        self.fail_at = None
        self.fail_after_effect = self.fail_readback = self.ignore_writes = False

    def verify(self):
        self.verifications += 1
        return deepcopy(self.identities)

    def mutate(self, request, effect):
        self.writes.append(request)
        failing = len(self.writes) == self.fail_at
        if not self.ignore_writes and (not failing or self.fail_after_effect):
            effect()
        if failing:
            raise TimeoutError("Synthetic secret-like failure details must stay omitted")


class StudyTransitionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan, _, _ = binding_fixture(self.root, "baseline")
        self.pair = MutationPair(self.plan)

    def execute(self, index, *, pair=None, plan=None, output=None, **overrides):
        plan = self.plan if plan is None else plan
        transition = plan["transitions"][index]
        arguments = {"approve_plan": plan["plan_sha256"], "approve_action": transition["action_sha256"], **overrides}
        return apply_transition(self.pair if pair is None else pair, plan, transition["from_phase"], transition["to_phase"],
                                self.root / ("transition-" + str(index) + ".json") if output is None else output, **arguments)

    def test_each_declared_transition_uses_only_narrow_requests_and_verified_captures(self):
        before = deepcopy(self.plan)
        original_mapping = {issue["id"]: issue["description"] for issue in self.pair.rows["linear.issues"]}
        for index, expected_status in enumerate(("valid", "blocked", "blocked")):
            receipt = self.execute(index)
            path = self.root / ("transition-" + str(index) + ".json")
            self.assertEqual(json.loads(path.read_text()), receipt)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(receipt["receipt_sha256"], _hash({k: v for k, v in receipt.items() if k != "receipt_sha256"}))
            self.assertEqual(receipt["outcome"], "applied")
            self.assertTrue(receipt["before_state_matches"])
            self.assertTrue(receipt["after_state_matches"])
            self.assertEqual(receipt["after_capture"]["receipt"]["validation_status"], expected_status)
            self.assertEqual(receipt["write_attempts"], len(self.plan["transitions"][index]["changes"]))
            self.assertEqual(receipt["write_responses_confirmed"], receipt["write_attempts"])
            self.assertFalse(receipt["workspace_imported"])
            self.assertFalse(receipt["claim_eligible"])
            self.assertEqual(receipt["model_calls"], 0)
            if expected_status == "blocked":
                self.assertIsNone(receipt["after_capture"]["tables"])
        self.assertEqual(self.plan, before)
        self.assertEqual({issue["id"]: issue["description"] for issue in self.pair.rows["linear.issues"]}, original_mapping)
        self.assertEqual(self.pair.writes[-1]["form"], {"metadata[is_internal]": ""})
        self.assertEqual(self.pair.writes[-2]["form"], {"metadata[account_id]":
            self.plan["transitions"][2]["changes"][0]["after"]["account_id"]})
        self.assertGreaterEqual(self.pair.reads.count("linear.issues"), 6)

    def test_bad_approvals_or_nonlocal_or_existing_output_make_no_source_calls(self):
        for overrides in ({"approve_plan": "0" * 64}, {"approve_action": "0" * 64}):
            with self.assertRaises(ValueError):
                self.execute(0, **overrides)
        self.pair.stripe.url = "https://example.invalid"
        with self.assertRaises(ValueError):
            self.execute(0)
        self.pair.stripe.url = self.pair.linear.url
        with self.assertRaises(ValueError):
            self.execute(0)
        self.pair.stripe.url = "http://127.0.0.1:18981"
        target = self.root / "existing.json"
        target.write_bytes(b"Preserve existing receipt")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.execute(0, output=target)
        self.assertEqual(target.read_bytes(), b"Preserve existing receipt")
        self.assertEqual(self.pair.verifications, 0)
        self.assertEqual(self.pair.reads, [])
        self.assertEqual(self.pair.writes, [])

    def test_skipping_repetition_and_wrong_before_state_cannot_write(self):
        with self.assertRaisesRegex(ValueError, "adjacent"):
            apply_transition(self.pair, self.plan, "baseline", "guard_a", self.root / "skip.json",
                             approve_plan=self.plan["plan_sha256"], approve_action=self.plan["transitions"][1]["action_sha256"])
        with self.assertRaises(TransitionError) as error:
            self.execute(1)
        self.assertEqual(error.exception.receipt["outcome"], "before_state_unverified")
        self.assertEqual(self.pair.writes, [])
        self.execute(0)
        calls = len(self.pair.writes)
        with self.assertRaises(TransitionError):
            self.execute(0, output=self.root / "repeat.json")
        self.assertEqual(len(self.pair.writes), calls)

    def test_unsafe_declared_route_identity_refuses_before_source_access(self):
        manifest = deepcopy(self.plan["phases"][0]["intent_manifest"])
        manifest["accounts"][0]["customer_id"] = "unsafe/id?query=1"
        plan = prepare_phases(manifest, self.root / "unsafe-plan.json")
        with self.assertRaisesRegex(ValueError, "physical record ID"):
            self.execute(2, plan=plan)
        self.assertEqual(self.pair.verifications, 0)
        self.assertEqual(self.pair.writes, [])

    def test_wrong_pins_fail_before_reading_records(self):
        self.pair.identities["stripe"]["version"] = "unsupported"
        with self.assertRaises(TransitionError) as error:
            self.execute(0)
        self.assertEqual(error.exception.receipt["outcome"], "before_state_unverified")
        self.assertEqual(self.pair.reads, [])
        self.assertEqual(self.pair.writes, [])

    def test_start_checkpoint_is_persisted_before_each_source_write(self):
        apply = study_transitions._apply
        observed = []
        target = self.root / "transition-0.json"

        def checked(pair, operation):
            saved = json.loads(target.read_text())
            index = len(pair.writes)
            self.assertEqual(saved["operations"][index]["status"], "in_progress")
            self.assertTrue(saved["before_state_matches"])
            self.assertIsNone(saved["after_capture"])
            observed.append(index)
            return apply(pair, operation)

        with patch("contextbase.study_transitions._apply", side_effect=checked):
            receipt = self.execute(0)
        self.assertEqual(observed, list(range(receipt["write_attempts"])))

    def test_failed_start_checkpoint_does_not_claim_a_source_write_attempt(self):
        checkpoint = study_transitions._checkpoint
        failed = []

        def save(path, receipt):
            if receipt["operations"][0]["status"] == "in_progress" and not failed:
                failed.append(True)
                raise OSError("Authored checkpoint failure")
            return checkpoint(path, receipt)

        with patch("contextbase.study_transitions._checkpoint", side_effect=save), self.assertRaises(TransitionError) as error:
            self.execute(0)
        receipt = error.exception.receipt
        self.assertEqual(receipt["write_attempts"], 0)
        self.assertEqual(receipt["operations"][0]["status"], "not_sent_checkpoint_failed")
        self.assertEqual(self.pair.writes, [])
        self.assertIsNotNone(receipt["after_capture"])

    def test_partial_failure_retains_readback_and_never_retries_remaining_writes(self):
        self.pair = MutationPair(self.plan, "guard_a")
        self.pair.fail_at = 1
        self.pair.fail_after_effect = True
        with self.assertRaises(TransitionError) as error:
            self.execute(2)
        receipt = error.exception.receipt
        self.assertEqual(receipt["outcome"], "write_uncertain")
        self.assertEqual(len(self.pair.writes), 1)
        self.assertEqual([op["status"] for op in receipt["operations"]], ["effect_uncertain", "not_attempted"])
        self.assertTrue(receipt["after_capture"]["receipt"]["complete"])
        self.assertEqual(receipt["after_capture"]["receipt"]["validation_status"], "valid")
        self.assertFalse(receipt["after_state_matches"])
        self.assertNotIn("secret-like", json.dumps(receipt))
        self.assertEqual(json.loads((self.root / "transition-2.json").read_text()), receipt)
        with self.assertRaises(TransitionError):
            self.execute(2, output=self.root / "partial-repeat.json")
        self.assertEqual(len(self.pair.writes), 1)

    def test_last_write_timeout_is_uncertain_even_when_readback_matches(self):
        self.pair = MutationPair(self.plan, "guard_a")
        self.pair.fail_at, self.pair.fail_after_effect = 2, True
        with self.assertRaises(TransitionError) as error:
            self.execute(2)
        receipt = error.exception.receipt
        self.assertEqual(receipt["outcome"], "write_uncertain")
        self.assertTrue(receipt["after_state_matches"])
        self.assertEqual(receipt["after_capture"]["receipt"]["validation_status"], "blocked")
        self.assertEqual(receipt["write_attempts"], 2)
        self.assertEqual(receipt["write_responses_confirmed"], 1)

    def test_acknowledgement_without_matching_readback_is_not_success(self):
        self.pair.ignore_writes = True
        with self.assertRaises(TransitionError) as error:
            self.execute(0)
        self.assertEqual(error.exception.receipt["outcome"], "after_state_unverified")
        self.assertFalse(error.exception.receipt["after_state_matches"])

    def test_incomplete_after_capture_cannot_certify_success(self):
        self.pair.fail_readback = True
        with self.assertRaises(TransitionError) as error:
            self.execute(0)
        receipt = error.exception.receipt
        self.assertEqual(receipt["outcome"], "after_state_unverified")
        self.assertFalse(receipt["after_capture"]["receipt"]["complete"])
        self.assertEqual(receipt["after_capture"]["receipt"]["validation_status"], "unverified")
        self.assertNotIn("secret-like", json.dumps(receipt))

    def test_checkpoint_failure_stops_writes_but_attempts_readback(self):
        self.pair = MutationPair(self.plan, "guard_a")
        checkpoint = study_transitions._checkpoint
        failed = []

        def save(path, receipt):
            if receipt["write_responses_confirmed"] == 1 and not failed:
                failed.append(True)
                raise OSError("Synthetic write failure")
            checkpoint(path, receipt)

        with patch("contextbase.study_transitions._checkpoint", side_effect=save), self.assertRaises(TransitionError) as error:
            self.execute(2)
        receipt = error.exception.receipt
        self.assertEqual(receipt["outcome"], "receipt_storage_failed")
        self.assertEqual(len(self.pair.writes), 1)
        self.assertIsNotNone(receipt["after_capture"])
        self.assertEqual(receipt["operations"][1]["status"], "not_attempted")
        self.assertEqual(list(self.root.glob(".phase-transition-*.tmp")), [])
