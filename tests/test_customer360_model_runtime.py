"""Budget/controller adversarial tests. These make zero provider calls."""
from copy import deepcopy
import os
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tests.customer360.configured_model_runtime import (
    BudgetLedger, BudgetedApplication, PROFILES, QUESTIONS, digest, publish, read_private, validate_plan, execute_smoke,
    verify_agent_receipt,
)


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.plan = {"spec": "oriel.customer360.configured-model-plan/v1", "run_id": str(uuid.uuid4()),
                     "allow_model_data": True, "synthetic": True, "total_max_cost_usd": 3.0,
                     "input_fingerprint": {"sha256": "synthetic-test-code"}, "profiles": []}
        for index, name in enumerate(PROFILES):
            self.plan["profiles"].append({"name": name, "origin": "http://127.0.0.1:" + str(9901 + index),
                "browser_output": str(self.root / (name + "-browser")),
                "bootstrap": {"intent": QUESTIONS[name], "max_cost_usd": .5, "max_turns": 4},
                "asks": {phase: {"question": QUESTIONS[name], "max_cost_usd": .5, "max_turns": 4}
                         for phase in ("initial", "refreshed")}})
        publish(self.root / "plan.json", self.plan)
        self.ledger = BudgetLedger(self.root, self.plan, fingerprint=lambda: self.plan["input_fingerprint"])

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def arguments(self, index=0):
        step = self.ledger.schedule[index]
        args = {"allow_model_data": True, "max_cost_usd": .5, "max_turns": 4, "profile_name": "customer_review",
                "snapshot_id": "c360-" + "a" * 64}
        if step["operation"] == "ask":
            args.update(question=step["question"], customer_id=str(uuid.uuid4()))
        else:
            args["intent"] = step["intent"]
        return args

    def reserve(self, index=0):
        step = self.ledger.schedule[index]
        return self.ledger.reserve(step["profile"], step["operation"], self.arguments(index), "job-" + str(index))

    def result(self, index=0, cost=.2):
        result = {"status": "candidate" if index % 3 == 0 else "answered", "model_origin": "configured_claude_cli",
                "evidence_origin": "model_agent", "approval_performed": False, "receipt_persisted": True, "receipt_sha256": "a" * 64,
                "usage": {"known_cost_usd": cost, "reported_cost_usd": cost, "cost_complete": True, "spend_unknown": False,
                          "model_calls_complete": True, "prepared_calls_with_dispatch_unknown": 0, "model_calls": 1,
                          "model_ids": ["unit-test-model-not-a-provider"], "calls": [{"status": "returned", "cost_known": True,
                            "is_error": False, "cost_usd": cost, "requested_max_cost_usd": .25}]}}
        if index % 3 == 0:
            intent = QUESTIONS[PROFILES[index // 3]]
            result["candidate"] = {"stages": [{"stage": stage, "intent": intent, "intent_sha256": digest(intent)}
                                                 for stage in ("observe", "define")]}
        return result

    def test_whole_cap_is_on_disk_before_any_model_operation(self):
        self.reserve()
        actual = read_private(self.root / "ledger.json")
        self.assertEqual(actual["total_reserved_usd"], .5)
        self.assertTrue(actual["spend_unknown"])
        self.assertEqual(actual["entries"][0]["status"], "reserved_dispatch_or_spend_unknown")
        with self.assertRaisesRegex(ValueError, "ledger_not_ready"):
            self.reserve(1)
        self.assertEqual(self.ledger.state["status"], "stopped")

    def test_missing_or_changed_intent_refuses_before_application_model_dispatch(self):
        from contextbase.customer360 import agent
        from contextbase.customer360.application import Application
        from contextbase.workspace import Workspace
        for index, intent in enumerate((None, "A different analysis objective")):
            with self.subTest(intent=intent):
                root = self.root / ("intent-case-" + str(index))
                root.mkdir(mode=0o700)
                plan = deepcopy(self.plan)
                for profile in plan["profiles"]:
                    profile["browser_output"] = str(root / (profile["name"] + "-browser"))
                publish(root / "plan.json", plan)
                ledger = BudgetLedger(root, plan, fingerprint=lambda: plan["input_fingerprint"])
                app = BudgetedApplication(Workspace(root / "workspace"), PROFILES[0])
                app.ledger, app._active = ledger, "job-0"
                arguments = self.arguments()
                if intent is None:
                    arguments.pop("intent")
                else:
                    arguments["intent"] = intent
                try:
                    with patch.object(Application, "_model") as dispatch, self.assertRaisesRegex(ValueError, "bootstrap_intent_changed"):
                        app._model(agent.bootstrap, **arguments)
                    dispatch.assert_not_called()
                    self.assertEqual(ledger.state["entries"], [])
                    self.assertEqual(ledger.state["total_reserved_usd"], 0)
                    self.assertEqual(ledger.state["status"], "stopped")
                finally:
                    app.close()
                    ledger.close()

    def test_candidate_intent_change_is_retained_as_paid_failure(self):
        self.reserve()
        result = self.result()
        result["candidate"]["stages"][1]["intent"] = "Substituted after dispatch"
        result["candidate"]["stages"][1]["intent_sha256"] = digest("Substituted after dispatch")
        with self.assertRaisesRegex(ValueError, "bootstrap_candidate_intent_changed"):
            self.ledger.settle(0, result)
        self.assertEqual(self.ledger.state["known_cost_usd"], .2)
        self.assertFalse(self.ledger.state["spend_unknown"])
        self.assertEqual(self.ledger.state["status"], "stopped")

    def test_six_jobs_require_exact_review_and_never_reuse_unused_budget(self):
        for index in range(6):
            self.reserve(index)
            self.ledger.settle(index, self.result(index))
            self.assertEqual(self.ledger.state["status"], "awaiting_independent_review")
            self.ledger.reviewed("job-" + str(index), {"checked": index})
        self.assertEqual(self.ledger.state["status"], "complete")
        self.assertEqual(self.ledger.state["known_cost_usd"], 1.2)
        self.assertEqual(self.ledger.state["total_reserved_usd"], 3.0)
        with self.assertRaises(ValueError):
            self.reserve()

    def test_unknown_cost_stops_later_dispatch_and_retains_known_charges(self):
        self.reserve()
        result = self.result(cost=.1)
        result["usage"].update(reported_cost_usd=None, spend_unknown=True, cost_complete=False)
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.ledger.settle(0, result)
        self.assertEqual(self.ledger.state["known_cost_usd"], .1)
        self.assertTrue(self.ledger.state["spend_unknown"])
        with self.assertRaises(ValueError):
            self.reserve(1)

    def test_above_call_limit_is_retained_and_never_retried(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "budget_exceeded"):
            self.ledger.settle(0, self.result(cost=.3))
        self.assertEqual(self.ledger.state["known_cost_usd"], .3)
        self.assertFalse(self.ledger.state["spend_unknown"])
        self.assertEqual(self.ledger.state["status"], "stopped")

    def test_paid_error_retains_charge_and_stops(self):
        self.reserve()
        result = self.result()
        result["usage"]["calls"][0]["is_error"] = True
        with self.assertRaisesRegex(ValueError, "failed_model_call"):
            self.ledger.settle(0, result)
        self.assertEqual(self.ledger.state["known_cost_usd"], .2)
        self.assertEqual(self.ledger.state["entries"][0]["usage"], result["usage"])

    def test_late_provider_return_preserves_cost_after_controller_stops(self):
        self.reserve()
        self.ledger.poison("browser_failed_while_provider_was_running")
        with self.assertRaisesRegex(ValueError, "settlement_order_changed"):
            self.ledger.settle(0, self.result())
        self.assertEqual(read_private(self.root / "ledger.json")["known_cost_usd"], .2)
        self.assertFalse(self.ledger.state["spend_unknown"])
        self.assertEqual(self.ledger.state["status"], "stopped")

    def test_paid_receipt_cannot_be_attached_to_a_different_model_output(self):
        from types import SimpleNamespace
        from contextbase.workspace import Workspace
        workspace = Workspace(self.root / "receipt-workspace")
        directory = workspace.root / "customer360/agent-runs/unit-test"
        directory.mkdir(mode=0o700, parents=True)
        intent = QUESTIONS[PROFILES[0]]
        output = {"candidate": self.result()["candidate"]}
        trace, request = {"events": []}, {"synthetic": True, "intent": intent}
        for filename, value in (("result.json", output), ("trace.json", trace), ("request.json", request)):
            publish(directory / filename, value)
        result = self.result()
        receipt = {"operation": "bootstrap", "usage": result["usage"], "status": result["status"], "approval_performed": False,
                   "model_origin": result["model_origin"], "evidence_origin": result["evidence_origin"],
                   "trace_sha256": digest(trace), "request_sha256": digest(request), "result_sha256": digest(output),
                   "implementation": {"synthetic": True}}
        receipt["receipt_sha256"] = digest(receipt)
        publish(directory / "receipt.json", receipt)
        result.update(receipt_path=str(directory / "receipt.json"), receipt_sha256=receipt["receipt_sha256"], **output)
        with patch("tests.customer360.configured_model_runtime.agent._implementation", return_value={"synthetic": True}):
            verify_agent_receipt(SimpleNamespace(workspace=workspace), result, expected_bootstrap_intent=intent)
            changed = {**request, "intent": "Self-consistent receipts with a different request"}
            publish(directory / "request.json", changed, replace=True)
            receipt["request_sha256"] = digest(changed)
            receipt["receipt_sha256"] = digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
            publish(directory / "receipt.json", receipt, replace=True)
            result["receipt_sha256"] = receipt["receipt_sha256"]
            with self.assertRaisesRegex(ValueError, "bootstrap_request_intent_changed"):
                verify_agent_receipt(SimpleNamespace(workspace=workspace), result, expected_bootstrap_intent=intent)
            result["candidate"] = {"only": "substituted result"}
            with self.assertRaisesRegex(ValueError, "actual_model_output_mismatch"):
                verify_agent_receipt(SimpleNamespace(workspace=workspace), result)

    def test_unreviewed_result_cannot_authorize_next_paid_job(self):
        self.reserve()
        self.ledger.settle(0, self.result())
        with self.assertRaisesRegex(ValueError, "review_order_changed"):
            self.ledger.reviewed("wrong-job", {"checked": True})
        with self.assertRaisesRegex(ValueError, "ledger_not_ready"):
            self.reserve(1)

    def test_reopen_or_duplicate_settlement_cannot_hide_or_double_usage(self):
        self.reserve()
        self.ledger.settle(0, self.result())
        with self.assertRaises(ValueError):
            self.ledger.settle(0, self.result())
        self.assertEqual(self.ledger.state["known_cost_usd"], .2)
        self.ledger.close()
        with self.assertRaises(FileExistsError):
            BudgetLedger(self.root, self.plan, fingerprint=lambda: self.plan["input_fingerprint"])

    def test_model_identity_change_cannot_pass_as_one_configured_run(self):
        self.reserve()
        self.ledger.settle(0, self.result())
        self.ledger.reviewed("job-0", {"checked": True})
        self.reserve(1)
        result = self.result(1)
        result["usage"]["model_ids"] = ["different-unit-test-model"]
        with self.assertRaisesRegex(ValueError, "configured_model_changed"):
            self.ledger.settle(1, result)
        self.assertEqual(self.ledger.state["known_cost_usd"], .4)

    def test_code_or_plan_change_prevents_dispatch(self):
        self.ledger.fingerprint = lambda: {"sha256": "different-code"}
        with self.assertRaisesRegex(ValueError, "verification_inputs_changed"):
            self.reserve()
        self.assertEqual(self.ledger.state["entries"], [])

    def test_modified_ledger_cannot_create_more_budget(self):
        disk = read_private(self.root / "ledger.json")
        disk["total_reserved_usd"] = -2
        publish(self.root / "ledger.json", disk, replace=True)
        with self.assertRaisesRegex(ValueError, "ledger_changed"):
            self.reserve()
        self.assertEqual(self.ledger.state["entries"], [])

    def test_bad_scope_or_questions_fail_plan_validation(self):
        for mutation in (lambda p: p.update(synthetic=False), lambda p: p.update(total_max_cost_usd=30.0),
                         lambda p: p["profiles"][0].update(origin="https://example.com"),
                         lambda p: p["profiles"][0]["asks"]["initial"].update(question="oracle answer leaked"),
                         lambda p: p["profiles"][0]["bootstrap"].pop("intent"),
                         lambda p: p["profiles"][0]["bootstrap"].update(intent="Different intent"),
                         lambda p: p["profiles"][0]["bootstrap"].update(intent=QUESTIONS[PROFILES[1]])):
            changed = deepcopy(self.plan)
            mutation(changed)
            with self.assertRaises(ValueError):
                validate_plan(changed, self.root)

    def test_changed_in_memory_and_saved_intent_cannot_redefine_the_frozen_plan(self):
        self.plan["profiles"][0]["bootstrap"]["intent"] = "Changed in both copies"
        publish(self.root / "plan.json", self.plan, replace=True)
        with self.assertRaisesRegex(ValueError, "plan_changed"):
            self.reserve()
        self.assertEqual(self.ledger.state["entries"], [])

    def test_browser_plan_and_dispatch_gate_refuse_changed_intent_without_a_browser(self):
        # Exercise the exact pure functions used by CDP's pre-dispatch handler;
        # importing the driver starts neither Chrome nor an application.
        root = Path(__file__).resolve().parents[1]
        node = shutil.which("node")
        self.assertIsNotNone(node, "The existing browser verification runtime is required.")
        source = """
import {loadConfiguredPlan,configuredRequestMatches,CONFIGURED_QUESTIONS} from './scripts/customer360_browser_check.mjs';
import {readFile} from 'node:fs/promises';
const path=process.argv[1], raw=JSON.parse(await readFile(path,'utf8')), profile=raw.profiles[0];
let accepted=false;try{await loadConfiguredPlan({'configured-model-plan':path,profile:profile.name,origin:profile.origin,output:profile.browser_output});accepted=true;}catch{}
const args={allow_model_data:true,max_cost_usd:.5,max_turns:4,intent:profile.bootstrap.intent};
process.stdout.write(JSON.stringify({accepted,questions:CONFIGURED_QUESTIONS,
  exact:configuredRequestMatches(profile,'context.bootstrap',args,0),
  changed:configuredRequestMatches(profile,'context.bootstrap',{...args,intent:'changed'},0),
  missing:configuredRequestMatches(profile,'context.bootstrap',{...args,intent:undefined},0),
  extra:configuredRequestMatches(profile,'context.bootstrap',args,3)}));
"""
        def inspect(path):
            result = subprocess.run([node, "--input-type=module", "-e", source, str(path)], cwd=root,
                                    capture_output=True, text=True, timeout=15, check=True)
            return json.loads(result.stdout)
        result = inspect(self.root / "plan.json")
        self.assertEqual(result["questions"], QUESTIONS)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["exact"])
        self.assertEqual([result[key] for key in ("changed", "missing", "extra")], [False] * 3)
        for index, value in enumerate((None, "Changed objective", QUESTIONS[PROFILES[1]])):
            plan = deepcopy(self.plan)
            if value is None:
                plan["profiles"][0]["bootstrap"].pop("intent")
            else:
                plan["profiles"][0]["bootstrap"]["intent"] = value
            path = self.root / ("browser-plan-" + str(index) + ".json")
            publish(path, plan)
            self.assertFalse(inspect(path)["accepted"])

    def test_missing_opt_in_cannot_start_any_paid_work(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "explicit_opt_in"):
                execute_smoke()

    def test_wrapper_reserves_before_default_application_model_path(self):
        from contextbase.customer360 import agent
        from contextbase.customer360.application import Application
        from contextbase.workspace import Workspace
        app = BudgetedApplication(Workspace(self.root / "app-workspace"), PROFILES[0])
        app.ledger, app._active = self.ledger, "job-0"
        result = self.result()
        def inspect_before_operation(actual, operation, **arguments):
            self.assertIsNone(actual.model_client_factory)
            self.assertEqual(actual.model_origin, "configured_claude_cli")
            self.assertEqual(read_private(self.root / "ledger.json")["status"], "inflight")
            self.assertIs(operation, agent.bootstrap)
            return result
        try:
            with patch.object(Application, "_model", inspect_before_operation):
                self.assertEqual(app._model(agent.bootstrap, **self.arguments()), result)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
