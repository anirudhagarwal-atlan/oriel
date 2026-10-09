"""Fabricated unit evidence tests consistency checks; these are not study results."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from contextbase.rystic import PINS
from contextbase.rystic_eval import score_answer, _guarded_state
from contextbase.rystic_validation import capture_validation
from contextbase.study_binding import bind_phase, _hash
from contextbase.study_phases import prepare_phases, PHASE_ORDER
from contextbase.study_report import ARTIFACT_FIELDS
from test_rystic_eval import fixture


def source_rows(plan, phase_id):
    """Authored synthetic raw records for fake pair tests, never native evidence."""
    manifest = next(phase["intent_manifest"] for phase in plan["phases"] if phase["id"] == phase_id)
    namespace = manifest["namespace"]
    rows = {"stripe.customers": [], "stripe.charges": [], "linear.issues": []}
    for account in manifest["accounts"]:
        metadata = {"contextbase_fixture": namespace, "account_id": account["account_id"]}
        if account["is_internal"] is not None:
            metadata["is_internal"] = "true" if account["is_internal"] else "false"
        rows["stripe.customers"].append({"id": account["customer_id"], "name": account["name"], "metadata": metadata})
        for charge in account["charges"]:
            rows["stripe.charges"].append({"id": charge["charge_id"], "customer": account["customer_id"],
                "amount": charge["amount_cents"], "amount_refunded": charge["refund_cents"], "currency": charge["currency"],
                "paid": charge["paid"], "captured": charge["captured"], "status": charge["status"],
                "disputed": charge["disputed"], "created": 1735689600})
        for issue in account["issues"]:
            account_id = plan["source_issue_account_ids"][issue["issue_id"]]
            rows["linear.issues"].append({"id": issue["issue_id"], "identifier": issue["identifier"],
                "title": issue["title"], "priority": issue["priority"],
                "description": f"contextbase_fixture={namespace}\ncontextbase_account_id={account_id}",
                "state": {"id": issue["state_id"], "type": issue["state_type"], "name": issue["state_name"]},
                "assignee": {"id": issue["owner_id"], "name": issue["owner_name"]} if issue["owner_id"] else None,
                "updatedAt": "2026-01-01T00:00:00Z"})
    return rows


class FakePair:
    """Only in-memory source pages; no network/runtime/client construction."""
    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.stripe = self
        self.linear = self

    def verify(self):
        return {source: {"product": product, "version": version} for source, (product, version) in PINS.items()}

    def request(self, method, path, *, query):
        assert method == "GET" and "starting_after" not in query
        return {"data": deepcopy(self.rows["stripe." + path.rsplit("/", 1)[-1]]), "has_more": False}

    def graphql(self, query, variables):
        assert variables == {"after": None}
        return {"issues": {"nodes": deepcopy(self.rows["linear.issues"]), "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def fake_snapshot(capture):
    """A fabricated receipt for consistency tests; no database was imported."""
    receipt = capture["receipt"]
    if receipt["validation_status"] != "valid":
        return None
    return {key: deepcopy(receipt[key]) for key in (
        "namespace", "simulators", "synthetic", "data_sha256", "snapshot_id", "observed_at", "source_read_seconds")} | {
        "refresh_mode": "full_cohort_replace", "source_validation_sha256": _hash(receipt),
        "rows": deepcopy(receipt["source_rows"])}


def binding_fixture(directory, phase_id):
    """Return plan, fake capture, fake snapshot for offline binding/CLI tests."""
    manifest = fixture()
    for account in manifest["accounts"]:
        for issue in account["issues"]:
            issue.update(title="Synthetic issue " + issue["issue_id"],
                         state_id="state_" + issue["state_type"], state_name=issue["state_type"].title())
    plan = prepare_phases(manifest, Path(directory) / ("prepared-" + phase_id + ".json"))
    capture = capture_validation(FakePair(source_rows(plan, phase_id)), manifest["namespace"])
    return plan, capture, fake_snapshot(capture)


def rehash_plan(plan):
    plan["plan_sha256"] = _hash({key: value for key, value in plan.items() if key != "plan_sha256"})


class StudyBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan, self.capture, self.snapshot = binding_fixture(self.root, "baseline")
        self.output = self.root / "bound.json"

    def bind(self, *, plan=None, capture=None, snapshot=..., phase="baseline", origin="synthetic_test"):
        return bind_phase(self.plan if plan is None else plan, phase,
                          self.capture if capture is None else capture, self.output,
                          snapshot=self.snapshot if snapshot is ... else snapshot, evidence_origin=origin)

    def rejected(self, **kwargs):
        with self.assertRaises(ValueError):
            self.bind(**kwargs)
        self.assertFalse(self.output.exists())

    def test_all_four_phases_bind_with_independent_gold_and_private_ids(self):
        expected_counts = (5, 5, 1, 1)
        for phase, count in zip(PHASE_ORDER, expected_counts):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                plan, capture, snapshot = binding_fixture(directory, phase)
                output = Path(directory) / "bound.json"
                originals = deepcopy((plan, capture, snapshot))
                packet = bind_phase(plan, phase, capture, output, snapshot=snapshot)
                self.assertEqual((plan, capture, snapshot), originals)
                self.assertEqual(json.loads(output.read_text()), packet)
                self.assertEqual(packet["spec"], "contextbase.phase-binding/v1")
                self.assertEqual(len(packet["public_tasks"]), count)
                self.assertEqual(len(packet["evaluator_tasks"]), count)
                self.assertEqual(packet["binding_sha256"], _hash({key: item for key, item in packet.items() if key != "binding_sha256"}))
                for public, private in zip(packet["public_tasks"], packet["evaluator_tasks"]):
                    self.assertEqual(set(public), {"question", "answer_schema"})
                    self.assertEqual(private["phase_id"], phase)
                    self.assertTrue(score_answer(private["expected"], private["expected"])["correct"])
                    artifacts = packet["task_artifacts"][private["id"]]
                    self.assertEqual(set(artifacts), ARTIFACT_FIELDS)
                    self.assertEqual(artifacts["question_sha256"], _hash(public["question"]))
                    self.assertEqual(artifacts["response_schema_sha256"], _hash(public["answer_schema"]))
                    self.assertEqual(artifacts["expected_sha256"], _hash(private["expected"]))
                    self.assertEqual(artifacts["scorer_sha256"], plan["scorer_sha256"])
                guarded = packet["evaluator_tasks"][-1]["expected"]
                self.assertEqual(guarded["status"], "answered" if count == 5 else "abstained")
                self.assertTrue(guarded["provenance"]["snapshot_id"].startswith("source-validation-"))
                self.assertEqual(packet["artifact_hashes"]["observed_source_sha256"], capture["receipt"]["observed_source_sha256"])
                self.assertNotEqual(packet["artifact_hashes"]["phase_intent_sha256"], packet["artifact_hashes"]["observed_source_sha256"])
                self.assertEqual(packet["artifact_hashes"]["normalized_tables_sha256"] is None, count == 1)
                for key in ("claim_eligible", "held_out", "execution_authorized", "model_calls", "source_calls"):
                    self.assertFalse(packet[key])

    def test_recorded_native_is_only_an_operator_assertion_never_claim_evidence(self):
        packet = self.bind(origin="recorded_native")
        self.assertEqual(packet["evidence_origin"], "recorded_native")
        self.assertEqual(packet["origin_authentication"], "operator_assertion_only")
        self.assertFalse(packet["claim_eligible"])
        self.assertFalse(packet["execution_authorized"])
        self.assertIn("not native execution", packet["limitations"][0])

    def test_public_guard_contract_does_not_reveal_phase_or_response(self):
        public = []
        for phase in PHASE_ORDER:
            capture = capture_validation(FakePair(source_rows(self.plan, phase)), self.plan["phases"][0]["intent_manifest"]["namespace"])
            packet = bind_phase(self.plan, phase, capture, self.root / (phase + ".json"), snapshot=fake_snapshot(capture))
            task = deepcopy(packet["public_tasks"][-1])
            provenance = packet["evaluator_tasks"][-1]["expected"]["provenance"]
            task["question"] = task["question"].replace(provenance["data_sha256"], "HASH").replace(provenance["snapshot_id"], "ALIAS")
            public.append(task)
            text = json.dumps(packet["public_tasks"])
            for hidden in ("guard_a", "guard_b", "source-state-", '"expected"', "6668", "Synthetic Owner"):
                self.assertNotIn(hidden, text)
        self.assertTrue(all(item == public[0] for item in public))

    def test_plan_scorer_intent_and_action_hashes_must_match(self):
        mutations = [
            lambda p: p.update(plan_sha256="0" * 64),
            lambda p: p.update(scorer_sha256="0" * 64),
            lambda p: p["phases"][0].update(intent_sha256="0" * 64),
            lambda p: p["transitions"][0].update(action_sha256="0" * 64),
            lambda p: p["transitions"][0].update(after_intent_sha256="0" * 64),
            lambda p: p["source_issue_account_ids"].update(issue_1="customer_002"),
            lambda p: p["phases"][2]["expected_guard"]["blockers"].clear(),
            lambda p: p.update(held_out=True),
            lambda p: p["tasks"][0].update(weight=2),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                plan = deepcopy(self.plan)
                mutate(plan)
                if number:
                    rehash_plan(plan)
                self.rejected(plan=plan)

    def test_rehashed_action_with_wrong_transition_values_fails(self):
        plan = deepcopy(self.plan)
        transition = plan["transitions"][0]
        transition["changes"][0]["after"]["state_id"] = "wrong-state"
        transition["action_sha256"] = _hash(transition["changes"])
        rehash_plan(plan)
        self.rejected(plan=plan)

    def test_rehashed_noop_refresh_cannot_stand_in_for_changed_source_state(self):
        plan = deepcopy(self.plan)
        original_issues = {issue["issue_id"]: issue for account in plan["phases"][0]["intent_manifest"]["accounts"] for issue in account["issues"]}
        for phase in plan["phases"][1:]:
            for account in phase["intent_manifest"]["accounts"]:
                for issue in account["issues"]:
                    issue.update({key: original_issues[issue["issue_id"]][key] for key in ("state_id", "state_name", "state_type")})
            phase["intent_sha256"] = _hash(phase["intent_manifest"])
            blockers, summary = _guarded_state(phase["intent_manifest"])
            phase["expected_guard"] = {"status": "abstained" if blockers else "answered", "result": summary, "blockers": blockers}
        for index, transition in enumerate(plan["transitions"]):
            transition["before_intent_sha256"] = plan["phases"][index]["intent_sha256"]
            transition["after_intent_sha256"] = plan["phases"][index + 1]["intent_sha256"]
            if index == 0:
                for change in transition["changes"]:
                    change["after"] = deepcopy(change["before"])
                transition["action_sha256"] = _hash(transition["changes"])
        rehash_plan(plan)
        with self.assertRaisesRegex(ValueError, "Refresh must change"):
            self.bind(plan=plan)
        self.assertFalse(self.output.exists())

    def test_validation_receipt_tampering_fails_before_output(self):
        mutations = [
            lambda c: c["receipt"].update(complete=False),
            lambda c: c["receipt"].update(complete=1),
            lambda c: c["receipt"].update(validation_status="unverified"),
            lambda c: c["receipt"].update(workspace_imported=True),
            lambda c: c["receipt"].update(synthetic=False),
            lambda c: c["receipt"].update(receipt_kind="workspace_snapshot"),
            lambda c: c["receipt"].update(data_sha256="0" * 64),
            lambda c: c["receipt"].update(observed_source_sha256="0" * 64),
            lambda c: c["receipt"].update(hash_kind="observed_cohort_projection/v1"),
            lambda c: c["receipt"].update(snapshot_id="rystic-wrong"),
            lambda c: c["receipt"]["source_rows"].update({"stripe.customers": True}),
            lambda c: c["receipt"]["simulators"]["stripe"].update(version="unsupported"),
            lambda c: c["receipt"].update(observed_at="2026-01-01T00:00:00"),
            lambda c: c["receipt"].update(source_read_seconds=-1),
            lambda c: c["receipt"].update(refresh_mode="full_cohort_replace"),
            lambda c: c["tables"]["stripe.charges"][0].update(amount_cents=99999),
            lambda c: c["observed_source"]["rows"]["stripe.customers"][0].update(extra="tampered"),
            lambda c: c["observed_source"]["rows"]["stripe.customers"][0].update(extra=float("nan")),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                capture = deepcopy(self.capture)
                mutate(capture)
                self.rejected(capture=capture)

    def test_success_snapshot_must_match_the_exact_capture(self):
        mutations = [
            lambda s: s.update(refresh_mode="bundled_capture"),
            lambda s: s.update(data_sha256="0" * 64),
            lambda s: s.update(source_validation_sha256="0" * 64),
            lambda s: s.update(observed_at="2026-01-01T00:00:00Z"),
            lambda s: s.update(receipt_kind="source_state_validation"),
            lambda s: s["rows"].update({"stripe.charges": 0}),
            lambda s: s["simulators"]["linear"].update(version="unknown"),
        ]
        self.rejected(snapshot=None)
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                snapshot = deepcopy(self.snapshot)
                mutate(snapshot)
                self.rejected(snapshot=snapshot)
        refreshed = capture_validation(FakePair(source_rows(self.plan, "refreshed")), self.capture["receipt"]["namespace"])
        self.rejected(phase="refreshed", capture=refreshed)

    def test_semantic_mismatches_fail_even_with_internally_consistent_receipts(self):
        mutations = [
            lambda r: r["stripe.customers"][0].update(name="Changed synthetic name"),
            lambda r: r["stripe.customers"][0]["metadata"].update(account_id="changed_account"),
            lambda r: r["stripe.customers"][0]["metadata"].update(is_internal="true"),
            lambda r: r["stripe.charges"][0].update(amount=10002),
            lambda r: r["stripe.charges"][0].update(amount_refunded=3334),
            lambda r: r["stripe.charges"][0].update(customer="cus_2"),
            lambda r: r["stripe.charges"][0].update(paid=False),
            lambda r: r["stripe.charges"][1].update(currency="usd"),
            lambda r: r["stripe.charges"].pop(),
            lambda r: r["linear.issues"][0].update(title="Changed synthetic title"),
            lambda r: r["linear.issues"][0]["state"].update(id="same-label-wrong-workflow-state"),
            lambda r: r["linear.issues"][0].update(assignee=None),
            lambda r: r["linear.issues"][0].update(identifier="OTHER-123"),
            lambda r: r["linear.issues"][0].update(priority=4),
            lambda r: r["linear.issues"][0].update(description=r["linear.issues"][0]["description"].replace("customer_001", "customer_002")),
            lambda r: r["linear.issues"].pop(),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                rows = source_rows(self.plan, "baseline")
                mutate(rows)
                capture = capture_validation(FakePair(rows), self.capture["receipt"]["namespace"])
                self.rejected(capture=capture, snapshot=fake_snapshot(capture))

    def test_guarded_receipts_require_all_blockers_and_fixed_issue_mappings(self):
        namespace = self.capture["receipt"]["namespace"]
        for phase in ("guard_a", "guard_b"):
            capture = capture_validation(FakePair(source_rows(self.plan, phase)), namespace)
            self.assertEqual(capture["receipt"]["validation_status"], "blocked")
            self.rejected(phase=phase, capture=capture)  # An old successful snapshot must not accompany refusal.
            tampered = deepcopy(capture)
            tampered["receipt"]["blockers"].pop()
            self.rejected(phase=phase, capture=tampered, snapshot=None)
            tampered = deepcopy(capture)
            tampered["tables"] = self.capture["tables"]
            self.rejected(phase=phase, capture=tampered, snapshot=None)
        rows = source_rows(self.plan, "guard_a")
        # Incorrectly infer Linear ownership from mutated Stripe logical metadata.
        rows["linear.issues"][6]["description"] = rows["linear.issues"][6]["description"].replace("customer_002", "customer_001")
        capture = capture_validation(FakePair(rows), namespace)
        self.assertEqual(capture["receipt"]["validation_status"], "blocked")
        self.rejected(phase="guard_a", capture=capture, snapshot=None)

    def test_complete_raw_projection_rejects_extra_or_duplicate_records(self):
        for source in ("stripe.customers", "stripe.charges", "linear.issues"):
            with self.subTest(source=source):
                capture = deepcopy(self.capture)
                capture["observed_source"]["rows"][source].append(deepcopy(capture["observed_source"]["rows"][source][0]))
                capture["receipt"]["observed_source_sha256"] = _hash(capture["observed_source"])
                self.rejected(capture=capture)
                rows = source_rows(self.plan, "baseline")
                extra = deepcopy(rows[source][0])
                extra["id"] = "extra_physical_record"
                rows[source].append(extra)
                capture = capture_validation(FakePair(rows), self.capture["receipt"]["namespace"])
                self.rejected(capture=capture, snapshot=fake_snapshot(capture))
        # A supplied unmarked row cannot disappear silently during normalization.
        capture = deepcopy(self.capture)
        capture["observed_source"]["rows"]["stripe.customers"].append({"id": "outside", "metadata": {}})
        capture["receipt"]["observed_source_sha256"] = _hash(capture["observed_source"])
        self.rejected(capture=capture)

    def test_invalid_policy_is_not_certified_as_appropriate_abstention(self):
        for value in ("", "false", False):
            rows = source_rows(self.plan, "guard_b")
            rows["stripe.customers"][0]["metadata"]["is_internal"] = value
            capture = capture_validation(FakePair(rows), self.capture["receipt"]["namespace"])
            self.rejected(phase="guard_b", capture=capture, snapshot=None)

    def test_invalid_origin_unknown_phase_and_non_json_values_fail(self):
        self.rejected(origin="authenticated_native")
        self.rejected(phase="not-declared")
        for value in (float("inf"), object(), ("tuple",), {1: "not-string-key"}):
            capture = deepcopy(self.capture)
            capture["receipt"]["extra"] = value
            self.rejected(capture=capture)

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text("keep this existing artifact")
        with self.assertRaises(FileExistsError):
            self.bind()
        self.assertEqual(self.output.read_text(), "keep this existing artifact")


if __name__ == "__main__":
    unittest.main()
