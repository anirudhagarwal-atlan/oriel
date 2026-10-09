"""Synthetic verifier contracts only; these are not Customer360 completion tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_customer360.py"
SPEC = importlib.util.spec_from_file_location("customer360_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class Customer360VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.suites = self.root / "tests" / "customer360"
        self.suites.mkdir(parents=True)
        self.manifest = self.root / verifier.MANIFEST
        self.manifest.parent.mkdir(parents=True)
        self.output = self.root / verifier.OUTPUT

    def gate(self, name="example", mode="local", cases=None):
        return {"id": name, "mode": mode, "suite": f"tests/customer360/test_{name}.py",
                "required_cases": cases or ["test_required"], "description": "Synthetic verifier fixture."}

    def write(self, gates, code=None):
        self.manifest.write_text(json.dumps({"schema_version": 1, "goal_id": "customer360-v1", "gates": gates}))
        if code is not None:
            (self.root / gates[0]["suite"]).write_text(code)

    def run_gate(self, body="pass", *, mode="local", include=(), required=None):
        gate = self.gate(mode=mode, cases=required)
        self.write([gate], "import unittest\nclass Example(unittest.TestCase):\n    def test_required(self):\n        " + body + "\n")
        return verifier.verify(self.root, include=include, timeout=3)

    def charter_gates(self):
        # The repository contract is an independent input, not reconstructed
        # from the implementation inventory under test.
        return json.loads((SCRIPT.parents[1] / verifier.MANIFEST).read_text())["gates"]

    def passing_suites(self, gates):
        self.write(gates)
        for gate in gates:
            code = "import unittest\nclass SyntheticRunnerFixture(unittest.TestCase):\n"
            code += "".join("    def " + case + "(self): pass\n" for case in gate["required_cases"])
            (self.root / gate["suite"]).write_text(code)

    def assert_no_completion(self, report):
        for field in ("implementation_verified", "model_smoke_verified", "live_vendor_verified", "overall_passed"):
            self.assertFalse(report[field], field)

    def test_passed_required_case_and_private_receipt(self):
        report = self.run_gate()
        self.assertFalse(report["overall_passed"])
        self.assertFalse(report["implementation_verified"])
        self.assertFalse(report["contract_complete"])
        self.assertEqual(report["charter_inventory_version"], 1)
        self.assertFalse(report["model_smoke_verified"])
        self.assertEqual(report["gates"][0]["required_cases"], {"test_required": ["passed"]})
        self.assertEqual(json.loads(self.output.read_text()), report)
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(report["input_fingerprint"]["sha256"]), 64)
        self.assertTrue(report["inputs_unchanged"])
        self.assertFalse(report["runner_tests_are_product_evidence"])

    def test_missing_suite_is_unimplemented(self):
        self.write([self.gate()])
        report = verifier.verify(self.root)
        self.assertEqual(report["gates"][0]["status"], "unimplemented")
        self.assertFalse(report["overall_passed"])

    def test_excluded_modes_do_not_execute_and_need_individual_opt_in(self):
        gates = self.charter_gates()
        self.passing_suites(gates)
        report = verifier.verify(self.root)
        self.assertTrue(report["contract_complete"])
        self.assertEqual(report["contract_issues"], [])
        self.assertTrue(report["implementation_verified"])
        self.assertTrue(all(g["status"] == ("passed" if g["mode"] == "local" else "not_run") for g in report["gates"]))
        self.assertFalse(report["overall_passed"])
        report = verifier.verify(self.root, include=["model"])
        self.assertTrue(report["model_smoke_verified"])
        self.assertFalse(report["live_vendor_verified"])
        report = verifier.verify(self.root, include=["model", "live"])
        self.assertTrue(report["overall_passed"])
        self.assertTrue(report["live_vendor_verified"])

    def test_three_passing_modes_cannot_replace_the_charter_or_bypass_cli(self):
        gates = [gate for gate in self.charter_gates() if gate["id"] in {
            "source_pipeline", "configured_model_smoke", "authorized_live_connectors"}]
        self.passing_suites(gates)
        report = verifier.verify(self.root, include=["model", "live"])
        self.assertTrue(all(gate["status"] == "passed" for gate in report["gates"]))
        self.assertFalse(report["contract_complete"])
        self.assertEqual(len(report["contract_issues"]), 7)
        self.assertTrue(all(issue["reason"] == "missing_gate" for issue in report["contract_issues"]))
        self.assert_no_completion(report)
        copied = self.root / "scripts" / SCRIPT.name
        copied.parent.mkdir()
        copied.write_bytes(SCRIPT.read_bytes())
        result = subprocess.run([sys.executable, str(copied), "--manifest", str(self.manifest),
                                 "--output", str(self.output), "--include", "model", "--include", "live"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["contract_complete"])
        self.assert_no_completion(json.loads(self.output.read_text()))

    def test_missing_required_case_cannot_be_omitted_from_a_complete_gate_list(self):
        gates = self.charter_gates()
        missing = gates[0]["required_cases"].pop()
        self.passing_suites(gates)
        report = verifier.verify(self.root, include=["model", "live"])
        self.assertTrue(all(gate["status"] == "passed" for gate in report["gates"]))
        self.assertEqual(report["contract_issues"], [{"gate_id": "source_pipeline",
                         "reason": "missing_required_cases", "required_cases": [missing]}])
        self.assertFalse(report["contract_complete"])
        self.assert_no_completion(report)

    def test_protected_external_mode_swap_refuses_execution_before_import(self):
        for external in ("configured_model_smoke", "authorized_live_connectors"):
            with self.subTest(gate=external):
                gates = self.charter_gates()
                gate = next(gate for gate in gates if gate["id"] == external)
                gate["mode"] = "local"
                self.passing_suites(gates)
                marker = self.root / (external + ".executed")
                (self.root / gate["suite"]).write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unexpected')\n")
                report = verifier.verify(self.root, include=["model", "live"])
                self.assertFalse(marker.exists())
                self.assertIn({"gate_id": external, "reason": "mode_mismatch"}, report["contract_issues"])
                outcome = next(row for row in report["gates"] if row["id"] == external)
                self.assertEqual(outcome["error_code"], "charter_gate_mismatch")
                self.assert_no_completion(report)

    def test_protected_suite_replacement_refuses_execution(self):
        gates = self.charter_gates()
        gates[0]["suite"] = "tests/customer360/test_replacement.py"
        self.passing_suites(gates)
        marker = self.root / "replacement.executed"
        (self.root / gates[0]["suite"]).write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unexpected')\n")
        report = verifier.verify(self.root, include=["model", "live"])
        self.assertFalse(marker.exists())
        self.assertEqual(report["contract_issues"], [{"gate_id": "source_pipeline", "reason": "suite_mismatch"}])
        self.assertEqual(report["gates"][0]["error_code"], "charter_gate_mismatch")
        self.assert_no_completion(report)

    def test_alias_cannot_execute_a_protected_external_suite_as_local(self):
        gates = self.charter_gates()
        self.passing_suites(gates)
        alias = self.gate("renamed_model", "local", ["test_extra_case"])
        alias["suite"] = "tests/customer360/test_configured_model_smoke.py"
        gates.append(alias)
        self.write(gates)
        marker = self.root / "alias.executed"
        (self.root / alias["suite"]).write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unexpected')\n")
        report = verifier.verify(self.root)
        self.assertFalse(marker.exists())
        self.assertIn({"gate_id": "renamed_model", "reason": "reserved_suite"}, report["contract_issues"])
        self.assertEqual(report["gates"][-1]["error_code"], "charter_gate_mismatch")
        self.assert_no_completion(report)

    def test_extra_requirements_strengthen_the_charter_and_still_must_pass(self):
        gates = self.charter_gates()
        gates[0]["required_cases"].append("test_additional_requirement")
        self.passing_suites(gates)
        suite = self.root / gates[0]["suite"]
        suite.write_text(suite.read_text().replace("def test_additional_requirement(self): pass",
                                                 "def test_additional_requirement(self): self.fail()"))
        report = verifier.verify(self.root, include=["model", "live"])
        self.assertTrue(report["contract_complete"])
        self.assertEqual(report["gates"][0]["status"], "failed")
        self.assertFalse(report["implementation_verified"])
        self.assertFalse(report["overall_passed"])

    def test_no_tests_or_missing_required_case_cannot_pass(self):
        for code in ("value = 1\n", "import unittest\nclass Example(unittest.TestCase):\n    def test_other(self): pass\n"):
            with self.subTest(code=code):
                self.write([self.gate()], code)
                report = verifier.verify(self.root)
                self.assertFalse(report["overall_passed"])
                self.assertEqual(report["gates"][0]["required_cases"]["test_required"], [])

    def test_failure_error_skip_and_expected_failure_cannot_pass(self):
        codes = [
            "    def test_required(self): self.fail('synthetic-private-detail')\n",
            "    def test_required(self): raise RuntimeError('synthetic-private-detail')\n",
            "    @unittest.skip('synthetic-private-detail')\n    def test_required(self): pass\n",
            "    @unittest.expectedFailure\n    def test_required(self): self.fail('synthetic-private-detail')\n",
            "    @unittest.expectedFailure\n    def test_required(self): pass\n",
            "    def test_required(self):\n        with self.subTest(value='synthetic-private-detail'): self.fail()\n",
        ]
        for code in codes:
            with self.subTest(code=code):
                self.write([self.gate()], "import unittest\nclass Example(unittest.TestCase):\n" + code)
                report = verifier.verify(self.root)
                self.assertFalse(report["overall_passed"])
                self.assertEqual(report["gates"][0]["status"], "failed")
                self.assertNotIn("synthetic-private-detail", self.output.read_text())

    def test_skipped_unrequired_test_prevents_completion(self):
        self.write([self.gate()], "import unittest\nclass Example(unittest.TestCase):\n    def test_required(self): pass\n    @unittest.skip('fixture')\n    def test_other(self): pass\n")
        self.assertFalse(verifier.verify(self.root)["overall_passed"])

    def test_import_error_and_zero_exit_without_receipt_fail(self):
        for code in ("raise RuntimeError('synthetic-private-detail')", "import os\nos._exit(0)"):
            with self.subTest(code=code):
                self.write([self.gate()], code)
                report = verifier.verify(self.root)
                self.assertFalse(report["overall_passed"])
                self.assertNotIn("synthetic-private-detail", self.output.read_text())

    def test_class_setup_error_and_skip_fail(self):
        for body in ("raise RuntimeError('synthetic-private-detail')", "raise unittest.SkipTest('synthetic-private-detail')"):
            self.write([self.gate()], "import unittest\nclass Example(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls): " + body + "\n    def test_required(self): pass\n")
            self.assertFalse(verifier.verify(self.root)["overall_passed"])

    def test_suite_timeout_is_bounded(self):
        self.write([self.gate()], "import time\ntime.sleep(10)\n")
        report = verifier.verify(self.root, timeout=0.1)
        self.assertEqual(report["gates"][0]["error_code"], "suite_timeout")
        self.assertFalse(report["overall_passed"])

    def test_duplicate_required_method_is_ambiguous(self):
        self.write([self.gate()], "import unittest\nclass First(unittest.TestCase):\n    def test_required(self): pass\nclass Second(unittest.TestCase):\n    def test_required(self): pass\n")
        report = verifier.verify(self.root)
        self.assertEqual(report["gates"][0]["required_cases"]["test_required"], ["passed", "passed"])
        self.assertFalse(report["overall_passed"])

    def test_source_changes_during_execution_invalidate_receipt(self):
        self.write([self.gate()], "import unittest\nfrom pathlib import Path\nclass Example(unittest.TestCase):\n    def test_required(self):\n        Path(__file__).write_text(Path(__file__).read_text() + '# changed\\n')\n")
        report = verifier.verify(self.root)
        self.assertEqual(report["gates"][0]["status"], "passed")
        self.assertFalse(report["inputs_unchanged"])
        self.assertFalse(report["implementation_verified"])

    def test_dependency_config_and_declared_fixture_inputs_are_fingerprinted(self):
        self.write([self.gate()])
        inputs = (
            "pyproject.toml", "uv.lock", "contextbase/connectors/catalog.json",
            "contextbase/connectors/source.yaml", "contextbase/connectors/source.yml",
            "examples/customer-360/fixtures/accounts.csv", "tests/customer360/fixtures/events.jsonl",
            "verification/customer360/expected-facts.json",
            "contextbase/cli.py", "contextbase/customer360/snapshots.py",
            "contextbase/connectors/evidence.py", "contextbase/connectors/native-verification.json",
            "contextbase/connectors/verified/sources/zendesk/helpers/credentials.py",
            "tests/test_customer360_profiles.py", "tests/test_customer360_snapshots.py",
            "tests/test_customer360_verifier.py", "tests/test_connector_worker.py",
            "scripts/render_docs.py", "examples/connectors/verify_sqlite.py",
            "contextbase/customer360/web/index.html", "contextbase/customer360/web/app.js",
            "contextbase/customer360/web/style.css", "scripts/customer360_browser_check.mjs",
            "README.md", "docs/customer-360-app.md",
            "examples/connectors/api_fixture_worker.py", "examples/connectors/fixture.json",
            "examples/tool-exports/verify.py",
        )
        prior = verifier._fingerprint(self.root, self.manifest, [self.gate()])
        for relative in inputs:
            with self.subTest(input=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture version one\n")
                current = verifier._fingerprint(self.root, self.manifest, [self.gate()])
                self.assertNotEqual(current["sha256"], prior["sha256"])
                path.write_text("synthetic fixture version two\n")
                changed = verifier._fingerprint(self.root, self.manifest, [self.gate()])
                self.assertNotEqual(changed["sha256"], current["sha256"])
                prior = changed

    def test_private_paths_and_symlink_targets_are_not_fingerprint_inputs(self):
        self.write([self.gate()])
        paths = (
            "contextbase/.env", "contextbase/credentials.json", "contextbase/private/records.json",
            "tests/customer360/workspaces/records.json", "examples/customer-360/.oriel/records.json",
            ".oriel/records.json", "verification/customer360/access-token.json",
            "contextbase/private/credentials.py", "tests/private/private_test.py",
            "examples/connectors/.env", "examples/connectors/credentials.json",
            "examples/connectors/secrets/token.py", "scripts/private/helper.py",
            "scripts/.env.local", "examples/connectors/worker.key",
        )
        before = verifier._fingerprint(self.root, self.manifest, [self.gate()])
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic excluded data\n")
        self.assertEqual(before, verifier._fingerprint(self.root, self.manifest, [self.gate()]))
        fixture = self.root / "examples/customer-360/linked.json"
        target = self.root / ".oriel/records.json"
        fixture.symlink_to(target)
        before = verifier._fingerprint(self.root, self.manifest, [self.gate()])
        target.write_text("synthetic excluded change\n")
        self.assertEqual(before, verifier._fingerprint(self.root, self.manifest, [self.gate()]))

    def test_failed_and_successful_runs_are_retained_separately(self):
        failed = self.run_gate("self.fail('synthetic-private-detail')")
        retained = self.output.parent / failed["retained_report"]
        first_bytes = retained.read_bytes()
        passed = self.run_gate()
        self.assertNotEqual(failed["retained_report"], passed["retained_report"])
        self.assertEqual(retained.read_bytes(), first_bytes)
        self.assertEqual(json.loads(first_bytes), failed)
        self.assertEqual(json.loads(self.output.read_text()), passed)
        self.assertEqual(json.loads((self.output.parent / passed["retained_report"]).read_text()), passed)
        self.assertNotIn("synthetic-private-detail", first_bytes.decode())
        if os.name == "posix":
            self.assertEqual(retained.stat().st_mode & 0o777, 0o600)

    def test_bad_manifests_fail_before_execution(self):
        self.write([self.gate()])
        original = json.loads(self.manifest.read_text())
        changes = [
            {**original, "schema_version": True}, {**original, "gates": []},
            {**original, "gates": [self.gate(), self.gate()]},
            {**original, "gates": [self.gate(), self.gate("other")]},
            {**original, "gates": [{**self.gate(), "suite": "../outside.py"}]},
            {**original, "gates": [{**self.gate(), "required_cases": ["test_required", "test_required"]}]},
            {**original, "gates": [{**self.gate(), "mode": "automatic"}]},
        ]
        for value in changes:
            with self.subTest(value=value):
                self.manifest.write_text(json.dumps(value))
                with self.assertRaises(verifier.ManifestError):
                    verifier.verify(self.root)

    def test_missing_dimensions_and_zero_gates_cannot_pass(self):
        for mode in ("local", "model", "live"):
            with self.subTest(mode=mode):
                report = self.run_gate(mode=mode, include=["model", "live"])
                self.assertEqual(report["gates"][0]["status"], "passed")
                self.assertFalse(report["overall_passed"])
        self.write([])
        with self.assertRaises(verifier.ManifestError):
            verifier.verify(self.root)

    def test_duplicate_json_keys_are_invalid(self):
        self.manifest.write_text('{"schema_version":1,"schema_version":1,"goal_id":"customer360-v1","gates":[]}')
        with self.assertRaises(verifier.ManifestError):
            verifier.verify(self.root)

    def test_cli_bad_manifest_exit_and_sanitized_output(self):
        self.manifest.write_text("not JSON: synthetic-private-detail")
        result = subprocess.run([sys.executable, str(SCRIPT), "--manifest", str(self.manifest), "--output", str(self.output)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("synthetic-private-detail", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
