"""Pilot harness verification with authored responses, never model evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextbase.context import approve_metric, export_bundle, propose_metric
from contextbase.rystic import RysticPair, _store_snapshot
from contextbase.rystic_benchmark import freeze_plan, run_pilot, _system, parse_structured_action
from contextbase.rystic_recipe import NAMESPACE, RECIPE_SPEC, _plan_accounts
from contextbase.rystic_workflow import propose_context
from contextbase.workspace import Workspace


def make_fixture(workspace):
    states = {state: {"id": state, "name": state.title()} for state in ("started", "completed", "canceled")}
    accounts = _plan_accounts(12, 31, states, [{"id": "owner_1", "name": "Synthetic Owner"}])
    tables = {"stripe.customers": [], "stripe.charges": [], "linear.issues": []}
    for index, account in enumerate(accounts):
        account["customer_id"] = f"cus_{index}"
        tables["stripe.customers"].append({key: account[key] for key in ("customer_id", "account_id", "name", "is_internal")})
        for number, charge in enumerate(account["charges"]):
            charge["charge_id"] = f"ch_{index}_{number}"
            tables["stripe.charges"].append({
                "charge_id": charge["charge_id"], "customer_id": account["customer_id"],
                "amount_cents": charge["amount_cents"], "amount_refunded_cents": charge["refund_cents"],
                "created_at": "2026-10-09T00:00:00+00:00",
                **{key: charge[key] for key in ("currency", "paid", "captured", "status", "disputed")}})
        for number, issue in enumerate(account["issues"]):
            issue.update(issue_id=f"issue_{index}_{number}", identifier=f"SYN-{index}-{number}")
            tables["linear.issues"].append({"account_id": account["account_id"], "updated_at": "2026-10-09T00:00:00Z",
                **{key: issue[key] for key in ("issue_id", "identifier", "title", "priority", "state_type",
                                               "state_name", "owner_id", "owner_name")}})
    for rows in tables.values():
        rows.sort(key=lambda row: next(iter(row.values())))
    # Match the adapter's stable ordering, including identifiers with two digits.
    for name, key in (("stripe.customers", "customer_id"), ("stripe.charges", "charge_id"), ("linear.issues", "issue_id")):
        tables[name].sort(key=lambda row: row[key])
    _store_snapshot(workspace, tables, {"namespace": NAMESPACE, "synthetic": True, "simulators": {},
        "refresh_mode": "full_cohort_replace", "observed_at": "2026-10-09T00:00:00Z", "consistency": "unit test fixture"})
    proposal = propose_context(workspace)["proposal"]
    approve_metric(workspace, proposal["name"], proposal["approval_sha256"], "synthetic_test_reviewer")
    return {"spec": RECIPE_SPEC, "namespace": NAMESPACE, "status": "complete", "synthetic": True,
            "account_count": 12, "accounts": accounts}, tables


class EmptyTools:
    def __init__(self): self.trace = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def definitions(self): return []


class AuthoredModel:
    def __init__(self, plan):
        self.answers = {t["question"]: t["expected"] for t in plan["tasks"]}
        self.prompts = []
    def complete(self, system, messages, **kwargs):
        self.prompts.append((system, deepcopy(messages)))
        return {"response": json.dumps({"answer": self.answers[messages[0]["content"]]}),
                "cost_usd": 0.01, "model_usage": {"unit-test-script": {}}}


class RysticBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "workspace")
        self.manifest, self.tables = make_fixture(self.workspace)
        self.pair = RysticPair("http://127.0.0.1:18981", "http://127.0.0.1:18982")

    def plan(self, **kwargs):
        return freeze_plan(self.workspace, self.manifest, self.root / "plan.json", **kwargs)

    def test_freeze_is_no_inference_exact_and_exclusive(self):
        plan = self.plan()
        self.assertEqual(len(plan["queue"]), 8)
        self.assertFalse(plan["held_out"])
        self.assertEqual(len(plan["plan_sha256"]), 64)
        self.assertIsNone(plan["accounting"]["refresh_usd"])
        with self.assertRaises(FileExistsError): self.plan()
        for task in plan["tasks"]:
            self.assertNotIn('"expected"', _system([], task, 8))
            self.assertNotIn('Synthetic Owner', _system([], task, 8))

    def test_manifest_drift_and_extra_tables_block_freeze(self):
        self.manifest["accounts"][0]["charges"][0]["amount_cents"] += 1
        with self.assertRaisesRegex(ValueError, "intent"): self.plan()
        with self.workspace._connect(writable=True) as conn:
            conn.execute("CREATE TABLE stripe.unrelated (id INTEGER)")
        with self.assertRaisesRegex(ValueError, "exactly"): self.plan()

    def test_changed_rows_block_freeze(self):
        with self.workspace._connect(writable=True) as conn:
            conn.execute("UPDATE stripe.charges SET amount_cents=amount_cents+1")
        with self.assertRaisesRegex(ValueError, "rows changed"): self.plan()

    def test_approved_constant_sql_cannot_impersonate_recipe(self):
        from contextbase.rystic_workflow import METRIC_NAME, RULES
        proposal = propose_metric(self.workspace, name=METRIC_NAME,
            sql="SELECT 123.45 AS net_exposed_collected_usd", description="Synthetic regression fixture",
            grain="All qualifying accounts", unit="USD", owner="synthetic_test_owner",
            rules=RULES, synthetic_example=True)
        approve_metric(self.workspace, METRIC_NAME, proposal["approval_sha256"], "synthetic_test_reviewer")
        with self.assertRaisesRegex(ValueError, "recipe policy"):
            self.plan()
        self.assertFalse((self.root / "plan.json").exists())

    def test_approval_and_consent_checked_before_model_access(self):
        plan = self.plan()
        for consent, digest in ((False, plan["plan_sha256"]), (True, "wrong")):
            with self.assertRaises(ValueError):
                run_pilot(self.workspace, self.pair, plan, self.root / "run.json", approve=digest,
                          allow_model_data=consent, client=AuthoredModel(plan))
        self.assertFalse((self.root / "run.json").exists())

    def test_scripted_complete_run_keeps_gates_unverified(self):
        plan = self.plan()
        client = AuthoredModel(plan)
        with patch("contextbase.rystic_benchmark.collect_snapshot", return_value=(self.tables, {"simulators": {}})):
            report = run_pilot(self.workspace, self.pair, plan, self.root / "run.json", approve=plan["plan_sha256"],
                allow_model_data=True, client=client, tools_factory=lambda arm: EmptyTools())
        self.assertTrue(report["execution_complete"])
        self.assertEqual(report["invocations"], 8)
        self.assertTrue(all(t["correct"] for t in report["trials"]))
        self.assertFalse(report["release_gates"]["claim_eligible"])
        self.assertTrue(all(s["all_in_cost_per_correct_answer_usd"] is None for s in report["summary"]))
        self.assertEqual(len(client.prompts), 8)
        self.assertEqual(report["source_stability"], "pre_and_post_match")

    def test_preflight_failure_records_all_planned_tasks_as_failures(self):
        plan = self.plan()
        client = AuthoredModel(plan)
        with patch("contextbase.rystic_benchmark.collect_snapshot", side_effect=ValueError("Unavailable")):
            report = run_pilot(self.workspace, self.pair, plan, self.root / "run.json", approve=plan["plan_sha256"],
                allow_model_data=True, client=client)
        self.assertFalse(report["execution_complete"])
        self.assertEqual(report["invocations"], 0)
        self.assertEqual(len(report["trials"]), 8)
        self.assertEqual(client.prompts, [])
        self.assertTrue(all(t["capped_seconds"] == 180 and not t["correct"] for t in report["trials"]))

    def test_failed_answers_are_completed_observations_not_dropped_trials(self):
        plan = self.plan()
        class InvalidModel:
            def complete(self, *args, **kwargs):
                return {"response": "not JSON", "cost_usd": 0.01, "model_usage": {"unit-test-script": {}}}
        with patch("contextbase.rystic_benchmark.collect_snapshot", return_value=(self.tables, {"simulators": {}})):
            report = run_pilot(self.workspace, self.pair, plan, self.root / "run.json", approve=plan["plan_sha256"],
                allow_model_data=True, client=InvalidModel(), tools_factory=lambda arm: EmptyTools())
        self.assertTrue(report["execution_complete"])
        self.assertEqual(report["answered_trials"], 0)
        self.assertTrue(all(not trial["correct"] for trial in report["trials"]))
        self.assertEqual(report["invocations"], 8)
        self.assertFalse(report["release_gates"]["claim_eligible"])

    def test_structured_action_rejects_duplicate_and_nonfinite_json(self):
        for raw in ('{"answer":{},"answer":{}}', '{"answer":{"n":NaN}}', '{"answer":{"n":1e999}}'):
            with self.assertRaises(ValueError): parse_structured_action(raw)
        self.assertEqual(parse_structured_action('{"answer":{"accounts":[]}}'), {"answer": {"accounts": []}})

    def test_synthetic_label_does_not_authorize_model_access(self):
        other = Workspace(self.root / "other")
        path = self.root / "source.csv"
        path.write_text("seats\n2\n3\n")
        other.ingest(path, "example", "seats")
        proposal = propose_metric(other, "total_seats", "SELECT SUM(seats) AS total FROM example.seats", "Total seats",
            grain="all seats", unit="seats", rules=["Sum imported synthetic seats"], owner="demo_owner", synthetic_example=True)
        self.assertTrue(proposal["synthetic"])
        approve_metric(other, "total_seats", proposal["approval_sha256"], "demo_reviewer")
        export_bundle(other, self.root / "bundle")
        native = json.loads((self.root / "bundle/metrics/total_seats.json").read_text())
        self.assertEqual(native["definition"]["status"], "candidate")
        self.assertFalse(other.is_synthetic())
        with self.assertRaisesRegex(ValueError, "allow-model-data"):
            from contextbase.agent import ask
            ask(other, "Total seats?", client=object())


if __name__ == "__main__":
    unittest.main()
