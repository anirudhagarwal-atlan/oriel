"""Context lifecycle acceptance over actual local SQLite/dlt publications.

The source arrays and all model replies are synthetic. Scripted transports
exercise orchestration and reference validation, not model reasoning quality,
vendor compatibility, or an authenticated human business review.
"""
import copy
from importlib import util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from contextbase.customer360 import agent, answers, contexts, profiles
from contextbase.customer360.contracts import sha256
from contextbase.workspace import Workspace


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "customer-360"


def _module(name):
    spec = util.spec_from_file_location("context_lifecycle_fixture_" + name, EXAMPLE / (name + ".py"))
    value = util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runtime, review = (_module(name) for name in ("runtime", "review"))


class ScriptedBootstrap:
    """Author deterministic candidates from observed logical capabilities only."""
    def __init__(self):
        self.inputs = []

    def complete(self, system, messages, max_cost_usd):
        supplied = json.loads(messages[0]["content"])
        self.inputs.append(copy.deepcopy(supplied))
        evidence = supplied["source_evidence"]
        if supplied["stage"] == "observe":
            observations = []
            for binding in evidence["profile"]["bindings"]:
                samples = [row for row in evidence["samples"]
                           if (row["connection"], row["entity"]) == (binding["connection"], binding["entity"])]
                observations.append({"connection": binding["connection"], "entity": binding["entity"],
                                     "kind": binding["kind"], "field_roles": sorted(binding["fields"]),
                                     "evidence_ids": [row["evidence_id"] for row in samples],
                                     "notes": "Observed mapped fields and bounded samples; selected business meaning requires review."})
            action = {"observations": observations, "gaps": [
                {"code": "recurrence_unknown", "description": "An amount does not establish recurrence or recurring revenue."},
                {"code": "text_timing_requires_review", "description": "Original source statements and later updates must retain their attribution; the latest truth is not inferred from a quotation alone."},
                {"code": "identity_coverage_requires_review", "description": "Unresolved and conflicting relationships remain visible in source coverage and are not attributed by name similarity."},
            ]}
        else:
            definitions = []
            kinds = {binding["kind"] for binding in evidence["profile"]["bindings"]}
            for kind in sorted(kinds & {"commercial", "billing"}):
                definitions.append({"name": "observed_" + kind + "_value",
                                    "description": "Proposed sum of mapped amounts using explicit source state, without a recurring-revenue claim.",
                                    "kind": kind, "operation": "sum", "field": "amount", "currency_field": "currency",
                                    "unit": "minor_currency" if kind == "billing" else "currency",
                                    "filters": [{"field": "status", "operator": "eq", "value": "open"}] if kind == "billing"
                                    else [{"field": "is_closed", "operator": "eq", "value": False}]})
            if "support" in kinds:
                definitions.append({"name": "observed_open_cases", "description": "Proposed count of attributed records with open source state.",
                                    "kind": "support", "operation": "count", "field": None, "unit": "records", "currency_field": None,
                                    "filters": [{"field": "status", "operator": "eq", "value": "open"}]})
            # Deliberately omit stage-one unknowns: the agent must retain them.
            action = {"definitions": definitions, "gaps": []}
        return {"response": json.dumps(action), "cost_usd": 0.0, "cost_known": True,
                "is_error": False, "model_usage": {"scripted-lifecycle-test": {}}}


class ScriptedMetric:
    def __init__(self, definition):
        self.definition, self.calls = definition, 0

    def complete(self, system, messages, max_cost_usd):
        self.calls += 1
        if self.calls == 1:
            value = {"tools": [{"tool": "metric", "arguments": {"definition_name": self.definition}}]}
        else:
            metric = json.loads(messages[-1]["content"])["result"]
            value = {"answer": {"metrics": [{"definition_name": self.definition, "result_id": metric["result_id"]}], "quotes": []}}
        return {"response": json.dumps(value), "cost_usd": 0.0, "cost_known": True,
                "is_error": False, "model_usage": {"scripted-lifecycle-test": {}}}


class ContextLifecycleAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.prerequisites()  # Missing actual connector dependencies fail, never skip.
        cls.base_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.base_directory.cleanup)
        cls.native = {}
        for label in ("salesforce-gong", "billing-support"):
            workspace = Workspace(Path(cls.base_directory.name) / label)
            extraction = runtime.load_profile(workspace, label, "initial")
            approved = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
            cls.native[label] = (workspace, extraction, approved)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cases = {}
        for label, (source, extraction, approved) in self.native.items():
            path = Path(self.directory.name) / label
            shutil.copytree(source.root, path)
            self.cases[label] = (Workspace(path), copy.deepcopy(extraction), copy.deepcopy(approved))

    def bootstrap(self, workspace, reviewed, profile=None, *, intent=None):
        client = ScriptedBootstrap()
        result = agent.bootstrap(workspace, profile or reviewed["profile"], reviewed["snapshot_id"],
                                 intent=intent, allow_model_data=True, max_turns=2, max_cost_usd=.01, client=client)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["evidence_origin"], "scripted_agent_test")
        self.assertFalse(result["approval_performed"])
        self.assertFalse(result["claim_eligible"])
        return result, client

    @staticmethod
    def approve(workspace, candidate):
        return contexts.approve_context(workspace, candidate["proposal_id"], candidate["approval_sha256"], "synthetic-lifecycle-reviewer")

    def test_context_stages_derive_profile_appropriate_candidates(self):
        expected_kinds = {"salesforce-gong": {"commercial"}, "billing-support": {"billing", "support"}}
        for label, (workspace, extraction, reviewed) in self.cases.items():
            with self.subTest(profile=label):
                self.assertTrue(extraction["actual_dlt_executed"])
                self.assertFalse(extraction["vendor_adapter_verified"])
                intent = "Review this customer's open commercial value and latest conversations." if label == "salesforce-gong" else "Review this customer's open invoices and latest support update."
                result, client = self.bootstrap(workspace, reviewed, intent=intent)
                candidate = result["candidate"]
                self.assertEqual({item["kind"] for item in candidate["definitions"]}, expected_kinds[label])
                actual = contexts.inspect_context(workspace, reviewed["profile"], reviewed["snapshot_id"])
                self.assertEqual(client.inputs[0]["source_evidence"], actual)
                self.assertEqual(client.inputs[1]["source_evidence"], actual)
                self.assertEqual([value["operator_intent"] for value in client.inputs], [intent, intent])
                self.assertEqual([stage["stage"] for stage in candidate["stages"]], ["observe", "define"])
                self.assertTrue(actual["samples"])
                for stage in candidate["stages"]:
                    self.assertEqual(stage["source_evidence_sha256"], agent._hash(actual))
                    self.assertEqual(stage["intent"], intent)
                    self.assertEqual(stage["intent_sha256"], sha256(intent))
                    self.assertEqual(stage["profile_approval"], reviewed["profile_approval"])
                    self.assertEqual(stage["materialization_id"], reviewed["materialization_id"])
                indexed = {row["evidence_id"]: row for row in actual["samples"]}
                for observation in candidate["stages"][0]["observations"]:
                    self.assertTrue(observation["evidence_ids"])
                    for evidence_id in observation["evidence_ids"]:
                        self.assertEqual(indexed[evidence_id]["kind"], observation["kind"])
                receipt = json.loads(Path(result["receipt_path"]).read_text())
                self.assertEqual(receipt["evidence_origin"], "scripted_agent_test")
                self.assertEqual(receipt["implementation_sha256"], agent._hash(receipt["implementation"]))
                request = json.loads((Path(result["receipt_path"]).parent / "request.json").read_text())
                self.assertEqual(request["intent"], intent)
                self.assertEqual(receipt["request_sha256"], sha256(request))
                self.assertEqual(contexts.current_context(workspace, reviewed["profile"], reviewed["snapshot_id"])["context_id"], reviewed["context_id"])

    def test_unreviewed_definitions_cannot_become_authoritative(self):
        for label, (workspace, _, reviewed) in self.cases.items():
            with self.subTest(profile=label):
                mapped = copy.deepcopy(profiles.load_profile(workspace, reviewed["profile"])["candidate"]["profile"])
                mapped["name"] = "pending_business_context"
                proposal = profiles.propose_profile(workspace, mapped, reviewed["snapshot_id"])
                profiles.approve_profile(workspace, proposal["proposal_id"], proposal["approval_sha256"], "synthetic-mapping-reviewer")
                profiles.materialize(workspace, mapped["name"], reviewed["snapshot_id"])
                result, _ = self.bootstrap(workspace, reviewed, mapped["name"])
                candidate = result["candidate"]
                with self.assertRaisesRegex(ValueError, "Review business context"):
                    contexts.current_context(workspace, mapped["name"], reviewed["snapshot_id"])
                customer = reviewed["customer_ids"]["customer_a"]
                client = ScriptedMetric(candidate["definitions"][0]["name"])
                refused = agent.ask(workspace, mapped["name"], reviewed["snapshot_id"], customer, "Show the proposed metric.",
                                    allow_model_data=True, max_cost_usd=.01, client=client)
                self.assertEqual((refused["status"], client.calls), ("validation_failed", 0))
                self.assertIsNone(refused["answer"])
                with self.assertRaises(ValueError):
                    contexts.approve_context(workspace, candidate["proposal_id"], "0" * 64, "synthetic-lifecycle-reviewer")
                approved = self.approve(workspace, candidate)
                self.assertEqual(approved["proposal_id"], candidate["proposal_id"])
                self.assertEqual(approved["approval_kind"], "local_operator_business_review")
                answered = agent.ask(workspace, mapped["name"], reviewed["snapshot_id"], customer, "Show the reviewed metric.",
                                     allow_model_data=True, max_turns=2, max_cost_usd=.01, client=client)
                self.assertEqual(answered["status"], "answered")
                self.assertEqual(answered["answer"]["context_id"], approved["context_id"])

    def test_conflicting_evidence_and_unknown_meaning_are_retained(self):
        workspace, _, _ = self.cases["billing-support"]
        extraction = runtime.load_profile(workspace, "billing-support", "refreshed")
        reviewed = review.setup_review(workspace, extraction, approve_synthetic_fixture=True)
        result, client = self.bootstrap(workspace, reviewed)
        actual = client.inputs[0]["source_evidence"]
        candidate = result["candidate"]
        unknowns = {item["code"] for item in candidate["gaps"]}
        self.assertTrue({"recurrence_unknown", "text_timing_requires_review", "identity_coverage_requires_review",
                         "upstream_completeness_unverified"} <= unknowns)
        self.assertEqual({item["code"] for item in candidate["stages"][0]["gaps"]},
                         {"recurrence_unknown", "text_timing_requires_review", "identity_coverage_requires_review"})
        self.assertTrue(any(item["identity"].get("conflict", 0) > 0 for item in actual["coverage"]))
        # Independently read both contradictory-time statements from the raw
        # authored source object, then locate their exact retained field values.
        source = json.loads((EXAMPLE / "profiles/billing-support/refreshed/intercom.conversations.json").read_text())[0]
        older = source["source"]["body"]
        later = source["conversation_parts"]["conversation_parts"][0]["body"]
        self.assertNotEqual(older, later)
        # Bounded bootstrap samples need not include every conflicting statement.
        # The complete projection must retain both, and scoped evidence reads
        # must still be able to cite them after context review.
        with profiles.readable_profile(workspace, reviewed["profile"], reviewed["snapshot_id"]) as scope:
            retained = copy.deepcopy(scope[-1]["records"])
        rows = [row for row in retained if row["kind"] == "support" and older in row["fields"].get("text", "")]
        self.assertEqual(len(rows), 1)
        self.assertIn(later, rows[0]["fields"]["updates"])
        self.assertIn(str(source["conversation_parts"]["conversation_parts"][0]["created_at"]), rows[0]["fields"]["updates"])
        inspected_ids = {row["evidence_id"] for row in actual["samples"]}
        self.assertTrue(all(set(item["evidence_ids"]) <= inspected_ids for item in candidate["stages"][0]["observations"]))
        self.approve(workspace, candidate)
        customer = reviewed["customer_ids"]["customer_a"]
        row = rows[0]
        selected = []
        for field, value in (("text", older), ("updates", later)):
            start = row["fields"][field].index(value)
            quote = answers.read_evidence(workspace, reviewed["profile"], reviewed["snapshot_id"], customer,
                                          row["evidence_id"], field=field, start=start, end=start + len(value))
            self.assertEqual(quote["text"], value)
            selected.append({key: quote[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")})
        answer = answers.validate_answer(workspace, reviewed["profile"], reviewed["snapshot_id"], customer,
                                         {"metrics": [], "quotes": selected})
        self.assertEqual({q["text"] for q in answer["quotes"]}, {older, later})
        self.assertIn("text_timing_requires_review", {gap["code"] for gap in answer["gaps"]})
        self.assertFalse(answer["model_generated_factual_prose"])

    def test_approved_context_reused_in_fresh_process(self):
        code = """
import json, socket, sys
from unittest.mock import patch
from contextbase.workspace import Workspace
from contextbase.customer360 import answers, contexts, agent
w, profile, snapshot, customer = Workspace(sys.argv[1]), *sys.argv[2:]
with patch.object(agent, 'ClaudeCLI', side_effect=AssertionError('Unexpected model construction')), patch.object(socket.socket, 'connect', side_effect=AssertionError('Unexpected network')):
    current = contexts.current_context(w, profile, snapshot)
    answer = answers.brief(w, profile, snapshot, customer)
    print(json.dumps({'context_id': current['context_id'], 'definition_sha256': current['definition_sha256'], 'version': current['version'], 'answer_id': answer['answer_id'], 'metrics': [m['result'] for m in answer['metrics']]}))
"""
        for label, (workspace, _, reviewed) in self.cases.items():
            with self.subTest(profile=label):
                result, _ = self.bootstrap(workspace, reviewed)
                approved = self.approve(workspace, result["candidate"])
                customer = reviewed["customer_ids"]["customer_a"]
                before = answers.brief(workspace, reviewed["profile"], reviewed["snapshot_id"], customer)
                process = subprocess.run([sys.executable, "-c", code, str(workspace.root), reviewed["profile"], reviewed["snapshot_id"], customer],
                                         capture_output=True, text=True, check=True, timeout=30)
                reused = json.loads(process.stdout)
                self.assertEqual(reused["context_id"], approved["context_id"])
                self.assertEqual(reused["definition_sha256"], approved["definition_sha256"])
                self.assertEqual(reused["version"], approved["version"])
                self.assertEqual(reused["answer_id"], before["answer_id"])
                self.assertEqual(reused["metrics"], [m["result"] for m in before["metrics"]])

    def test_definition_revision_invalidates_dependents(self):
        for label, (workspace, _, reviewed) in self.cases.items():
            with self.subTest(profile=label):
                profile, snapshot, customer = reviewed["profile"], reviewed["snapshot_id"], reviewed["customer_ids"]["customer_a"]
                first = contexts.current_context(workspace, profile, snapshot)
                definition = first["definitions"][0]["name"]
                metric = answers.execute_metric(workspace, profile, snapshot, customer, definition)
                query = "SSO" if label == "salesforce-gong" else "Invoice"
                quote = answers.search_evidence(workspace, profile, snapshot, customer, query, limit=1)["quotes"][0]
                selected = {"metrics": [{"definition_name": definition, "result_id": metric["result_id"]}],
                            "quotes": [{key: quote[key] for key in ("evidence_id", "field", "start", "end", "text", "quote_id")}]}
                before = answers.validate_answer(workspace, profile, snapshot, customer, selected)
                rules = copy.deepcopy(first["definitions"])
                rules[0]["filters"][0]["value"] = True if label == "salesforce-gong" else "paid"
                candidate = contexts.propose_context(workspace, profile, snapshot, rules, gaps=first["gaps"], origin="scripted_agent_test")
                self.assertEqual(answers.validate_answer(workspace, profile, snapshot, customer, selected)["answer_id"], before["answer_id"])
                second = self.approve(workspace, candidate)
                self.assertEqual(second["version"], first["version"] + 1)
                self.assertEqual(second["previous_context_id"], first["context_id"])
                self.assertNotEqual(second["definition_sha256"], first["definition_sha256"])
                with self.assertRaisesRegex(ValueError, "Metric reference changed"):
                    answers.validate_answer(workspace, profile, snapshot, customer, selected)
                with self.assertRaisesRegex(ValueError, "Quote text or reference"):
                    answers.validate_answer(workspace, profile, snapshot, customer, {"metrics": [], "quotes": selected["quotes"]})
                after = answers.execute_metric(workspace, profile, snapshot, customer, definition)
                self.assertNotEqual(after["result_id"], metric["result_id"])
                self.assertNotEqual(after["result"]["values"], metric["result"]["values"])
                self.assertEqual(after["context_id"], second["context_id"])
                self.assertEqual(before["answer_id"], sha256({key: value for key, value in before.items() if key != "answer_id"}))
                with workspace._connect() as connection:
                    history = connection.execute("SELECT context_id FROM _contextbase.customer360_context_approvals WHERE profile=? ORDER BY version", [profile]).fetchall()
                self.assertEqual(history, [(first["context_id"],), (second["context_id"],)])

    def test_exported_pack_excludes_customer_rows_and_text(self):
        for label, (workspace, _, reviewed) in self.cases.items():
            with self.subTest(profile=label):
                profile, snapshot = reviewed["profile"], reviewed["snapshot_id"]
                current = contexts.current_context(workspace, profile, snapshot)
                rules = copy.deepcopy(current["definitions"])
                rules[0]["description"] = "synthetic-private-definition-description"
                rules[0]["filters"][0]["value"] = "synthetic-private-rule-literal"
                candidate = contexts.propose_context(workspace, profile, snapshot, rules,
                                                     gaps=[{"code": "private_unknown", "description": "synthetic-private-gap"}],
                                                     stages=[{"raw_note": "synthetic-private-agent-trace"}], origin="scripted_agent_test")
                approved = self.approve(workspace, candidate)
                output = Path(self.directory.name) / (label + "-portable.json")
                receipt = contexts.export_template(workspace, profile, snapshot, output)
                raw = output.read_text()
                template = json.loads(raw)
                self.assertEqual(receipt["template_sha256"], sha256(template))
                self.assertEqual(template["status"], "requires_local_binding_and_review")
                self.assertEqual(template["source_definition_sha256"], approved["definition_sha256"])
                self.assertEqual(set(template), {"spec_version", "status", "source_definition_sha256", "definitions"})
                self.assertNotIn("synthetic-private-", raw)
                for forbidden in (snapshot, approved["context_id"], approved["materialization_id"], *reviewed["customer_ids"].values()):
                    self.assertNotIn(forbidden, raw)
                with profiles.readable_profile(workspace, profile, snapshot) as scope:
                    rows = scope[-1]["records"]
                for row in rows:
                    self.assertNotIn(row["source_id"], raw)
                    self.assertNotIn(row["evidence_id"], raw)
                    for role in ("text", "updates", "label"):
                        if row["fields"].get(role):
                            self.assertNotIn(row["fields"][role], raw)
                for definition in template["definitions"]:
                    self.assertNotIn("description", definition)
                    for predicate in definition["filters"]:
                        self.assertEqual(set(predicate), {"field", "operator", "requires_local_value"})
                        self.assertTrue(predicate["requires_local_value"])
                with self.assertRaises(FileExistsError):
                    contexts.export_template(workspace, profile, snapshot, output)


if __name__ == "__main__":
    unittest.main()
