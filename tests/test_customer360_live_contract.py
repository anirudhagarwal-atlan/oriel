"""Adversarial offline schema tests, NOT live acceptance evidence.

Every authorization, vendor, principal and artifact below is invented. Origin
labels exercise the parser and must never be promoted to live-run receipts.
"""
import copy
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location("live_contract", Path(__file__).parent / "customer360" / "live_contract.py")
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


def stamp(time):
    return "2026-10-09T" + time + "Z"


class SyntheticContract:
    def __init__(self):
        self.evidence, self.adapters = {}, {}
        self.contract = {
            "spec": live.SPEC, "created_at": stamp("09:00:00"),
            "authorization": {"reference": "synthetic-authorization-not-real", "approved_by": "synthetic-operator",
                "approved_at": stamp("08:00:00"), "expires_at": stamp("13:00:00"),
                "source_ids": ["crm", "support", "usage"], "source_scope_sha256": {},
                "read_only": True, "model_data_allowed": False, "evidence_sha256": ""},
            "initial_sources": ["crm", "support"], "additional_source": "usage", "sources": [],
            "max_evidence_age_seconds": 10800, "max_reference_skew_seconds": 60, "contract_sha256": ""}
        self.receipts, self.references = [], []
        for index, source_id in enumerate(("crm", "support", "usage")):
            fields = ["id", "updated_at", "account_id", "amount", "currency", "note"]
            capability = {"spec": live.CAPABILITY_SPEC, "connector": source_id + "_connector",
                "implementation_sha256": live.digest(["invented-code", index]),
                "origins": [["https://api.salesforce.com", "https://api.intercom.io", "https://api.linear.app"][index]], "resources": {"records": fields},
                "controls": {key: "adapter_enforced" for key in live.CONTROLS}, "refresh_modes": ["fresh_replace"],
                "reviewed_by": "synthetic-reviewer", "reviewed_at": stamp("08:30:00"), "evidence_sha256": ""}
            capability["evidence_sha256"] = self.proof("capability_review", "controlled_runtime_verification",
                live.subject(capability, "evidence_sha256"), stamp("08:20:00"), "synthetic-reviewer")
            cap_hash = live.digest(capability)
            self.adapters[cap_hash] = capability
            source = {"source_id": source_id, "connector": capability["connector"], "endpoint": capability["origins"][0],
                "tenant_ref": "synthetic-tenant-ref", "principal_ref": "synthetic-readonly-principal",
                "capability_sha256": cap_hash, "resources": [{"name": "records", "fields": fields, "id_field": "id",
                    "history": {"field": "updated_at", "start": stamp("00:00:00"), "end": "2026-10-10T00:00:00Z"},
                    "record_ids": None, "numeric_fields": ["amount"], "currency_field": "currency", "text_fields": ["note"]}],
                "permissions": {"resources": ["records"], "fields": {"records": fields},
                                "exclusions": [{"resource": "records", "field": "private_note", "reason": "not authorized"}]},
                "limits": {"max_requests": 10, "max_pages": 10, "max_rows": 100, "max_seconds": 60},
                "refresh_mode": "fresh_replace", "enforcement_sha256": ""}
            self.contract["sources"].append(source)
        self.refreeze()
        for source in self.contract["sources"]:
            previous = None
            for phase, hour in (("initial", "10"), ("refreshed", "11")):
                rows = [{"resource": "records", "id": identifier, "values": {
                    "id": identifier, "updated_at": stamp("09:00:00"), "account_id": "customer_a",
                    "amount": ("10.01" if phase == "initial" else "4.01") if currency == "USD" else "3.00",
                    "currency": currency, "note": "Synthetic observed statement " + phase}}
                    for identifier, currency in (("record_a", "USD"), ("record_b", "EUR"))]
                common = {"source_id": source["source_id"], "phase": phase, "contract_sha256": self.contract["contract_sha256"],
                    "scope_sha256": live.subject(source, "enforcement_sha256"), "origin": "authenticated_vendor",
                    "started_at": stamp(hour + ":00:00"), "finished_at": stamp(hour + ":00:20"),
                    "source_observed_at": stamp(hour + ":00:10"), "permissions": copy.deepcopy(source["permissions"]),
                    "resource_coverage": [{"resource": "records", "fields": list(source["resources"][0]["fields"]),
                        "row_count": 2, "complete": True, "source_observed_at": stamp(hour + ":00:10")}],
                    "rows": rows, "evidence_sha256": ""}
                receipt = {**copy.deepcopy(common), "spec": live.RECEIPT_SPEC,
                    "capability_sha256": source["capability_sha256"],
                    "adapter_sha256": self.adapters[source["capability_sha256"]]["implementation_sha256"],
                    "endpoint": source["endpoint"], "tenant_ref": source["tenant_ref"], "principal_ref": source["principal_ref"],
                    "refresh_mode": "fresh_replace", "previous_receipt_sha256": previous,
                    "request_count": 2, "page_count": 2, "complete": True, "status": "completed", "receipt_sha256": ""}
                self.resign(receipt)
                self.receipts.append(receipt)
                previous = receipt["receipt_sha256"]
                reference = {**copy.deepcopy(common), "spec": live.REFERENCE_SPEC, "method": "vendor_export",
                    "observer_ref": "synthetic-independent-observer", "independent_of_extractor": True,
                    "derived_from_sha256": [], "reference_sha256": ""}
                self.resign(reference)
                self.references.append(reference)

    def proof(self, kind, origin, subject, at, producer):
        record = {"kind": kind, "origin": origin, "subject_sha256": subject, "recorded_at": at,
                  "producer_ref": producer, "artifact_sha256": live.digest(["invented-artifact", len(self.evidence)]),
                  "derived_from_sha256": []}
        key = live.digest(record)
        self.evidence[key] = record
        return key

    def refreeze(self):
        auth = self.contract["authorization"]
        for source in self.contract["sources"]:
            source_hash = live.subject(source, "enforcement_sha256")
            auth["source_scope_sha256"][source["source_id"]] = source_hash
            source["enforcement_sha256"] = self.proof("scope_enforcement", "authenticated_vendor", source_hash,
                                                     stamp("08:40:00"), "synthetic-operator")
        auth["evidence_sha256"] = self.proof("authorization", "operator_assertion", live.subject(auth, "evidence_sha256"),
                                            stamp("08:50:00"), "synthetic-operator")
        self.contract["contract_sha256"] = live.subject(self.contract, "contract_sha256")

    def resign(self, row, *, proof_changes=None):
        extraction = "receipt_sha256" in row
        key = "receipt_sha256" if extraction else "reference_sha256"
        proof_hash = self.proof("extraction" if extraction else "reference", "authenticated_vendor",
            live.subject(row, "evidence_sha256", key), row["finished_at"],
            "synthetic-extractor" if extraction else "synthetic-independent-observer")
        if proof_changes:
            proof = {**self.evidence[proof_hash], **proof_changes}
            proof_hash = live.digest(proof)
            self.evidence[proof_hash] = proof
        row["evidence_sha256"] = proof_hash
        row[key] = live.subject(row, key)

    def rechain(self):
        for source in ("crm", "support", "usage"):
            initial, refreshed = [row for row in self.receipts if row["source_id"] == source]
            refreshed["previous_receipt_sha256"] = initial["receipt_sha256"]
            self.resign(refreshed)

    def validate(self):
        return live.validate_contract(self.contract, adapter_capabilities=self.adapters, evidence=self.evidence)

    def reconcile(self, **kwargs):
        return live.reconcile_receipts(self.contract, self.receipts, self.references,
            adapter_capabilities=self.adapters, evidence=self.evidence, checked_at=kwargs.get("checked_at", stamp("12:00:00")))


class LiveContractTests(unittest.TestCase):
    def test_valid_synthetic_parser_fixture_is_only_conditional_never_live_evidence(self):
        fixture = SyntheticContract()
        validated = fixture.validate()
        validated["authorization"]["reference"] = "detached copy"
        self.assertNotEqual(validated, fixture.contract)
        result = fixture.reconcile()
        self.assertTrue(result["conditional_reconciliation_passed"])
        self.assertFalse(result["live_gate_passed"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["origin_authentication"], "operator_assertion_only")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(len(result["outcomes"]), 6)
        self.assertGreater(len(result["limitations"]), 2)
        text = str(result)
        self.assertNotIn("Synthetic observed statement", text)
        self.assertNotIn("record_a", text)
        initial = next(row for row in result["outcomes"] if row["phase"] == "initial")
        self.assertEqual(initial["resources"]["records"]["totals"], [
            {"field": "amount", "currency": "EUR", "value": "3.00"},
            {"field": "amount", "currency": "USD", "value": "10.01"}])

    def test_missing_proofs_or_unknown_fields_refused(self):
        for case in ("no_auth_proof", "no_capability_proof", "no_enforcement", "extra_auth", "extra_receipt"):
            with self.subTest(case=case):
                fixture = SyntheticContract()
                if case == "no_auth_proof":
                    del fixture.evidence[fixture.contract["authorization"]["evidence_sha256"]]
                elif case == "no_capability_proof":
                    del fixture.evidence[next(iter(fixture.adapters.values()))["evidence_sha256"]]
                elif case == "no_enforcement":
                    del fixture.evidence[fixture.contract["sources"][0]["enforcement_sha256"]]
                elif case == "extra_auth":
                    fixture.contract["authorization"]["token"] = "synthetic-placeholder"
                else:
                    fixture.receipts[0]["extra_spend"] = "0.10"
                with self.assertRaises(live.LiveContractError):
                    fixture.reconcile()

    def test_authorization_commits_exact_scope_not_source_names_only(self):
        for field, value in (("principal_ref", "another-principal"), ("endpoint", "https://other.example.invalid")):
            fixture = SyntheticContract()
            fixture.contract["sources"][0][field] = value
            fixture.contract["contract_sha256"] = live.subject(fixture.contract, "contract_sha256")
            with self.assertRaisesRegex(live.LiveContractError, "authorization_scope_mismatch"):
                fixture.validate()
        fixture = SyntheticContract()
        del fixture.contract["authorization"]["source_scope_sha256"]
        with self.assertRaises(live.LiveContractError):
            fixture.validate()

    def test_exact_initial_pair_and_distinct_third_connector_required(self):
        for case in ("same_source", "same_connector", "missing_auth_source", "extra_source"):
            fixture = SyntheticContract()
            if case == "same_source":
                fixture.contract["additional_source"] = "crm"
            elif case == "same_connector":
                fixture.contract["sources"][2]["connector"] = fixture.contract["sources"][0]["connector"]
            elif case == "missing_auth_source":
                fixture.contract["authorization"]["source_ids"].pop()
            else:
                fixture.contract["sources"].append(copy.deepcopy(fixture.contract["sources"][0]))
            fixture.refreeze()
            with self.assertRaises(live.LiveContractError):
                fixture.validate()

    def test_no_write_or_model_authority_and_expired_authorization_refused(self):
        for field, value in (("read_only", False), ("model_data_allowed", True), ("expires_at", stamp("08:30:00"))):
            fixture = SyntheticContract()
            fixture.contract["authorization"][field] = value
            fixture.refreeze()
            with self.assertRaises(live.LiveContractError):
                fixture.validate()
        with self.assertRaisesRegex(live.LiveContractError, "verification_outside_authorization"):
            SyntheticContract().reconcile(checked_at=stamp("14:00:00"))

    def test_decorative_or_unsupported_adapter_controls_refused(self):
        for control, value in (("fields", "declared_only"), ("seconds", "vendor_permission_enforced")):
            fixture = SyntheticContract()
            old_key, capability = next(iter(fixture.adapters.items()))
            capability["controls"][control] = value
            capability["evidence_sha256"] = fixture.proof("capability_review", "controlled_runtime_verification",
                live.subject(capability, "evidence_sha256"), stamp("08:20:00"), "synthetic-reviewer")
            new_key = live.digest(capability)
            fixture.adapters[new_key] = capability
            del fixture.adapters[old_key]
            fixture.contract["sources"][0]["capability_sha256"] = new_key
            fixture.refreeze()
            with self.assertRaises(live.LiveContractError):
                fixture.validate()
        fixture = SyntheticContract()
        fixture.adapters.clear()
        with self.assertRaisesRegex(live.LiveContractError, "adapter_not_allowlisted"):
            fixture.validate()

    def test_fresh_replace_cannot_be_relabelled_incremental(self):
        fixture = SyntheticContract()
        fixture.contract["sources"][0]["refresh_mode"] = "incremental"
        fixture.refreeze()
        with self.assertRaisesRegex(live.LiveContractError, "durable_incremental"):
            fixture.validate()
        fixture = SyntheticContract()
        fixture.receipts[0]["refresh_mode"] = "incremental"
        fixture.resign(fixture.receipts[0])
        with self.assertRaisesRegex(live.LiveContractError, "execution_authority"):
            fixture.reconcile()

    def test_fixture_origins_and_fixture_endpoints_refused(self):
        for origin in ("synthetic", "synthetic_local_integration", "scripted_agent_test", "recorded_native"):
            fixture = SyntheticContract()
            fixture.receipts[0]["origin"] = origin
            fixture.resign(fixture.receipts[0])
            with self.assertRaisesRegex(live.LiveContractError, "actual_vendor_receipt"):
                fixture.reconcile()
        for origin in ("http://api.example.invalid", "https://127.0.0.1", "https://localhost", "https://user:pass@api.example.invalid",
                       "https://api.example.invalid", "https://api.example.org"):
            fixture = SyntheticContract()
            fixture.contract["sources"][0]["endpoint"] = origin
            fixture.refreeze()
            with self.assertRaises(live.LiveContractError):
                fixture.validate()

    def test_missing_duplicate_and_partial_receipts_refused(self):
        for case in ("missing", "duplicate", "failed", "partial", "empty_coverage"):
            fixture = SyntheticContract()
            row = fixture.receipts[0]
            if case == "missing":
                fixture.receipts.pop()
            elif case == "duplicate":
                fixture.receipts[-1] = copy.deepcopy(row)
            elif case == "failed":
                row["status"] = "failed"
            elif case == "partial":
                row["resource_coverage"][0]["complete"] = False
            else:
                row["resource_coverage"] = []
            fixture.resign(row)
            with self.assertRaises(live.LiveContractError):
                fixture.reconcile()

    def test_independent_reference_cannot_reuse_extractor_or_derived_artifact(self):
        for case in ("producer", "artifact", "setup_artifact", "derived", "proof_derived", "flag"):
            fixture = SyntheticContract()
            ref = fixture.references[0]
            changes = {}
            if case == "producer":
                changes["producer_ref"] = "synthetic-extractor"
                ref["observer_ref"] = "synthetic-extractor"
            elif case == "artifact":
                changes["artifact_sha256"] = fixture.evidence[fixture.receipts[0]["evidence_sha256"]]["artifact_sha256"]
            elif case == "setup_artifact":
                changes["artifact_sha256"] = fixture.evidence[fixture.contract["authorization"]["evidence_sha256"]]["artifact_sha256"]
            elif case == "derived":
                ref["derived_from_sha256"] = [fixture.receipts[0]["receipt_sha256"]]
            elif case == "proof_derived":
                changes["derived_from_sha256"] = [fixture.receipts[0]["receipt_sha256"]]
            else:
                ref["independent_of_extractor"] = False
            fixture.resign(ref, proof_changes=changes)
            with self.assertRaises(live.LiveContractError):
                fixture.reconcile()

    def test_ids_exact_values_quotes_and_customer_refs_reconcile_not_just_totals(self):
        for field, replacement in (("amount", "12.01"), ("note", "Unobserved synthetic text"), ("account_id", "customer_b")):
            fixture = SyntheticContract()
            fixture.receipts[0]["rows"][0]["values"][field] = replacement
            fixture.resign(fixture.receipts[0])
            with self.assertRaisesRegex(live.LiveContractError, "independent_record_reconciliation_failed"):
                fixture.reconcile()
        fixture = SyntheticContract()
        fixture.receipts[0]["rows"][0]["id"] = "replacement_id"
        fixture.receipts[0]["rows"][0]["values"]["id"] = "replacement_id"
        fixture.resign(fixture.receipts[0])
        with self.assertRaisesRegex(live.LiveContractError, "independent_record_reconciliation_failed"):
            fixture.reconcile()

    def test_duplicate_rows_and_missing_currency_do_not_inflate_or_default_to_zero(self):
        for case in ("duplicate", "currency", "null_amount", "float", "bool"):
            fixture = SyntheticContract()
            row = fixture.receipts[0]
            if case == "duplicate":
                row["rows"].append(copy.deepcopy(row["rows"][0]))
            else:
                row["rows"][0]["values"]["currency" if case == "currency" else "amount"] = {
                    "currency": None, "null_amount": None, "float": 0.1, "bool": True}[case]
            with self.assertRaises(live.LiveContractError):
                fixture.resign(row)
                fixture.reconcile()

    def test_record_history_field_and_permission_bounds_are_observed(self):
        for case in ("history", "field", "permission", "exclusion"):
            fixture = SyntheticContract()
            row = fixture.receipts[0]
            if case == "history":
                row["rows"][0]["values"]["updated_at"] = "2026-10-08T00:00:00Z"
            elif case == "field":
                row["rows"][0]["values"]["private_note"] = "synthetic excluded field"
            elif case == "permission":
                row["permissions"]["fields"]["records"].pop()
            else:
                row["permissions"]["exclusions"] = []
            fixture.resign(row)
            with self.assertRaises(live.LiveContractError):
                fixture.reconcile()

    def test_real_request_page_row_and_elapsed_bounds_and_clock_order_required(self):
        for field, value in (("request_count", 11), ("page_count", 11), ("request_count", True),
                             ("finished_at", stamp("10:01:01")), ("source_observed_at", stamp("09:59:59"))):
            fixture = SyntheticContract()
            fixture.receipts[0][field] = value
            fixture.resign(fixture.receipts[0])
            with self.assertRaises(live.LiveContractError):
                fixture.reconcile()
        fixture = SyntheticContract()
        fixture.receipts[0]["resource_coverage"][0]["row_count"] = 1
        fixture.resign(fixture.receipts[0])
        with self.assertRaisesRegex(live.LiveContractError, "resource_count_mismatch"):
            fixture.reconcile()

    def test_freshness_reference_skew_and_refresh_predecessor_required(self):
        fixture = SyntheticContract()
        fixture.contract["max_evidence_age_seconds"] = 60
        fixture.refreeze()
        # Bind the changed contract without altering observed times.
        for row in fixture.receipts + fixture.references:
            row["contract_sha256"] = fixture.contract["contract_sha256"]
            fixture.resign(row)
        with self.assertRaisesRegex(live.LiveContractError, "source_observation_stale"):
            fixture.reconcile()
        fixture = SyntheticContract()
        ref = fixture.references[0]
        ref["started_at"], ref["finished_at"], ref["source_observed_at"] = map(stamp, ("10:02:00", "10:02:20", "10:02:10"))
        ref["resource_coverage"][0]["source_observed_at"] = ref["source_observed_at"]
        fixture.resign(ref)
        with self.assertRaisesRegex(live.LiveContractError, "reference_observation_skew"):
            fixture.reconcile()
        fixture = SyntheticContract()
        fixture.receipts[1]["previous_receipt_sha256"] = live.digest("wrong predecessor")
        fixture.resign(fixture.receipts[1])
        with self.assertRaisesRegex(live.LiveContractError, "refresh_chain_broken"):
            fixture.reconcile()

    def test_refresh_must_demonstrate_a_changed_fact(self):
        fixture = SyntheticContract()
        for collection in (fixture.receipts, fixture.references):
            for index in (1, 3, 5):
                collection[index]["rows"] = copy.deepcopy(collection[index - 1]["rows"])
                fixture.resign(collection[index])
        with self.assertRaisesRegex(live.LiveContractError, "changed_source_fact_not_demonstrated"):
            fixture.reconcile()

    def test_timestamp_only_refresh_is_not_a_changed_customer_fact(self):
        fixture = SyntheticContract()
        for collection in (fixture.receipts, fixture.references):
            for index in (1, 3, 5):
                collection[index]["rows"] = copy.deepcopy(collection[index - 1]["rows"])
                for row in collection[index]["rows"]:
                    row["values"]["updated_at"] = stamp("10:30:00")
                fixture.resign(collection[index])
        with self.assertRaisesRegex(live.LiveContractError, "changed_source_fact_not_demonstrated"):
            fixture.reconcile()

    def test_input_aggregate_bound_prevents_many_individually_small_cells(self):
        with self.assertRaisesRegex(live.LiveContractError, "json_aggregate_byte_bound"):
            live.digest(["x" * 1000] * 20_000)
        with self.assertRaisesRegex(live.LiveContractError, "json_aggregate_node_bound"):
            live.digest([[None, None]] * 70_000)

    def test_exact_large_decimal_sum_and_addition_deletion_summary(self):
        fixture = SyntheticContract()
        for collection in (fixture.receipts, fixture.references):
            row = collection[0]
            row["rows"][0]["values"]["amount"] = "123456789012345678901234567890.12"
            row["rows"][1]["values"]["amount"] = "0.01"
            row["rows"][1]["values"]["currency"] = "USD"
            fixture.resign(row)
        fixture.rechain()
        result = fixture.reconcile()
        initial = next(row for row in result["outcomes"] if row["source_id"] == "crm" and row["phase"] == "initial")
        self.assertEqual(initial["resources"]["records"]["totals"], [
            {"field": "amount", "currency": "USD", "value": "123456789012345678901234567890.13"}])
        for collection in (fixture.receipts, fixture.references):
            row = collection[1]
            row["rows"][1]["id"] = "new_record"
            row["rows"][1]["values"]["id"] = "new_record"
            fixture.resign(row)
        changes = next(row for row in fixture.reconcile()["outcomes"] if row["source_id"] == "crm" and row["phase"] == "refreshed")
        self.assertEqual(changes["changes"]["records"], {"added": 1, "deleted": 1, "updated": 1})

    def test_hash_tamper_and_malformed_inputs_fail_without_echoing_values(self):
        fixture = SyntheticContract()
        fixture.receipts[0]["rows"][0]["values"]["note"] = "SYNTHETIC_MUST_NOT_ECHO"
        with self.assertRaises(live.LiveContractError) as failure:
            fixture.reconcile()
        self.assertNotIn("SYNTHETIC_MUST_NOT_ECHO", str(failure.exception))
        for malformed in (None, [], {"spec": live.SPEC}):
            with self.assertRaises(live.LiveContractError):
                live.validate_contract(malformed, adapter_capabilities={}, evidence={})


if __name__ == "__main__":
    unittest.main()
