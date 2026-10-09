"""Installed-package sample staging through actual local dlt, without models."""
from importlib import util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

from contextbase.connectors.worker import _PROCESS_ENV
from contextbase.customer360 import answers, contexts, identity, profiles, samples, snapshots
from contextbase.workspace import Workspace


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "customer-360"
spec = util.spec_from_file_location("staged_sample_independent_oracle", EXAMPLE / "oracle.py")
oracle = util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class StagedSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        samples.prerequisites()
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.runs = {}
        for profile in ("salesforce-gong", "billing-support"):
            workspace = Workspace(cls.root / profile)
            extraction = samples.connect(workspace, profile)
            with workspace._connect() as conn:
                tables = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='_contextbase'").fetchall()}
            plan = samples.prepare(workspace, profile)
            cls.runs[profile] = workspace, extraction, plan, tables

    def copy(self, profile):
        original = self.runs[profile][0]
        workspace = Workspace(self.root / (self._testMethodName + "-" + profile))
        shutil.copytree(original.root, workspace.root)
        return workspace

    def approve_links(self, workspace, plan):
        for proposed in plan["links"]:
            if proposed["status"] == "proposed":
                identity.approve_links(workspace, proposed["proposal_id"], proposed["approval_sha256"], "synthetic-staged-review")

    def approve_profile_and_context(self, workspace, plan):
        self.approve_links(workspace, plan)
        mapping = profiles.propose_profile(workspace, plan["mapping"], plan["snapshot_id"])
        approved = profiles.approve_profile(workspace, mapping["proposal_id"], mapping["approval_sha256"], "synthetic-staged-review")
        profiles.materialize(workspace, plan["profile"], plan["snapshot_id"])
        context = contexts.propose_context(workspace, plan["profile"], plan["snapshot_id"], plan["definitions"],
                                           gaps=plan["gaps"], origin="scripted_agent_test")
        accepted = contexts.approve_context(workspace, context["proposal_id"], context["approval_sha256"], "synthetic-staged-review")
        return approved, accepted

    def assert_metrics(self, workspace, profile, phase, plan):
        expected = oracle.evaluate(profile, phase)
        for customer in expected["customers"]:
            name = "open_pipeline" if profile == "salesforce-gong" else "outstanding_invoices"
            result = answers.execute_metric(workspace, plan["profile"], plan["snapshot_id"], plan["customer_ids"][customer["customer_id"]], name)
            self.assertEqual({row["currency"]: Decimal(row["value"]) for row in result["result"]["values"]},
                             {currency: Decimal(str(value)) for currency, value in customer["amounts_by_currency"].items()})

    def test_packaged_inputs_are_raw_synthetic_sources_without_expected_answers(self):
        manifest = json.loads((samples.ROOT / "manifest.json").read_text())
        expected_files = {"manifest.json"}
        for profile in manifest["profiles"].values():
            expected_files.add(profile["mapping_decisions"])
            for phase in profile["phases"].values():
                expected_files.update(phase.values())
        actual = {str(path.relative_to(samples.ROOT)) for path in samples.ROOT.rglob("*") if path.is_file()}
        self.assertEqual(actual, expected_files)
        self.assertEqual(len(actual), 25)
        self.assertFalse(any("oracle" in path or "expected" in path or path.endswith(".py") for path in actual))
        for path in actual:
            self.assertEqual((samples.ROOT / path).read_bytes(), (EXAMPLE / path).read_bytes())

    def test_connect_uses_actual_dlt_sqlite_and_never_reviews(self):
        for profile, (workspace, extraction, plan, tables_after_connect) in self.runs.items():
            with self.subTest(profile=profile):
                self.assertEqual(extraction["status"], "passed")
                self.assertTrue(extraction["actual_dlt_executed"])
                self.assertEqual(extraction["runtime_versions"]["dlt"], "1.24.0")
                self.assertEqual(extraction["chunk_size"], 2)
                self.assertFalse(extraction["reviews_performed"])
                self.assertFalse(extraction["vendor_adapter_verified"])
                self.assertEqual(extraction["model_calls"], 0)
                self.assertFalse(any(name.startswith("customer360_") for name in tables_after_connect))
                self.assertTrue(all(item["source_type"] == "sqlite" and item["status"] == "synced" for item in extraction["publications"]))
                raw, _ = samples._fixtures(samples.ROOT, profile, "initial")
                with workspace._connect() as connection:
                    for resource in extraction["resources"]:
                        rows = connection.execute("SELECT record_json FROM " + resource["table"] + " ORDER BY fixture_position").fetchall()
                        self.assertEqual([json.loads(row[0]) for row in rows], raw["initial"][resource["resource"]])
                state_path = workspace.root / "customer360-samples/state.json"
                state = json.loads(state_path.read_text())
                self.assertEqual(state["status"], "extracted")
                self.assertFalse(Path(state["receipt"]).is_absolute())
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_prepare_is_pending_only_and_idempotent_across_process_restart(self):
        for profile, (workspace, _, plan, _) in self.runs.items():
            with self.subTest(profile=profile):
                self.assertEqual(plan["scope"], "synthetic_sample_review_preparation")
                self.assertFalse(plan["approvals_performed"])
                self.assertFalse(plan["human_business_review"])
                self.assertEqual(sum(value["status"] == "proposed" for value in plan["links"]), 1)
                self.assertTrue(any(value["status"] == "conflict" for value in plan["links"]))
                with workspace._connect() as connection:
                    for table in ("customer360_links", "customer360_link_reviews"):
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM _contextbase." + table).fetchone(), (0,))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name IN "
                                                        "('customer360_profile_candidates','customer360_profile_approvals',"
                                                        "'customer360_materializations','customer360_context_approvals')").fetchone(), (0,))
                environment = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
                child = subprocess.run([sys.executable, "-c", "import json,sys; from contextbase.workspace import Workspace; "
                                        "from contextbase.customer360 import samples; print(json.dumps(samples.prepare(Workspace(sys.argv[1]),sys.argv[2])))",
                                        str(workspace.root), profile], env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, 0, "Fresh process could not prepare the installed sample module.")
                self.assertEqual(json.loads(child.stdout), plan)
                with self.assertRaises(ValueError):
                    answers.brief(workspace, plan["profile"], plan["snapshot_id"], plan["customer_ids"]["customer_a"])

    def test_normal_exact_review_apis_activate_both_sample_profiles(self):
        for profile in self.runs:
            with self.subTest(profile=profile):
                workspace = self.copy(profile)
                plan = samples.prepare(workspace, profile)
                pending = next(value for value in plan["links"] if value["status"] == "proposed")
                with self.assertRaisesRegex(ValueError, "exact stored"):
                    identity.approve_links(workspace, pending["proposal_id"], "0" * 64, "synthetic-staged-review")
                for proposed in plan["links"]:
                    if proposed["status"] == "conflict":
                        with self.assertRaisesRegex(ValueError, "Conflicting identity"):
                            identity.approve_links(workspace, proposed["proposal_id"], proposed["approval_sha256"], "synthetic-staged-review")
                with self.assertRaises(ValueError):
                    contexts.propose_context(workspace, plan["profile"], plan["snapshot_id"], plan["definitions"])
                self.approve_profile_and_context(workspace, plan)
                self.assert_metrics(workspace, profile, "initial", plan)
                after = samples.prepare(workspace, profile)
                self.assertEqual(after["customer_ids"], plan["customer_ids"])
                self.assertFalse(any(value["status"] == "proposed" for value in after["links"]))

    def test_refresh_proposes_new_aliases_and_reuses_reviewed_meaning(self):
        for profile in self.runs:
            with self.subTest(profile=profile):
                workspace = self.copy(profile)
                initial = samples.prepare(workspace, profile)
                mapping, context = self.approve_profile_and_context(workspace, initial)
                samples.connect(workspace, profile, "refreshed")
                refreshed = samples.prepare(workspace, profile)
                self.assertEqual(initial["customer_ids"], refreshed["customer_ids"])
                self.assertEqual(initial["mapping"], refreshed["mapping"])
                self.assertEqual(initial["definitions"], refreshed["definitions"])
                new_links = [link for proposal in refreshed["links"] if proposal["status"] == "proposed" for link in proposal["links"]]
                old_links = [link for proposal in initial["links"] if proposal["status"] == "proposed" for link in proposal["links"]]
                self.assertTrue(new_links)
                self.assertLess(len(new_links), len(old_links))
                with self.assertRaises(ValueError):
                    answers.brief(workspace, initial["profile"], initial["snapshot_id"], initial["customer_ids"]["customer_a"])
                self.approve_links(workspace, refreshed)
                profiles.materialize(workspace, refreshed["profile"], refreshed["snapshot_id"])
                current = contexts.current_context(workspace, refreshed["profile"], refreshed["snapshot_id"])
                self.assertEqual(current["context_id"], context["context_id"])
                self.assertEqual(current["definition_sha256"], context["definition_sha256"])
                self.assertEqual(profiles.load_profile(workspace, refreshed["profile"])["approval_sha256"], mapping["approval_sha256"])
                self.assert_metrics(workspace, profile, "refreshed", refreshed)

    def test_failed_connect_blocks_preparation_and_keeps_prior_snapshot(self):
        workspace = self.copy("billing-support")
        initial = samples.prepare(workspace, "billing-support")
        original = snapshots.load_snapshot(workspace, initial["snapshot_id"])
        database = workspace.root / "customer360-fixture-runtime/stripe.sqlite"
        backup = database.with_suffix(".retained")
        database.rename(backup)
        database.symlink_to(backup)
        with self.assertRaisesRegex(ValueError, "symlink"):
            samples.connect(workspace, "billing-support", "refreshed")
        state = json.loads((workspace.root / "customer360-samples/state.json").read_text())
        self.assertEqual(state["status"], "failed")
        with self.assertRaisesRegex(ValueError, "successful extraction"):
            samples.prepare(workspace, "billing-support")
        self.assertEqual(snapshots.load_snapshot(workspace, initial["snapshot_id"]), original)

    def test_invalid_profile_and_tampered_receipt_are_refused(self):
        workspace = Workspace(self.root / "never-created")
        for profile in ("anything", "../outside", None):
            with self.assertRaises(ValueError):
                samples.connect(workspace, profile)
            with self.assertRaises(ValueError):
                samples.prepare(workspace, profile)
        self.assertFalse(workspace.root.exists())
        with self.assertRaisesRegex(ValueError, "Connect a synthetic"):
            samples.prepare(workspace, "billing-support")
        workspace = self.copy("billing-support")
        state_path = workspace.root / "customer360-samples/state.json"
        state = json.loads(state_path.read_text())
        state["receipt"] = "../outside.json"
        samples._write(state_path, state)
        with self.assertRaisesRegex(ValueError, "Invalid retained"):
            samples.prepare(workspace, "billing-support")


if __name__ == "__main__":
    unittest.main()
